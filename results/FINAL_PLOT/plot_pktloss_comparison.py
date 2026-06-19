"""
plot_pktloss_comparison.py
==========================
Packet loss (%) comparison: XDP vs VPP vs Kernel
Scenario: Single Traffic — Port 15  (node1 TX → node5 RX)

Loss = (TX_opackets - RX_ipackets) / TX_opackets * 100
Averaged across 3 reps; shaded band = ±1 std-dev.

Output: result_pktloss/pktloss_compare_Single_15.png/.svg
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
PROTOCOLS = ["XDP", "VPP", "Kernel"]


# ── helpers ────────────────────────────────────────────────────────────────────
def stable_mean_rate(csv_path, metric, port):
    df = pd.read_csv(csv_path, parse_dates=["Time"])
    df["Port"] = pd.to_numeric(df["Port"], errors="coerce")
    df = df.dropna(subset=["Time", "Port"])
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
    upper = (Q1 + 1.5 * IQR) if (median > 0 and IQR / median > 0.20) else (Q3 + 1.5 * IQR)
    stable = y[y <= upper]
    return float(stable.mean()) if stable.size > 0 else float(y.mean())


# ── collect data ───────────────────────────────────────────────────────────────
base_dir   = os.path.dirname(os.path.abspath(__file__))
result_dir = os.path.join(base_dir, "result_pktloss")
os.makedirs(result_dir, exist_ok=True)

# raw[(proto, n_port)] = [loss_rep1, loss_rep2, loss_rep3]
raw = defaultdict(list)

for proto in PROTOCOLS:
    folders = sorted(glob.glob(os.path.join(base_dir, f"{proto}_*_Port_No_Block_15_rep*")))
    for folder in folders:
        name = os.path.basename(folder)
        m = re.match(r"(\w+)_(\d+)-(\d+)_Port_No_Block_15_rep(\d+)", name)
        if not m:
            continue
        n_port  = int(m.group(3)) - int(m.group(2)) + 1
        tx_path = os.path.join(folder, "node1.csv")
        rx_path = os.path.join(folder, "node5.csv")
        if not (os.path.exists(tx_path) and os.path.exists(rx_path)):
            continue
        tx = stable_mean_rate(tx_path, "opackets", 0)
        rx = stable_mean_rate(rx_path, "ipackets", 0)
        loss = (tx - rx) / tx * 100 if tx > 0 else 0.0
        raw[(proto, n_port)].append(loss)

# aggregate
series = {}   # proto → (ports, means, stds)
for proto in PROTOCOLS:
    pts = sorted({n for p, n in raw if p == proto})
    means, stds = [], []
    for n in pts:
        v = raw[(proto, n)]
        means.append(float(np.mean(v)))
        stds.append(float(np.std(v)))
    series[proto] = (pts, means, stds)


# ── plot ───────────────────────────────────────────────────────────────────────
fig, ax = plt.subplots(figsize=(14, 8))

for proto in PROTOCOLS:
    pts, means, stds = series[proto]
    st = PROTO_STYLES[proto]
    ax.plot(pts, means, color=st["color"], marker=st["marker"],
            linewidth=2.5, markersize=8, label=st["label"], zorder=3)
    ax.fill_between(pts,
                    [m - s for m, s in zip(means, stds)],
                    [m + s for m, s in zip(means, stds)],
                    color=st["color"], alpha=0.15, zorder=2)

# annotate min loss per protocol (best value)
for proto in PROTOCOLS:
    pts, means, stds = series[proto]
    min_idx = int(np.argmin(means))
    x_best  = pts[min_idx]
    y_best  = means[min_idx]
    st = PROTO_STYLES[proto]
    offset = 12 if proto != "Kernel" else -18
    va     = "bottom" if offset >= 0 else "top"
    ax.annotate(f"min: {y_best:.1f}%",
                xy=(x_best, y_best),
                xytext=(0, offset), textcoords="offset points",
                ha="center", va=va, fontsize=11, fontweight="bold",
                color=st["color"], zorder=5)

ax.set_title("Packet Loss Comparison  –  Single Traffic (Port 15)\n"
             "Node 1 (TX)  →  Node 5 (RX)",
             fontweight="bold", pad=14)
ax.set_xlabel("No. of ports per node used to send/receive traffic")
ax.set_ylabel("Packet Loss (%)")

all_ports = sorted({p for pts, _, _ in series.values() for p in pts})
ax.set_xticks(all_ports)
ax.set_xticklabels([str(p) for p in all_ports])
ax.set_xlim(all_ports[0] - 0.3, all_ports[-1] + 0.3)
ax.set_ylim(0, 105)
ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(decimals=0))

ax.grid(True, which="major", linestyle="-",  linewidth=0.75, alpha=0.30)
ax.minorticks_on()
ax.grid(True, which="minor", linestyle="--", linewidth=0.30, alpha=0.15)
ax.set_axisbelow(True)

# add horizontal reference lines
for y_ref, label, color in [(12, "≈12% (XDP target)", "#e8895c"),
                             (60, "≈60%",              "#888888")]:
    ax.axhline(y_ref, color=color, linewidth=1.2, linestyle="--", alpha=0.55, zorder=1)
    ax.text(all_ports[-1] + 0.05, y_ref + 1.5, label,
            color=color, fontsize=11, va="bottom", ha="right")

# legend on the right
legend_entries = []
for proto in PROTOCOLS:
    pts, means, stds = series[proto]
    min_loss = min(means)
    max_loss = max(means)
    st = PROTO_STYLES[proto]
    legend_entries.append(
        Line2D([0], [0], color=st["color"], linewidth=2.5,
               marker=st["marker"], markersize=8,
               label=f"{st['label']}   (min={min_loss:.1f}%, max={max_loss:.1f}%)")
    )

ax.legend(handles=legend_entries,
          loc="upper left", bbox_to_anchor=(1.02, 1),
          borderaxespad=0, frameon=True, framealpha=0.95,
          ncol=1, handlelength=2.5, borderpad=0.9, labelspacing=0.7)

import matplotlib.ticker
plt.tight_layout(pad=3.0)
plt.subplots_adjust(right=0.72)

out_stem = os.path.join(result_dir, "pktloss_compare_Single_15")
fig.savefig(f"{out_stem}.png", dpi=150, bbox_inches="tight")
fig.savefig(f"{out_stem}.svg",           bbox_inches="tight")
plt.close(fig)
print(f"[saved] result_pktloss/pktloss_compare_Single_15.png  +  .svg")
print("\nDone.")
