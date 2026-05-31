#!/bin/bash
# start_turbo.sh — System tuning + launch xdpd in turbo mode
#
# Usage:
#   sudo ./start_turbo.sh [iface] [redirect-dev] [num_cpus]
#
# Defaults:
#   iface       : enp1s0f1np1
#   redirect-dev: enp1s0f0np0
#   num_cpus    : semua CPU (nproc)
#
# Contoh pakai 4 CPU saja:
#   sudo ./start_turbo.sh enp1s0f1np1 enp1s0f0np0 4

set -euo pipefail

IFACE="${1:-enp1s0f1np1}"
REDIRECT_DEV="${2:-enp1s0f0np0}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
XDPD="$SCRIPT_DIR/xdpd"
TURBO_CFG="$SCRIPT_DIR/turbo.json"
DB_PATH="/tmp/xdpd.db"

[[ $EUID -eq 0 ]] || { echo "ERROR: Harus root. Jalankan: sudo $0"; exit 1; }
[[ -x "$XDPD" ]] || { echo "ERROR: $XDPD tidak ditemukan. Build dulu: go build -o xdpd ./cmd/xdpd/"; exit 1; }

MAX_CPUS=$(nproc)
if [[ -n "${3:-}" ]]; then
    NUM_CPUS="${3}"
    if (( NUM_CPUS < 1 || NUM_CPUS > MAX_CPUS )); then
        echo "ERROR: num_cpus harus antara 1 dan $MAX_CPUS (kamu punya $MAX_CPUS CPU)"
        exit 1
    fi
else
    NUM_CPUS=$MAX_CPUS
fi
ALL_CPUS=$(seq -s, 0 $(( NUM_CPUS - 1 )))

echo "╔══════════════════════════════════════════════════╗"
echo "║   xdp-go-optimized — Turbo Mode Startup         ║"
echo "║   Ingress : $IFACE"
echo "║   Egress  : $REDIRECT_DEV"
echo "║   CPUs    : $NUM_CPUS dari $MAX_CPUS (core 0-$(( NUM_CPUS - 1 )))"
echo "╚══════════════════════════════════════════════════╝"

# ── 1. Stop irqbalance ────────────────────────────────────────────────────────
echo; echo "=== 1. Disable irqbalance ==="
if systemctl is-active --quiet irqbalance 2>/dev/null; then
    systemctl stop irqbalance
    echo "  irqbalance stopped"
else
    echo "  irqbalance tidak berjalan (OK)"
fi

# ── 2. CPU governor → performance ────────────────────────────────────────────
echo; echo "=== 2. CPU governor → performance ==="
for cpu in $(seq 0 $(( NUM_CPUS - 1 ))); do
    gov="/sys/devices/system/cpu/cpu${cpu}/cpufreq/scaling_governor"
    if [[ -f "$gov" ]]; then
        echo performance > "$gov"
        echo "  CPU $cpu: performance"
    fi
done

# ── 3. NIC queues = jumlah CPU ───────────────────────────────────────────────
echo; echo "=== 3. NIC queues → $NUM_CPUS ==="
if ethtool -L "$IFACE" combined "$NUM_CPUS" 2>/dev/null; then
    echo "  Combined queues = $NUM_CPUS"
elif ethtool -L "$IFACE" rx "$NUM_CPUS" tx "$NUM_CPUS" 2>/dev/null; then
    echo "  RX/TX queues = $NUM_CPUS"
else
    echo "  WARN: ethtool -L gagal (lanjut)"
fi

# ── 4. IRQ affinity: satu IRQ per CPU ────────────────────────────────────────
echo; echo "=== 4. IRQ affinity: NIC IRQs → CPU per queue ==="
mapfile -t IRQS < <(
    PCI_SLOT=$(basename "$(readlink /sys/class/net/${IFACE}/device)" 2>/dev/null)
    grep -E "(${IFACE}|${PCI_SLOT})" /proc/interrupts \
        | awk -F: '{print $1}' | tr -d ' ' | sort -n
)
if [[ ${#IRQS[@]} -eq 0 ]]; then
    echo "  WARN: Tidak ada IRQ ditemukan untuk $IFACE (driver mungkin pakai polling)"
else
    i=0
    for irq in "${IRQS[@]}"; do
        cpu=$(( i % NUM_CPUS ))
        mask=$(printf "%x" $(( 1 << cpu )))
        echo "$mask" > "/proc/irq/${irq}/smp_affinity"
        echo "  IRQ $irq (queue-$i) → CPU $cpu"
        (( i++ )) || true
    done
fi

# ── 5. XPS per TX queue ───────────────────────────────────────────────────────
echo; echo "=== 5. XPS per TX queue ==="
mapfile -t XPS_FILES < <(
    find "/sys/class/net/${IFACE}/queues/" -name "xps_cpus" 2>/dev/null | sort
)
i=0
for f in "${XPS_FILES[@]}"; do
    cpu=$(( i % NUM_CPUS ))
    mask=$(printf "%x" $(( 1 << cpu )))
    echo "$mask" > "$f"
    echo "  $(basename "$(dirname "$f")")/xps_cpus → CPU $cpu"
    (( i++ )) || true
done

# ── 6. Lakukan hal yang sama untuk redirect dev ───────────────────────────────
echo; echo "=== 6. IRQ affinity untuk $REDIRECT_DEV ==="
mapfile -t IRQS2 < <(
    PCI_SLOT2=$(basename "$(readlink /sys/class/net/${REDIRECT_DEV}/device)" 2>/dev/null)
    grep -E "(${REDIRECT_DEV}|${PCI_SLOT2})" /proc/interrupts \
        | awk -F: '{print $1}' | tr -d ' ' | sort -n
)
i=0
for irq in "${IRQS2[@]}"; do
    cpu=$(( i % NUM_CPUS ))
    mask=$(printf "%x" $(( 1 << cpu )))
    echo "$mask" > "/proc/irq/${irq}/smp_affinity"
    echo "  IRQ $irq (queue-$i) → CPU $cpu"
    (( i++ )) || true
done

# ── 7. Launch xdpd ───────────────────────────────────────────────────────────
echo; echo "=== 7. Launch xdpd (turbo mode) ==="
echo "  taskset -c $ALL_CPUS $XDPD -iface $IFACE -redirect-dev $REDIRECT_DEV -config $TURBO_CFG -static $SCRIPT_DIR/frontend/dist"
echo "  Ctrl+C untuk stop"
echo

trap '
    echo; echo "Stopping xdpd..."
    kill "$XDPD_PID" 2>/dev/null || true
    wait "$XDPD_PID" 2>/dev/null || true
    echo "Restoring irqbalance..."
    systemctl start irqbalance 2>/dev/null || true
    exit 0
' INT TERM

taskset -c "$ALL_CPUS" "$XDPD" \
    -iface "$IFACE" \
    -redirect-dev "$REDIRECT_DEV" \
    -config "$TURBO_CFG" \
    -db "$DB_PATH" \
    -addr :9898 \
    -static "$SCRIPT_DIR/frontend/dist" &
XDPD_PID=$!

wait "$XDPD_PID"
