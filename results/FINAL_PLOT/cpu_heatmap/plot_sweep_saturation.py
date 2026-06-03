#!/usr/bin/env python3
"""
Heatmap + line chart distribusi saturasi CPU core XDP (data results/ baru):
  n_ports: 10, 20, ..., 100  (step 10)
  directions: 15, 41, 15_41
  metric: median(100 - %idle) per core over time  (1 repetisi)

Output:
  xdp_sweep_core_saturation.png     — heatmap 3-panel
  xdp_sweep_core_saturation_avg.png — line chart
"""

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
from pathlib import Path

RESULTS_DIR = Path(__file__).parent.parent.parent / "results"
OUTPUT_DIR  = Path(__file__).parent

VARIANTS = ["15", "41", "15_41"]
VARIANT_LABELS = {
    "15":    "Traffic: Port 15 only",
    "41":    "Traffic: Port 41 only",
    "15_41": "Traffic: Port 15 + 41",
}
N_CORES    = 24
PORT_COUNTS = list(range(10, 101, 10))   # [10, 20, ..., 100]

COLORS  = {"15": "#E67E22", "41": "#2980B9", "15_41": "#8E44AD"}
MARKERS = {"15": "o", "41": "s", "15_41": "^"}


def read_from_csv(csv_path: Path) -> np.ndarray | None:
    """Read node6_cpu.csv → [N_CORES x n_times] utilization matrix."""
    try:
        df = pd.read_csv(csv_path)
    except Exception:
        return None
    idle_cols = [f"cpu{i}_%idle" for i in range(N_CORES)]
    if any(c not in df.columns for c in idle_cols):
        return None
    util = 100.0 - df[idle_cols].values.T   # [N_CORES, n_times]
    return util.astype(float)


def read_from_mpstat(log_path: Path) -> np.ndarray | None:
    """Read node6_mpstat.log → [N_CORES x n_times] utilization matrix."""
    data: dict = {}
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
                ts = parts[0]
                try:
                    idle = float(parts[-1].replace(",", "."))
                except ValueError:
                    continue
                data.setdefault(ts, {})[cpu_id] = idle
    except OSError:
        return None
    if not data:
        return None
    timestamps = sorted(data.keys())
    n_times = len(timestamps)
    matrix = np.full((N_CORES, n_times), np.nan)
    for t_idx, ts in enumerate(timestamps):
        for cpu_id, idle in data[ts].items():
            if cpu_id < N_CORES:
                matrix[cpu_id, t_idx] = 100.0 - idle
    return matrix


def read_core_utilization(exp_dir: Path) -> np.ndarray | None:
    """Try node6_cpu.csv first, fall back to node6_mpstat.log."""
    util = read_from_csv(exp_dir / "node6_cpu.csv")
    if util is not None:
        return util
    return read_from_mpstat(exp_dir / "node6_mpstat.log")


def build_matrix(variant: str) -> np.ndarray:
    """Return [N_CORES x len(PORT_COUNTS)] of median CPU utilization per core."""
    matrix = np.full((N_CORES, len(PORT_COUNTS)), np.nan)

    for col_idx, n_ports in enumerate(PORT_COUNTS):
        end_port = 1024 + n_ports - 1
        exp_dir = RESULTS_DIR / f"XDP_1024-{end_port}_Port_No_Block_{variant}"
        util = read_core_utilization(exp_dir)
        if util is None:
            continue
        matrix[:, col_idx] = np.nanmedian(util, axis=1)

    return matrix


# ── Heatmap ──────────────────────────────────────────────────────────────────

def plot_heatmap():
    fig, axes = plt.subplots(1, 3, figsize=(18, 8), sharey=True)
    fig.suptitle(
        "XDP — Distribusi Saturasi CPU Core vs Jumlah Port\n"
        "(median utilization per core, 1 repetisi)",
        fontsize=13, fontweight="bold", y=1.01,
    )

    for ax, variant in zip(axes, VARIANTS):
        mat = build_matrix(variant)

        im = ax.imshow(mat, aspect="auto", cmap="YlOrRd",
                       vmin=0, vmax=100,
                       interpolation="nearest", origin="upper")

        ax.set_title(VARIANT_LABELS[variant], fontsize=11, fontweight="bold", pad=8)
        ax.set_xlabel("Jumlah Port", fontsize=11)
        if ax is axes[0]:
            ax.set_ylabel("CPU Core", fontsize=11)

        ax.set_xticks(range(len(PORT_COUNTS)))
        ax.set_xticklabels([str(n) for n in PORT_COUNTS], fontsize=9)
        ax.set_yticks(range(N_CORES))
        ax.set_yticklabels([f"CPU {i}" for i in range(N_CORES)], fontsize=8)

        for row in range(N_CORES):
            for col in range(len(PORT_COUNTS)):
                val = mat[row, col]
                if np.isnan(val):
                    continue
                txt_color = "white" if val > 55 else "black"
                ax.text(col, row, f"{val:.0f}", ha="center", va="center",
                        fontsize=6, color=txt_color, fontweight="bold")

        cbar = fig.colorbar(im, ax=ax, fraction=0.03, pad=0.03)
        cbar.set_label("CPU Utilization (%)", fontsize=9)

    plt.tight_layout()
    out = OUTPUT_DIR / "xdp_sweep_core_saturation.png"
    plt.savefig(out, dpi=150, bbox_inches="tight")
    print(f"Saved: {out.name}")
    plt.close()


# ── Line chart ────────────────────────────────────────────────────────────────

def plot_avg():
    fig, ax = plt.subplots(figsize=(10, 5.5))

    for variant in VARIANTS:
        mat = build_matrix(variant)          # [24 x 10]
        avg_per_port = np.nanmean(mat, axis=0)

        ax.plot(PORT_COUNTS, avg_per_port,
                color=COLORS[variant], marker=MARKERS[variant],
                linewidth=2, markersize=7, label=VARIANT_LABELS[variant])

        for x, y in zip(PORT_COUNTS, avg_per_port):
            if not np.isnan(y):
                ax.annotate(f"{y:.1f}%", xy=(x, y),
                            xytext=(0, 7), textcoords="offset points",
                            ha="center", fontsize=8, color=COLORS[variant])

    ax.set_title(
        "XDP — Rata-rata Saturasi CPU Core vs Jumlah Port\n"
        "(mean across 24 cores, median over time, 1 repetisi)",
        fontsize=12, fontweight="bold", pad=10,
    )
    ax.set_xlabel("Jumlah Port", fontsize=11)
    ax.set_ylabel("Rata-rata CPU Utilization per Core (%)", fontsize=11)
    ax.set_xticks(PORT_COUNTS)
    ax.set_ylim(bottom=0)
    ax.yaxis.set_major_formatter(ticker.FormatStrFormatter("%.1f"))
    ax.grid(True, linestyle="--", alpha=0.5)
    ax.legend(fontsize=10, loc="upper left")

    plt.tight_layout()
    out = OUTPUT_DIR / "xdp_sweep_core_saturation_avg.png"
    plt.savefig(out, dpi=150, bbox_inches="tight")
    print(f"Saved: {out.name}")
    plt.close()


if __name__ == "__main__":
    plot_heatmap()
    plot_avg()
