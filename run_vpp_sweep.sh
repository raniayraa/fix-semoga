#!/bin/bash
# VPP experiment sweep: port count 1–10 × traffic directions (41, 15, 15_41)
#
# Usage:
#   bash run_vpp_sweep.sh [OPTIONS]
#
# Options:
#   --dry-run            Print plan without executing anything
#   --duration N         Seconds of active traffic per run       (default: 15)
#   --setup-wait N       Seconds to wait for pktgen to init      (default: 10)
#   --reps N             Repetitions per combination             (default: 1)
#   --cooldown N         Seconds to settle between runs          (default: 5)
#   --skip-setup         Skip 01/02/03 setup playbooks (faster on re-runs)
#   --min-ports N        Start port count from N (resume support) (default: 1)
#   --max-ports N        End port count at N                      (default: 10)
#   --directions D       Comma-separated directions to run, e.g. 15,15_41    (default: 41,15,15_41)
#
# Prerequisites: node6 reachable via ansible inventory

set -uo pipefail

# ─── Defaults ─────────────────────────────────────────────────────────────────
BASE_PORT=1024
MIN_PORTS=1
MAX_PORTS=10
DIRECTIONS=(41 15 15_41)

DURATION=15
SETUP_WAIT=10
REPS=1
COOLDOWN=5
DRY_RUN=0
SKIP_SETUP=0

# ─── Arg parsing ──────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)       DRY_RUN=1 ;;
    --skip-setup)    SKIP_SETUP=1 ;;
    --duration)      shift; DURATION="$1" ;;
    --duration=*)    DURATION="${1#*=}" ;;
    --setup-wait)    shift; SETUP_WAIT="$1" ;;
    --setup-wait=*)  SETUP_WAIT="${1#*=}" ;;
    --reps)          shift; REPS="$1" ;;
    --reps=*)        REPS="${1#*=}" ;;
    --cooldown)      shift; COOLDOWN="$1" ;;
    --cooldown=*)    COOLDOWN="${1#*=}" ;;
    --min-ports)     shift; MIN_PORTS="$1" ;;
    --min-ports=*)   MIN_PORTS="${1#*=}" ;;
    --max-ports)     shift; MAX_PORTS="$1" ;;
    --max-ports=*)   MAX_PORTS="${1#*=}" ;;
    --directions)    shift; IFS=',' read -ra DIRECTIONS <<< "$1" ;;
    --directions=*)  IFS=',' read -ra DIRECTIONS <<< "${1#*=}" ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
  shift
done

# ─── Paths ────────────────────────────────────────────────────────────────────
ROOT_DIR="$HOME/final_t40"
ANSIBLE_DIR="$ROOT_DIR/ansible"
INVENTORY="$ANSIBLE_DIR/inventory.ini"
PKT_DIR="$ROOT_DIR/dashboard/pkt_files"
PKTGEN_CFG="$ROOT_DIR/dashboard/pktgen_config.json"
RESULTS_DIR="$ROOT_DIR/results"
SIGNAL_START="/tmp/ansible_pktgen_start"
SIGNAL_STOP="/tmp/ansible_pktgen_stop"
NIC_INGRESS="enp1s0f1np1"

SWEEP_TS=$(date +%Y%m%d_%H%M%S)
LOG_FILE="/tmp/vpp_sweep_${SWEEP_TS}.log"
SUMMARY_CSV="${RESULTS_DIR}/vpp_sweep_summary_${SWEEP_TS}.csv"

SETUP_PLAYBOOKS=(01_basic_setup.yaml 02_setup_route.yaml 03_setup_scripts.yaml)
VPP_PLAYBOOK="04_setup_vpp_node6.yaml"
PKTGEN_PLAYBOOK="05_start_pktgen.yaml"

# ─── Logging ──────────────────────────────────────────────────────────────────
log() {
  local msg="[$(date +%H:%M:%S)] $*"
  echo "$msg"
  echo "$msg" >> "$LOG_FILE"
}

die() { log "ERROR: $*"; exit 1; }

# ─── Ctrl+C cleanup ───────────────────────────────────────────────────────────
_cleanup() {
  echo ""
  log "Interrupted — cleaning up ..."
  rm -f "$SIGNAL_START" "$SIGNAL_STOP"
  pkill -f "$PKTGEN_PLAYBOOK" 2>/dev/null || true
  log "Done. Partial results saved in ${RESULTS_DIR}."
  exit 130
}
trap _cleanup INT TERM

