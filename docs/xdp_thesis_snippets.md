# 1.1.2 Penyusunan Kode Program eBPF/XDP

Bagian ini menguraikan implementasi logika data plane yang dieksekusi di dalam kernel serta mekanisme kontrol pada user-space. Kode program disusun menggunakan bahasa C untuk kernel-side dan bahasa Go untuk user-side daemon, dengan eBPF Maps sebagai jembatan komunikasi antar keduanya.

## 1.1.2.1 Logika In-Kernel: Arsitektur Pipeline XDP

Implementasi program XDP dirancang untuk memproses paket pada tahap paling dini di dalam network stack, yaitu pada level NIC driver. Hal ini memungkinkan sistem untuk mengambil keputusan terhadap paket sebelum kernel melakukan alokasi memori `sk_buff` yang intensif sumber daya.

### A. Alur Keputusan Paket (Decision Pipeline)

Logika program kernel disusun dalam bentuk pipeline sekuensial sembilan langkah untuk memastikan low-latency processing. Fungsi XDP dimulai dengan mengekstraksi pointer ke header paket dari konteks `xdp_md`. Paket yang bukan merupakan IPv4 langsung dikembalikan ke kernel untuk menghindari gangguan pada protokol discovery jaringan seperti ARP.

```c
// bpf/xdp_prog_kern.c
SEC("xdp")
int xdp_firewall_fwd(struct xdp_md *ctx)
{
    void *data_end = (void *)(long)ctx->data_end;
    void *data     = (void *)(long)ctx->data;

    struct hdr_cursor nh = { .pos = data };
    struct ethhdr *eth;

    int eth_type = parse_ethhdr(&nh, data_end, &eth);
    if (eth_type != bpf_htons(ETH_P_IP))
        return XDP_PASS;
    // ...
}
```

Pointer `data` dan `data_end` dideklarasikan dari field konteks `xdp_md` — bukan sebagai variabel bebas — sehingga BPF verifier dapat memvalidasi setiap akses pointer terhadap batas paket. Setiap fungsi parser (`parse_ethhdr`, `parse_iphdr`, dst.) menerima kedua pointer ini untuk melakukan bounds check sebelum men-dereference header.

Setelah proses parsing berhasil, pipeline berlanjut ke tahap stateless firewalling yang menggunakan pola fail-fast: begitu satu kondisi pemblokiran terpenuhi, paket langsung dijatuhkan tanpa memeriksa kondisi berikutnya. Flag-flag pemblokiran dibaca sekali di awal fungsi dari `fw_config` BPF map, bukan di-lookup ulang setiap kali kondisi diperiksa, untuk menghindari overhead per-kondisi pada hot path.

```c
// bpf/xdp_prog_kern.c — Step 3: Firewall L3
if (block_fragments &&
    (iph->frag_off & bpf_htons(IP_MF | IP_OFFSET))) {
    stats_update(STAT_DROP, pkt_len);
    if (security_events_enabled)
        emit_event_security(iph->saddr, iph->daddr,
                            0, 0, iph->protocol,
                            PKT_ACTION_DROP, (__u16)pkt_len);
    return XDP_DROP;
}
```

Paket yang lolos dari seluruh pemeriksaan firewall kemudian memasuki tahap forwarding table lookup. Program melakukan exact-match lookup pada `fwd_table` menggunakan destination IP sebagai key. `bpf_map_lookup_elem()` mengembalikan pointer langsung ke value dalam map — bukan salinannya. Jika lookup gagal karena tidak ada entry yang cocok, paket diserahkan ke kernel routing agar konektivitas tetap terjaga untuk trafik yang tidak didefinisikan dalam tabel.

```c
// bpf/xdp_prog_kern.c — Step 7
entry = bpf_map_lookup_elem(&fwd_table, &iph->daddr);
if (!entry) {
    stats_update(STAT_PASS, pkt_len);
    return XDP_PASS;
}
```

Apabila entry ditemukan, program memasuki tahap header rewrite sebelum paket di-forward. MAC address destination diubah ke next-hop agar NIC penerima mau menerima frame, sementara MAC source diubah ke interface egress router agar reply dapat kembali. TTL kemudian dikurangi satu menggunakan incremental checksum update sesuai RFC 1624, menghindari kalkulasi ulang seluruh 20-byte IP header.

```c
// bpf/xdp_prog_kern.c — Step 8
memcpy(eth->h_dest,   entry->dst_mac, ETH_ALEN);
memcpy(eth->h_source, entry->src_mac, ETH_ALEN);
ip_decrease_ttl(iph);
```

### B. Analisis Verdikt XDP

