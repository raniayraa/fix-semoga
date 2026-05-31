/* SPDX-License-Identifier: GPL-2.0 */
/*
 * xdp_prog_kern.c — XDP Firewall + Fast Forwarder (Kernel Program)
 *                   [OPTIMIZED VERSION — High-throughput sampling]
 *
 * Menggabungkan dua program:
 *   1. combine-parser-firewall  — stateless firewall (L3 + L4 rules)
 *   2. fast-forwarding          — XDP fast path (MAC rewrite + XDP_TX/REDIRECT)
 *
 * Optimasi performa vs xdp-go original:
 *   - emit_event() TIDAK dipanggil di setiap paket.
 *   - DROP / TTL_EXCEEDED → selalu emit (security event, FORCE_WAKEUP).
 *   - PASS / TX / REDIRECT → di-sample 1 dari SAMPLE_RATE paket (NO_WAKEUP).
 *   - bpf_printk() dihapus dari hot path (tiap call = write ke trace_pipe = overhead).
 *   - sample_counter menggunakan PERCPU_ARRAY → zero atomic contention.
 *
 * Alur eksekusi per paket:
 * ┌────────────────────────────────────────────────────────────────────────┐
 * │  NIC (ingress)                                                         │
 * │     │                                                                   │
 * │  Step 1: Parse Ethernet → skip non-IPv4 (ARP, IPv6, dll.) → PASS      │
 * │     │                                                                   │
 * │  Step 2: Parse IPv4 header                                              │
 * │     │                                                                   │
 * │  Step 3: Firewall L3                                                    │
 * │     ├─ IP fragment?       → DROP (always emit)                          │
 * │     ├─ Broadcast dst?     → DROP (always emit)                          │
 * │     ├─ Multicast dst?     → DROP (always emit)                          │
 * │     └─ Blocked protocol?  → DROP (always emit)                          │
 * │     │                                                                   │
 * │  Step 4+5: Parse L4 + Firewall L4                                       │
 * │     ├─ TCP: block all / malformed flags / blocked dst port → DROP       │
 * │     ├─ UDP: block all / blocked dst port                   → DROP       │
 * │     └─ ICMP: block echo request (ping)                     → DROP       │
 * │     │                                                                   │
 * │  Step 6: TTL guard → TTL ≤ 1 → PASS (kernel sends ICMP TTL Exceeded)  │
 * │     │                                                                   │
 * │  Step 7: Lookup fwd_table (exact match on dst IP)                      │
 * │     ├─ No entry → PASS (kernel routing handles it)                     │
 * │     └─ Entry found:                                                     │
 * │           Step 8: Rewrite eth->h_dest  = entry->dst_mac               │
 * │                   Rewrite eth->h_source = entry->src_mac               │
 * │                   Decrement TTL + update checksum (RFC 1624)           │
 * │           Step 9: entry->action == TX?       → XDP_TX                 │
 * │                   entry->action == REDIRECT? → bpf_redirect_map()     │
 * └────────────────────────────────────────────────────────────────────────┘
 */

#include <linux/bpf.h>
#include <linux/if_ether.h>
#include <linux/ip.h>
#include <linux/tcp.h>
#include <linux/udp.h>
#include <linux/icmp.h>
#include <linux/in.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_endian.h>

#include "headers/parsing_helpers.h"
#include "common_kern_user.h"

#ifndef memcpy
#define memcpy(dest, src, n) __builtin_memcpy((dest), (src), (n))
#endif

#ifndef IP_MF
#define IP_MF     0x2000
#endif
#ifndef IP_OFFSET
#define IP_OFFSET 0x1FFF
#endif

/*
 * SAMPLE_RATE — 1 dari setiap N paket non-security (PASS/TX/REDIRECT)
 * yang dikirim ke ring buffer. Nilai 1000 artinya ~0.1% traffic dicatat
 * untuk monitoring, tanpa membebani hot path.
 *
 * Ubah ke 100 untuk sampling lebih padat, atau 10000 untuk lebih ringan.
 */
#define SAMPLE_RATE             1000
#define SECURITY_RATE_LIMIT_NS  10000000ULL   /* 10ms → max 100 events/sec/CPU */

/* ═══════════════════════════════════════════════════════════════════════════
 * BPF Maps
 * ═══════════════════════════════════════════════════════════════════════════ */

