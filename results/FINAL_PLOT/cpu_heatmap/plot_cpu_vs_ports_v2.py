#!/usr/bin/env python3
"""
CPU utilization vs number of blocked ports (1-10), per protocol & traffic variant.

Data source: same folders as aggregate_heatmap.py
  /home/telmat/final_t40/FINAL_PLOT/{Proto}_{start}-{end}_Port_No_Block_{variant}_rep{N}/
Port count = end - start + 1  (e.g. 1024-1033 = 10 ports)
CPU value  = median of (per-timestep mean across all cores), averaged across reps.
"""

import re
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
from collections import defaultdict
from pathlib import Path

RESULTS_DIR = Path(__file__).parent.parent   # /home/telmat/final_t40/FINAL_PLOT
OUTPUT_DIR  = Path(__file__).parent          # /home/telmat/final_t40/FINAL_PLOT/cpu_heatmap

FOLDER_RE = re.compile(
    r'^(?P<proto>Kernel|XDP|VPP)_(?P<start>\d+)-(?P<end>\d+)_Port_No_Block_'
    r'(?P<variant>[\d_]+)_rep\d+(?:_v\d+)?$'
)

PROTOCOLS = ["Kernel", "VPP", "XDP"]
VARIANTS  = ["15", "41", "15_41"]
VARIANT_LABELS = {
    "15":    "Traffic: Port 15 only",
    "41":    "Traffic: Port 41 only",
    "15_41": "Traffic: Port 15 + 41",
}
COLORS  = {"Kernel": "#E74C3C", "VPP": "#3498DB", "XDP": "#2ECC71"}
MARKERS = {"Kernel": "o",       "VPP": "s",        "XDP": "^"}


def parse_mpstat(log_path: Path) -> np.ndarray | None:
    """Return [n_cpus x n_times] utilization matrix or None."""
    data = {}
    try:
        with open(log_path) as f:
            for line in f:
                parts = line.split()
                if len(parts) < 11:
                    continue
                if parts[1] in ("CPU", "all"):
                    continue
                try:
                    cpu_id = int(parts[1])
                except ValueError:
                    continue
                timestamp = parts[0]
                try:
                    idle = float(parts[-1].replace(",", "."))
                except ValueError:
                    continue
                data.setdefault(timestamp, {})[cpu_id] = idle
    except OSError:
        return None
    if not data:
        return None

    timestamps = sorted(data.keys())
    n_cpus  = max(max(v.keys()) for v in data.values()) + 1
    n_times = len(timestamps)
    matrix  = np.full((n_cpus, n_times), np.nan)
    for t_idx, ts in enumerate(timestamps):
        for cpu_id, idle in data[ts].items():
            matrix[cpu_id, t_idx] = 100.0 - idle
    return matrix


def average_matrices(matrices: list) -> np.ndarray:
    """Average reps: trim to shortest duration, pad to most CPUs."""
    n_cpus  = max(m.shape[0] for m in matrices)
    n_times = min(m.shape[1] for m in matrices)
    stack = []
    for m in matrices:
        if m.shape[0] < n_cpus:
            pad = np.full((n_cpus - m.shape[0], m.shape[1]), np.nan)
            m = np.vstack([m, pad])
        stack.append(m[:n_cpus, :n_times])
    return np.nanmean(np.stack(stack, axis=0), axis=0)


def median_cpu(matrix: np.ndarray) -> float:
    """Median of per-timestep mean CPU utilization across all cores."""
    per_timestep = np.nanmean(matrix, axis=0)   # mean across CPUs → 1-D array
    return float(np.nanmedian(per_timestep))