Setiap paket yang diproses akan menghasilkan salah satu dari tiga return code utama, dan pemilihan verdikt yang tepat menjadi inti dari efisiensi sistem ini. `XDP_DROP` digunakan oleh subsistem firewall untuk membuang paket di level driver — mekanisme paling efisien karena driver langsung melepas descriptor DMA tanpa melibatkan beban kerja kernel lainnya, berbeda dengan `iptables DROP` yang baru membuang paket setelah alokasi `sk_buff` dan traversal seluruh Netfilter chain. Pengecekan ganda `blocked && *blocked` pada kode berikut mencerminkan persyaratan BPF verifier, yaitu pointer hasil lookup tidak boleh langsung di-dereference tanpa pemeriksaan NULL terlebih dahulu.

```c
// bpf/xdp_prog_kern.c — Step 4+5: blocked TCP port
blocked = bpf_map_lookup_elem(&blocked_ports_tcp, &port);
if (blocked && *blocked) {
    stats_update(STAT_DROP, pkt_len);
    if (security_events_enabled)
        emit_event_security(iph->saddr, iph->daddr,
                            l4_sport, l4_dport,
                            IPPROTO_TCP, PKT_ACTION_DROP, (__u16)pkt_len);
    return XDP_DROP;
}
```

`XDP_PASS` berfungsi sebagai jalur fallback untuk trafik manajemen atau paket yang tidak terdefinisi dalam tabel fast-forwarding. Pada program ini terdapat dua titik di mana `XDP_PASS` dikembalikan: saat frame non-IPv4 diterima, dan saat TTL paket menyentuh nilai satu atau kurang agar kernel dapat mengirimkan respons ICMP Time Exceeded. Pada kasus yang kedua, paket tidak di-drop melainkan di-pass sesuai kewajiban RFC 792 untuk mendukung alat diagnostik seperti `traceroute`.

```c
// bpf/xdp_prog_kern.c — Step 6: TTL Guard
if (iph->ttl <= 1) {
    stats_update(STAT_TTL_EXCEEDED, pkt_len);
    if (security_events_enabled)
        emit_event_security(iph->saddr, iph->daddr,
                            l4_sport, l4_dport,
                            iph->protocol,
                            PKT_ACTION_TTL_EXCEEDED, (__u16)pkt_len);
    return XDP_PASS;
}
```

`XDP_REDIRECT` memungkinkan paket berpindah antar-NIC secara langsung melalui DEVMAP tanpa melewati software interrupt (`ksoftirqd`) maupun kernel routing, dan inilah yang menjadi kunci performa throughput tinggi pada skenario forwarding. `bpf_redirect_map()` menereferensikan slot `tx_port_key` pada DEVMAP `tx_port` untuk mendapatkan `ifindex` NIC tujuan. Argumen ketiga `XDP_PASS` merupakan verdikt fallback jika slot tersebut belum terisi, sehingga paket tidak dibuang dalam kondisi konfigurasi parsial.

```c
// bpf/xdp_prog_kern.c — Step 9
return bpf_redirect_map(&tx_port, entry->tx_port_key, XDP_PASS);
```

## 1.1.2.2 Implementasi eBPF Maps dan Komunikasi User-Space

Komunikasi antara program Go (control plane) dan program XDP (data plane) dilakukan secara asinkron melalui eBPF Maps. Struktur data ini memungkinkan perubahan kebijakan firewall dan routing secara runtime tanpa perlu melakukan detach atau re-attach program kernel.

### A. Mekanisme Observabilitas: PERCPU_ARRAY dan RINGBUF

Untuk menjaga performa pada hot path, digunakan dua jenis map yang berbeda sesuai karakteristik data masing-masing. Map `xdp_stats` bertipe `PERCPU_ARRAY` digunakan untuk mencatat statistik trafik. Pemilihan tipe PERCPU bertujuan mengeliminasi cache-line bouncing dan lock contention, karena setiap CPU core memiliki salinan datanya sendiri sehingga tidak ada instruksi atomik (`lock xadd`) yang dibutuhkan saat counter diperbarui.

```c
// bpf/xdp_prog_kern.c
struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __type(key,   __u32);
    __type(value, struct stats_rec);
    __uint(max_entries, STAT_MAX);
} xdp_stats SEC(".maps");

static __always_inline void stats_update(__u32 key, __u32 bytes)
{
    struct stats_rec *rec = bpf_map_lookup_elem(&xdp_stats, &key);
    if (rec) {
        rec->packets++;
        rec->bytes += bytes;
    }
}
```

Agregasi data dilakukan secara berkala oleh aplikasi Go di user-space dengan menjumlahkan seluruh CPU slice. `m.Lookup()` pada PERCPU_ARRAY mengembalikan slice sepanjang `numCPU` — satu elemen per core — dan penjumlahan ini hanya terjadi saat API statistik dipanggil, bukan per-paket, sehingga tidak menambah overhead pada data plane.