/*
 * xdp_stats — per-CPU packet/byte counter per action.
 * PERCPU_ARRAY menghindari atomic contention di hot path.
 * Userspace menjumlahkan semua CPU saat menampilkan statistik.
 */
struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__type(key,   __u32);
	__type(value, struct stats_rec);
	__uint(max_entries, STAT_MAX);
} xdp_stats SEC(".maps");

/* Firewall: TCP destination ports yang diblokir */
struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__type(key,   __u32);
	__type(value, __u8);
	__uint(max_entries, 65536);
} blocked_ports_tcp SEC(".maps");

/* Firewall: UDP destination ports yang diblokir */
struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__type(key,   __u32);
	__type(value, __u8);
	__uint(max_entries, 65536);
} blocked_ports_udp SEC(".maps");

/* Firewall: IP protocol numbers yang diblokir */
struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__type(key,   __u32);
	__type(value, __u8);
	__uint(max_entries, 256);
} blocked_protos SEC(".maps");

/* Firewall: feature flags on/off (key = enum fw_config_key) */
struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__type(key,   __u32);
	__type(value, __u8);
	__uint(max_entries, FW_CFG_MAX);
} fw_config SEC(".maps");

/*
 * fwd_table — forwarding table.
 * Key:   destination IPv4 (network byte order).
 * Value: struct fwd_entry (next-hop MAC, egress MAC, action, port-key).
 */
struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key,   __be32);
	__type(value, struct fwd_entry);
	__uint(max_entries, FWD_TABLE_MAX_ENTRIES);
} fwd_table SEC(".maps");

/*
 * tx_port — DEVMAP untuk XDP_REDIRECT ke egress NIC.
 * Key:   __u32 slot (stored di fwd_entry.tx_port_key).
 * Value: __u32 ifindex dari egress NIC.
 * Slot 0 = egress NIC utama (diisi oleh -r flag saat attach).
 */
struct {
	__uint(type, BPF_MAP_TYPE_DEVMAP);
	__type(key,   __u32);
	__type(value, __u32);
	__uint(max_entries, FWD_DEVMAP_MAX_ENTRIES);
} tx_port SEC(".maps");

/*
 * packet_events — ring buffer untuk stream per-packet events ke userspace.
 * 256 KB cukup untuk burst traffic; Go consumer membaca dan flush ke SQLite
 * setiap 100ms atau 500 events.
 *
 * Dengan sampling SAMPLE_RATE=1000, volume event turun ~1000x sehingga
 * ring buffer jauh lebih jarang penuh.
 */
struct {
	__uint(type, BPF_MAP_TYPE_RINGBUF);
	__uint(max_entries, 256 * 1024);
} packet_events SEC(".maps");

/*
 * sample_counter — per-CPU counter untuk sampling non-security events.
 * PERCPU_ARRAY = masing-masing CPU punya slot sendiri, zero atomic ops.
 * Key 0 = satu-satunya entry; value = jumlah paket yang diproses CPU ini.
 */
struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__type(key,   __u32);
	__type(value, __u64);
	__uint(max_entries, 1);
} sample_counter SEC(".maps");

/* security_emit_ts — per-CPU timestamp of last emitted security event.
 * Used for time-based rate limiting in emit_event_security(). */
struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__type(key,   __u32);
	__type(value, __u64);
	__uint(max_entries, 1);
} security_emit_ts SEC(".maps");

/* ═══════════════════════════════════════════════════════════════════════════
 * Helpers
 * ═══════════════════════════════════════════════════════════════════════════ */

/*
 * emit_event_security() — kirim event DROP/TTL_EXCEEDED ke ring buffer.
 *
 * Selalu emit tanpa sampling. Menggunakan BPF_RB_NO_WAKEUP agar wakeup
 * di-batch bersama consumer timer (100ms poll) — eliminasi per-DROP
 * context switch yang terjadi dengan BPF_RB_FORCE_WAKEUP.
 */
static __always_inline void emit_event_security(
	__be32 src_ip, __be32 dst_ip,
	__u16 src_port, __u16 dst_port,
	__u8 protocol, __u8 action, __u16 pkt_len)
{
	__u32 key = 0;
	__u64 *last_ts = bpf_map_lookup_elem(&security_emit_ts, &key);

	if (!last_ts)
		return;

	__u64 now = bpf_ktime_get_ns();
	if (now - *last_ts < SECURITY_RATE_LIMIT_NS)
		return;
	*last_ts = now;

	struct packet_event ev = {
		.timestamp_ns = now,
		.src_ip       = src_ip,
		.dst_ip       = dst_ip,
		.src_port     = src_port,
		.dst_port     = dst_port,
		.protocol     = protocol,
		.action       = action,
		.pkt_len      = pkt_len,
	};
	bpf_ringbuf_output(&packet_events, &ev, sizeof(ev), BPF_RB_NO_WAKEUP);
}

