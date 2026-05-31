# Flowchart Alur Kerja eBPF/XDP — Sistem xdpd

Dokumen ini menjabarkan alur kerja lengkap sistem XDP dari source code aktual di folder ini,
beserta cara memvalidasi setiap langkah melalui perintah verifikasi.

---

## Daftar Isi

1. [Arsitektur Sistem Keseluruhan](#1-arsitektur-sistem-keseluruhan)
2. [Fase 1 — Kompilasi & Build](#2-fase-1--kompilasi--build)
3. [Fase 2 — Startup Daemon (xdpd)](#3-fase-2--startup-daemon-xdpd)
4. [Fase 3 — Loading & Attaching BPF Program](#4-fase-3--loading--attaching-bpf-program)
5. [Fase 4 — Pemrosesan Paket di Kernel (XDP Hot Path)](#5-fase-4--pemrosesan-paket-di-kernel-xdp-hot-path)
6. [Fase 5 — BPF Maps (Shared State)](#6-fase-5--bpf-maps-shared-state)
7. [Fase 6 — Userspace Control Plane (Go)](#7-fase-6--userspace-control-plane-go)
8. [Fase 7 — Ring Buffer → SQLite Pipeline](#8-fase-7--ring-buffer--sqlite-pipeline)
9. [Fase 8 — REST API & Dashboard](#9-fase-8--rest-api--dashboard)
10. [Ringkasan Validasi Per Langkah](#10-ringkasan-validasi-per-langkah)

---

## 1. Arsitektur Sistem Keseluruhan

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                         SISTEM xdpd (XDP Firewall + Forwarder)                  │
│                                                                                  │
│  ┌──────────────┐    ┌──────────────────────────────────────────────────────┐   │
│  │  SOURCE CODE │    │              KERNEL SPACE                            │   │
│  │              │    │  ┌─────────────────────────────────────────────────┐ │   │
│  │ xdp_prog_    │───▶│  │  XDP Program: xdp_firewall_fwd()               │ │   │
│  │ kern.c       │    │  │                                                  │ │   │
│  │ (BPF/C)      │    │  │  [Ingress NIC Driver]                           │ │   │
│  │              │    │  │       ↓                                          │ │   │
│  │ common_kern_ │    │  │  Step 1: Parse Ethernet                         │ │   │
│  │ user.h       │    │  │       ↓                                          │ │   │
│  │              │    │  │  Step 2: Parse IPv4                              │ │   │
│  │ parsing_     │    │  │       ↓                                          │ │   │
│  │ helpers.h    │    │  │  Step 3: Firewall L3 (fragment/broadcast/mcast) │ │   │
│  └──────────────┘    │  │       ↓                                          │ │   │
│                       │  │  Step 4+5: Parse + Firewall L4 (TCP/UDP/ICMP)  │ │   │
│  ┌──────────────┐    │  │       ↓                                          │ │   │
│  │  Go USERSPACE│    │  │  Step 6: TTL Guard                               │ │   │
│  │              │    │  │       ↓                                          │ │   │
│  │ cmd/xdpd/    │    │  │  Step 7: Forwarding Table Lookup                │ │   │
│  │ main.go      │    │  │       ↓                                          │ │   │
│  │              │    │  │  Step 8: MAC Rewrite + TTL Decrement            │ │   │
│  │ internal/    │    │  │       ↓                                          │ │   │
│  │ xdp/         │    │  │  Step 9: XDP_TX / XDP_REDIRECT / XDP_PASS /    │ │   │
│  │ manager.go   │◀──▶│  │          XDP_DROP                               │ │   │
│  │              │    │  └─────────────────────────────────────────────────┘ │   │
│  │ internal/    │    │                   ↕ BPF Maps                         │   │
│  │ maps/        │    │  ┌──────────────────────────────────────────────────┐│   │
│  │ internal/api/│    │  │  xdp_stats  blocked_ports_tcp  blocked_ports_udp ││   │
│  │ internal/db/ │    │  │  blocked_protos  fw_config  fwd_table            ││   │
│  └──────────────┘    │  │  tx_port (DEVMAP)  packet_events (RING BUFFER)  ││   │
│                       │  └──────────────────────────────────────────────────┘│   │
│  ┌──────────────┐    └──────────────────────────────────────────────────────┘   │
│  │  REACT UI    │                                                                │
│  │  frontend/   │◀──── HTTP REST API (:8080) ◀──── Go API Server               │
│  └──────────────┘                                                                │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Fase 1 — Kompilasi & Build

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  FASE 1: KOMPILASI                                                           │
│                                                                              │
│  ┌──────────────────────────┐                                                │
│  │ bpf/xdp_prog_kern.c      │                                                │
│  │ bpf/common_kern_user.h   │──── clang -target bpf ────▶ xdpprog_bpfel.o  │
│  │ bpf/headers/             │                              (BPF bytecode)    │
│  └──────────────────────────┘                                                │
│                                      ↓ bpf2go (go generate)                 │
│                              ┌───────────────────────────────────────┐       │
│                              │ internal/bpfobj/xdpprog_bpfel.go      │       │
│                              │ internal/bpfobj/xdpprog_bpfeb.go      │       │
│                              │ (Go wrappers: LoadXdpProgObjects())    │       │
│                              └───────────────────────────────────────┘       │
│                                      ↓ go build                              │
│                              ┌───────────────────────────────────────┐       │
│                              │ ./xdpd  (binary tunggal)              │       │
│                              │ - embed BPF .o bytecode               │       │
│                              │ - embed React frontend/dist/          │       │
│                              └───────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Validasi Fase 1

```bash
# 1a. Verifikasi binary xdpd ada dan memiliki BPF object ter-embed
ls -lh /home/telmat/final_t40/xdp-go-optimized/xdpd
file /home/telmat/final_t40/xdp-go-optimized/xdpd

# 1b. Cek BPF object (.o) ter-compile
ls -lh /home/telmat/final_t40/xdp-go-optimized/internal/bpfobj/
# Harus ada: xdpprog_bpfel.o  xdpprog_bpfeb.o

# 1c. Cek Go wrapper ter-generate dari bpf2go
head -5 /home/telmat/final_t40/xdp-go-optimized/internal/bpfobj/xdpprog_bpfel.go
# Harus ada komentar "// Code generated by bpf2go"

# 1d. Verifikasi BPF program sections di dalam .o file
llvm-objdump -d /home/telmat/final_t40/xdp-go-optimized/internal/bpfobj/xdpprog_bpfel.o
# atau
readelf -S /home/telmat/final_t40/xdp-go-optimized/internal/bpfobj/xdpprog_bpfel.o | grep xdp

# 1e. Rebuild manual (opsional, butuh clang + libbpf)
cd /home/telmat/final_t40/xdp-go-optimized && make
```

---

## 3. Fase 2 — Startup Daemon (xdpd)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  FASE 2: STARTUP DAEMON                                                      │
│                                                                              │
│  User menjalankan:                                                           │
│  sudo ./xdpd -iface eth0 -redirect-dev eth1 -config turbo.json -addr :8080  │
│                     │                                                        │
│                     ▼                                                        │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │ main.go: Cek UID == 0 (root check)                                   │    │
│  │   ├── NO (UID != 0) → log.Fatal("must run as root")                  │    │
│  │   └── YES → lanjut                                                   │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                     │                                                        │
│                     ▼                                                        │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │ Cek -stats mode?                                                     │    │
│  │   ├── YES → runStats(): buka pinned xdp_stats map, print table       │    │
│  │   └── NO  → daemon mode (lanjut)                                     │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                     │                                                        │
│                     ▼                                                        │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │ Load optional JSON config (-config turbo.json)                       │    │
│  │   config.Load() → parse blocked_ports, routes, fw_flags              │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                     │                                                        │
│                     ▼                                                        │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │ db.Open("/tmp/xdpd.db") → SQLite database untuk traffic logs         │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                     │                                                        │
│                     ▼                                                        │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │ xdp.NewManager(ifname, redirectDev, cfg) → buat Manager struct        │    │
│  │ api.NewServer(mgr, store, cfgPath) → buat HTTP server                 │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                     │                                                        │
│                     ▼                                                        │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │ http.ListenAndServe(:8080) → mulai HTTP server di goroutine           │    │
│  │ signal.NotifyContext(SIGINT, SIGTERM) → tunggu shutdown signal        │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Validasi Fase 2

```bash
# 2a. Cek proses xdpd berjalan
ps aux | grep xdpd

# 2b. Cek xdpd listening di port 8080
ss -tlnp | grep 8080
# atau
netstat -tlnp | grep 8080

# 2c. Test HTTP API endpoint
curl -s http://localhost:8080/api/status | python3 -m json.tool

# 2d. Cek log startup
journalctl -u xdpd --no-pager | tail -20
# atau jika dijalankan langsung:
# lihat output terminal yang menampilkan "xdpd listening on :8080"

# 2e. Cek SQLite database terbuat
ls -lh /tmp/xdpd.db
sqlite3 /tmp/xdpd.db ".tables"

# 2f. Verifikasi config file terbaca (jika pakai -config)
curl -s http://localhost:8080/api/config | python3 -m json.tool
```

---

## 4. Fase 3 — Loading & Attaching BPF Program

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  FASE 3: mgr.Start() — Load & Attach XDP Program                            │
│                                                                              │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │ Step 3.1: Buat pin directory                                          │   │
│  │   os.MkdirAll("/sys/fs/bpf/<ifname>/", 0700)                         │   │
│  │   Contoh: /sys/fs/bpf/eth0/                                           │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                          │                                                   │
│                          ▼                                                   │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │ Step 3.2: Load BPF Object dari embed (ELF bytecode)                  │   │
│  │   bpfobj.LoadXdpProgObjects(&objs, {PinPath: "/sys/fs/bpf/eth0"})    │   │
│  │   cilium/ebpf parse .o file → verifier kernel → load ke kernel       │   │
│  │                                                                       │   │
│  │   BPF Maps yang ter-load:                                             │   │
│  │   ├── xdp_stats (PERCPU_ARRAY)                                        │   │
│  │   ├── blocked_ports_tcp (ARRAY 65536 entries)                         │   │
│  │   ├── blocked_ports_udp (ARRAY 65536 entries)                         │   │
│  │   ├── blocked_protos (ARRAY 256 entries)                              │   │
│  │   ├── fw_config (ARRAY 9 entries = FW_CFG_MAX)                        │   │
│  │   ├── fwd_table (HASH, max 4096 entries)                              │   │
│  │   ├── tx_port (DEVMAP, max 16 entries)                                 │   │
│  │   └── packet_events (RING BUFFER, 256 KB)                             │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                          │                                                   │
│                          ▼                                                   │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │ Step 3.3: Pin semua maps ke /sys/fs/bpf/<ifname>/                    │   │
│  │   Setiap map di-pin satu file:                                        │   │
│  │   /sys/fs/bpf/eth0/xdp_stats                                          │   │
│  │   /sys/fs/bpf/eth0/blocked_ports_tcp                                  │   │
│  │   /sys/fs/bpf/eth0/fwd_table      ... dst                             │   │
│  │                                                                       │   │
│  │   [PENTING] Pin = file di BPF filesystem = map tetap hidup            │   │
│  │   walaupun xdpd restart (persistent across process restarts)          │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                          │                                                   │
│                          ▼                                                   │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │ Step 3.4: Detach XDP lama (jika ada)                                  │   │
│  │   exec: "ip link set dev eth0 xdp off"                                │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                          │                                                   │
│                          ▼                                                   │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │ Step 3.5: Attach XDP Program ke NIC (Driver Mode)                    │   │
│  │   link.AttachXDP({                                                    │   │
│  │     Program:   objs.XdpFirewallFwd,                                   │   │
│  │     Interface: iface.Index,                                           │   │
│  │     Flags:     XDPDriverMode,    ← native/driver mode (paling cepat) │   │
│  │   })                                                                  │   │
│  │                                                                       │   │
│  │   Hasil: setiap paket ingress NIC langsung trigger xdp_firewall_fwd()│   │
│  │   SEBELUM masuk ke kernel networking stack                            │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                          │                                                   │
│                          ▼                                                   │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │ Step 3.6: Set default firewall flags di fw_config map                │   │
│  │   FW_CFG_SECURITY_EVENTS = 1 (aktifkan log DROP/TTL_EXCEEDED)        │   │
│  │   FW_CFG_EVENTS_ENABLED  = 0 (default off = turbo mode)              │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                          │                                                   │
│                          ▼                                                   │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │ Step 3.7: Seed default port blocklists (jika map masih kosong)       │   │
│  │   TCP: 20,21,22,23,69,135,137,138,139,445,1433,1521,3306,3389,       │   │
│  │        5432,5900 (FTP,SSH,Telnet,NetBIOS,SMB,RDP,VNC,DB ports)       │   │
│  │   UDP: 53,69,123,137,138,161,162,11211 (DNS,NTP,SNMP,Memcached)      │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                          │                                                   │
│                          ▼                                                   │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │ Step 3.8: Setup egress NIC (jika -redirect-dev diset)                │   │
│  │   a. Lookup ifindex egress NIC (misal eth1)                           │   │
│  │   b. maps.SetDevmapSlot(tx_port, slot=0, ifindex=eth1)                │   │
│  │   c. attachEgressPass(eth1):                                          │   │
│  │      - buat program XDP_PASS trivial (2 instruksi BPF)               │   │
│  │      - attach ke eth1 ← wajib untuk bpf_redirect_map() di kernel 5.9+│   │
│  └──────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Validasi Fase 3

```bash
# 3a. Cek XDP program ter-attach ke NIC
ip link show eth0
# Output harus ada: "xdp" atau "xdpdrv" di baris prog

# Atau lebih detail:
ip link show dev eth0 | grep xdp

# 3b. Cek via bpftool (cara paling informatif)
sudo bpftool net show dev eth0
# Harus tampil: xdpdrv id <prog_id>  name xdp_firewall_fwd

# 3c. Lihat semua BPF programs yang loaded di kernel
sudo bpftool prog list | grep xdp_firewall

# 3d. Verifikasi pinned maps di bpf filesystem
ls -la /sys/fs/bpf/eth0/
# Harus ada: xdp_stats, blocked_ports_tcp, blocked_ports_udp,
#            blocked_protos, fw_config, fwd_table, tx_port, packet_events

# 3e. Cek map details (type, max_entries, dll)
sudo bpftool map show pinned /sys/fs/bpf/eth0/xdp_stats
sudo bpftool map show pinned /sys/fs/bpf/eth0/fwd_table
sudo bpftool map show pinned /sys/fs/bpf/eth0/packet_events

# 3f. Verifikasi default TCP port yang diblokir
sudo bpftool map dump pinned /sys/fs/bpf/eth0/blocked_ports_tcp | grep -v '"value": 0'
# Harus muncul entry untuk port 22, 3389, dll.

# 3g. Cek fw_config flags
sudo bpftool map dump pinned /sys/fs/bpf/eth0/fw_config
# key 8 (FW_CFG_SECURITY_EVENTS) harus bernilai 1

# 3h. Cek tx_port DEVMAP (jika redirect-dev diset)
sudo bpftool map dump pinned /sys/fs/bpf/eth0/tx_port
# Harus ada slot 0 dengan ifindex egress NIC

# 3i. Verifikasi via API
curl -s http://localhost:8080/api/status | python3 -m json.tool
# "attached": true
```

---

## 5. Fase 4 — Pemrosesan Paket di Kernel (XDP Hot Path)

Ini adalah inti sistem — dieksekusi oleh kernel untuk SETIAP paket yang masuk ke NIC.

```
┌────────────────────────────────────────────────────────────────────────────────┐
│  FASE 4: xdp_firewall_fwd() — Kernel XDP Hot Path                             │
│  Source: bpf/xdp_prog_kern.c:302                                               │
│                                                                                 │
│  [NIC Hardware Driver]                                                          │
│        │ paket masuk (DMA)                                                      │
│        ▼                                                                        │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │ INIT: Baca context (struct xdp_md *ctx)                                  │   │
│  │   data     = ctx->data      (pointer ke awal frame)                      │   │
│  │   data_end = ctx->data_end  (pointer ke akhir frame)                     │   │
│  │   pkt_len  = data_end - data                                             │   │
│  │                                                                          │   │
│  │   CACHE CONFIG FLAGS (1x bpf_map_lookup per flag):                       │   │
│  │   events_enabled, security_events_enabled, block_fragments,              │   │
│  │   block_broadcast, block_multicast, block_all_tcp, block_bad_tcp,        │   │
│  │   block_all_udp, block_icmp_ping                                         │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│        │                                                                        │
│        ▼                                                                        │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │ STEP 1: Parse Ethernet Header                                            │   │
│  │   parse_ethhdr(&nh, data_end, &eth)                                      │   │
│  │   → Bounds check: eth + sizeof(ethhdr) > data_end? → verifier safe      │   │
│  │   → Skip VLAN tags (up to depth 2)                                       │   │
│  │   → Kembalikan EtherType                                                 │   │
│  │                                                                          │   │
│  │   eth_type == ETH_P_IP (0x0800)?                                         │   │
│  │   ├── NO  (ARP/IPv6/dll) → stats_update(STAT_PASS) → XDP_PASS           │   │
│  │   └── YES → lanjut ke Step 2                                             │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│        │                                                                        │
│        ▼                                                                        │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │ STEP 2: Parse IPv4 Header                                                │   │
│  │   parse_iphdr(&nh, data_end, &iph)                                       │   │
│  │   → Bounds check (variable length: iph->ihl * 4)                        │   │
│  │   → Sanity check: hdrsize >= sizeof(iphdr)?                              │   │
│  │   → Kembalikan ip_proto (IPPROTO_TCP / UDP / ICMP / dll)                │   │
│  │                                                                          │   │
│  │   ip_proto < 0 (malformed)?                                              │   │
│  │   ├── YES → stats_update(STAT_PASS) → XDP_PASS                          │   │
│  │   └── NO  → lanjut ke Step 3                                             │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│        │                                                                        │
│        ▼                                                                        │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │ STEP 3: Firewall L3                                                      │   │
│  │                                                                          │   │
│  │ [3a] block_fragments == 1?                                               │   │
│  │       iph->frag_off & (IP_MF | IP_OFFSET) != 0?                         │   │
│  │       └── YES → STAT_DROP + emit_security → XDP_DROP                    │   │
│  │                                                                          │   │
│  │ [3b] block_broadcast == 1?                                               │   │
│  │       iph->daddr == 0xFFFFFFFF (255.255.255.255)?                        │   │
│  │       └── YES → STAT_DROP + emit_security → XDP_DROP                    │   │
│  │                                                                          │   │
│  │ [3c] block_multicast == 1?                                               │   │
│  │       (ntohl(iph->daddr) & 0xF0000000) == 0xE0000000 (224.0.0.0/4)?    │   │
│  │       └── YES → STAT_DROP + emit_security → XDP_DROP                    │   │
│  │                                                                          │   │
│  │ [3d] blocked_protos[iph->protocol] != 0?                                 │   │
│  │       bpf_map_lookup_elem(&blocked_protos, &proto)                       │   │
│  │       └── YES → STAT_DROP + emit_security → XDP_DROP                    │   │
│  │                                                                          │   │
│  │       Semua lolos → lanjut ke Step 4                                     │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│        │                                                                        │
│        ▼                                                                        │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │ STEP 4+5: Parse L4 Header + Firewall L4                                  │   │
│  │                                                                          │   │
│  │ BRANCH ip_proto:                                                         │   │
│  │                                                                          │   │
│  │ ┌── ip_proto == IPPROTO_TCP (6) ─────────────────────────────────────┐  │   │
│  │ │  parse_tcphdr(&nh, data_end, &tcph)                                 │  │   │
│  │ │  → Variable-length (tcph->doff * 4), bounds checked                 │  │   │
│  │ │  l4_sport = ntohs(tcph->source)                                     │  │   │
│  │ │  l4_dport = ntohs(tcph->dest)                                       │  │   │
│  │ │                                                                      │  │   │
│  │ │  [5a] block_all_tcp == 1?                                            │  │   │
│  │ │        → DROP + emit_security                                         │  │   │
│  │ │                                                                      │  │   │
│  │ │  [5b] block_bad_tcp == 1 && tcp_flags_malformed(tcph)?               │  │   │
│  │ │        Deteksi: NULL scan (no flags), XMAS (FIN+PSH+URG),            │  │   │
│  │ │                 SYN+FIN, RST+FIN                                      │  │   │
│  │ │        → DROP + emit_security                                         │  │   │
│  │ │                                                                      │  │   │
│  │ │  [5c] blocked_ports_tcp[l4_dport] != 0?                              │  │   │
│  │ │        bpf_map_lookup_elem(&blocked_ports_tcp, &dport)               │  │   │
│  │ │        → DROP + emit_security                                         │  │   │
│  │ └─────────────────────────────────────────────────────────────────────┘  │   │
│  │                                                                          │   │
│  │ ┌── ip_proto == IPPROTO_UDP (17) ────────────────────────────────────┐  │   │
│  │ │  parse_udphdr(&nh, data_end, &udph)                                 │  │   │
│  │ │  l4_sport = ntohs(udph->source)                                     │  │   │
│  │ │  l4_dport = ntohs(udph->dest)                                       │  │   │
│  │ │                                                                      │  │   │
│  │ │  [5d] block_all_udp == 1? → DROP + emit_security                    │  │   │
│  │ │  [5e] blocked_ports_udp[l4_dport] != 0? → DROP + emit_security      │  │   │
│  │ └─────────────────────────────────────────────────────────────────────┘  │   │
│  │                                                                          │   │
│  │ ┌── ip_proto == IPPROTO_ICMP (1) ────────────────────────────────────┐  │   │
│  │ │  parse_icmphdr_common(&nh, data_end, &icmph)                        │  │   │
│  │ │  [5f] block_icmp_ping == 1 && icmph->type == ICMP_ECHO (8)?         │  │   │
│  │ │        → DROP + emit_security                                         │  │   │
│  │ └─────────────────────────────────────────────────────────────────────┘  │   │
│  │                                                                          │   │
│  │   [jika parse L4 gagal → goto fwd_check (skip L4 firewall)]             │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│        │                                                                        │
│        ▼                                                                        │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │ STEP 6: TTL Guard                                                        │   │
│  │   iph->ttl <= 1?                                                         │   │
│  │   ├── YES → STAT_TTL_EXCEEDED + emit_security → XDP_PASS               │   │
│  │   │         (kernel akan kirim ICMP Time Exceeded ke sender)            │   │
│  │   └── NO  → lanjut ke Step 7                                            │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│        │                                                                        │
│        ▼                                                                        │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │ STEP 7: Forwarding Table Lookup                                          │   │
│  │   entry = bpf_map_lookup_elem(&fwd_table, &iph->daddr)                  │   │
│  │   Key: dst IP (network byte order, __be32)                               │   │
│  │                                                                          │   │
│  │   entry == NULL (tidak ada entry)?                                       │   │
│  │   ├── YES → STAT_PASS + emit_sampled → XDP_PASS                        │   │
│  │   │         (packet naik ke kernel networking stack / routing)           │   │
│  │   └── NO  → lanjut ke Step 8                                            │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│        │                                                                        │
│        ▼                                                                        │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │ STEP 8: MAC Rewrite + TTL Decrement                                      │   │
│  │   memcpy(eth->h_dest,   entry->dst_mac, ETH_ALEN)  ← tulis next-hop MAC│   │
│  │   memcpy(eth->h_source, entry->src_mac, ETH_ALEN)  ← tulis egress MAC  │   │
│  │   ip_decrease_ttl(iph):                                                  │   │
│  │     check += htons(0x0100)        ← RFC 1624 incremental checksum       │   │
│  │     iph->check = check + (carry)                                         │   │
│  │     iph->ttl--                                                           │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│        │                                                                        │
│        ▼                                                                        │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │ STEP 9: Forward                                                          │   │
│  │                                                                          │   │
│  │   entry->action == FWD_ACTION_TX (0)?                                    │   │
│  │   ├── YES → STAT_TX + emit_sampled                                      │   │
│  │   │         return XDP_TX   ← hairpin: kirim kembali lewat NIC SAMA     │   │
│  │   │                                                                      │   │
│  │   └── NO  → STAT_REDIRECT + emit_sampled                                │   │
│  │             return bpf_redirect_map(&tx_port, entry->tx_port_key, PASS) │   │
│  │             ← redirect ke NIC LAIN via DEVMAP (tanpa kernel stack)      │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                                                                 │
│  RINGKASAN OUTPUT XDP:                                                          │
│  ┌────────────────┬──────────────────────────────────────────────────────┐     │
│  │ XDP_DROP       │ Paket dibuang di driver level (paling efisien)       │     │
│  │ XDP_PASS       │ Paket naik ke kernel TCP/IP stack (normal path)      │     │
│  │ XDP_TX         │ Paket dikirim kembali lewat NIC yang sama (hairpin)  │     │
│  │ XDP_REDIRECT   │ Paket diteruskan ke NIC lain via DEVMAP              │     │
│  └────────────────┴──────────────────────────────────────────────────────┘     │
└────────────────────────────────────────────────────────────────────────────────┘
```

### Validasi Fase 4

```bash
# 4a. Monitor XDP stats secara real-time (gunakan -stats mode)
sudo /home/telmat/final_t40/xdp-go-optimized/xdpd -iface eth0 -stats -stats-interval 2

# 4b. Lihat stats via API
curl -s http://localhost:8080/api/stats/live | python3 -m json.tool

# 4c. Baca stats langsung dari BPF map (tanpa daemon)
sudo bpftool map dump pinned /sys/fs/bpf/eth0/xdp_stats

# 4d. Uji STEP 1 — paket non-IPv4 (ARP) harus PASS
# Kirim ARP dari mesin lain, cek STAT_PASS naik
arping -c 3 <ip-target> -I eth0
curl -s http://localhost:8080/api/stats/live | python3 -m json.tool | grep pass

# 4e. Uji STEP 3 — blokir broadcast
# Aktifkan block_broadcast dulu
curl -X PUT http://localhost:8080/api/config \
  -H "Content-Type: application/json" \
  -d '{"firewall_flags": {"block_broadcast": true}}'
# Kirim broadcast, cek STAT_DROP naik

# 4f. Uji STEP 4+5 — blokir port SSH (22)
# Port 22 sudah ada di default blocklist
# Coba konek SSH dari mesin lain: seharusnya langsung drop (bukan timeout)
# ssh -v user@<ip-xdpd-node>
# Di sisi xdpd, stats DROP harus naik

# 4g. Uji STEP 5b — TCP malformed flags (NULL scan)
# Install nmap, lakukan NULL scan
sudo nmap -sN <ip-target>
# Cek STAT_DROP naik (jika block_malformed_tcp aktif)

# 4h. Uji STEP 6 — TTL guard
# Kirim paket dengan TTL=1
sudo hping3 --ttl 1 -c 5 <ip-target>
# Cek STAT_TTL_EXCEEDED naik
curl -s http://localhost:8080/api/stats/live | python3 -m json.tool

# 4i. Uji STEP 7 — forwarding lookup (jika ada route)
curl -s http://localhost:8080/api/routes | python3 -m json.tool

# 4j. Pantau BPF trace (debugging, perlu kernel CONFIG_FTRACE)
# CATATAN: bpf_printk sudah dihapus dari hot path di versi optimized ini
sudo cat /sys/kernel/debug/tracing/trace_pipe

# 4k. Verifikasi XDP program bytecode (advanced)
sudo bpftool prog dump xlated name xdp_firewall_fwd | head -50
```

---

## 6. Fase 5 — BPF Maps (Shared State)

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│  FASE 5: BPF MAPS — Mekanisme Komunikasi Kernel ↔ Userspace                    │
│                                                                                  │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │ xdp_stats [PERCPU_ARRAY]                                                   │ │
│  │   Key: enum xdp_stat_key (0=DROP,1=TX,2=REDIRECT,3=PASS,4=TTL_EXCEEDED)   │ │
│  │   Value: struct stats_rec { packets u64, bytes u64 }                       │ │
│  │   Per-CPU: tidak ada atomic contention → zero overhead di hot path         │ │
│  │   Userspace: jumlahkan semua CPU core untuk total                          │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                  │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │ blocked_ports_tcp / blocked_ports_udp [ARRAY, 65536 entries]               │ │
│  │   Key: port number (0-65535)                                               │ │
│  │   Value: u8 (0=allow, 1=block)                                             │ │
│  │   Lookup: O(1), langsung index ke array → sangat cepat                     │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                  │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │ blocked_protos [ARRAY, 256 entries]                                         │ │
│  │   Key: IP protocol number (IPPROTO_TCP=6, IPPROTO_UDP=17, dll.)            │ │
│  │   Value: u8 (0=allow, 1=block)                                             │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                  │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │ fw_config [ARRAY, 9 entries = FW_CFG_MAX]                                  │ │
│  │   Key: enum fw_config_key                                                  │ │
│  │   Value: u8 flag (0=off, 1=on)                                             │ │
│  │   Keys:                                                                    │ │
│  │     0 = FW_CFG_BLOCK_ICMP_PING                                             │ │
│  │     1 = FW_CFG_BLOCK_IP_FRAGMENTS                                          │ │
│  │     2 = FW_CFG_BLOCK_MALFORMED_TCP                                         │ │
│  │     3 = FW_CFG_BLOCK_ALL_TCP                                               │ │
│  │     4 = FW_CFG_BLOCK_ALL_UDP                                               │ │
│  │     5 = FW_CFG_BLOCK_BROADCAST                                             │ │
│  │     6 = FW_CFG_BLOCK_MULTICAST                                             │ │
│  │     7 = FW_CFG_EVENTS_ENABLED     (0=off=turbo mode)                       │ │
│  │     8 = FW_CFG_SECURITY_EVENTS    (1=on default)                           │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                  │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │ fwd_table [HASH MAP, max 4096 entries]                                     │ │
│  │   Key:   __be32 dst IP (network byte order)  ← exact match                │ │
│  │   Value: struct fwd_entry {                                                 │ │
│  │            dst_mac[6]    — MAC tujuan (next-hop)                           │ │
│  │            src_mac[6]    — MAC source (egress interface)                   │ │
│  │            tx_port_key   — DEVMAP slot untuk XDP_REDIRECT                  │ │
│  │            action        — FWD_ACTION_TX(0) atau REDIRECT(1)              │ │
│  │          }                                                                  │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                  │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │ tx_port [DEVMAP, max 16 entries]                                            │ │
│  │   Key:   u32 slot number                                                   │ │
│  │   Value: u32 ifindex dari egress NIC                                       │ │
│  │   Slot 0: egress NIC utama (seeded dari -redirect-dev flag)               │ │
│  │   Digunakan oleh bpf_redirect_map() untuk fast path ke NIC lain           │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                  │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │ packet_events [RING BUFFER, 256 KB]                                         │ │
│  │   Kernel → Userspace event stream                                          │ │
│  │   struct packet_event { timestamp_ns, src_ip, dst_ip,                     │ │
│  │                          src_port, dst_port, protocol, action, pkt_len }   │ │
│  │   Security events (DROP/TTL_EXCEEDED): SELALU emit (tidak di-sample)      │ │
│  │   Normal events (PASS/TX/REDIRECT): sample 1 per 1000 paket per CPU       │ │
│  │   BPF_RB_NO_WAKEUP: batch wakeup → eliminasi per-event context switch     │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                  │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │ sample_counter [PERCPU_ARRAY, 1 entry]                                     │ │
│  │   Key: 0                                                                   │ │
│  │   Value: u64 counter per CPU                                               │ │
│  │   Digunakan di emit_event_sampled() untuk SAMPLE_RATE=1000 logic          │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### Validasi Fase 5

```bash
# 5a. Dump semua entries fwd_table
sudo bpftool map dump pinned /sys/fs/bpf/eth0/fwd_table
# atau via API:
curl -s http://localhost:8080/api/routes | python3 -m json.tool

# 5b. Dump DEVMAP tx_port
sudo bpftool map dump pinned /sys/fs/bpf/eth0/tx_port
# atau:
curl -s http://localhost:8080/api/devmap | python3 -m json.tool

# 5c. Cek fw_config flags secara detail
sudo bpftool map dump pinned /sys/fs/bpf/eth0/fw_config
# Key 0 = block_icmp_ping, Key 8 = security_events_enabled, dll.

# 5d. Monitor ring buffer packet events secara live (advanced)
# Perlu bpftool versi baru yang support ring buffer
sudo bpftool map dump pinned /sys/fs/bpf/eth0/packet_events

# 5e. Verifikasi blocked ports default TCP
sudo bpftool map lookup pinned /sys/fs/bpf/eth0/blocked_ports_tcp key 22 0 0 0
# key: port 22 dalam little-endian 4 bytes = 0x16 0x00 0x00 0x00
sudo bpftool map lookup pinned /sys/fs/bpf/eth0/blocked_ports_tcp \
  key hex 16 00 00 00
# value: 01 → port 22 diblokir

# 5f. Tambah route forwarding via API dan verifikasi
curl -X POST http://localhost:8080/api/routes \
  -H "Content-Type: application/json" \
  -d '{
    "ip": "10.0.0.2",
    "dst_mac": "aa:bb:cc:dd:ee:ff",
    "src_mac": "11:22:33:44:55:66",
    "action": "redirect",
    "port_key": 0
  }'

# Verifikasi masuk ke BPF map
sudo bpftool map dump pinned /sys/fs/bpf/eth0/fwd_table

# 5g. Toggle flag firewall dan verifikasi perubahan
curl -X PUT http://localhost:8080/api/config \
  -H "Content-Type: application/json" \
  -d '{"firewall_flags": {"block_icmp_ping": true}}'

# Cek fw_config key 0 berubah jadi 1
sudo bpftool map lookup pinned /sys/fs/bpf/eth0/fw_config key 0 0 0 0
```

---

## 7. Fase 6 — Userspace Control Plane (Go)

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│  FASE 6: GO CONTROL PLANE — Manajemen BPF Maps dari Userspace                  │
│                                                                                  │
│  internal/xdp/manager.go                                                        │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │ Manager struct:                                                              │ │
│  │   mu sync.RWMutex    — thread-safe concurrent access                       │ │
│  │   ifname string      — NIC yang ter-attach                                  │ │
│  │   redirectDev string — egress NIC untuk REDIRECT                            │ │
│  │   objs XdpProgObjects — handle ke semua BPF maps                            │ │
│  │   xdpLink link.Link  — BPF link handle (detach saat Close())               │ │
│  │   egressLink link.Link — XDP pass program di egress NIC                    │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                  │
│  internal/maps/*.go — Helpers untuk operasi BPF maps:                           │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │ maps/ports.go   : AddPort, RemovePort, ListPorts, SetPorts                  │ │
│  │ maps/config.go  : ReadFlags, WriteFlags, SetFlag                            │ │
│  │ maps/routes.go  : AddRoute, DeleteRoute, ListRoutes                         │ │
│  │                   SetDevmapSlot, ListDevmapSlots, DeleteDevmapSlot          │ │
│  │ maps/stats.go   : PollStats, ReadStats (sum PERCPU values)                  │ │
│  │ maps/protos.go  : SetProtos                                                  │ │
│  │ maps/ringbuf.go : ConsumeRingBuf (100ms batch → SQLite)                    │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                  │
│  internal/api/*.go — HTTP Handlers:                                             │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │ GET  /api/status          → mgr.IsAttached() + ifname                      │ │
│  │ POST /api/start           → mgr.Start() + mulai ConsumeRingBuf goroutine   │ │
│  │ POST /api/stop            → cancel ringbuf goroutine + mgr.Stop()          │ │
│  │ POST /api/restart         → Stop() + Start()                               │ │
│  │ GET  /api/config          → baca semua BPF maps → JSON                     │ │
│  │ PUT  /api/config          → tulis ke BPF maps + persist turbo.json         │ │
│  │ GET  /api/stats/live      → baca xdp_stats PERCPU map → sum → JSON         │ │
│  │ GET  /api/logs            → query SQLite traffic_logs table                 │ │
│  │ GET  /api/routes          → ListRoutes(fwd_table)                           │ │
│  │ POST /api/routes          → AddRoute(fwd_table)                             │ │
│  │ DEL  /api/routes/{ip}     → DeleteRoute(fwd_table)                          │ │
│  │ GET  /api/devmap          → ListDevmapSlots(tx_port)                        │ │
│  │ POST /api/devmap          → SetDevmapSlot(tx_port)                          │ │
│  │ DEL  /api/devmap/{slot}   → DeleteDevmapSlot(tx_port)                       │ │
│  │ GET  /api/system/cpu      → baca CPU affinity / isolasi                     │ │
│  │ PUT  /api/system/cpu      → set CPU affinity                                │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### Validasi Fase 6

```bash
# 6a. Test semua API endpoints

# Status
curl -s http://localhost:8080/api/status | python3 -m json.tool

# Start/Stop XDP
curl -X POST http://localhost:8080/api/start
curl -X POST http://localhost:8080/api/stop
curl -X POST http://localhost:8080/api/restart

# Config read/write
curl -s http://localhost:8080/api/config | python3 -m json.tool

# Live stats
curl -s http://localhost:8080/api/stats/live | python3 -m json.tool

# Traffic logs
curl -s "http://localhost:8080/api/logs?limit=10" | python3 -m json.tool

# Routes
curl -s http://localhost:8080/api/routes | python3 -m json.tool

# 6b. Verifikasi turbo.json config persistence
cat /home/telmat/final_t40/xdp-go-optimized/turbo.json

# 6c. Cek goroutine ring buffer consumer aktif
# Cek apakah SQLite DB mendapat data setelah traffic masuk
sqlite3 /tmp/xdpd.db "SELECT count(*) FROM traffic_logs;"
sqlite3 /tmp/xdpd.db "SELECT * FROM traffic_logs ORDER BY id DESC LIMIT 5;"

# 6d. Verifikasi Manager thread-safety (bisa test concurrent API calls)
for i in {1..10}; do
  curl -s http://localhost:8080/api/stats/live &
done
wait
# Semua harus return valid JSON tanpa error
```

---

## 8. Fase 7 — Ring Buffer → SQLite Pipeline

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│  FASE 7: RING BUFFER EVENT PIPELINE                                             │
│                                                                                  │
│  KERNEL SIDE (xdp_prog_kern.c):                                                 │
│                                                                                  │
│  emit_event_security():                    emit_event_sampled():                │
│  ┌──────────────────────────────┐          ┌──────────────────────────────┐    │
│  │ Dipanggil untuk:             │          │ Dipanggil untuk:             │    │
│  │ DROP / TTL_EXCEEDED          │          │ PASS / TX / REDIRECT         │    │
│  │                              │          │                              │    │
│  │ SELALU emit (100%)           │          │ sample_counter++ per CPU     │    │
│  │                              │          │ if (counter % 1000 != 0)     │    │
│  │ bpf_ringbuf_output(          │          │   return  ← skip 999/1000   │    │
│  │   &packet_events,            │          │                              │    │
│  │   &ev, sizeof(ev),           │          │ bpf_ringbuf_output(          │    │
│  │   BPF_RB_NO_WAKEUP)          │          │   &packet_events,            │    │
│  │                              │          │   &ev, sizeof(ev),           │    │
│  │ BPF_RB_NO_WAKEUP:            │          │   BPF_RB_NO_WAKEUP)          │    │
│  │ wakeup di-batch, bukan       │          │                              │    │
│  │ per-event (kurangi ctx-sw)   │          │                              │    │
│  └──────────────────────────────┘          └──────────────────────────────┘    │
│                           │                              │                       │
│                           └──────────┬───────────────────┘                       │
│                                      ▼                                           │
│                          [packet_events RING BUFFER — 256 KB]                   │
│                                      │                                           │
│  USERSPACE SIDE (maps/ringbuf.go):   │                                           │
│                                      ▼                                           │
│  ┌─────────────────────────────────────────────────────────────────────────┐    │
│  │ ConsumeRingBuf() goroutine (berjalan terus selama XDP attached)          │    │
│  │                                                                          │    │
│  │  ringbuf.NewReader(m) → buka reader dari RING BUFFER                    │    │
│  │                                                                          │    │
│  │  LOOP:                                                                   │    │
│  │    rd.Read() → terima raw sample                                         │    │
│  │    binary.Read(LittleEndian) → unmarshal ke struct packetEvent           │    │
│  │    toTrafficLog(ev) → convert ke db.TrafficLog                           │    │
│  │    append ke buf []TrafficLog                                            │    │
│  │                                                                          │    │
│  │    FLUSH ketika:                                                         │    │
│  │      len(buf) >= 500 (batchSize)                                         │    │
│  │      ATAU ticker.C (setiap 100ms)                                        │    │
│  │                                                                          │    │
│  │    store.BatchInsert(context, buf) → INSERT ke SQLite                    │    │
│  └─────────────────────────────────────────────────────────────────────────┘    │
│                                      │                                           │
│                                      ▼                                           │
│                          [SQLite: /tmp/xdpd.db]                                 │
│                          table: traffic_logs                                     │
│                          (timestamp, src_ip, dst_ip, src_port, dst_port,        │
│                           protocol, action, pkt_len)                            │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### Validasi Fase 7

```bash
# 7a. Cek SQLite database aktif
ls -lh /tmp/xdpd.db

# 7b. Verifikasi data masuk ke DB setelah ada traffic
sqlite3 /tmp/xdpd.db "SELECT count(*) FROM traffic_logs;"

# 7c. Lihat event terbaru
sqlite3 /tmp/xdpd.db \
  "SELECT datetime(timestamp_ns/1000000000, 'unixepoch'), \
          src_ip, dst_ip, src_port, dst_port, protocol, action \
   FROM traffic_logs ORDER BY id DESC LIMIT 10;"

# 7d. Cek hanya events DROP (security events)
sqlite3 /tmp/xdpd.db \
  "SELECT * FROM traffic_logs WHERE action = 0 ORDER BY id DESC LIMIT 5;"
# action 0 = PKT_ACTION_DROP

# 7e. Verifikasi sampling ratio
# Kirim 10000 UDP paket, cek berapa yang masuk ke DB (harusnya ~10 = 0.1%)
# (pakai hping3 atau pktgen)
hping3 --udp -c 10000 <target-ip>
sqlite3 /tmp/xdpd.db "SELECT count(*) FROM traffic_logs WHERE action = 1;"
# action 1 = PASS, harusnya ~10 (1000 sampling rate)

# 7f. Query via API
curl -s "http://localhost:8080/api/logs?limit=20&action=drop" \
  | python3 -m json.tool

# 7g. Verifikasi ring buffer size (256 KB)
sudo bpftool map show pinned /sys/fs/bpf/eth0/packet_events
# max_entries: 262144 (256*1024)
```

---

## 9. Fase 8 — REST API & Dashboard

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│  FASE 8: REST API & REACT DASHBOARD                                             │
│                                                                                  │
│  HTTP Server (:8080)                                                             │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │ Router (go-chi/chi):                                                        │ │
│  │   Middleware: Logger, Recoverer, CORS (permissive untuk dev)               │ │
│  │                                                                             │ │
│  │   /api/*     → Go handlers (JSON responses)                                │ │
│  │   /*         → SPA handler → serve React frontend/dist/                   │ │
│  │               fallback ke index.html untuk React Router                    │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                  │
│  React Frontend (frontend/src/):                                                 │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │ Pages:                                                                      │ │
│  │   /                  → App.tsx (dashboard utama + status)                  │ │
│  │   /monitoring        → Monitoring.tsx (live stats charts)                  │ │
│  │   /routes            → Routes.tsx (manage fwd_table)                       │ │
│  │   /firewall          → FirewallConfig.tsx (manage fw flags + ports)        │ │
│  │                                                                             │ │
│  │ Components:                                                                 │ │
│  │   PortList.tsx        → render daftar blocked ports                         │ │
│  │   StatusBadge.tsx     → status indicator (attached/detached)               │ │
│  │                                                                             │ │
│  │ API Client: frontend/src/api/client.ts                                     │ │
│  │   → fetch() calls ke /api/* endpoints                                      │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### Validasi Fase 8

```bash
# 8a. Cek HTTP server responding
curl -I http://localhost:8080/
# HTTP 200 OK + Content-Type: text/html

# 8b. Cek semua API endpoints
curl -s http://localhost:8080/api/status
curl -s http://localhost:8080/api/config
curl -s http://localhost:8080/api/stats/live
curl -s http://localhost:8080/api/routes
curl -s http://localhost:8080/api/devmap
curl -s "http://localhost:8080/api/logs?limit=5"

# 8c. Verifikasi SPA fallback (React Router paths)
curl -s http://localhost:8080/monitoring | head -5
# Harus return index.html (bukan 404)

# 8d. Test CORS headers
curl -I -X OPTIONS http://localhost:8080/api/status \
  -H "Origin: http://localhost:3000" \
  -H "Access-Control-Request-Method: GET"
# Harus ada: Access-Control-Allow-Origin: *

# 8e. Buka dashboard di browser
# http://localhost:8080 → dashboard utama
# http://localhost:8080/monitoring → live stats
# http://localhost:8080/routes → forwarding table
# http://localhost:8080/firewall → firewall config

# 8f. Test timeout handling (WriteTimeout=30s, ReadTimeout=10s)
curl --max-time 35 http://localhost:8080/api/stats/live
```

---

## 10. Ringkasan Validasi Per Langkah

Tabel cepat untuk memvalidasi setiap komponen sistem:

| Langkah | Komponen | Command Validasi | Expected Output |
|---------|----------|-----------------|-----------------|
| 1 | Binary xdpd | `file xdpd` | ELF 64-bit executable |
| 1 | BPF .o file | `ls internal/bpfobj/*.o` | xdpprog_bpfel.o ada |
| 2 | Daemon running | `ps aux \| grep xdpd` | process xdpd tampil |
| 2 | HTTP listen | `ss -tlnp \| grep 8080` | LISTEN 0.0.0.0:8080 |
| 3 | XDP attached | `ip link show eth0` | baris "xdp" muncul |
| 3 | BPF prog load | `sudo bpftool prog list \| grep xdp_firewall` | prog id + name |
| 3 | Maps pinned | `ls /sys/fs/bpf/eth0/` | 8 file maps |
| 4 | XDP stats | `curl /api/stats/live` | JSON dengan drop/pass/tx |
| 4 | Drop test | kirim ke port 22 + cek stats | DROP count naik |
| 5 | fwd_table | `sudo bpftool map dump pinned .../fwd_table` | entries muncul |
| 5 | fw_config | `sudo bpftool map dump pinned .../fw_config` | key 8 = value 1 |
| 6 | API routes | `curl /api/routes` | JSON array routes |
| 6 | Config persist | `cat turbo.json` | port lists tersimpan |
| 7 | SQLite DB | `sqlite3 /tmp/xdpd.db ".tables"` | traffic_logs ada |
| 7 | Events log | `curl /api/logs` | JSON traffic entries |
| 8 | Dashboard | `curl -I http://localhost:8080/` | HTTP 200 OK |
| 8 | SPA routing | `curl http://localhost:8080/monitoring` | HTML content |

---

## Diagram Aliran Data Lengkap (End-to-End)

```
PAKET NETWORK
     │
     ▼
┌─────────────┐      XDP_DROP → ────────────────────────────────────┐
│ NIC DRIVER  │                                                      │
│ (hardware)  │      XDP_PASS → ──┐                                  │
└─────────────┘                   │                                  │
     │ attach XDP                 ▼                                  │
     │                   ┌──────────────┐                           │
     ▼                   │ KERNEL TCP/IP│                           │
┌─────────────────────┐  │    STACK     │                           │
│  xdp_firewall_fwd() │  └──────────────┘                          │
│  (BPF Program)      │                                              │
│                     │      XDP_TX  → ──────────────────────┐      │
│  Step 1: Eth parse  │                                       │      │
│  Step 2: IP parse   │      XDP_REDIRECT → ─────────────┐   │      │
│  Step 3: FW L3      │                                   │   │      │
│  Step 4+5: FW L4    │                                   ▼   ▼      │
│  Step 6: TTL guard  │                           ┌──────────────┐   │
│  Step 7: FWD lookup │                           │  EGRESS NIC  │   │
│  Step 8: MAC rewrite│                           │  (eth1 dst)  │   │
│  Step 9: Forward    │                           └──────────────┘   │
└─────────────────────┘                                              │
     │                                                               │
     │ bpf_ringbuf_output                                            │
     ▼                                                               ▼
┌──────────────┐     ┌─────────────────┐     ┌──────────────────────────┐
│ packet_events│────▶│ ConsumeRingBuf() │────▶│   SQLite /tmp/xdpd.db   │
│ (RING BUFFER)│     │  (Go goroutine)  │     │   table: traffic_logs   │
└──────────────┘     └─────────────────┘     └──────────────────────────┘
                                                          │
     │                                                    │
     │ BPF maps R/W                                       ▼
     ▼                                          ┌─────────────────┐
┌──────────────────────────────────┐            │  HTTP REST API  │
│  Go Control Plane (xdpd daemon)  │◀──────────▶│  /api/logs      │
│                                  │            │  /api/stats     │
│  xdp/manager.go                  │            │  /api/config    │
│  maps/*.go                       │            │  /api/routes    │
│  api/*.go                        │            └─────────────────┘
└──────────────────────────────────┘                    │
     │                                                  ▼
     │ /sys/fs/bpf/eth0/ (pinned maps)       ┌─────────────────┐
     │                                        │  React Dashboard │
     ▼                                        │  :8080          │
┌────────────────────────────┐               └─────────────────┘
│  BPF MAPS (kernel objects)  │
│  xdp_stats                  │
│  blocked_ports_tcp          │
│  blocked_ports_udp          │
│  blocked_protos             │
│  fw_config                  │
│  fwd_table                  │
│  tx_port (DEVMAP)           │
└────────────────────────────┘
```

---

## Tools yang Dibutuhkan untuk Validasi

```bash
# Install tools validasi
sudo apt-get install -y linux-tools-common linux-tools-$(uname -r)  # bpftool
sudo apt-get install -y sqlite3       # SQLite CLI
sudo apt-get install -y hping3        # packet generator untuk testing
sudo apt-get install -y nmap          # port scanner untuk uji firewall
sudo apt-get install -y tcpdump       # packet capture verifikasi
sudo apt-get install -y iproute2      # ip, ss commands
sudo apt-get install -y llvm          # llvm-objdump untuk cek BPF bytecode

# Verifikasi bpftool tersedia
sudo bpftool version

# Verifikasi BPF filesystem mounted
mount | grep bpf
# Harus: none on /sys/fs/bpf type bpf
```
