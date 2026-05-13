import csv
import json
import statistics
from pathlib import Path

CACHE_FILE = "metrics_summary.json"


def _load_csv_all_ports(path: Path) -> dict[str, dict[str, dict[str, int]]]:
    """Load a CSV and return {port: {timestamp: {metric: cumulative_value}}}."""
    by_port: dict[str, dict[str, dict[str, int]]] = {}
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            p = row["Port"]
            t = row["Time"]
            by_port.setdefault(p, {}).setdefault(t, {})
            try:
                by_port[p][t][row["Metric"]] = int(row["Value"])
            except ValueError:
                pass
    return by_port


def _peak_for_metric(by_port: dict[str, dict[str, dict[str, int]]], metric: str) -> tuple[str, int]:
    """Return (port, peak_cumulative_value) for the port with the highest last snapshot of metric."""
    best_port, best_val = "0", -1
    for port, by_time in by_port.items():
        times = sorted(by_time.keys())
        if not times:
            continue
        val = by_time[times[-1]].get(metric, 0)
        if val > best_val:
            best_val = val
            best_port = port
    return best_port, best_val


def _compute_deltas(by_time: dict[str, dict[str, int]], metric: str) -> list[int]:
    """Compute per-second deltas from cumulative counters.

    Sorts timestamps, computes consecutive differences, and skips the first 2
    deltas to exclude the ramp-up window (first ~2 seconds of traffic).
    Uses max(0, curr - prev) to handle counter wraps/resets gracefully.
    """
    times = sorted(by_time.keys())
    deltas: list[int] = []
    for i in range(1, len(times)):
        prev_val = by_time[times[i - 1]].get(metric, 0)
        curr_val = by_time[times[i]].get(metric, 0)
        deltas.append(max(0, curr_val - prev_val))
    # Skip first 2 deltas (ramp-up window)
    return deltas[2:] if len(deltas) > 2 else deltas


def _p95(values: list[float]) -> float:
    """Return the 95th percentile using linear interpolation (matches numpy default)."""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    idx = 0.95 * (n - 1)
    lo = int(idx)
    hi = lo + 1
    if hi >= n:
        return float(sorted_vals[-1])
    frac = idx - lo
    return sorted_vals[lo] + frac * (sorted_vals[hi] - sorted_vals[lo])


def _safe_mean(vals: list[float]) -> float:
    return statistics.mean(vals) if vals else 0.0


def _safe_pstdev(vals: list[float]) -> float:
    return statistics.pstdev(vals) if vals else 0.0


def compute_metrics(exp_dir: Path) -> dict:
    """Compute all 8 summary metrics from CSVs under exp_dir.

    Raises FileNotFoundError if required CSVs are missing.
    Returns a plain dict matching MetricsSummary fields.
    """
    node1_ports = _load_csv_all_ports(exp_dir / "node1.csv")
    node4_ports = _load_csv_all_ports(exp_dir / "node4.csv")
    node5_ports = _load_csv_all_ports(exp_dir / "node5.csv")

    # Detect sender: node+port with highest cumulative opackets
    candidates = {
        "node1": (node1_ports, _peak_for_metric(node1_ports, "opackets")),
        "node4": (node4_ports, _peak_for_metric(node4_ports, "opackets")),
        "node5": (node5_ports, _peak_for_metric(node5_ports, "opackets")),
    }
    sender_name = max(candidates, key=lambda k: candidates[k][1][1])
    sender_port, _ = candidates[sender_name][1]
    sender_by_time = candidates[sender_name][0].get(sender_port, {})

    # Detect receiver: node+port with highest cumulative ipackets (excluding sender)
    rx_candidates = {k: v for k, v in {
        "node1": (node1_ports, _peak_for_metric(node1_ports, "ipackets")),
        "node4": (node4_ports, _peak_for_metric(node4_ports, "ipackets")),
        "node5": (node5_ports, _peak_for_metric(node5_ports, "ipackets")),
    }.items() if k != sender_name}
    receiver_name = max(rx_candidates, key=lambda k: rx_candidates[k][1][1])
    receiver_port, _ = rx_candidates[receiver_name][1]
    receiver_by_time = rx_candidates[receiver_name][0].get(receiver_port, {})

    # Forwarded traffic (auto-detected receiver)
    rx_pkt_deltas = _compute_deltas(receiver_by_time, "ipackets")
    rx_byte_deltas = _compute_deltas(receiver_by_time, "ibytes")

    # Sender injection (auto-detected sender)
    tx_pkt_deltas = _compute_deltas(sender_by_time, "opackets")

    # NIC drops (node4 port 0 — XDP router imissed)
    drop_deltas = _compute_deltas(node4_ports.get("0", {}), "imissed")

    # Align tx/rx to the same length for ratio metrics
    n = min(len(rx_pkt_deltas), len(tx_pkt_deltas))
    rx_aligned = rx_pkt_deltas[:n]
    tx_aligned = tx_pkt_deltas[:n]

    loss_series: list[float] = []
    efficiency_series: list[float] = []
    for tx, rx in zip(tx_aligned, rx_aligned):
        if tx > 0:
            loss_series.append(max(0.0, (tx - rx) / tx * 100))
            efficiency_series.append(min(100.0, rx / tx * 100))

    # Add 24 bytes/pkt of Ethernet overhead excluded from DPDK ibytes:
    # preamble+SFD (8) + IFG (12) + CRC stripped by NIC (4) = 24
    gbps_series = [(b + n * 24) * 8 / 1e9 for b, n in zip(rx_byte_deltas, rx_pkt_deltas)]

    return {
        "peak_forwarded_pps": _p95(list(map(float, rx_pkt_deltas))),
        "peak_forwarded_gbps": _p95(gbps_series),
        "sender_injection_pps": _safe_mean(list(map(float, tx_pkt_deltas))),
        "packet_loss_pct": _safe_mean(loss_series),
        "nic_drop_rate_mean": _safe_mean(list(map(float, drop_deltas))),
        "nic_drop_rate_peak": float(max(drop_deltas)) if drop_deltas else 0.0,
        "forwarding_efficiency_pct": _safe_mean(efficiency_series),
        "throughput_std_dev": _safe_pstdev(list(map(float, rx_pkt_deltas))),
    }


def get_or_compute_metrics(exp_dir: Path) -> dict:
    """Return cached metrics if available, otherwise compute and cache them."""
    cache_path = exp_dir / CACHE_FILE
    if cache_path.exists():
        return json.loads(cache_path.read_text())
    result = compute_metrics(exp_dir)
    cache_path.write_text(json.dumps(result, indent=2))
    return result
