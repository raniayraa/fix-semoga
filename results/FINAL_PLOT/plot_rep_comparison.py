"""
plot_rep_comparison.py
======================
9 output files — one per (protocol × traffic variant).

  Single_15 / Single_41 : 1 subplot   — 3 rep lines
  Multi                  : 2 subplots side-by-side (Node 1 RX | Node 5 RX)
                           each with 3 rep lines

Color : Red=Rep1, Blue=Rep2, Green=Rep3
Output: result_rep_compare/rep_compare_{protocol}_{variant}.png/.svg
"""

import os, re, glob
from collections import defaultdict
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as _fm
from matplotlib.lines import Line2D

# ── fonts ──────────────────────────────────────────────────────────────────────
_available = {f.name for f in _fm.fontManager.ttflist}
_MONO = "Courier New" if "Courier New" in _available else "DejaVu Sans Mono"
plt.rcParams.update({
    "font.family":      _MONO,
    "font.size":        20,
    "axes.titlesize":   22,
    "axes.labelsize":   20,
    "xtick.labelsize":  18,
    "ytick.labelsize":  18,
    "legend.fontsize":  15,
})

REP_COLORS = {1: "#e05c3a", 2: "#3a78c9", 3: "#2daa55"}
REP_LABELS = {1: "Rep 1",   2: "Rep 2",   3: "Rep 3"}

VARIANT_TITLES = {
    "Single_15": "Single Traffic  –  Port 15",
    "Single_41": "Single Traffic  –  Port 41",
    "Multi":     "Multi Traffic   –  Port 15 + 41",
}

# Named RX specs: (rx_label, csv, metric, port_index)
RX_SPECS = {
    "Single_15": [("Node 5 (RX)", "node5.csv", "ipackets", 0)],
    "Single_41": [("Node 1 (RX)", "node1.csv", "ipackets", 1)],
    "Multi":     [("Node 1 (RX)", "node1.csv", "ipackets", 1),
                  ("Node 5 (RX)", "node5.csv", "ipackets", None)],
}

PROTOCOLS = ["XDP", "VPP", "Kernel"]
VARIANTS  = ["Single_15", "Single_41", "Multi"]


# ── helpers ────────────────────────────────────────────────────────────────────
def detect_variant(folder_name):
    c = re.sub(r"_rep\d+$", "", folder_name)
    if re.search(r"_15_41$", c): return "Multi"
    if re.search(r"_41$",    c): return "Single_41"
    if re.search(r"_15$",    c): return "Single_15"
    return None


def load_max_stable(csv_path, metric, port):
    df = pd.read_csv(csv_path, parse_dates=["Time"])
    df["Port"] = pd.to_numeric(df["Port"], errors="coerce")
    df = df.dropna(subset=["Time", "Port", "Metric", "Value"])
    mask = df["Metric"] == metric
    if port is not None:
        mask &= df["Port"] == port
    df_f = df[mask].sort_values("Time").reset_index(drop=True)
    if df_f.empty:
        return 0.0
    mpps = df_f["Value"].diff().fillna(0).clip(lower=0) / 1e6
    y = mpps.values[mpps.values > 0]
    if y.size == 0:
        return 0.0
    Q1, Q3 = np.percentile(y, [25, 75])
    stable = y[y <= Q3 + 1.5 * (Q3 - Q1)]
    return float(stable.max()) if stable.size > 0 else float(y.max())


def _grid(ax):
    ax.grid(True, which="major", linestyle="-",  linewidth=0.75, alpha=0.30)
    ax.minorticks_on()
    ax.grid(True, which="minor", linestyle="--", linewidth=0.30, alpha=0.15)
    ax.set_axisbelow(True)


LABEL_OFFSETS_PX = {1: 14, 2: -18, 3: 30}

def _draw_rep_ax(ax, protocol, variant, rx_label, series_dict, title):
    """Draw one rep-comparison subplot. series_dict = {rep_num: (ports, mpps_vals)}"""
    y_global_max = 0.0
    for rep_num in [1, 2, 3]:
        if rep_num not in series_dict:
            continue
        ports, mpps_vals = series_dict[rep_num]
        y_global_max = max(y_global_max, max(mpps_vals))
        ax.plot(ports, mpps_vals,
                color=REP_COLORS[rep_num], linestyle="-",
                linewidth=2.5, marker="o", markersize=8,
                label=REP_LABELS[rep_num], zorder=3)

    y_top = max(5.0, y_global_max * 1.18)

    # annotations
    for rep_num, (ports, mpps_vals) in series_dict.items():
        off = LABEL_OFFSETS_PX.get(rep_num, 14)
        for x, y in zip(ports, mpps_vals):
            ax.annotate(f"{y:.2f}", xy=(x, y),
                        xytext=(0, off), textcoords="offset points",
                        ha="center", va="bottom" if off >= 0 else "top",
                        fontsize=10, fontweight="bold",
                        color=REP_COLORS[rep_num], zorder=5)

    ax.set_title(f"{title}\n{protocol}  –  {rx_label}", fontweight="bold", pad=14)
    ax.set_xlabel("Number of ports")
    ax.set_ylabel("Max stable RX rate (Mpps)")
    _grid(ax)

    all_ports = sorted({p for pts in series_dict.values() for p in pts[0]})
    if all_ports:
        ax.set_xticks(all_ports)
        ax.set_xticklabels([str(p) for p in all_ports])
        ax.set_xlim(all_ports[0] - 0.3, all_ports[-1] + 0.3)
    ax.set_ylim(0, y_top)

    return y_top


