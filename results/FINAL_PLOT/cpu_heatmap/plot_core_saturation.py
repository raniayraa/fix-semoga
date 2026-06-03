#!/usr/bin/env python3
"""
Heatmap distribusi saturasi CPU core XDP:
  X = jumlah port (1-10)
  Y = CPU core (0-23)
  warna = median CPU utilization (%)
"""

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent))
from plot_cpu_vs_ports_v2 import parse_mpstat

RESULTS_DIR = Path(__file__).parent.parent
OUTPUT_DIR  = Path(__file__).parent

VARIANTS = ["15", "41", "15_41"]
VARIANT_LABELS = {
    "15":    "Traffic: Port 15 only",
    "41":    "Traffic: Port 41 only",
    "15_41": "Traffic: Port 15 + 41",
}
N_CORES = 24
PORT_COUNTS = list(range(1, 11))


def _rep_median_cpu(m: np.ndarray) -> float:
    """Mean across cores → median over time for a single rep matrix."""
    return float(np.nanmedian(np.nanmean(m, axis=0)))


def build_matrix(variant: str) -> np.ndarray:
    """Returns matrix [n_cores x n_ports] of median CPU % per core per port count."""
    matrix = np.full((N_CORES, len(PORT_COUNTS)), np.nan)

    for col_idx, n_ports in enumerate(PORT_COUNTS):
        end_port = 1024 + n_ports - 1
        matrices = []
        for rep in [1, 2, 3]:
            log = RESULTS_DIR / f"XDP_1024-{end_port}_Port_No_Block_{variant}_rep{rep}" / "node6_mpstat.log"
            if not log.exists():
                continue
            m = parse_mpstat(log)
            if m is not None:
                matrices.append(m)
        if not matrices:
            continue

        # filter outlier reps: exclude any rep whose median deviates >30% from group median
        rep_medians = np.array([_rep_median_cpu(m) for m in matrices])
        group_median = np.median(rep_medians)
        if group_median > 0:
            keep = np.abs(rep_medians - group_median) / group_median <= 0.30
            filtered = [m for m, ok in zip(matrices, keep) if ok]
            excluded = [(i + 1, v) for i, (v, ok) in enumerate(zip(rep_medians, keep)) if not ok]
            if excluded:
                print(f"  EXCLUDED outlier reps for {variant} {n_ports}p: "
                      f"{[f'rep{r}={v:.1f}%' for r, v in excluded]}")
            matrices = filtered if filtered else matrices

        # average reps
        n_times = min(m.shape[1] for m in matrices)
        stacked = np.stack([m[:N_CORES, :n_times] for m in matrices], axis=0)
        avg = np.nanmean(stacked, axis=0)  # [n_cores x n_times]

        # median per core across time
        matrix[:, col_idx] = np.nanmedian(avg, axis=1)

    return matrix


def main():
    fig, axes = plt.subplots(1, 3, figsize=(18, 8), sharey=True)
    fig.suptitle("XDP — Distribusi Saturasi CPU Core vs Jumlah Port\n(median utilization per core, avg 3 repetisi)",
                 fontsize=13, fontweight="bold", y=1.01)

    for ax, variant in zip(axes, VARIANTS):
        mat = build_matrix(variant)

        im = ax.imshow(mat, aspect="auto", cmap="YlOrRd",
                       vmin=0, vmax=100,
                       interpolation="nearest", origin="upper")

        ax.set_title(VARIANT_LABELS[variant], fontsize=11, fontweight="bold", pad=8)
        ax.set_xlabel("Jumlah Port", fontsize=11)
        if ax == axes[0]:
            ax.set_ylabel("CPU Core", fontsize=11)

        ax.set_xticks(range(len(PORT_COUNTS)))
        ax.set_xticklabels([str(n) for n in PORT_COUNTS], fontsize=10)
        ax.set_yticks(range(N_CORES))
        ax.set_yticklabels([f"CPU {i}" for i in range(N_CORES)], fontsize=8)

        # annotate nilai di tiap cell
        for row in range(N_CORES):
            for col in range(len(PORT_COUNTS)):
                val = mat[row, col]
                if np.isnan(val):
                    continue
                txt_color = "white" if val > 55 else "black"
                ax.text(col, row, f"{val:.0f}", ha="center", va="center",
                        fontsize=6.5, color=txt_color, fontweight="bold")

        cbar = fig.colorbar(im, ax=ax, fraction=0.03, pad=0.03)
        cbar.set_label("CPU Utilization (%)", fontsize=9)

    plt.tight_layout()
    out = OUTPUT_DIR / "xdp_core_saturation.png"
    plt.savefig(out, dpi=150, bbox_inches="tight")
    print(f"Saved: {out.name}")
    plt.close()


if __name__ == "__main__":
    main()