# ─── Playbook runner ──────────────────────────────────────────────────────────
run_playbook() {
  local pb="$1"
  log "    → ${pb} ..."
  if [[ $DRY_RUN -eq 1 ]]; then
    log "      [dry-run] would run: ansible-playbook -i ${INVENTORY} ${ANSIBLE_DIR}/${pb}"
    return 0
  fi
  ansible-playbook -i "$INVENTORY" "${ANSIBLE_DIR}/${pb}" >> "$LOG_FILE" 2>&1
  local rc=$?
  [[ $rc -ne 0 ]] && log "      FAILED (exit ${rc})"
  return $rc
}

# ─── Pkt file updater ─────────────────────────────────────────────────────────
_update_pkt_file() {
  local f="$1" ps="$2" pe="$3"
  sed -i -E "s/(range 0 (src|dst) port (start|min))[[:space:]]+[0-9]+/\1 ${ps}/" "$f"
  sed -i -E "s/(range 0 (src|dst) port max)[[:space:]]+[0-9]+/\1   ${pe}/"       "$f"
}

update_pkt_files() {
  local ps=$1 pe=$2
  if [[ $DRY_RUN -eq 1 ]]; then
    log "    [dry-run] set port ${ps}–${pe} in node1_send.pkt, node4_send.pkt"; return
  fi
  _update_pkt_file "$PKT_DIR/node1_send.pkt" "$ps" "$pe"
  _update_pkt_file "$PKT_DIR/node4_send.pkt" "$ps" "$pe"
}

# ─── Pktgen config ────────────────────────────────────────────────────────────
update_pktgen_config() {
  local dir=$1
  if [[ $DRY_RUN -eq 1 ]]; then
    log "    [dry-run] pktgen_config.json → direction=${dir}"; return
  fi
  case "$dir" in
    41)
      printf '{\n  "10.90.1.4": "/home/telmat/node4_send.pkt"\n}\n' > "$PKTGEN_CFG"
      ;;
    15)
      printf '{\n  "10.90.1.1": "/home/telmat/node1_send.pkt"\n}\n' > "$PKTGEN_CFG"
      ;;
    15_41)
      printf '{\n  "10.90.1.4": "/home/telmat/node4_send.pkt",\n  "10.90.1.1": "/home/telmat/node1_send.pkt"\n}\n' > "$PKTGEN_CFG"
      ;;
    *) die "Unknown direction: $dir" ;;
  esac
}

# ─── Unique dir name ──────────────────────────────────────────────────────────
unique_name() {
  local base="$1"
  [[ ! -d "$base" ]] && { echo "$base"; return; }
  local v=2
  while [[ -d "${base}_v${v}" ]]; do (( v++ )) || true; done
  echo "${base}_v${v}"
}

# ─── Find newly created result dir ────────────────────────────────────────────
find_new_result() {
  local snap="$1" newest="" newest_mtime=0
  for d in "$RESULTS_DIR"/pktgen_stats_*; do
    [[ -d "$d" ]] || continue
    grep -qxF "$d" "$snap" && continue
    local mt; mt=$(stat -c %Y "$d")
    (( mt > newest_mtime )) && { newest_mtime=$mt; newest="$d"; }
  done
  echo "$newest"
}

# ─── Save experiment metadata ─────────────────────────────────────────────────
save_meta() {
  local dir="$1" n_ports="$2" ps="$3" pe="$4" direction="$5" rep="$6"
  cat > "${dir}/sweep_meta.json" << EOF
{
  "forwarder": "VPP",
  "n_ports": ${n_ports},
  "port_start": ${ps},
  "port_end": ${pe},
  "direction": "${direction}",
  "rep": ${rep},
  "duration_s": ${DURATION},
  "timestamp": "$(date -Iseconds)"
}
EOF
}

# ─── NIC queue count ──────────────────────────────────────────────────────────
nic_queue_count() {
  ethtool -l "$NIC_INGRESS" 2>/dev/null \
    | awk '/Current hardware/{found=1} found && /[Cc]ombined/{print $NF; exit}
           found && /RX/{print $NF; exit}'
}

# ─── ETA tracker ──────────────────────────────────────────────────────────────
ETA_START=0
eta_init() { ETA_START=$(date +%s); }
eta_str() {
  local done=$1 total=$2
  [[ $done -eq 0 ]] && { echo "--:--"; return; }
  local elapsed=$(( $(date +%s) - ETA_START ))
  local rem=$(( elapsed * (total - done) / done ))
  printf "%dm%02ds" $(( rem / 60 )) $(( rem % 60 ))
}

# ─── Pre-flight checks ────────────────────────────────────────────────────────
preflight() {
  [[ $DRY_RUN -eq 1 ]] && return

  ansible -i "$INVENTORY" 10.90.1.6 -m ping > /dev/null 2>&1 \
    || die "Node 6 not reachable via ansible"
  log "Node 6 reachable OK"

  local q; q=$(nic_queue_count)
  if [[ -n "$q" ]]; then
    log "NIC ${NIC_INGRESS}: ${q} active RX queue(s)"
    if (( q < 4 )); then
      log "  WARNING: Only ${q} queue(s) active."
      log "  With ≤2 port flows, RSS will hash everything to 1 CPU → saturation."
    fi
  fi
}

