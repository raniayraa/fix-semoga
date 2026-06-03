import re
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker

log_path = "node6_mpstat.log"
output_path = "heatmap_node6_cpu_utilization.png"

# Parse: collect per-second per-cpu %idle, skip 'all' rows
data = {}  # {timestamp: {cpu_id: idle}}

with open(log_path) as f:
    for line in f:
        line = line.strip()
        parts = line.split()
        if len(parts) < 11:
            continue
        # Skip header and 'all' rows
        if parts[1] == 'CPU' or parts[1] == 'all':
            continue
        # Must be a numeric CPU id
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
        if timestamp not in data:
            data[timestamp] = {}
        data[timestamp][cpu_id] = idle

timestamps = sorted(data.keys())
n_cpus = max(max(v.keys()) for v in data.values()) + 1
n_times = len(timestamps)

# Build matrix: rows=CPU, cols=time
matrix = np.full((n_cpus, n_times), np.nan)
for t_idx, ts in enumerate(timestamps):
    for cpu_id, idle in data[ts].items():
        matrix[cpu_id, t_idx] = 100.0 - idle

# Relative time labels (seconds from start)
time_labels = list(range(n_times))

fig_width = max(14, n_times * 0.18)
fig_height = max(6, n_cpus * 0.45)
fig, ax = plt.subplots(figsize=(fig_width, fig_height))

im = ax.imshow(matrix, aspect='auto', cmap='hot_r', vmin=0, vmax=100,
               interpolation='nearest', origin='upper')

cbar = fig.colorbar(im, ax=ax, fraction=0.03, pad=0.02)
cbar.set_label('CPU Utilization (%)', fontsize=12)

ax.set_xlabel('Time (s)', fontsize=12)
ax.set_ylabel('CPU Core', fontsize=12)
ax.set_title('Node6 CPU Utilization Heatmap (XDP_1024-1033 Port No Block 15-41 rep1)', fontsize=13)

# X-axis ticks every 10 seconds
step = max(1, n_times // 20)
ax.set_xticks(range(0, n_times, step))
ax.set_xticklabels([str(i) for i in range(0, n_times, step)], fontsize=8)

# Y-axis: one tick per CPU
ax.set_yticks(range(n_cpus))
ax.set_yticklabels([f'CPU {i}' for i in range(n_cpus)], fontsize=8)

plt.tight_layout()
plt.savefig(output_path, dpi=150, bbox_inches='tight')
print(f"Saved: {output_path}  (shape: {n_cpus} CPUs x {n_times} timesteps)")
