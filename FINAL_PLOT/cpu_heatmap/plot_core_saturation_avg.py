#!/usr/bin/env python3
"""
Line chart rata-rata saturasi core XDP:
  X = jumlah port (1-10)
  Y = rata-rata median CPU utilization across all 24 cores (%)
  3 garis = 3 variant traffic (15, 41, 15_41)
"""

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent))
from plot_core_saturation import build_matrix

OUTPUT_DIR = Path(__file__).parent

VARIANTS = ["15", "41", "15_41"]
VARIANT_LABELS = {"15": "Port 15 only", "41": "Port 41 only", "15_41": "Port 15 + 41"}
COLORS  = {"15": "#E67E22", "41": "#2980B9", "15_41": "#8E44AD"}
MARKERS = {"15": "o", "41": "s", "15_41": "^"}
PORT_COUNTS = list(range(1, 11))


def main():
    fig, ax = plt.subplots(figsize=(9, 5))

    for variant in VARIANTS:
        mat = build_matrix(variant)  # [24 x 10]

        # rata-rata semua core per kolom (port count)
        avg_per_port = np.nanmean(mat, axis=0)

        ax.plot(PORT_COUNTS, avg_per_port,
                color=COLORS[variant], marker=MARKERS[variant],
                linewidth=2, markersize=7, label=VARIANT_LABELS[variant])

        # anotasi nilai di tiap titik
        for x, y in zip(PORT_COUNTS, avg_per_port):
            ax.annotate(f"{y:.1f}%", xy=(x, y),
                        xytext=(0, 7), textcoords="offset points",
                        ha="center", fontsize=8, color=COLORS[variant])

    ax.set_title("XDP — Rata-rata Saturasi CPU Core vs Jumlah Port\n"
                 "(mean across 24 cores, median over time, avg 3 repetisi)",
                 fontsize=12, fontweight="bold", pad=10)
    ax.set_xlabel("Jumlah Port", fontsize=11)
    ax.set_ylabel("Rata-rata CPU Utilization per Core (%)", fontsize=11)
    ax.set_xticks(PORT_COUNTS)
    ax.set_ylim(bottom=0)
    ax.yaxis.set_major_formatter(ticker.FormatStrFormatter("%.1f"))
    ax.grid(True, linestyle="--", alpha=0.5)
    ax.legend(fontsize=10, loc="upper left")

    plt.tight_layout()
    out = OUTPUT_DIR / "xdp_core_saturation_avg.png"
    plt.savefig(out, dpi=150, bbox_inches="tight")
    print(f"Saved: {out.name}")
    plt.close()


if __name__ == "__main__":
    main()
