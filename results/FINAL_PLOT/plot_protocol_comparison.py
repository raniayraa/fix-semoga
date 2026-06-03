"""
plot_protocol_comparison.py
===========================
3 output files — one per traffic variant.

  Single_15 : 1 subplot  — Node 5 RX
  Single_41 : 1 subplot  — Node 1 RX
  Multi      : 2 subplots side-by-side — Node 1 RX | Node 5 RX

Each subplot compares VPP vs Kernel vs eBPF/XDP.
Y : mean max-stable RX Mpps across 3 reps  (shaded band = ±1 std-dev)
X : number of ports (1–10)

Output: result_protocol_compare/compare_{variant}.png/.svg
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

PROTO_STYLES = {
    "XDP":    {"color": "#e8895c", "marker": "s", "label": "eBPF / XDP"},
    "VPP":    {"color": "#6a408d", "marker": "^", "label": "VPP"},
    "Kernel": {"color": "#3a9bbf", "marker": "o", "label": "Kernel"},
}

VARIANT_TITLES = {
    "Single_15": "Single Traffic",
    "Single_41": "Single Traffic  –  Port 41",
    "Multi":     "Multi Traffic",
}

# Named RX specs per variant: (rx_label, csv, metric, port_index)
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
    IQR = Q3 - Q1
    median = np.median(y)
    # If IQR is large relative to median (bimodal data), anchor the fence to Q1
    # to avoid the upper cluster inflating the Tukey bound.
    if median > 0 and IQR / median > 0.20:
        upper = Q1 + 1.5 * IQR
    else:
        upper = Q3 + 1.5 * IQR
    stable = y[y <= upper]
    return float(stable.max()) if stable.size > 0 else float(y.max())


def _grid(ax):
    ax.grid(True, which="major", linestyle="-",  linewidth=0.75, alpha=0.30)
    ax.minorticks_on()
    ax.grid(True, which="minor", linestyle="--", linewidth=0.30, alpha=0.15)
    ax.set_axisbelow(True)


def _draw_proto_ax(ax, variant, rx_label, series_dict, title):
    """Draw one protocol-comparison subplot. series_dict = {protocol: (ports, means, stds)}"""
    y_global_max = 0.0
    for protocol in PROTOCOLS:
        if protocol not in series_dict:
            continue
        ports, means, stds = series_dict[protocol]
        y_global_max = max(y_global_max, max(means))
        st = PROTO_STYLES[protocol]
        ax.plot(ports, means, color=st["color"], marker=st["marker"],
                linewidth=2.5, markersize=8, label=st["label"], zorder=3)
        ax.fill_between(ports,
                        [m - s for m, s in zip(means, stds)],
                        [m + s for m, s in zip(means, stds)],
                        color=st["color"], alpha=0.15, zorder=2)

    y_top = max(5.0, y_global_max * 1.18)

    # annotations
    proto_list = [p for p in PROTOCOLS if p in series_dict]
    offsets_px = dict(zip(proto_list, [14, -18, 30]))
    for protocol in proto_list:
        ports, means, _ = series_dict[protocol]
        off = offsets_px.get(protocol, 14)
        for x, y in zip(ports, means):
            ax.annotate(f"{y:.2f}", xy=(x, y),
                        xytext=(0, off), textcoords="offset points",
                        ha="center", va="bottom" if off >= 0 else "top",
                        fontsize=10, fontweight="bold",
                        color=PROTO_STYLES[protocol]["color"], zorder=5)

    ax.set_title(f"{title}\n{rx_label}", fontweight="bold", pad=14)
    ax.set_xlabel("Number of ports")
    ax.set_ylabel("Max stable RX rate (Mpps)")
    _grid(ax)

    all_ports = sorted({p for prt in series_dict.values() for p in prt[0]})
    if all_ports:
        ax.set_xticks(all_ports)
        ax.set_xticklabels([str(p) for p in all_ports])
        ax.set_xlim(all_ports[0] - 0.3, all_ports[-1] + 0.3)
    ax.set_ylim(0, y_top)

    return y_top


# ── collect raw data ───────────────────────────────────────────────────────────
base_dir   = os.path.dirname(os.path.abspath(__file__))
result_dir = os.path.join(base_dir, "result_protocol_compare")
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

# average across reps
# avg[(protocol, variant, rx_label)] = {n_port: (mean, std)}
avg = {}
for protocol in PROTOCOLS:
    for variant in VARIANTS:
        for rx_label, *_ in RX_SPECS[variant]:
            port_vals = defaultdict(list)
            for rep in [1, 2, 3]:
                for n_port, mpps in raw.get((protocol, variant, rep, rx_label), {}).items():
                    port_vals[n_port].append(mpps)
            if port_vals:
                avg[(protocol, variant, rx_label)] = {
                    p: (float(np.mean(v)), float(np.std(v)))
                    for p, v in port_vals.items()
                }


# ── one figure per (variant × rx_node) ───────────────────────────────────────
# Single_15/41 → 1 file;  Multi → 2 files (Node1, Node5)
NODE_SLUG = {"Node 1 (RX)": "Node1", "Node 5 (RX)": "Node5"}

for variant in VARIANTS:
    for rx_label, *_ in RX_SPECS[variant]:

        # build series dict for this rx_label
        series_dict = {}
        for protocol in PROTOCOLS:
            key = (protocol, variant, rx_label)
            if key not in avg:
                continue
            pts   = sorted(avg[key].items())
            ports = [p for p, _ in pts]
            means = [m for _, (m, _) in pts]
            stds  = [s for _, (_, s) in pts]
            series_dict[protocol] = (ports, means, stds)

        if not series_dict:
            continue

        fig, ax = plt.subplots(figsize=(14, 8))

        _draw_proto_ax(ax, variant, rx_label, series_dict,
                       f"Protocol Comparison  –  {VARIANT_TITLES[variant]}")

        # legend outside on the right
        legend_entries = []
        for protocol in PROTOCOLS:
            if protocol not in series_dict:
                continue
            _, means, _ = series_dict[protocol]
            max_mean    = max(means)
            st          = PROTO_STYLES[protocol]
            legend_entries.append(
                Line2D([0], [0], color=st["color"], linewidth=2.5,
                       marker=st["marker"], markersize=8,
                       label=f"{st['label']}   (max avg = {max_mean:.3f} Mpps)")
            )

        ax.legend(handles=legend_entries,
                  loc="upper center", bbox_to_anchor=(0.5, -0.14),
                  borderaxespad=0, frameon=True, framealpha=0.95,
                  ncol=1, handlelength=2.5,
                  borderpad=0.9, labelspacing=0.7)

        plt.tight_layout(pad=3.0)
        plt.subplots_adjust(bottom=0.22)

        slug     = NODE_SLUG.get(rx_label, rx_label.replace(" ", "_"))
        out_name = f"compare_{variant}_{slug}" if variant == "Multi" \
                   else f"compare_{variant}"
        out_stem = os.path.join(result_dir, out_name)
        fig.savefig(f"{out_stem}.png", dpi=150, bbox_inches="tight")
        fig.savefig(f"{out_stem}.svg",           bbox_inches="tight")
        plt.close(fig)
        print(f"[saved] result_protocol_compare/{out_name}.png  +  .svg")

print("\nDone.")