/*
 * emit_event_sampled() — kirim event PASS/TX/REDIRECT ke ring buffer,
 * hanya untuk 1 dari setiap SAMPLE_RATE paket per CPU.
 *
 * Menggunakan BPF_RB_NO_WAKEUP — consumer di-wake up secara batch,
 * bukan per-event, untuk mengurangi overhead context switch.
 *
 * sample_counter adalah PERCPU_ARRAY sehingga tidak ada atomic contention.
 */
static __always_inline void emit_event_sampled(
	__be32 src_ip, __be32 dst_ip,
	__u16 src_port, __u16 dst_port,
	__u8 protocol, __u8 action, __u16 pkt_len)
{
	__u32 key = 0;
	__u64 *counter = bpf_map_lookup_elem(&sample_counter, &key);

	if (!counter)
		return;

	(*counter)++;
	if ((*counter % SAMPLE_RATE) != 0)
		return;

	struct packet_event ev = {
		.timestamp_ns = bpf_ktime_get_ns(),
		.src_ip       = src_ip,
		.dst_ip       = dst_ip,
		.src_port     = src_port,
		.dst_port     = dst_port,
		.protocol     = protocol,
		.action       = action,
		.pkt_len      = pkt_len,
	};
	bpf_ringbuf_output(&packet_events, &ev, sizeof(ev), BPF_RB_NO_WAKEUP);
}

static __always_inline void stats_update(__u32 key, __u32 bytes)
{
	struct stats_rec *rec = bpf_map_lookup_elem(&xdp_stats, &key);

	if (rec) {
		rec->packets++;
		rec->bytes += bytes;
	}
}

static __always_inline int fw_cfg_enabled(__u32 key)
{
	__u8 *val = bpf_map_lookup_elem(&fw_config, &key);

	return val && *val;
}

/*
 * tcp_flags_malformed() — deteksi TCP scan techniques:
 *   NULL scan  : tidak ada flag (probe untuk bypass firewall)
 *   XMAS scan  : FIN + PSH + URG
 *   SYN + FIN  : kontradiktif (tidak valid per RFC)
 *   RST + FIN  : kontradiktif (dipakai scanning tools)
 */
static __always_inline int tcp_flags_malformed(struct tcphdr *tcph)
{
	if (!tcph->fin && !tcph->syn && !tcph->rst &&
	    !tcph->psh && !tcph->ack && !tcph->urg)
		return 1;
	if (tcph->fin && tcph->psh && tcph->urg)
		return 1;
	if (tcph->syn && tcph->fin)
		return 1;
	if (tcph->rst && tcph->fin)
		return 1;
	return 0;
}

/*
 * ip_decrease_ttl() — decrement TTL dan update checksum secara inkremental.
 * RFC 1624 §3: carry folding dengan ones'-complement wrap-around.
 */
static __always_inline void ip_decrease_ttl(struct iphdr *iph)
{
	__u32 check = (__u32)iph->check;

	check += bpf_htons(0x0100);
	iph->check = (__u16)(check + (check >= 0xFFFF));
	iph->ttl--;
}

/* ═══════════════════════════════════════════════════════════════════════════
 * XDP Program
 * ═══════════════════════════════════════════════════════════════════════════ */