```go
// internal/maps/stats.go
func ReadStats(m *ebpf.Map) (*StatsMap, error) {
    numCPU, _ := ebpf.PossibleCPU()
    for key := uint32(0); key < StatMax; key++ {
        perCPU := make([]perCPUStatsRec, numCPU)
        if err := m.Lookup(key, &perCPU); err != nil {
            continue
        }
        for _, cpu := range perCPU {
            recs[key].Packets += cpu.Packets
            recs[key].Bytes   += cpu.Bytes
        }
    }
    return result, nil
}
```

Berbeda dengan statistik agregat, map `packet_events` bertipe `RINGBUF` digunakan untuk mengirimkan event keamanan secara detail ke user-space. RINGBUF dipilih karena mendukung fitur reservation dan epoll-based notification, yang jauh lebih efisien dibandingkan `PERF_EVENT_ARRAY` dalam menangani arus data log yang besar. Flag `BPF_RB_NO_WAKEUP` pada pemanggilan berikut menunda wakeup konsumen Go hingga ticker periodik (100 ms) memicunya, mencegah context switch per-event yang berlebihan saat laju DROP tinggi.

```c
// bpf/xdp_prog_kern.c
static __always_inline void emit_event_security(
    __be32 src_ip, __be32 dst_ip,
    __u16 src_port, __u16 dst_port,
    __u8 protocol, __u8 action, __u16 pkt_len)
{
    struct packet_event ev = {
        .timestamp_ns = bpf_ktime_get_ns(),
        .src_ip = src_ip,  .dst_ip   = dst_ip,
        .src_port = src_port, .dst_port = dst_port,
        .protocol = protocol, .action  = action,
        .pkt_len  = pkt_len,
    };
    bpf_ringbuf_output(&packet_events, &ev, sizeof(ev), BPF_RB_NO_WAKEUP);
}
```

### B. Manajemen Konfigurasi dan Forwarding

Flag fitur firewall disimpan dalam map `fw_config` bertipe `ARRAY`. Hal ini memungkinkan operator mengaktifkan atau menonaktifkan fitur secara instan melalui API tanpa menyentuh program kernel. Pada sisi kernel, seluruh flag dibaca ke variabel lokal sekali di awal fungsi XDP menggunakan helper `fw_cfg_enabled()`, kemudian digunakan sepanjang pipeline tanpa lookup ulang.

```c
// bpf/xdp_prog_kern.c
static __always_inline int fw_cfg_enabled(__u32 key)
{
    __u8 *val = bpf_map_lookup_elem(&fw_config, &key);
    return val && *val;
}

// dibaca sekali di awal xdp_firewall_fwd()
int block_fragments = fw_cfg_enabled(FW_CFG_BLOCK_IP_FRAGMENTS);
int block_all_tcp   = fw_cfg_enabled(FW_CFG_BLOCK_ALL_TCP);
int events_enabled  = fw_cfg_enabled(FW_CFG_EVENTS_ENABLED);
```

Di sisi Go, perubahan flag ditulis ke map yang sama melalui `bpf()` syscall. Pada paket berikutnya, program XDP membaca nilai terbaru — latensi efektif perubahan konfigurasi adalah waktu pemrosesan satu paket, praktis instan, tanpa reload program.

```go
// internal/maps/config.go
func WriteFlags(m *ebpf.Map, f FwFlags) error {
    pairs := []struct{ key uint32; val bool }{
        {FwCfgBlockIPFragments, f.BlockIPFragments},
        {FwCfgBlockAllTCP,      f.BlockAllTCP},
        {FwCfgEventsEnabled,    f.EventsEnabled},
        // ...
    }
    for _, p := range pairs {
        if err := SetFlag(m, p.key, p.val); err != nil {
            return err
        }
    }
    return nil
}
```

Map `fwd_table` bertipe `HASH` digunakan untuk mendukung exact-match lookup pada alamat IP tujuan, dan aplikasi Go bertanggung jawab atas sinkronisasi antara keputusan routing dan entri di dalam map. Detail kritis yang perlu diperhatikan adalah representasi key sebagai `[4]byte` alih-alih `uint32`: library `cilium/ebpf` melakukan byte-swap pada tipe integer di host little-endian, yang akan menghasilkan mismatch permanen terhadap `iph->daddr` kernel yang disimpan dalam network byte order. Dengan `[4]byte`, byte ditulis verbatim sehingga lookup selalu konsisten antara kedua sisi.

```go
// internal/maps/routes.go
func AddRoute(fwdMap *ebpf.Map, r RouteEntry) error {
    key, _    := ipToKey(r.IP)      // [4]byte dalam network byte order
    dstMAC, _ := parseMACBytes(r.DstMAC)
    srcMAC, _ := parseMACBytes(r.SrcMAC)

    entry := FwdEntry{
        DstMAC: dstMAC, SrcMAC: srcMAC,
        TxPortKey: r.TxPortKey,
        Action:    FwdActionRedirect,
    }
    return fwdMap.Put(key, entry)
}
```
