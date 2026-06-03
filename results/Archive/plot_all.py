import os
import re
import glob
from collections import defaultdict
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ── font: prefer Courier New, fall back to DejaVu Sans Mono ──────────────────
import matplotlib.font_manager as _fm
_available_fonts = {f.name for f in _fm.fontManager.ttflist}
_MONO_FONT = "Courier New" if "Courier New" in _available_fonts else "DejaVu Sans Mono"

FONT_SETTINGS = {
    "font.family":      _MONO_FONT,
    "font.size":        20,
    "axes.titlesize":   20,
    "axes.labelsize":   20,
    "xtick.labelsize":  20,
    "ytick.labelsize":  20,
    "legend.fontsize":  18,
    "figure.titlesize": 20,
}

LINE_COLOR        = "#77b5b6"
HLINE_CLEAN_COLOR = "#6a408d"

TECH_STYLES = {
    "Kernel": {"color": "#77b5b6", "marker": "o"},
    "XDP":    {"color": "#e8895c", "marker": "s"},
    "VPP":    {"color": "#6a408d", "marker": "^"},
}

# Per-folder plot specs:  (row, col, csv, metric, port, y_max, line1)  — Multi
#                          (col,      csv, metric, port, y_max, line1)  — Single
TEMPLATES = {
    "Multi": [
        (0, 0, "node1.csv", "opackets",  0,    42, "Node 1 (TX)"),
        (0, 1, "node1.csv", "ipackets",  1,    37, "Node 1 (RX)"),
        (1, 0, "node4.csv", "opackets",  None, 42, "Node 4 (TX)"),
        (1, 1, "node5.csv", "ipackets",  None, 37, "Node 5 (RX)"),
    ],
    "Single_15": [
        (0, "node1.csv", "opackets", 0, 42, "Node 1 (TX)"),
        (1, "node5.csv", "ipackets", 0, 37, "Node 5 (RX)"),
    ],
    "Single_41": [
        (0, "node4.csv", "opackets", 0, 42, "Node 4 (TX)"),
        (1, "node1.csv", "ipackets", 1, 37, "Node 1 (RX)"),
    ],
}

# Summary plot specs:  (rx_label, csv, metric, port, y_max)
RX_SPECS = {
    "Multi": [
        ("Node 1 (RX)", "node1.csv", "ipackets", 1,    37),
        ("Node 5 (RX)", "node5.csv", "ipackets", None, 37),
    ],
    "Single_15": [
        ("Node 5 (RX)", "node5.csv", "ipackets", 0, 37),
    ],
    "Single_41": [
        ("Node 1 (RX)", "node1.csv", "ipackets", 1, 37),
    ],
}


# ── shared helpers ────────────────────────────────────────────────────────────

def _detect_variant(folder_name):
    if re.search(r"_15_41$", folder_name):
        return "Multi", "Multi Traffic"
    if re.search(r"_41$", folder_name):
        return "Single_41", "Single Traffic"
    if re.search(r"_15$", folder_name):
        return "Single_15", "Single Traffic"
    raise RuntimeError(
        f"Cannot detect traffic variant from folder name: {folder_name!r}\n"
        "Expected suffix '_15_41', '_41', or '_15'."
    )


def _load_data(csv_file, metric, port):
    df = pd.read_csv(csv_file, parse_dates=["Time"])
    df["Port"] = pd.to_numeric(df["Port"], errors="coerce")
    df = df.dropna(subset=["Time", "Port", "Metric", "Value"])
    mask = df["Metric"] == metric
    if port is not None:
        mask &= df["Port"] == port
    df_node = df[mask].copy()
    df_node = df_node.sort_values("Time").reset_index(drop=True)

    t0 = df_node["Time"].iloc[0]
    df_node["elapsed"] = (df_node["Time"] - t0).dt.total_seconds()
    df_node["mpps"]    = df_node["Value"].diff().fillna(0).clip(lower=0) / 1e6

    return df_node["elapsed"].values, df_node["mpps"].values


def _max_stable(y):
    y_nz = y[y > 0]
    if y_nz.size == 0:
        return 0.0
    Q1, Q3 = np.percentile(y_nz, [25, 75])
    fence   = Q3 + 1.5 * (Q3 - Q1)
    stable  = y_nz[y_nz <= fence]
    return stable.max() if stable.size > 0 else y_nz.max()