SEC("xdp")
int xdp_firewall_fwd(struct xdp_md *ctx)
{
	void *data_end = (void *)(long)ctx->data_end;
	void *data     = (void *)(long)ctx->data;
	__u32 pkt_len  = (__u32)(ctx->data_end - ctx->data);

	struct hdr_cursor     nh   = { .pos = data };
	struct ethhdr        *eth;
	struct iphdr         *iph  = NULL;
	struct tcphdr        *tcph;
	struct udphdr        *udph;
	struct icmphdr_common *icmph;
	struct fwd_entry     *entry;
	__u8  *blocked;
	int    eth_type, ip_proto;
	__u16  l4_sport = 0, l4_dport = 0;  /* L4 ports; 0 jika belum di-parse */

	/*
	 * Cek events_enabled SEKALI di awal, simpan di local variable.
	 * Kalau 0 (turbo mode): skip seluruh ring buffer + sample_counter overhead.
	 * Hot path menjadi identik dengan combine-firewall-forwarder.
	 */
	int events_enabled          = fw_cfg_enabled(FW_CFG_EVENTS_ENABLED);
	int security_events_enabled = fw_cfg_enabled(FW_CFG_SECURITY_EVENTS);
	int block_fragments         = fw_cfg_enabled(FW_CFG_BLOCK_IP_FRAGMENTS);
	int block_broadcast         = fw_cfg_enabled(FW_CFG_BLOCK_BROADCAST);
	int block_multicast         = fw_cfg_enabled(FW_CFG_BLOCK_MULTICAST);
	int block_all_tcp           = fw_cfg_enabled(FW_CFG_BLOCK_ALL_TCP);
	int block_bad_tcp           = fw_cfg_enabled(FW_CFG_BLOCK_MALFORMED_TCP);
	int block_all_udp           = fw_cfg_enabled(FW_CFG_BLOCK_ALL_UDP);
	int block_icmp_ping         = fw_cfg_enabled(FW_CFG_BLOCK_ICMP_PING);

	/* ── Step 1: Ethernet ─────────────────────────────────────────────── */

	eth_type = parse_ethhdr(&nh, data_end, &eth);
	if (eth_type != bpf_htons(ETH_P_IP)) {
		stats_update(STAT_PASS, pkt_len);
		if (events_enabled)
			emit_event_sampled(0, 0, 0, 0, 0, PKT_ACTION_PASS, (__u16)pkt_len);
		return XDP_PASS;
	}

	/* ── Step 2: IPv4 ─────────────────────────────────────────────────── */
	ip_proto = parse_iphdr(&nh, data_end, &iph);
	if (ip_proto < 0) {
		stats_update(STAT_PASS, pkt_len);
		if (events_enabled)
			emit_event_sampled(0, 0, 0, 0, 0, PKT_ACTION_PASS, (__u16)pkt_len);
		return XDP_PASS;
	}

	/* ── Step 3: Firewall L3 ──────────────────────────────────────────── */

	if (block_fragments &&
	    (iph->frag_off & bpf_htons(IP_MF | IP_OFFSET))) {
		stats_update(STAT_DROP, pkt_len);
		if (security_events_enabled)
			emit_event_security(iph->saddr, iph->daddr, 0, 0, iph->protocol, PKT_ACTION_DROP, (__u16)pkt_len);
		return XDP_DROP;
	}

	if (block_broadcast &&
	    iph->daddr == 0xFFFFFFFF) {
		stats_update(STAT_DROP, pkt_len);
		if (security_events_enabled)
			emit_event_security(iph->saddr, iph->daddr, 0, 0, iph->protocol, PKT_ACTION_DROP, (__u16)pkt_len);
		return XDP_DROP;
	}

	if (block_multicast &&
	    (bpf_ntohl(iph->daddr) & 0xF0000000) == 0xE0000000) {
		stats_update(STAT_DROP, pkt_len);
		if (security_events_enabled)
			emit_event_security(iph->saddr, iph->daddr, 0, 0, iph->protocol, PKT_ACTION_DROP, (__u16)pkt_len);
		return XDP_DROP;
	}

	{
		__u32 proto = iph->protocol;

		blocked = bpf_map_lookup_elem(&blocked_protos, &proto);
		if (blocked && *blocked) {
			stats_update(STAT_DROP, pkt_len);
			if (security_events_enabled)
				emit_event_security(iph->saddr, iph->daddr, 0, 0, proto, PKT_ACTION_DROP, (__u16)pkt_len);
			return XDP_DROP;
		}
	}

	/* ── Step 4+5: Parse L4 + Firewall L4 ────────────────────────────── */

	if (ip_proto == IPPROTO_TCP) {
		if (parse_tcphdr(&nh, data_end, &tcph) < 0)
			goto fwd_check;

		l4_sport = bpf_ntohs(tcph->source);
		l4_dport = bpf_ntohs(tcph->dest);

		if (block_all_tcp) {
			stats_update(STAT_DROP, pkt_len);
			if (security_events_enabled)
				emit_event_security(iph->saddr, iph->daddr, l4_sport, l4_dport, IPPROTO_TCP, PKT_ACTION_DROP, (__u16)pkt_len);
			return XDP_DROP;
		}

		if (block_bad_tcp &&
		    tcp_flags_malformed(tcph)) {
			stats_update(STAT_DROP, pkt_len);
			if (security_events_enabled)
				emit_event_security(iph->saddr, iph->daddr, l4_sport, l4_dport, IPPROTO_TCP, PKT_ACTION_DROP, (__u16)pkt_len);
			return XDP_DROP;
		}

		{
			__u32 port = l4_dport;

			blocked = bpf_map_lookup_elem(&blocked_ports_tcp, &port);
			if (blocked && *blocked) {
				stats_update(STAT_DROP, pkt_len);
				if (security_events_enabled)
					emit_event_security(iph->saddr, iph->daddr, l4_sport, l4_dport, IPPROTO_TCP, PKT_ACTION_DROP, (__u16)pkt_len);
				return XDP_DROP;
			}
		}

	} else if (ip_proto == IPPROTO_UDP) {
		if (parse_udphdr(&nh, data_end, &udph) < 0)
			goto fwd_check;

		l4_sport = bpf_ntohs(udph->source);
		l4_dport = bpf_ntohs(udph->dest);

		if (block_all_udp) {
			stats_update(STAT_DROP, pkt_len);
			if (security_events_enabled)
				emit_event_security(iph->saddr, iph->daddr, l4_sport, l4_dport, IPPROTO_UDP, PKT_ACTION_DROP, (__u16)pkt_len);
			return XDP_DROP;
		}

		{
			__u32 port = l4_dport;

			blocked = bpf_map_lookup_elem(&blocked_ports_udp, &port);
			if (blocked && *blocked) {
				stats_update(STAT_DROP, pkt_len);
				if (security_events_enabled)
					emit_event_security(iph->saddr, iph->daddr, l4_sport, l4_dport, IPPROTO_UDP, PKT_ACTION_DROP, (__u16)pkt_len);
				return XDP_DROP;
			}
		}

	} else if (ip_proto == IPPROTO_ICMP) {
		if (parse_icmphdr_common(&nh, data_end, &icmph) < 0)
			goto fwd_check;

		if (block_icmp_ping &&
		    icmph->type == ICMP_ECHO) {
			stats_update(STAT_DROP, pkt_len);
			if (security_events_enabled)
				emit_event_security(iph->saddr, iph->daddr, 0, 0, IPPROTO_ICMP, PKT_ACTION_DROP, (__u16)pkt_len);
			return XDP_DROP;
		}
	}

fwd_check:
	/* ── Step 6: TTL Guard ────────────────────────────────────────────── */
	if (iph->ttl <= 1) {
		stats_update(STAT_TTL_EXCEEDED, pkt_len);
		if (security_events_enabled)
			emit_event_security(iph->saddr, iph->daddr, l4_sport, l4_dport, iph->protocol, PKT_ACTION_TTL_EXCEEDED, (__u16)pkt_len);
		return XDP_PASS;
	}

	/* ── Step 7: Forwarding Table Lookup ──────────────────────────────── */
	entry = bpf_map_lookup_elem(&fwd_table, &iph->daddr);
	if (!entry) {
		stats_update(STAT_PASS, pkt_len);
		if (events_enabled)
			emit_event_sampled(iph->saddr, iph->daddr, l4_sport, l4_dport, iph->protocol, PKT_ACTION_PASS, (__u16)pkt_len);
		return XDP_PASS;
	}

	/* ── Step 8: MAC Rewrite + TTL Decrement ──────────────────────────── */
	memcpy(eth->h_dest,   entry->dst_mac, ETH_ALEN);
	memcpy(eth->h_source, entry->src_mac, ETH_ALEN);
	ip_decrease_ttl(iph);

	/* ── Step 9: Forward ──────────────────────────────────────────────── */
	if (entry->action == FWD_ACTION_TX) {
		stats_update(STAT_TX, pkt_len);
		if (events_enabled)
			emit_event_sampled(iph->saddr, iph->daddr, l4_sport, l4_dport, iph->protocol, PKT_ACTION_TX, (__u16)pkt_len);
		return XDP_TX;
	}

	stats_update(STAT_REDIRECT, pkt_len);
	if (events_enabled)
		emit_event_sampled(iph->saddr, iph->daddr, l4_sport, l4_dport, iph->protocol, PKT_ACTION_REDIRECT, (__u16)pkt_len);
	return bpf_redirect_map(&tx_port, entry->tx_port_key, XDP_PASS);
}

char _license[] SEC("license") = "GPL";