# ── collect raw data ───────────────────────────────────────────────────────────
base_dir   = os.path.dirname(os.path.abspath(__file__))
result_dir = os.path.join(base_dir, "result_rep_compare")
os.makedirs(result_dir, exist_ok=True)

all_folders = sorted(
    glob.glob(os.path.join(base_dir, "Kernel_*_Port_No_Block_*")) +
    glob.glob(os.path.join(base_dir, "VPP_*_Port_No_Block_*"))    +
    glob.glob(os.path.join(base_dir, "XDP_*_Port_No_Block_*"))
)
print(f"Using font : {_MONO}")
print(f"Folders    : {len(all_folders)} found\n")

# raw[(protocol, variant, rep, rx_label)] = {n_port: mpps}
raw = defaultdict(dict)

for folder in all_folders:
    name = os.path.basename(folder)
    m = re.match(r"(Kernel|VPP|XDP)_(\d+)-(\d+)_Port", name)
    if not m:
        continue
    protocol = m.group(1)
    n_port   = int(m.group(3)) - int(m.group(2)) + 1
    variant  = detect_variant(name)
    if variant is None:
        continue
    m_rep   = re.search(r"_rep(\d+)$", name)
    rep_num = int(m_rep.group(1)) if m_rep else 1

    for rx_label, csv_file, metric, port in RX_SPECS[variant]:
        path = os.path.join(folder, csv_file)
        if not os.path.exists(path):
            continue
        try:
            mpps = load_max_stable(path, metric, port)
            raw[(protocol, variant, rep_num, rx_label)][n_port] = mpps
        except Exception as e:
            print(f"  Warning [{name} / {csv_file}]: {e}")


# ── figures: one per (protocol × variant × rx_node) ──────────────────────────
# Single_15/41 → 1 file each;  Multi → 2 files (Node1, Node5)
NODE_SLUG = {"Node 1 (RX)": "Node1", "Node 5 (RX)": "Node5"}

for protocol in PROTOCOLS:
    for variant in VARIANTS:
        for rx_label, csv_file, metric, port in RX_SPECS[variant]:

            # build series for this specific rx_label
            series_dict = {}
            for rep_num in [1, 2, 3]:
                key = (protocol, variant, rep_num, rx_label)
                if key not in raw:
                    continue
                pts = sorted(raw[key].items())
                if not pts:
                    continue
                ports, mpps_vals = zip(*pts)
                series_dict[rep_num] = (list(ports), list(mpps_vals))

            if not series_dict:
                continue

            fig, ax = plt.subplots(figsize=(14, 8))

            _draw_rep_ax(ax, protocol, variant, rx_label, series_dict,
                         VARIANT_TITLES[variant])

            # legend outside on the right
            legend_entries = []
            for rep_num in [1, 2, 3]:
                if rep_num not in series_dict:
                    continue
                _, mpps_vals = series_dict[rep_num]
                max_mpps = max(mpps_vals)
                legend_entries.append(
                    Line2D([0], [0], color=REP_COLORS[rep_num], linewidth=2.5,
                           marker="o", markersize=8,
                           label=f"{REP_LABELS[rep_num]}   (max = {max_mpps:.3f} Mpps)")
                )

            ax.legend(handles=legend_entries,
                      loc="upper left", bbox_to_anchor=(1.02, 1),
                      borderaxespad=0, frameon=True, framealpha=0.95,
                      ncol=1, handlelength=2.5, borderpad=0.9, labelspacing=0.7)

            plt.tight_layout(pad=3.0)
            plt.subplots_adjust(right=0.72)

            slug     = NODE_SLUG.get(rx_label, rx_label.replace(" ", "_"))
            out_name = f"rep_compare_{protocol}_{variant}_{slug}" if variant == "Multi" \
                       else f"rep_compare_{protocol}_{variant}"
            out_stem = os.path.join(result_dir, out_name)
            fig.savefig(f"{out_stem}.png", dpi=150, bbox_inches="tight")
            fig.savefig(f"{out_stem}.svg",           bbox_inches="tight")
            plt.close(fig)
            print(f"[saved] result_rep_compare/{out_name}.png  +  .svg")

print("\nDone.")
