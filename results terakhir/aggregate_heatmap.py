#!/usr/bin/env python3
"""
Aggregate node6 CPU heatmaps across repetitions and forwarder types.

Groups:
  1. Per-experiment: average rep1/rep2/rep3 (and _vN variants) → one plot each
  2. Per-type: average all Kernel_*, XDP_*, VPP_* → one plot per type
"""

import re
import os
import sys
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from collections import defaultdict
from pathlib import Path

RESULTS_DIR = Path(__file__).parent
OUTPUT_DIR = RESULTS_DIR / "cpu_heatmap"
OUTPUT_DIR.mkdir(exist_ok=True)

LOG_NAME = "node6_mpstat.log"

# Regex: captures base name (type + ports + block), rep number, optional version
FOLDER_RE = re.compile(
    r'^(?P<base>(?:Kernel|XDP|VPP)_\d+-\d+_Port_No_Block_[\d_]+)_rep\d+(?:_v\d+)?$'
)
TYPE_RE = re.compile(r'^(?P<fwdr>Kernel|XDP|VPP)_')


def parse_mpstat(log_path: Path) -> np.ndarray | None:
    """Return utilization matrix [n_cpus x n_times] or None if unreadable."""
    data = {}
    try:
        with open(log_path) as f:
            for line in f:
                parts = line.split()
                if len(parts) < 11:
                    continue
                if parts[1] in ('CPU', 'all'):
                    continue
                try:
                    cpu_id = int(parts[1])
                except ValueError:
                    continue
                timestamp = parts[0]
                idle_str = parts[-1].replace(',', '.')
                try:
                    idle = float(idle_str)
                except ValueError:
                    continue
                data.setdefault(timestamp, {})[cpu_id] = idle
    except OSError:
        return None

    if not data:
        return None

    timestamps = sorted(data.keys())
    n_cpus = max(max(v.keys()) for v in data.values()) + 1
    n_times = len(timestamps)

    matrix = np.full((n_cpus, n_times), np.nan)
    for t_idx, ts in enumerate(timestamps):
        for cpu_id, idle in data[ts].items():
            matrix[cpu_id, t_idx] = 100.0 - idle
    return matrix


def average_matrices(matrices: list[np.ndarray]) -> np.ndarray:
    """Average a list of [n_cpus x n_times] matrices, aligning by relative index."""
    n_cpus = max(m.shape[0] for m in matrices)
    n_times = min(m.shape[1] for m in matrices)  # trim to shortest

    stack = []
    for m in matrices:
        rows = m.shape[0]
        if rows < n_cpus:
            pad = np.full((n_cpus - rows, m.shape[1]), np.nan)
            m = np.vstack([m, pad])
        stack.append(m[:n_cpus, :n_times])

    return np.nanmean(np.stack(stack, axis=0), axis=0)


def save_heatmap(matrix: np.ndarray, title: str, out_path: Path) -> None:
    n_cpus, n_times = matrix.shape
    fig_width = max(14, n_times * 0.18)
    fig_height = max(6, n_cpus * 0.45)
    fig, ax = plt.subplots(figsize=(fig_width, fig_height))

    im = ax.imshow(matrix, aspect='auto', cmap='hot_r', vmin=0, vmax=100,
                   interpolation='nearest', origin='upper')

    cbar = fig.colorbar(im, ax=ax, fraction=0.03, pad=0.02)
    cbar.set_label('CPU Utilization (%)', fontsize=12)

    ax.set_xlabel('Time (s)', fontsize=12)
    ax.set_ylabel('CPU Core', fontsize=12)
    ax.set_title(title, fontsize=13)

    step = max(1, n_times // 20)
    ax.set_xticks(range(0, n_times, step))
    ax.set_xticklabels([str(i) for i in range(0, n_times, step)], fontsize=8)
    ax.set_yticks(range(n_cpus))
    ax.set_yticklabels([f'CPU {i}' for i in range(n_cpus)], fontsize=8)

    plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(f"Saved: {out_path.relative_to(RESULTS_DIR)}  ({n_cpus} CPUs x {n_times} timesteps)")


def collect_folders() -> dict[str, list[Path]]:
    """Map base experiment name → list of matching folders (all reps/versions)."""
    groups: dict[str, list[Path]] = defaultdict(list)
    for entry in sorted(RESULTS_DIR.iterdir()):
        if not entry.is_dir():
            continue
        m = FOLDER_RE.match(entry.name)
        if not m:
            continue
        log = entry / LOG_NAME
        if not log.exists():
            continue
        groups[m.group('base')].append(entry)
    return groups


def main() -> None:
    groups = collect_folders()
    print(f"Found {len(groups)} unique experiment bases across "
          f"{sum(len(v) for v in groups.values())} folders.\n")

    # per-type accumulator: {fwdr: [matrix, ...]}
    type_matrices: dict[str, list[np.ndarray]] = defaultdict(list)

    # 1. Per-experiment aggregation
    for base, folders in sorted(groups.items()):
        matrices = []
        for folder in folders:
            m = parse_mpstat(folder / LOG_NAME)
            if m is not None:
                matrices.append(m)

        if not matrices:
            print(f"  SKIP {base}: no parseable data")
            continue

        avg = average_matrices(matrices)
        label = base.replace('_', ' ')
        title = f"Node6 CPU Utilization – {label}\n(avg of {len(matrices)} runs)"
        out_file = OUTPUT_DIR / f"{base}.png"
        save_heatmap(avg, title, out_file)

        fwdr_m = TYPE_RE.match(base)
        if fwdr_m:
            type_matrices[fwdr_m.group('fwdr')].append(avg)

    # 2. Per-type aggregation
    print()
    for fwdr, matrices in sorted(type_matrices.items()):
        avg = average_matrices(matrices)
        title = f"Node6 CPU Utilization – {fwdr} (avg of all experiments, {len(matrices)} bases)"
        out_file = OUTPUT_DIR / f"{fwdr}_all.png"
        save_heatmap(avg, title, out_file)


if __name__ == '__main__':
    main()
