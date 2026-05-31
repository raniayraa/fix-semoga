#!/bin/bash
# XDP Firewall experiment sweep
#
# Two experiment modes:
#
#   sweep       : total ports 10,20,...,100 × directions
#                 firewall blocks HALF the ports each run
#                 expected: ~50% throughput reduction from blocking
#
#   incremental : fixed 10 total ports × blocked count 1..10 × directions
#                 firewall blocks +1 port per step until all 10 are blocked
#
# Usage:
#   bash run_xdp_firewall_sweep.sh [OPTIONS]
#
# Options:
#   --mode M         sweep|incremental|both                   (default: both)
#   --protocol P     udp|tcp|both  — which protocol to block  (default: udp)
#   --directions D   Comma-separated directions, e.g. 15,41   (default: 41,15,15_41)
#   --duration N     Seconds of active traffic per run        (default: 15)
#   --setup-wait N   Seconds to wait for pktgen to init       (default: 10)
#   --reps N         Repetitions per combination              (default: 1)
#   --cooldown N     Seconds to settle between runs           (default: 5)
#   --skip-setup     Skip 01/02/03 setup playbooks
#   --dry-run        Print plan without executing anything
#
# Prerequisites: xdpd running on :9898  →  bash start2.sh

set -uo pipefail

# ─── Defaults ─────────────────────────────────────────────────────────────────
BASE_PORT=1024
DIRECTIONS=(41 15 15_41)
MODE="both"        # sweep | incremental | both
PROTOCOL="tcp"     # udp | tcp | both

DURATION=15
SETUP_WAIT=10
REPS=1
COOLDOWN=5
DRY_RUN=0
SKIP_SETUP=0

# ─── Arg parsing ──────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)        DRY_RUN=1 ;;
    --skip-setup)     SKIP_SETUP=1 ;;
    --mode)           shift; MODE="$1" ;;
    --mode=*)         MODE="${1#*=}" ;;
    --protocol)       shift; PROTOCOL="$1" ;;
    --protocol=*)     PROTOCOL="${1#*=}" ;;
    --duration)       shift; DURATION="$1" ;;
    --duration=*)     DURATION="${1#*=}" ;;
    --setup-wait)     shift; SETUP_WAIT="$1" ;;
    --setup-wait=*)   SETUP_WAIT="${1#*=}" ;;
    --reps)           shift; REPS="$1" ;;
    --reps=*)         REPS="${1#*=}" ;;
    --cooldown)       shift; COOLDOWN="$1" ;;
    --cooldown=*)     COOLDOWN="${1#*=}" ;;
    --directions)     shift; IFS=',' read -ra DIRECTIONS <<< "$1" ;;
    --directions=*)   IFS=',' read -ra DIRECTIONS <<< "${1#*=}" ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
  shift
done

# Validate mode and protocol
case "$MODE" in
  sweep|incremental|both) ;;
  *) echo "Invalid --mode: $MODE (use sweep|incremental|both)"; exit 1 ;;
esac
case "$PROTOCOL" in
  udp|tcp|both) ;;
  *) echo "Invalid --protocol: $PROTOCOL (use udp|tcp|both)"; exit 1 ;;
esac

# ─── Paths ────────────────────────────────────────────────────────────────────
ROOT_DIR="$HOME/final_t40"
ANSIBLE_DIR="$ROOT_DIR/ansible"
INVENTORY="$ANSIBLE_DIR/inventory.ini"
PKT_DIR="$ROOT_DIR/dashboard/pkt_files"
PKTGEN_CFG="$ROOT_DIR/dashboard/pktgen_config.json"
RESULTS_DIR="$ROOT_DIR/results"
SIGNAL_START="/tmp/ansible_pktgen_start"
SIGNAL_STOP="/tmp/ansible_pktgen_stop"
XDP_API="http://localhost:9898/api"
NIC_INGRESS="enp1s0f1np1"

