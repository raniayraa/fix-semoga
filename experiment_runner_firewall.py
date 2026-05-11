#!/usr/bin/env python3
"""
Experiment automation script for pktgen port-range sweeps with XDP firewall blocking.

Like experiment_runner.py but adds --block-udp-ports: after the XDP forwarder
is set up (which resets BPF maps), this script calls the XDP REST API to block
the specified UDP destination ports before pktgen starts.

--ports takes actual port numbers (start-end), not counts:
  Single port:    --ports 1024
  Range:          --ports 1024-1033
  Multiple:       --ports 1024-1033,2000-2010

Usage examples:
  # Send to 1024-1033, block 1029-1033 (half blocked)
  python experiment_runner_firewall.py --ports 1024-1033 --traffic 41 \
      --forwarder xdp --block-udp-ports 1029-1033 \
      --inventory ansible_automasi/inventory.ini --ansible-dir ansible_automasi

  python experiment_runner_firewall.py --ports 1024-1033 --traffic 41 \
      --forwarder xdp --block-udp-ports 1029-1033 --dry-run
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths (match what ansible scripts expect)
# ---------------------------------------------------------------------------
PKT_FILES_DIR = Path("/home/telmat/final_t40/dashboard/pkt_files")
PKTGEN_CONFIG = Path("/home/telmat/final_t40/dashboard/pktgen_config.json")
RESULTS_DIR   = Path("/home/telmat/final_t40/results")
SIGNAL_START  = Path("/tmp/ansible_pktgen_start")
SIGNAL_STOP   = Path("/tmp/ansible_pktgen_stop")

PKT_NODES = ["node1_send.pkt", "node4_send.pkt"]

FORWARDERS = ["vpp", "xdp", "kernel"]

FORWARDER_PLAYBOOK = {
    "vpp":    "05_setup_vpp_node6.yaml",
    "xdp":    "04_setup_xdp_node6.yaml",
    "kernel": "04_setup_kernel_node6.yaml",
}

FORWARDER_LABEL = {
    "vpp":    "VPP",
    "xdp":    "XDP",
    "kernel": "Kernel",
}

PKTGEN_CONFIG_MAP = {
    "41":    {"10.90.1.4": "/home/telmat/node4_send.pkt"},
    "15":    {"10.90.1.1": "/home/telmat/node1_send.pkt"},
    "15_41": {
        "10.90.1.4": "/home/telmat/node4_send.pkt",
        "10.90.1.1": "/home/telmat/node1_send.pkt",
    },
}

VALID_DIRECTIONS = {"41", "15", "15_41"}

SETUP_PLAYBOOKS = [
    "01_basic_setup.yaml",
    "02_setup_route.yaml",
    "03_setup_scripts.yaml",
]

XDP_API_BASE = "http://localhost:9898/api"

# ---------------------------------------------------------------------------
# Port range parser
# ---------------------------------------------------------------------------

def parse_ports(spec: str) -> list[tuple[int, int]]:
    """Parse port spec into a list of (start, end) ranges.

    Examples:
      "1024"                → [(1024, 1024)]
      "1024-1033"           → [(1024, 1033)]
      "1024-1033,2000-2010" → [(1024, 1033), (2000, 2010)]
    """
    result = []
    seen = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            lo, hi = int(lo.strip()), int(hi.strip())
            if lo > hi:
                raise ValueError(f"Invalid range '{part}': start > end")
        else:
            lo = hi = int(part)
        key = (lo, hi)
        if key not in seen:
            seen.add(key)
            result.append(key)
    if not result:
        raise ValueError("No ports parsed from spec: " + spec)
    return result


def expand_ports(ranges: list[tuple[int, int]]) -> list[int]:
    """Expand a list of (start, end) tuples into a flat sorted list of port numbers."""
    result = set()
    for lo, hi in ranges:
        result.update(range(lo, hi + 1))
    return sorted(result)


def parse_directions(spec: str) -> list[str]:
    dirs = [d.strip() for d in spec.split(",") if d.strip()]
    for d in dirs:
        if d not in VALID_DIRECTIONS:
            raise ValueError(f"Unknown direction '{d}'. Valid: {', '.join(sorted(VALID_DIRECTIONS))}")
    return dirs

# ---------------------------------------------------------------------------
# Pkt file modification
# ---------------------------------------------------------------------------

PORT_LINE_RE = re.compile(
    r"^(range\s+0\s+(?:src|dst)\s+port\s+(?:start|min|max))\s+(\d+)\s*$"
)


def set_port_range(content: str, port_start: int, port_end: int) -> str:
    """Rewrite all src/dst port start/min/max lines to [port_start, port_end]."""
    value_map = {"start": port_start, "min": port_start, "max": port_end}

    lines = content.splitlines(keepends=True)
    out = []
    for line in lines:
        m = PORT_LINE_RE.match(line)
        if m:
            prefix = m.group(1)
            keyword = prefix.split()[-1]  # start / min / max
            value = value_map[keyword]
            gap = line[len(m.group(1)) : line.index(m.group(2), len(m.group(1)))]
            out.append(f"{prefix}{gap}{value}\n")
        else:
            out.append(line)
    return "".join(out)


def update_pkt_files(port_start: int, port_end: int, dry_run: bool) -> None:
    for fname in PKT_NODES:
        path = PKT_FILES_DIR / fname
        if dry_run:
            print(f"    [dry-run] would write port range {port_start}-{port_end} to {path}")
            continue
        original = path.read_text()
        updated = set_port_range(original, port_start, port_end)
        path.write_text(updated)


def update_pktgen_config(direction: str, dry_run: bool) -> None:
    config = PKTGEN_CONFIG_MAP[direction]
    if dry_run:
        print(f"    [dry-run] would write pktgen_config.json: {json.dumps(config)}")
        return
    PKTGEN_CONFIG.write_text(json.dumps(config, indent=2) + "\n")

# ---------------------------------------------------------------------------
# XDP API
# ---------------------------------------------------------------------------

def xdp_set_blocked_ports(tcp_ports: list[int], udp_ports: list[int], dry_run: bool) -> bool:
    """Push blocked TCP and/or UDP port lists to the XDP firewall via REST API.

    Called AFTER the forwarder playbook restarts XDP (which resets BPF maps),
    so the block list is applied on the fresh XDP instance.
    Returns True on success.
    """
    if not tcp_ports and not udp_ports:
        return True

    url = f"{XDP_API_BASE}/config"
    payload: dict = {}
    if tcp_ports:
        payload["tcp_ports"] = tcp_ports
    if udp_ports:
        payload["udp_ports"] = udp_ports
    body = json.dumps(payload).encode()

    if dry_run:
        if tcp_ports:
            print(f"    [dry-run] would PUT {url} tcp_ports={tcp_ports}")
        if udp_ports:
            print(f"    [dry-run] would PUT {url} udp_ports={udp_ports}")
        return True

    if tcp_ports:
        print(f"    Setting blocked TCP ports {tcp_ports} via XDP API ...", end=" ", flush=True)
    if udp_ports:
        print(f"    Setting blocked UDP ports {udp_ports} via XDP API ...", end=" ", flush=True)
    try:
        req = urllib.request.Request(
            url, data=body, method="PUT",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        print("OK")
        return True
    except urllib.error.URLError as e:
        print(f"FAILED ({e})")
        return False

# ---------------------------------------------------------------------------
# Ansible runner
# ---------------------------------------------------------------------------

def run_playbook(playbook: str, ansible_dir: Path, inventory: str,
                 label: str, dry_run: bool,
                 extra_vars: dict | None = None) -> bool:
    """Run a playbook synchronously. Returns True on success."""
    cmd = ["ansible-playbook", "-i", inventory, str(ansible_dir / playbook)]
    if extra_vars:
        cmd += ["-e", json.dumps(extra_vars)]
    if dry_run:
        print(f"    [dry-run] would run: {' '.join(cmd)}")
        return True
    print(f"    Running {playbook} ...", end=" ", flush=True)
    result = subprocess.run(cmd, capture_output=False)
    if result.returncode != 0:
        print(f"FAILED (exit {result.returncode})")
        return False
    print("OK")
    return True

# ---------------------------------------------------------------------------
# Result directory renaming and validation
# ---------------------------------------------------------------------------

def unique_name(base: Path) -> Path:
    """Return base, or base_v2, base_v3 etc. if base already exists."""
    if not base.exists():
        return base
    v = 2
    while True:
        candidate = base.parent / f"{base.name}_v{v}"
        if not candidate.exists():
            return candidate
        v += 1


NODE5_CSV_HEADER_SIZE = 23  # "Time,Port,Metric,Value\n" — getstats.lua header only


def validate_results(result_dir: Path, direction: str) -> bool:
    """Check result quality. Returns True if OK, False if node5 data is missing."""
    if direction not in ("15", "15_41"):
        return True

    node5_csv = result_dir / "node5.csv"
    if not node5_csv.exists():
        print(f"  WARNING [node5]: node5.csv not found in {result_dir.name}")
        return False

    size = node5_csv.stat().st_size
    if size <= NODE5_CSV_HEADER_SIZE:
        print(
            f"  WARNING [node5]: node5.csv is header-only ({size} bytes) in "
            f"{result_dir.name} — VPP/XDP may not be forwarding Node1→Node5 traffic."
        )
        return False

    return True

# ---------------------------------------------------------------------------
# Single experiment
# ---------------------------------------------------------------------------

def run_experiment(
    port_start: int,
    port_end: int,
    forwarder: str,
    direction: str,
    ansible_dir: Path,
    inventory: str,
    duration: int,
    setup_wait: int,
    block_tcp_ports: list[int],
    block_udp_ports: list[int],
    block_half: bool,
    dry_run: bool,
) -> tuple[bool, bool]:
    """Run one experiment. Returns (ansible_ok, data_ok)."""
    port_label = f"{port_start}-{port_end}" if port_start != port_end else str(port_start)

    # --block-half: override block lists with the upper half of this port range.
    if block_half and forwarder == "xdp":
        n = port_end - port_start + 1
        half_start = port_start + n // 2
        block_tcp_ports = list(range(half_start, port_end + 1))
        block_udp_ports = list(range(half_start, port_end + 1))
    total_steps = 5

    print(f"\n  [1/{total_steps}] Modifying pkt files ...", end=" ", flush=True)
    update_pkt_files(port_start, port_end, dry_run)
    if not dry_run:
        print("OK")

    print(f"  [2/{total_steps}] Updating pktgen_config.json ...", end=" ", flush=True)
    update_pktgen_config(direction, dry_run)
    if not dry_run:
        print("OK")

    print(f"  [3/{total_steps}] Running setup playbooks ...")
    for pb in SETUP_PLAYBOOKS:
        ok = run_playbook(pb, ansible_dir, inventory, pb, dry_run)
        if not ok:
            print(f"  ERROR: {pb} failed — skipping this experiment.")
            return False, False

    print(f"  [4/{total_steps}] Running forwarder setup ({forwarder}) ...")
    extra = None
    if forwarder == "xdp":
        extra_data = {}
        if block_tcp_ports:
            extra_data["blocked_tcp_ports"] = block_tcp_ports
        if block_udp_ports:
            extra_data["blocked_udp_ports"] = block_udp_ports
        if extra_data:
            extra = extra_data
    ok = run_playbook(FORWARDER_PLAYBOOK[forwarder], ansible_dir, inventory,
                      FORWARDER_PLAYBOOK[forwarder], dry_run, extra_vars=extra)
    if not ok:
        print(f"  ERROR: forwarder setup failed — skipping this experiment.")
        return False, False

    # Apply port blocks via XDP API after forwarder setup resets BPF maps.
    if forwarder == "xdp":
        if not xdp_set_blocked_ports(block_tcp_ports, block_udp_ports, dry_run):
            print("  WARNING: failed to set blocked ports via XDP API — continuing anyway.")

    print(f"  [{total_steps}/{total_steps}] Running pktgen experiment ...")

    has_block = forwarder == "xdp" and (block_tcp_ports or block_udp_ports)
    block_label = "Block" if has_block else "No_Block"

    if dry_run:
        print(f"    [dry-run] would: launch 05_start_pktgen.yaml, wait {setup_wait}s,")
        print(f"              touch start signal, wait {duration}s, touch stop signal,")
        print(f"              wait for ansible, rename result dir to "
              f"{FORWARDER_LABEL[forwarder]}_{port_label}_Port_{block_label}_{direction}")
        print(f"              validate node5.csv for direction '{direction}'")
        return True, True

    # Snapshot existing result directories
    existing = set(RESULTS_DIR.iterdir()) if RESULTS_DIR.exists() else set()

    # Clear stale signals
    SIGNAL_START.unlink(missing_ok=True)
    SIGNAL_STOP.unlink(missing_ok=True)

    cmd = ["ansible-playbook", "-i", inventory,
           str(ansible_dir / "05_start_pktgen.yaml")]
    proc = subprocess.Popen(cmd)

    print(f"        Waiting {setup_wait}s for pktgen to initialize ...", end=" ", flush=True)
    time.sleep(setup_wait)
    SIGNAL_START.touch()
    print("STARTED")

    print(f"        Running for {duration}s ...", end=" ", flush=True)
    time.sleep(duration)
    SIGNAL_STOP.touch()
    print("STOPPED")

    print(f"        Waiting for ansible to collect results ...", end=" ", flush=True)
    proc.wait()
    print(f"done (exit {proc.returncode})")

    if proc.returncode != 0:
        print("  WARNING: 05_start_pktgen.yaml exited non-zero — results may be incomplete.")

    # Find and rename new result directory
    result_dir = None
    if RESULTS_DIR.exists():
        new_dirs = set(RESULTS_DIR.iterdir()) - existing
        if new_dirs:
            newest = max(new_dirs, key=lambda d: d.stat().st_mtime)
            target_name = f"{FORWARDER_LABEL[forwarder]}_{port_label}_Port_{block_label}_{direction}"  # noqa: E501
            target = unique_name(RESULTS_DIR / target_name)
            newest.rename(target)
            result_dir = target
            print(f"        Results saved as: {target.name}")
        else:
            print("  WARNING: No new result directory found to rename.")

    data_ok = validate_results(result_dir, direction) if result_dir else False
    return proc.returncode == 0, data_ok

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Automate pktgen experiments with XDP firewall port blocking.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--ports", required=True,
        help=(
            'Port range(s) for traffic (actual port numbers, not counts). '
            'Single: "1024". Range: "1024-1033". Multiple: "1024-1033,2000-2010".'
        ),
    )
    parser.add_argument(
        "--block-half", action="store_true",
        help=(
            "For each port range, automatically block the upper half of the ports "
            "(both TCP and UDP) in XDP. Overrides --block-tcp-ports and --block-udp-ports."
        ),
    )
    parser.add_argument(
        "--block-tcp-ports", default="",
        help=(
            'TCP destination ports to block in XDP firewall (applied after forwarder setup). '
            'Same format as --ports. Example: "1029-1033" or "1029,1030,1031".'
        ),
    )
    parser.add_argument(
        "--block-udp-ports", default="",
        help=(
            'UDP destination ports to block in XDP firewall (applied after forwarder setup). '
            'Same format as --ports. Example: "1029-1033" or "1029,1030,1031".'
        ),
    )
    parser.add_argument(
        "--traffic", required=True,
        help='Traffic direction(s): "41", "15", "15_41", or comma-separated combination.',
    )
    parser.add_argument(
        "--duration", type=int, default=15,
        help="Seconds to run pktgen traffic per experiment (default: 15).",
    )
    parser.add_argument(
        "--setup-wait", type=int, default=10,
        help="Seconds to wait after launching pktgen before sending start signal (default: 10).",
    )
    parser.add_argument(
        "--inventory", default=os.environ.get("ANSIBLE_INVENTORY", ""),
        help="Ansible inventory file path (or set ANSIBLE_INVENTORY env var).",
    )
    parser.add_argument(
        "--ansible-dir", default="ansible",
        help="Path to the ansible/ directory (default: ./ansible).",
    )
    parser.add_argument(
        "--forwarder",
        nargs="+",
        choices=FORWARDERS,
        default=FORWARDERS,
        metavar="FORWARDER",
        help="Forwarder(s) to run: vpp, xdp, kernel (default: all three).",
    )
    parser.add_argument(
        "--xdp-api", default=XDP_API_BASE,
        help=f"XDP REST API base URL (default: {XDP_API_BASE}).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would happen without executing anything.",
    )
    args = parser.parse_args()

    if args.xdp_api != XDP_API_BASE:
        globals()["XDP_API_BASE"] = args.xdp_api

    if not args.dry_run and not args.inventory:
        parser.error("--inventory is required (or set ANSIBLE_INVENTORY env var)")

    try:
        ports = parse_ports(args.ports)
    except ValueError as e:
        parser.error(str(e))

    block_tcp_ports: list[int] = []
    if args.block_tcp_ports:
        try:
            block_tcp_ports = expand_ports(parse_ports(args.block_tcp_ports))
        except ValueError as e:
            parser.error(f"--block-tcp-ports: {e}")

    block_udp_ports: list[int] = []
    if args.block_udp_ports:
        try:
            block_udp_ports = expand_ports(parse_ports(args.block_udp_ports))
        except ValueError as e:
            parser.error(f"--block-udp-ports: {e}")

    try:
        directions = parse_directions(args.traffic)
    except ValueError as e:
        parser.error(str(e))

    ansible_dir = Path(args.ansible_dir)
    if not args.dry_run and not ansible_dir.is_dir():
        parser.error(f"ansible-dir not found: {ansible_dir}")

    # Build full experiment list
    experiments = [
        (pstart, pend, fw, direction)
        for (pstart, pend) in ports
        for fw in args.forwarder
        for direction in directions
    ]
    total = len(experiments)

    port_labels = [f"{s}-{e}" if s != e else str(s) for s, e in ports]
    print(f"\nExperiment sweep: {len(ports)} range(s) × {len(args.forwarder)} forwarder(s) × {len(directions)} direction(s) = {total} runs")
    print(f"  Port ranges:       {port_labels}")
    print(f"  Blocked TCP ports: {'upper half per range (--block-half)' if args.block_half else (block_tcp_ports if block_tcp_ports else '(none)')}")
    print(f"  Blocked UDP ports: {'upper half per range (--block-half)' if args.block_half else (block_udp_ports if block_udp_ports else '(none)')}")
    print(f"  Forwarders:        {args.forwarder}")
    print(f"  Directions:        {directions}")
    print(f"  Duration:          {args.duration}s per run")
    print(f"  Setup wait:        {args.setup_wait}s")
    if args.dry_run:
        print("  [DRY RUN — no changes will be made]")

    failed = []
    data_warnings = []
    for idx, (pstart, pend, forwarder, direction) in enumerate(experiments, 1):
        port_label = f"{pstart}-{pend}" if pstart != pend else str(pstart)
        label = f"{FORWARDER_LABEL[forwarder]} | Ports {port_label} | Direction: {direction}"
        if forwarder == "xdp":
            if args.block_half:
                n = pend - pstart + 1
                half_start = pstart + n // 2
                label += f" | Blocking TCP+UDP {half_start}-{pend} (upper half)"
            else:
                if block_tcp_ports:
                    label += f" | Blocking TCP {block_tcp_ports}"
                if block_udp_ports:
                    label += f" | Blocking UDP {block_udp_ports}"
        print(f"\n{'═' * 60}")
        print(f"[{idx}/{total}] {label}")
        print(f"{'═' * 60}")

        ansible_ok, data_ok = run_experiment(
            port_start=pstart,
            port_end=pend,
            forwarder=forwarder,
            direction=direction,
            ansible_dir=ansible_dir,
            inventory=args.inventory,
            duration=args.duration,
            setup_wait=args.setup_wait,
            block_tcp_ports=block_tcp_ports,
            block_udp_ports=block_udp_ports,
            block_half=args.block_half,
            dry_run=args.dry_run,
        )
        has_block = forwarder == "xdp" and (block_tcp_ports or block_udp_ports)
        block_label = "Block" if has_block else "No_Block"
        run_name = f"{FORWARDER_LABEL[forwarder]}_{port_label}_Port_{block_label}_{direction}"
        if not ansible_ok:
            failed.append(run_name)
        if not data_ok and not args.dry_run:
            data_warnings.append(run_name)

    print(f"\n{'═' * 60}")
    print(f"Sweep complete: {total - len(failed)}/{total} ansible runs succeeded.")
    if failed:
        print("\nFailed (ansible error):")
        for name in failed:
            print(f"  {name}")
    if data_warnings:
        print("\nDegraded results (node5.csv empty — forwarder may not have reached Node 5):")
        for name in data_warnings:
            print(f"  {name}")
    sys.exit(1 if (failed or data_warnings) else 0)


if __name__ == "__main__":
    main()
