import os
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker

BASE_DIR = "/home/telmat/final_t40/Archive"
OUTPUT_DIR = "/home/telmat/final_t40/FINAL_PLOT/cpu_heatmap"

PROTOCOLS = ["Kernel", "VPP", "XDP"]
PORT_COUNTS = list(range(1, 11))
VARIANTS = ["15", "41", "15_41"]
VARIANT_LABELS = {"15": "Port 15 (Low Load)", "41": "Port 41 (High Load)", "15_41": "Port 15 + 41 (Mixed)"}

COLORS = {"Kernel": "#E74C3C", "VPP": "#3498DB", "XDP": "#2ECC71"}
MARKERS = {"Kernel": "o", "VPP": "s", "XDP": "^"}


def get_median_cpu_from_csv(csv_path):
    df = pd.read_csv(csv_path)
    idle_cols = [c for c in df.columns if c.endswith("_%idle")]
    if not idle_cols:
        return None
    cpu_util_per_second = 100.0 - df[idle_cols].mean(axis=1)
    return float(np.median(cpu_util_per_second))


def get_median_cpu_from_mpstat(mpstat_path):
    util_values = []
    with open(mpstat_path, "r") as f:
        for line in f:
            parts = line.split()
            if len(parts) < 12:
                continue
            if parts[1] != "all":
                continue
            try:
                idle = float(parts[-1].replace(",", "."))
                util_values.append(100.0 - idle)
            except ValueError:
                continue
    if not util_values:
        return None
    return float(np.median(util_values))


def get_median_cpu(folder_path):
    csv_path = os.path.join(folder_path, "node6_cpu.csv")
    if os.path.exists(csv_path):
        return get_median_cpu_from_csv(csv_path)
    mpstat_path = os.path.join(folder_path, "node6_mpstat.log")
    if os.path.exists(mpstat_path):
        return get_median_cpu_from_mpstat(mpstat_path)
    return None


def build_data():
    data = {}
    for variant in VARIANTS:
        data[variant] = {}
        for proto in PROTOCOLS:
            vals = []
            for n_ports in PORT_COUNTS:
                folder = os.path.join(BASE_DIR, f"{proto}_{n_ports}_Port_No_Block_{variant}")
                median_cpu = get_median_cpu(folder) if os.path.isdir(folder) else None
                vals.append(median_cpu)
            data[variant][proto] = vals
    return data


def plot_variant(variant, proto_data, ax):
    for proto in PROTOCOLS:
        vals = proto_data[proto]
        x = [PORT_COUNTS[i] for i, v in enumerate(vals) if v is not None]
        y = [v for v in vals if v is not None]
        if not x:
            continue
        ax.plot(x, y, color=COLORS[proto], marker=MARKERS[proto],
                linewidth=2, markersize=7, label=proto)

    ax.set_title(VARIANT_LABELS[variant], fontsize=13, fontweight="bold", pad=10)
    ax.set_xlabel("Number of Blocked Ports", fontsize=11)
    ax.set_ylabel("Median CPU Utilization (%)", fontsize=11)
    ax.set_xticks(PORT_COUNTS)
    ax.yaxis.set_major_formatter(ticker.FormatStrFormatter("%.1f"))
    ax.set_ylim(bottom=0)
    ax.grid(True, linestyle="--", alpha=0.5)
    ax.legend(fontsize=10, loc="upper left")


def main():
    data = build_data()

    fig, axes = plt.subplots(1, 3, figsize=(18, 5), sharey=False)
    fig.suptitle("CPU Utilization vs Number of Blocked Ports\n(Median over experiment duration)",
                 fontsize=14, fontweight="bold", y=1.02)

    for ax, variant in zip(axes, VARIANTS):
        plot_variant(variant, data[variant], ax)

    plt.tight_layout()
    out_path = os.path.join(OUTPUT_DIR, "cpu_vs_ports_median.png")
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    print(f"Saved: {out_path}")
    plt.close()

    for variant in VARIANTS:
        fig, ax = plt.subplots(figsize=(8, 5))
        plot_variant(variant, data[variant], ax)
        plt.tight_layout()
        out = os.path.join(OUTPUT_DIR, f"cpu_vs_ports_median_{variant}.png")
        plt.savefig(out, dpi=150, bbox_inches="tight")
        print(f"Saved: {out}")
        plt.close()


if __name__ == "__main__":
    main()