def _apply_yticks(ax, y_max, peak_val, peak_color, y_tick_step=5):
    base_ticks     = list(range(0, y_max, y_tick_step))
    filtered_ticks = [t for t in base_ticks if abs(t - peak_val) > 0.3]
    combined_ticks  = filtered_ticks + [peak_val]
    combined_labels = [str(t) for t in filtered_ticks] + [f"{peak_val:.3f}"]
    tick_colors     = ["black"] * len(filtered_ticks) + [peak_color]

    ax.set_ylim(0, y_max)
    ax.set_yticks(combined_ticks)
    ax.set_yticklabels(combined_labels)
    for lbl, color in zip(ax.get_yticklabels(), tick_colors):
        lbl.set_color(color)


def _grid(ax):
    ax.grid(True, which="major", linestyle="-", linewidth=0.75, alpha=0.25)
    ax.minorticks_on()
    ax.grid(True, which="minor", linestyle="-", linewidth=0.25, alpha=0.15)
    ax.set_axisbelow(True)


# ── per-folder time-series plot ───────────────────────────────────────────────

def draw_on_ax(ax, csv_file, metric, title_line1, title_line2,
               port=None, y_max=37):
    x, y = _load_data(csv_file, metric, port)
    max_cleaned = _max_stable(y)

    ax.set_title(f"{title_line1}\n{title_line2}",
                 pad=55, fontweight="bold", fontsize=24)
    ax.set_xlabel("Elapsed time (s)")
    ax.set_ylabel("Packet rate (Mpps)")
    _grid(ax)

    ax.plot(x, y, color=LINE_COLOR, linewidth=2.0, zorder=2,
            label="Packet rate (Mpps)")
    ax.axhline(max_cleaned, color=HLINE_CLEAN_COLOR, linewidth=1.5,
               linestyle=":", zorder=3,
               label=f"Max (stable) = {max_cleaned:.3f} Mpps")

    _apply_yticks(ax, y_max, max_cleaned, HLINE_CLEAN_COLOR)

    x_pad = (x.max() - x.min()) * 0.03
    ax.set_xlim(x.min() - x_pad, x.max() + x_pad)

    ax.legend(loc="lower center", bbox_to_anchor=(0.5, 1.01),
              ncol=3, frameon=False)


# ── summary port-scaling plot ─────────────────────────────────────────────────

def draw_summary_ax(ax, rx_label, points, technology, traffic_label, y_max=37):
    style = TECH_STYLES.get(technology, {"color": "gray", "marker": "o"})
    ports = [p for p, _ in points]
    mpps  = [m for _, m in points]
    peak  = max(mpps) if mpps else 0.0

    ax.set_title(f"{rx_label}\n{technology} - {traffic_label} - Port Scaling",
                 pad=55, fontweight="bold", fontsize=24)
    ax.set_xlabel("Number of ports")
    ax.set_ylabel("Max stable packet rate (Mpps)")
    _grid(ax)

    ax.plot(ports, mpps, color=style["color"], marker=style["marker"],
            linewidth=2.0, markersize=8, zorder=2,
            label=f"Max stable Mpps  (peak = {peak:.3f})")
    ax.axhline(peak, color=style["color"], linewidth=1.5,
               linestyle=":", zorder=3)

    _apply_yticks(ax, y_max, peak, style["color"])

    sorted_ports = sorted(ports)
    ax.set_xticks(sorted_ports)
    ax.set_xticklabels([str(p) for p in sorted_ports])
    pad = max(0.3, (sorted_ports[-1] - sorted_ports[0]) * 0.03) if len(sorted_ports) > 1 else 0.5
    ax.set_xlim(sorted_ports[0] - pad, sorted_ports[-1] + pad)

    ax.legend(loc="lower center", bbox_to_anchor=(0.5, 1.01),
              ncol=1, frameon=False)


# ── main ──────────────────────────────────────────────────────────────────────

base_dir = os.path.dirname(os.path.abspath(__file__))
plt.rcParams.update(FONT_SETTINGS)

result_dir = os.path.join(base_dir, "result")
os.makedirs(result_dir, exist_ok=True)