SWEEP_TS=$(date +%Y%m%d_%H%M%S)
LOG_FILE="/tmp/xdp_fw_sweep_${SWEEP_TS}.log"
SUMMARY_CSV="${RESULTS_DIR}/xdp_fw_sweep_summary_${SWEEP_TS}.csv"

SETUP_PLAYBOOKS=(01_basic_setup.yaml 02_setup_route.yaml 03_setup_scripts.yaml)
XDP_PLAYBOOK="04_setup_xdp_node6.yaml"
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
  log "Interrupted — clearing firewall rules and cleaning up ..."
  rm -f "$SIGNAL_START" "$SIGNAL_STOP"
  pkill -f "$PKTGEN_PLAYBOOK" 2>/dev/null || true
  # Clear block rules on exit so the forwarder is left in a clean state
  curl -sf -X PUT "${XDP_API}/config" \
    -H "Content-Type: application/json" \
    -d '{"tcp_ports":[],"udp_ports":[]}' > /dev/null 2>&1 || true
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

# ─── Firewall rule manager ────────────────────────────────────────────────────
# Builds a JSON array of integers from port_start to port_end (inclusive).
_ports_json() {
  local ps=$1 pe=$2
  python3 -c "import json; print(json.dumps(list(range($ps, $pe+1))))"
}

# Pushes block rules to xdpd via PUT /api/config.
# Args: port_start  port_end  (ports to BLOCK)
set_blocked_ports() {
  local ps=$1 pe=$2
  local count=$(( pe - ps + 1 ))

  if [[ $DRY_RUN -eq 1 ]]; then
    log "    [dry-run] PUT /api/config → block ports ${ps}–${pe} (${count} ports) [${PROTOCOL}]"
    return 0
  fi

  local ports_arr; ports_arr=$(_ports_json "$ps" "$pe")

  local body
  case "$PROTOCOL" in
    udp)  body="{\"tcp_ports\":[],\"udp_ports\":${ports_arr}}" ;;
    tcp)  body="{\"tcp_ports\":${ports_arr},\"udp_ports\":[]}" ;;
    both) body="{\"tcp_ports\":${ports_arr},\"udp_ports\":${ports_arr}}" ;;
  esac

  local http_code
  http_code=$(curl -sf -o /dev/null -w "%{http_code}" \
    -X PUT "${XDP_API}/config" \
    -H "Content-Type: application/json" \
    -d "$body" 2>>"$LOG_FILE")

  if [[ "$http_code" != "200" ]]; then
    log "    WARNING: PUT /api/config returned HTTP ${http_code}"
    return 1
  fi
  log "    Firewall: blocking ports ${ps}–${pe} (${count} ports, protocol=${PROTOCOL}) ✓"
}

# Clears all blocked ports (open firewall).
clear_blocked_ports() {
  if [[ $DRY_RUN -eq 1 ]]; then
    log "    [dry-run] PUT /api/config → clear all block rules"
    return 0
  fi
  curl -sf -X PUT "${XDP_API}/config" \
    -H "Content-Type: application/json" \
    -d '{"tcp_ports":[],"udp_ports":[]}' >> "$LOG_FILE" 2>&1 || true
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
  local dir="$1" exp_type="$2" n_ports="$3" n_blocked="$4" \
        ps="$5" pe="$6" block_ps="$7" block_pe="$8" direction="$9" rep="${10}"
  cat > "${dir}/sweep_meta.json" << EOF
{
  "forwarder": "XDP_Firewall",
  "experiment_type": "${exp_type}",
  "n_ports": ${n_ports},
  "n_blocked": ${n_blocked},
  "port_start": ${ps},
  "port_end": ${pe},
  "block_port_start": ${block_ps},
  "block_port_end": ${block_pe},
  "protocol": "${PROTOCOL}",
  "direction": "${direction}",
  "rep": ${rep},
  "duration_s": ${DURATION},
  "timestamp": "$(date -Iseconds)"
}
EOF
}

