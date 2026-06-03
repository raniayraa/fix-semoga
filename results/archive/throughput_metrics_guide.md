# Throughput Metrics Guide

## Topology

```
node1 (pktgen sender)
  port 0 TX → [node4: XDP router] → node5 port 0 RX
  port 1 RX ← (return/reflected traffic)
```

**Traffic profile:** 64-byte UDP, single flow, theoretical max at 10G ≈ **14.88 Mpps**

> All CSV values are **cumulative counters**. Compute per-second deltas: `rate(t) = value[t] - value[t-1]`

---

## Metrics

### 1. Forwarded Throughput *(primary)*
**Source:** `node5` — `ipackets`, `ibytes`

```
pps(t)  = Δipackets / 1s
Gbps(t) = Δibytes × 8 / 1e9
```

The ground truth — packets that actually reached the destination through the XDP router.

---

### 2. Sender Injection Rate *(reference baseline)*
**Source:** `node1` port 0 — `opackets`, `obytes`

```
tx_pps(t)  = Δopackets / 1s
tx_Gbps(t) = Δobytes × 8 / 1e9
```

Confirms the offered load. Low forwarding rate is only meaningful if the sender was actually injecting at line rate.

---

### 3. Packet Loss Rate *(critical)*
```
loss% = (tx_pps - rx_pps) / tx_pps × 100
```

Max throughput alone is misleading — a router forwarding 10 Mpps with 30% loss is not better than one forwarding 8 Mpps with 0% loss.

---

### 4. NIC-Level Drop Rate *(bottleneck locator)*
**Source:** `node4` — `imissed`

```
hw_drop(t) = Δimissed / 1s
```

`imissed` = NIC dropped the packet before XDP ever saw it (RX ring overflow). High `imissed` means the bottleneck is hardware saturation, not XDP logic.

---

### 5. Forwarding Efficiency *(cross-experiment normalization)*
```
efficiency = rx_pps_node5 / tx_pps_node1 × 100%
```

Normalizes results even if sender rates differ slightly between runs.

---

## Experiment Comparison Table

| Metric | Source | Aggregation |
|--------|--------|-------------|
| Peak forwarded PPS | node5 Δipackets | P95 of stable window |
| Peak forwarded Gbps | node5 Δibytes × 8 | P95 of stable window |
| Sender injection rate | node1 port0 Δopackets | mean |
| Packet loss % | (tx − rx) / tx | mean over stable window |
| NIC drop rate (imissed) | node4 Δimissed | mean + peak |
| Forwarding efficiency | rx / tx × 100 | mean |
| Throughput std dev | node5 Δipackets | std dev (stability indicator) |

> **Stable window:** skip first 2 seconds (ramp-up), use the rest. Use **P95** not raw max to avoid transient spikes.

---

## Notes

- `oerrors` on **node5** grows during experiment — verify whether node5 is also transmitting (bidirectional mode). TX queue saturation on the receiver can cap how many packets it accepts and skew results.
- Compare across configurations using **zero-loss max throughput** (RFC 2544 approach): the highest `tx_pps` at which `loss% ≈ 0` is the true max forwarding capacity.