def collect_data() -> dict:
    """
    Returns: data[variant][proto][n_ports] = median_cpu_percent
    """
    # First pass: group folder paths by (proto, variant, n_ports)
    groups: dict = defaultdict(list)  # key=(proto,variant,n_ports) → [Path,...]

    for entry in sorted(RESULTS_DIR.iterdir()):
        if not entry.is_dir():
            continue
        m = FOLDER_RE.match(entry.name)
        if not m:
            continue
        log = entry / "node6_mpstat.log"
        if not log.exists():
            continue
        proto   = m.group("proto")
        variant = m.group("variant")
        start   = int(m.group("start"))
        end     = int(m.group("end"))
        n_ports = end - start + 1
        groups[(proto, variant, n_ports)].append(log)

    # Second pass: parse + filter outlier reps + average → single median value
    data: dict = defaultdict(lambda: defaultdict(dict))
    for (proto, variant, n_ports), logs in groups.items():
        matrices = [parse_mpstat(p) for p in logs]
        matrices = [m for m in matrices if m is not None]
        if not matrices:
            continue

        # Filter outlier reps: exclude any rep whose median deviates >50% from group median
        rep_medians = np.array([median_cpu(m) for m in matrices])
        group_median = np.median(rep_medians)
        if group_median > 0:
            keep = np.abs(rep_medians - group_median) / group_median <= 0.5
            filtered = [m for m, ok in zip(matrices, keep) if ok]
            excluded = [(i + 1, v) for i, (v, ok) in enumerate(zip(rep_medians, keep)) if not ok]
            if excluded:
                print(f"  EXCLUDED outlier reps for {proto}_{n_ports}p_v{variant}: "
                      f"{[f'rep{r}={v:.1f}%' for r, v in excluded]}")
            matrices = filtered if filtered else matrices

        avg = average_matrices(matrices)
        data[variant][proto][n_ports] = median_cpu(avg)

    return data


def plot_variant(variant: str, variant_data: dict, ax):
    for proto in PROTOCOLS:
        proto_data = variant_data.get(proto, {})
        if not proto_data:
            continue
        port_counts = sorted(proto_data.keys())
        cpu_vals    = [proto_data[n] for n in port_counts]
        ax.plot(port_counts, cpu_vals,
                color=COLORS[proto], marker=MARKERS[proto],
                linewidth=2, markersize=7, label=proto)

    ax.set_title(VARIANT_LABELS[variant], fontsize=12, fontweight="bold", pad=8)
    ax.set_xlabel("Number of Blocked Ports", fontsize=11)
    ax.set_ylabel("Median CPU Utilization (%)", fontsize=11)
    ax.set_xticks(range(1, 11))
    ax.set_ylim(bottom=0)
    ax.yaxis.set_major_formatter(ticker.FormatStrFormatter("%.1f"))
    ax.grid(True, linestyle="--", alpha=0.5)
    ax.legend(fontsize=10, loc="upper left")


def main():
    data = collect_data()

    # Print summary table
    for v in VARIANTS:
        print(f"=== Variant: {v} ===")
        for p in PROTOCOLS:
            vals = {n: f"{cpu:.1f}%" for n, cpu in sorted(data[v][p].items())}
            print(f"  {p}: {vals}")
        print()

    # Combined 3-panel figure
    fig, axes = plt.subplots(1, 3, figsize=(18, 5), sharey=False)
    fig.suptitle(
        "CPU Utilization vs Number of Blocked Ports\n"
        "(median over time, averaged across 3 repetitions)",
        fontsize=13, fontweight="bold", y=1.02
    )
    for ax, variant in zip(axes, VARIANTS):
        plot_variant(variant, data[variant], ax)
    plt.tight_layout()
    out = OUTPUT_DIR / "cpu_vs_ports_v2.png"
    plt.savefig(out, dpi=150, bbox_inches="tight")
    print(f"Saved: {out.name}")
    plt.close()

    # Individual figures
    for variant in VARIANTS:
        fig, ax = plt.subplots(figsize=(8, 5))
        plot_variant(variant, data[variant], ax)
        plt.tight_layout()
        out = OUTPUT_DIR / f"cpu_vs_ports_v2_{variant}.png"
        plt.savefig(out, dpi=150, bbox_inches="tight")
        print(f"Saved: {out.name}")
        plt.close()


if __name__ == "__main__":
    main()