data_folders = sorted(
    glob.glob(os.path.join(base_dir, "Kernel_*_Port_No_Block_*")) +
    glob.glob(os.path.join(base_dir, "VPP_*_Port_No_Block_*"))    +
    glob.glob(os.path.join(base_dir, "XDP_*_Port_No_Block_*"))
)
if not data_folders:
    raise RuntimeError("No Kernel_*, VPP_*, or XDP_*_Port_No_Block_* folders found.")

# summary_data[(technology, variant_key)][rx_label] = [(n_port, mpps), ...]
summary_data   = defaultdict(lambda: defaultdict(list))
traffic_labels = {}

# ── pass 1: per-folder time-series plots ─────────────────────────────────────
print(f"Using font: {_MONO_FONT}")
print(f"Found {len(data_folders)} data folder(s).\n")

for folder in data_folders:
    folder_name = os.path.basename(folder)

    try:
        variant_key, traffic_label = _detect_variant(folder_name)
    except RuntimeError as e:
        print(f"Skipping {folder_name}: {e}")
        continue

    tech_match = re.match(r"(Kernel|VPP|XDP)_(\d+)_Port", folder_name)
    if not tech_match:
        print(f"Skipping {folder_name}: cannot parse technology/port count.")
        continue

    technology  = tech_match.group(1)
    n_port      = tech_match.group(2)
    title_line2 = f"{technology} - {traffic_label} - {n_port} Port"
    template    = TEMPLATES[variant_key]
    is_multi    = variant_key == "Multi"

    if is_multi:
        fig, axes = plt.subplots(2, 2, figsize=(28, 14))
        for row, col, csv_file, metric, port, y_max, line1 in template:
            draw_on_ax(axes[row][col], os.path.join(folder, csv_file),
                       metric, line1, title_line2, port=port, y_max=y_max)
    else:
        fig, axes = plt.subplots(1, 2, figsize=(28, 7))
        for col, csv_file, metric, port, y_max, line1 in template:
            draw_on_ax(axes[col], os.path.join(folder, csv_file),
                       metric, line1, title_line2, port=port, y_max=y_max)

    plt.tight_layout(pad=4.0)
    stem = os.path.join(result_dir, f"plot_{folder_name}")
    plt.savefig(f"{stem}.png", dpi=150, bbox_inches="tight")
    plt.savefig(f"{stem}.svg",           bbox_inches="tight")
    plt.close(fig)
    print(f"[plot]    result/plot_{folder_name}.png  [{technology} | {traffic_label} | {n_port} port]")

    # accumulate data for summary plots
    key = (technology, variant_key)
    traffic_labels[key] = traffic_label
    for rx_label, csv_file, metric, port, _y_max in RX_SPECS[variant_key]:
        try:
            _, y    = _load_data(os.path.join(folder, csv_file), metric, port)
            max_val = _max_stable(y)
            summary_data[key][rx_label].append((int(n_port), max_val))
        except Exception as exc:
            print(f"  Warning: {folder_name} [{rx_label}]: {exc}")

# ── pass 2: summary port-scaling plots ────────────────────────────────────────
print()
for key, rx_data in sorted(summary_data.items()):
    technology, variant_key = key
    traffic_label = traffic_labels[key]
    rx_spec_list  = RX_SPECS[variant_key]
    n_rx          = len(rx_spec_list)

    fig, axes = plt.subplots(1, n_rx, figsize=(14 * n_rx, 7))
    if n_rx == 1:
        axes = [axes]

    for ax, (rx_label, _csv, _metric, _port, y_max) in zip(axes, rx_spec_list):
        points = sorted(rx_data.get(rx_label, []))
        draw_summary_ax(ax, rx_label, points, technology, traffic_label,
                        y_max=y_max)

    plt.tight_layout(pad=4.0)
    stem = os.path.join(result_dir, f"summary_{technology}_{variant_key}")
    plt.savefig(f"{stem}.png", dpi=150, bbox_inches="tight")
    plt.savefig(f"{stem}.svg",           bbox_inches="tight")
    plt.close(fig)
    print(f"[summary] result/summary_{technology}_{variant_key}.png  [{technology} | {traffic_label}]")

print("\nDone.")