# ─── Post-sweep summary ───────────────────────────────────────────────────────
generate_summary() {
  [[ $DRY_RUN -eq 1 ]] && return
  log ""
  log "Generating summary → ${SUMMARY_CSV}"
  python3 - "$RESULTS_DIR" "$SUMMARY_CSV" << 'PYEOF'
import sys, csv, json, os
from pathlib import Path

results_dir = Path(sys.argv[1])
out_csv     = Path(sys.argv[2])

def median_delta(path, port, metric):
    vals, prev = [], None
    try:
        with open(path) as f:
            for row in csv.DictReader(f):
                if row["Port"] == str(port) and row["Metric"] == metric:
                    v = int(row["Value"])
                    if prev is not None:
                        vals.append(v - prev)
                    prev = v
    except Exception:
        return None
    if not vals:
        return None
    vals.sort()
    return vals[len(vals) // 2]

rows = []
for d in sorted(results_dir.iterdir()):
    if not d.is_dir():
        continue
    meta_f = d / "sweep_meta.json"
    if not meta_f.exists():
        continue
    meta = json.loads(meta_f.read_text())
    if meta.get("forwarder") != "VPP":
        continue
    direction = meta["direction"]
    n_ports   = meta["n_ports"]
    rep       = meta.get("rep", 1)

    if direction in ("15", "15_41"):
        tx_csv, rx_csv = d / "node1.csv", d / "node5.csv"
    else:
        tx_csv, rx_csv = d / "node4.csv", d / "node1.csv"

    tx = median_delta(tx_csv, 0, "opackets")
    rx = median_delta(rx_csv, 0, "ipackets")
    if tx is None or rx is None:
        continue

    drop = round((1 - rx / tx) * 100, 2) if tx > 0 else 0.0
    rows.append({
        "experiment": d.name,
        "n_ports":    n_ports,
        "direction":  direction,
        "rep":        rep,
        "tx_pps":     tx,
        "rx_pps":     rx,
        "drop_pct":   drop,
    })

if not rows:
    print("  No VPP sweep results with sweep_meta.json found yet.")
    sys.exit(0)

with open(out_csv, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=rows[0].keys())
    w.writeheader()
    w.writerows(rows)

hdr = f"{'Experiment':<50} {'ports':>5} {'dir':<6} {'rep':>3} {'TX pps':>14} {'RX pps':>14} {'drop%':>7}"
print(hdr)
print("─" * len(hdr))
for r in rows:
    print(f"{r['experiment']:<50} {r['n_ports']:>5} {r['direction']:<6} {r['rep']:>3} "
          f"{r['tx_pps']:>14,} {r['rx_pps']:>14,} {r['drop_pct']:>6.1f}%")
print(f"\n{len(rows)} experiments written to {out_csv}")
PYEOF
}

# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════
total=$(( (MAX_PORTS - MIN_PORTS + 1) * ${#DIRECTIONS[@]} * REPS ))

log "═══════════════════════════════════════════════════════════"
log " VPP Experiment Sweep"
log " Port counts  : ${MIN_PORTS}–${MAX_PORTS}  (base port ${BASE_PORT})"
log " Directions   : ${DIRECTIONS[*]}"
log "   41    = Node4 → Node1"
log "   15    = Node1 → Node5"
log "   15_41 = Node4 → Node1  AND  Node1 → Node5"
log " Repetitions  : ${REPS}x per combination"
log " Total runs   : ${total}"
log " Timing       : ${DURATION}s traffic | ${SETUP_WAIT}s pktgen init | ${COOLDOWN}s cooldown"
[[ $SKIP_SETUP -eq 1 ]] && log " Setup playbooks: SKIPPED (--skip-setup)"
[[ $DRY_RUN -eq 1 ]]    && log " [DRY RUN — no changes will be made]"
log " Log file     : ${LOG_FILE}"
log "═══════════════════════════════════════════════════════════"

preflight
mkdir -p "$RESULTS_DIR"
eta_init

failed=()
run=0

for n_ports in $(seq "$MIN_PORTS" "$MAX_PORTS"); do
  port_start=$BASE_PORT
  port_end=$(( BASE_PORT + n_ports - 1 ))
  port_label="${port_start}-${port_end}"

  for direction in "${DIRECTIONS[@]}"; do
    for rep in $(seq 1 "$REPS"); do
      (( run++ )) || true
      run_label="VPP | ports=${n_ports} (${port_label}) | dir=${direction} | rep=${rep}/${REPS}"
      eta=$(eta_str $(( run - 1 )) "$total")

      log ""
      log "───────────────────────────────────────────────────────────"
      log "[${run}/${total}]  ${run_label}  (ETA remaining: ${eta})"
      log "───────────────────────────────────────────────────────────"

      # ── [1] Update pkt files ─────────────────────────────────────────────────
      log "  [1/5] Pkt files → port ${port_start}–${port_end}"
      update_pkt_files "$port_start" "$port_end"

      # ── [2] Update pktgen config ─────────────────────────────────────────────
      log "  [2/5] pktgen_config.json → direction ${direction}"
      update_pktgen_config "$direction"

      # ── [3] Setup playbooks ──────────────────────────────────────────────────
      if [[ $SKIP_SETUP -eq 0 ]]; then
        log "  [3/5] Setup playbooks ..."
        for pb in "${SETUP_PLAYBOOKS[@]}"; do
          if ! run_playbook "$pb"; then
            log "  WARNING: ${pb} failed — continuing anyway (NIC may already be bound to VPP/DPDK)"
          fi
        done
      else
        log "  [3/5] Setup playbooks — SKIPPED"
      fi

      # ── [4] VPP forwarder ────────────────────────────────────────────────────
      log "  [4/5] Configure VPP forwarder ..."
      if ! run_playbook "$VPP_PLAYBOOK"; then
        log "  SKIP: VPP playbook failed → ${run_label}"
        failed+=("$run_label"); continue
      fi

      # ── [5] Pktgen experiment ────────────────────────────────────────────────
      log "  [5/5] Pktgen experiment ..."

      if [[ $DRY_RUN -eq 1 ]]; then
        rep_suffix=$([[ $REPS -gt 1 ]] && echo "_rep${rep}" || echo "")
        log "    [dry-run] launch → start signal → ${DURATION}s → stop signal → rename"
        log "    [dry-run] result: VPP_${port_label}_Port_No_Block_${direction}${rep_suffix}"
        continue
      fi

      snap=$(mktemp)
      find "$RESULTS_DIR" -maxdepth 1 -name "pktgen_stats_*" -type d > "$snap"
      rm -f "$SIGNAL_START" "$SIGNAL_STOP"

      ansible-playbook -i "$INVENTORY" "${ANSIBLE_DIR}/${PKTGEN_PLAYBOOK}" \
        >> "$LOG_FILE" 2>&1 &
      pktgen_pid=$!

      log "    Waiting ${SETUP_WAIT}s for pktgen to initialize ..."
      sleep "$SETUP_WAIT"
      touch "$SIGNAL_START"
      log "    Traffic STARTED"

      sleep "$DURATION"
      touch "$SIGNAL_STOP"
      log "    Traffic STOPPED — waiting for result collection ..."

      wait "$pktgen_pid" || true
      pktgen_exit=$?
      log "    Ansible finished (exit ${pktgen_exit})"
      [[ $pktgen_exit -ne 0 ]] && log "    WARNING: pktgen playbook exited non-zero"

      new_dir=$(find_new_result "$snap")
      rm -f "$snap"

      if [[ -n "$new_dir" ]]; then
        rep_suffix=$([[ $REPS -gt 1 ]] && echo "_rep${rep}" || echo "")
        target_name="VPP_${port_label}_Port_No_Block_${direction}${rep_suffix}"
        target=$(unique_name "${RESULTS_DIR}/${target_name}")
        mv "$new_dir" "$target"
        save_meta "$target" "$n_ports" "$port_start" "$port_end" "$direction" "$rep"
        log "    Saved → $(basename "$target")"
      else
        log "    WARNING: No new result directory found."
        failed+=("$run_label")
      fi

      # Cooldown (skip after last run)
      if [[ $run -lt $total && $COOLDOWN -gt 0 ]]; then
        log "  Cooldown ${COOLDOWN}s ..."
        sleep "$COOLDOWN"
      fi

    done  # reps
  done    # directions
done      # port counts

# ─── Summary ──────────────────────────────────────────────────────────────────
log ""
log "═══════════════════════════════════════════════════════════"
log "Sweep complete: $(( total - ${#failed[@]} ))/${total} runs succeeded."
if [[ ${#failed[@]} -gt 0 ]]; then
  log "Failed runs:"
  for f in "${failed[@]}"; do log "  ✗ ${f}"; done
fi

generate_summary

log ""
log "Full log : ${LOG_FILE}"
log "Summary  : ${SUMMARY_CSV}"
log "═══════════════════════════════════════════════════════════"

[[ ${#failed[@]} -eq 0 ]]