# ─── Pre-flight checks ────────────────────────────────────────────────────────
preflight() {
  [[ $DRY_RUN -eq 1 ]] && return

  curl -sf "${XDP_API}/routes" > /dev/null 2>&1 \
    || die "xdpd not reachable at ${XDP_API} — start it first: bash start2.sh"
  log "xdpd API OK (${XDP_API})"

  curl -sf -o /dev/null -w "%{http_code}" \
    -X PUT "${XDP_API}/config" \
    -H "Content-Type: application/json" \
    -d '{"tcp_ports":[],"udp_ports":[]}' > /dev/null 2>&1 \
    || log "  WARNING: PUT /api/config pre-flight failed — check xdpd supports /config"

  local q; q=$(ethtool -l "$NIC_INGRESS" 2>/dev/null \
    | awk '/Current hardware/{found=1} found && /[Cc]ombined/{print $NF; exit}
           found && /RX/{print $NF; exit}')
  [[ -n "$q" ]] && log "NIC ${NIC_INGRESS}: ${q} active RX queue(s)"
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

# ─── Core: run one experiment ─────────────────────────────────────────────────
# Args: exp_type  n_ports  n_blocked  port_start  port_end  direction  rep  result_label
run_one() {
  local exp_type="$1" n_ports="$2" n_blocked="$3" \
        port_start="$4" port_end="$5" direction="$6" rep="$7" label="$8"
  local block_port_end=$(( port_start + n_blocked - 1 ))

  # ── [1] Pkt files ────────────────────────────────────────────────────────────
  log "  [1/6] Pkt files → port ${port_start}–${port_end}"
  update_pkt_files "$port_start" "$port_end"

  # ── [2] Pktgen config ────────────────────────────────────────────────────────
  log "  [2/6] pktgen_config.json → direction ${direction}"
  update_pktgen_config "$direction"

  # ── [3] Setup playbooks ──────────────────────────────────────────────────────
  if [[ $SKIP_SETUP -eq 0 ]]; then
    log "  [3/6] Setup playbooks ..."
    local setup_ok=1
    for pb in "${SETUP_PLAYBOOKS[@]}"; do
      if ! run_playbook "$pb"; then setup_ok=0; break; fi
    done
    if [[ $setup_ok -eq 0 ]]; then
      log "  SKIP: setup playbook failed → ${label}"
      return 1
    fi
  else
    log "  [3/6] Setup playbooks — SKIPPED"
  fi

  # ── [4] XDP forwarder (no block rules yet) ───────────────────────────────────
  log "  [4/6] Configure XDP forwarder (clean state) ..."
  if ! run_playbook "$XDP_PLAYBOOK"; then
    log "  SKIP: XDP playbook failed → ${label}"
    return 1
  fi

  # ── [5] Apply firewall block rules ───────────────────────────────────────────
  log "  [5/6] Apply firewall: block ports ${port_start}–${block_port_end} (${n_blocked}/${n_ports})"
  if ! set_blocked_ports "$port_start" "$block_port_end"; then
    log "  SKIP: failed to set firewall rules → ${label}"
    return 1
  fi

  # ── [6] Pktgen experiment ────────────────────────────────────────────────────
  log "  [6/6] Pktgen experiment ..."

  if [[ $DRY_RUN -eq 1 ]]; then
    log "    [dry-run] launch → start signal → ${DURATION}s → stop → rename → ${label}"
    clear_blocked_ports
    return 0
  fi

  local snap; snap=$(mktemp)
  find "$RESULTS_DIR" -maxdepth 1 -name "pktgen_stats_*" -type d > "$snap"
  rm -f "$SIGNAL_START" "$SIGNAL_STOP"

  ansible-playbook -i "$INVENTORY" "${ANSIBLE_DIR}/${PKTGEN_PLAYBOOK}" \
    >> "$LOG_FILE" 2>&1 &
  local pktgen_pid=$!

  log "    Waiting ${SETUP_WAIT}s for pktgen to initialize ..."
  sleep "$SETUP_WAIT"
  touch "$SIGNAL_START"
  log "    Traffic STARTED"

  sleep "$DURATION"
  touch "$SIGNAL_STOP"
  log "    Traffic STOPPED — waiting for result collection ..."

  wait "$pktgen_pid" || true
  local pktgen_exit=$?
  log "    Ansible finished (exit ${pktgen_exit})"
  [[ $pktgen_exit -ne 0 ]] && log "    WARNING: pktgen playbook exited non-zero"

  # Clear firewall rules after experiment
  clear_blocked_ports
  log "    Firewall rules cleared."

  local new_dir; new_dir=$(find_new_result "$snap")
  rm -f "$snap"

  if [[ -n "$new_dir" ]]; then
    local target; target=$(unique_name "${RESULTS_DIR}/${label}")
    mv "$new_dir" "$target"
    save_meta "$target" "$exp_type" "$n_ports" "$n_blocked" \
      "$port_start" "$port_end" "$port_start" "$block_port_end" "$direction" "$rep"
    log "    Saved → $(basename "$target")"
  else
    log "    WARNING: No new result directory found."
    return 1
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
    if meta.get("forwarder") != "XDP_Firewall":
        continue

    direction  = meta["direction"]
    n_ports    = meta["n_ports"]
    n_blocked  = meta["n_blocked"]
    exp_type   = meta.get("experiment_type", "?")
    rep        = meta.get("rep", 1)

    if direction in ("15", "15_41"):
        tx_csv, rx_csv = d / "node1.csv", d / "node5.csv"
    else:
        tx_csv, rx_csv = d / "node4.csv", d / "node1.csv"

    tx = median_delta(tx_csv, 0, "opackets")
    rx = median_delta(rx_csv, 0, "ipackets")
    if tx is None or rx is None:
        continue

    drop     = round((1 - rx / tx) * 100, 2) if tx > 0 else 0.0
    pass_pct = round(100 - drop, 2)
    rows.append({
        "experiment":  d.name,
        "type":        exp_type,
        "n_ports":     n_ports,
        "n_blocked":   n_blocked,
        "block_pct":   round(n_blocked / n_ports * 100, 1),
        "direction":   direction,
        "rep":         rep,
        "tx_pps":      tx,
        "rx_pps":      rx,
        "drop_pct":    drop,
        "pass_pct":    pass_pct,
    })

if not rows:
    print("  No XDP Firewall sweep results found.")
    sys.exit(0)

with open(out_csv, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=rows[0].keys())
    w.writeheader()
    w.writerows(rows)

hdr = (f"{'Experiment':<55} {'type':<11} {'ports':>5} {'blk':>4} "
       f"{'blk%':>5} {'dir':<6} {'rep':>3} {'TX pps':>14} {'RX pps':>14} "
       f"{'drop%':>7} {'pass%':>7}")
print(hdr)
print("─" * len(hdr))
for r in rows:
    print(f"{r['experiment']:<55} {r['type']:<11} {r['n_ports']:>5} "
          f"{r['n_blocked']:>4} {r['block_pct']:>4.0f}% {r['direction']:<6} "
          f"{r['rep']:>3} {r['tx_pps']:>14,} {r['rx_pps']:>14,} "
          f"{r['drop_pct']:>6.1f}% {r['pass_pct']:>6.1f}%")
print(f"\n{len(rows)} experiments written to {out_csv}")
PYEOF
}

# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════

# Count total runs
n_dirs=${#DIRECTIONS[@]}
n_sweep=0
n_incr=0
[[ "$MODE" == "sweep" || "$MODE" == "both" ]] && \
  n_sweep=$(( (100 - 10) / 10 + 1 ))   # 10 steps: 10,20,...,100
[[ "$MODE" == "incremental" || "$MODE" == "both" ]] && \
  n_incr=10                              # block 1..10
total=$(( (n_sweep + n_incr) * n_dirs * REPS ))

log "═══════════════════════════════════════════════════════════"
log " XDP Firewall Experiment Sweep"
log " Mode         : ${MODE}"
log " Protocol     : ${PROTOCOL} (blocked protocol)"
log " Directions   : ${DIRECTIONS[*]}"
log "   41    = Node4 → Node1"
log "   15    = Node1 → Node5"
log "   15_41 = Node4 → Node1  AND  Node1 → Node5"
if [[ "$MODE" == "sweep" || "$MODE" == "both" ]]; then
  log " [A] Sweep    : total ports 10,20,...,100  |  block = half"
fi
if [[ "$MODE" == "incremental" || "$MODE" == "both" ]]; then
  log " [B] Incr.    : fixed 10 total ports  |  block 1,2,...,10"
fi
log " Repetitions  : ${REPS} per combination"
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

# ─── Experiment A: Port Sweep (10,20,...,100), block half ─────────────────────
if [[ "$MODE" == "sweep" || "$MODE" == "both" ]]; then
  log ""
  log "━━━━ EXPERIMENT A: Port Sweep (block half) ━━━━━━━━━━━━━━"

  for n_ports in 10 20 30 40 50 60 70 80 90 100; do
    port_start=$BASE_PORT
    port_end=$(( BASE_PORT + n_ports - 1 ))
    n_blocked=$(( n_ports / 2 ))         # block first half
    port_label="${port_start}-${port_end}"

    for direction in "${DIRECTIONS[@]}"; do
      for rep in $(seq 1 "$REPS"); do
        (( run++ )) || true
        rep_suffix=$([[ $REPS -gt 1 ]] && echo "_rep${rep}" || echo "")
        label="XDPFW_${n_ports}ports_${n_blocked}blocked_dir${direction}${rep_suffix}"
        run_label="sweep | ports=${n_ports} blk=${n_blocked} dir=${direction} rep=${rep}/${REPS}"
        eta=$(eta_str $(( run - 1 )) "$total")

        log ""
        log "───────────────────────────────────────────────────────────"
        log "[${run}/${total}]  ${run_label}  (ETA remaining: ${eta})"
        log "───────────────────────────────────────────────────────────"

        if ! run_one "sweep" "$n_ports" "$n_blocked" \
               "$port_start" "$port_end" "$direction" "$rep" "$label"; then
          failed+=("$run_label")
        fi

        if [[ $run -lt $total && $COOLDOWN -gt 0 ]]; then
          log "  Cooldown ${COOLDOWN}s ..."
          sleep "$COOLDOWN"
        fi
      done  # reps
    done    # directions
  done      # port counts
fi

# ─── Experiment B: Incremental blocking (10 ports, block 1..10) ──────────────
if [[ "$MODE" == "incremental" || "$MODE" == "both" ]]; then
  log ""
  log "━━━━ EXPERIMENT B: Incremental Blocking (10 fixed ports) ━━"

  n_ports=10
  port_start=$BASE_PORT
  port_end=$(( BASE_PORT + n_ports - 1 ))

  for n_blocked in $(seq 1 10); do
    for direction in "${DIRECTIONS[@]}"; do
      for rep in $(seq 1 "$REPS"); do
        (( run++ )) || true
        rep_suffix=$([[ $REPS -gt 1 ]] && echo "_rep${rep}" || echo "")
        label="XDPFW_incr_10ports_${n_blocked}blocked_dir${direction}${rep_suffix}"
        run_label="incr | ports=10 blk=${n_blocked} dir=${direction} rep=${rep}/${REPS}"
        eta=$(eta_str $(( run - 1 )) "$total")

        log ""
        log "───────────────────────────────────────────────────────────"
        log "[${run}/${total}]  ${run_label}  (ETA remaining: ${eta})"
        log "───────────────────────────────────────────────────────────"

        if ! run_one "incremental" "$n_ports" "$n_blocked" \
               "$port_start" "$port_end" "$direction" "$rep" "$label"; then
          failed+=("$run_label")
        fi

        if [[ $run -lt $total && $COOLDOWN -gt 0 ]]; then
          log "  Cooldown ${COOLDOWN}s ..."
          sleep "$COOLDOWN"
        fi
      done  # reps
    done    # directions
  done      # blocked counts
fi

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
