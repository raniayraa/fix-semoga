#!/usr/bin/env python3
"""
Experiment automation script for pktgen port-range sweeps.

Sweeps across port ranges, forwarder types (VPP → XDP → Kernel), and traffic
directions, running the full ansible playbook sequence for each combination.

--ports takes actual port numbers (start-end), not counts:
  Single port:    --ports 1024
  Range:          --ports 1024-1029
  Multiple:       --ports 1024-1029,2000-2010
  Many ranges:    --ports 1024-1029,1024-1099,1024-1999

Usage examples:
  python experiment_runner.py --ports 1024-1029 --traffic 41,15,15_41 --inventory hosts.ini
  python experiment_runner.py --ports 1024-1029,1024-2048 --traffic 41 --dry-run
  python experiment_runner.py --ports 1024-1543 --traffic 15_41 --duration 30
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
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
    "vpp":    "04_setup_vpp_node6.yaml",
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

# ---------------------------------------------------------------------------
# Multi-route XDP support
# ---------------------------------------------------------------------------

XDP_API_BASE  = "http://localhost:9898/api"
XDP_IFACE_IN  = "enp1s0f1np1"
XDP_IFACE_OUT = "enp1s0f0np0"
XDP_SRC_MAC   = "64:9d:99:ff:f5:9a"   # Node 6 egress NIC — same for every route

REAL_ROUTES: list[dict] = [
    {"ip": "192.168.56.5", "dst_mac": "64:9d:99:ff:e6:cf"},
    {"ip": "192.168.56.1", "dst_mac": "64:9d:99:ff:f5:7b"},
    {"ip": "192.168.46.4", "dst_mac": "64:9d:99:ff:e7:af"},
    {"ip": "192.168.46.1", "dst_mac": "64:9d:99:ff:f5:7a"},
]
_REAL_SUBNETS = frozenset(r["ip"].split(".")[2] for r in REAL_ROUTES)   # {'46', '56'}
_REAL_IPS     = frozenset(r["ip"] for r in REAL_ROUTES)

MULTIROUTE_PLAYBOOK = "04_setup_xdp_node6_multiroute.yaml"


def _dummy_mac(index: int) -> str:
    """Locally-administered unicast MAC derived from a counter."""
    return (
        f"02:00:00:"
        f"{(index >> 16) & 0xFF:02x}:"
        f"{(index >>  8) & 0xFF:02x}:"
        f"{index         & 0xFF:02x}"
    )


def _dummy_ips(need: int) -> list[str]:
    """Collect dummy IPs from 192.168.x.y, skipping real subnets and real IPs."""
    out: list[str] = []
    for third in range(1, 256):
        if str(third) in _REAL_SUBNETS:
            continue
        for fourth in range(1, 255):
            ip = f"192.168.{third}.{fourth}"
            if ip not in _REAL_IPS:
                out.append(ip)
                if len(out) == need:
                    return out
    raise ValueError(f"Cannot generate {need} dummy IPs from 192.168.0.0/16")


def build_xdp_routes(total: int) -> list[dict]:
    """Return exactly `total` route dicts, always starting with REAL_ROUTES."""
    if total < len(REAL_ROUTES):
        raise ValueError(
            f"--route-count must be >= {len(REAL_ROUTES)} "
            f"(the {len(REAL_ROUTES)} real routes are always included)"
        )
    routes = [
        {"ip": r["ip"], "dst_mac": r["dst_mac"],
         "src_mac": XDP_SRC_MAC, "action": "redirect"}
        for r in REAL_ROUTES
    ]
    extra = total - len(REAL_ROUTES)
    if extra:
        for idx, ip in enumerate(_dummy_ips(extra)):
            routes.append({
                "ip":      ip,
                "dst_mac": _dummy_mac(idx),
                "src_mac": XDP_SRC_MAC,
                "action":  "redirect",
            })
    return routes


# Tasks block is a plain string so Jinja2 {{ }} delimiters survive as-is.
_XDP_TASKS = """\
  tasks:
    - name: Stop XDP if already running
      ansible.builtin.uri:
        url: "{{ api_base }}/stop"
        method: POST
        status_code: [200, 409]
      ignore_errors: true

    - name: Set ingress and egress interfaces
      ansible.builtin.uri:
        url: "{{ api_base }}/system/settings"
        method: PUT
        body_format: json
        body:
          iface: "{{ iface_ingress }}"
          redirect_dev: "{{ iface_egress }}"
        status_code: 200

    - name: Start XDP program
      ansible.builtin.uri:
        url: "{{ api_base }}/start"
        method: POST
        status_code: 200

    - name: Register egress NIC in devmap (slot 0)
      ansible.builtin.uri:
        url: "{{ api_base }}/devmap"
        method: POST
        body_format: json
        body:
          slot: 0
          iface: "{{ iface_egress }}"
        status_code: 200

    - name: Add forwarding table entries
      ansible.builtin.uri:
        url: "{{ api_base }}/routes"
        method: POST
        body_format: json
        body:
          ip: "{{ item.ip }}"
          dst_mac: "{{ item.dst_mac }}"
          src_mac: "{{ item.src_mac }}"
          action: "{{ item.action }}"
        status_code: 201
      loop: "{{ fwd_entries }}"

    - name: Verify forwarding table
      ansible.builtin.uri:
        url: "{{ api_base }}/routes"
        method: GET
        status_code: 200
      register: routes_out

    - name: Print forwarding table
      ansible.builtin.debug:
        msg: "{{ routes_out.json }}"
"""


def render_xdp_multiroute_playbook(routes: list[dict]) -> str:
    """Return a complete Ansible playbook YAML string with `routes` embedded."""
    n_dummy = len(routes) - len(REAL_ROUTES)
    entries = "\n".join(
        f'      - ip: "{r["ip"]}"\n'
        f'        dst_mac: "{r["dst_mac"]}"\n'
        f'        src_mac: "{r["src_mac"]}"\n'
        f'        action: "{r["action"]}"'
        for r in routes
    )
    header = (
        "---\n"
        "# Auto-generated by experiment_runner.py — do not edit by hand.\n"
        f"# {len(routes)} forwarding entries: {len(REAL_ROUTES)} real + {n_dummy} dummy.\n"
        f"- name: Configure XDP forwarder on Node 6 via API ({len(routes)} route entries)\n"
        "  hosts: localhost\n"
        "  connection: local\n"
        "  gather_facts: false\n"
        "\n"
        "  vars:\n"
        f'    api_base: "{XDP_API_BASE}"\n'
        f'    iface_ingress: "{XDP_IFACE_IN}"\n'
        f'    iface_egress:  "{XDP_IFACE_OUT}"\n'
        "    fwd_entries:\n"
        f"{entries}\n"
        "\n"
    )
    return header + _XDP_TASKS

# ---------------------------------------------------------------------------

SETUP_PLAYBOOKS = [
    "01_basic_setup.yaml",
    "02_setup_route.yaml",
    "03_setup_scripts.yaml",
]

# ---------------------------------------------------------------------------
# Port range parser
# ---------------------------------------------------------------------------

def parse_ports(spec: str) -> list[tuple[int, int]]:
    """Parse port spec into a list of (start, end) ranges.

    Examples:
      "1024"             → [(1024, 1024)]
      "1024-1029"        → [(1024, 1029)]
      "1024-1029,2000-2010" → [(1024, 1029), (2000, 2010)]
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
# Ansible runner
# ---------------------------------------------------------------------------

def run_playbook(playbook: str, ansible_dir: Path, inventory: str,
                 label: str, dry_run: bool) -> bool:
    """Run a playbook synchronously. Returns True on success."""
    cmd = ["ansible-playbook", "-i", inventory, str(ansible_dir / playbook)]
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
    """Check result quality. Returns True if OK, False if node5 data is missing.

    For direction "15" or "15_41", Node 1 sends traffic to Node 5 so node5.csv
    should have more than just the header row. Header-only means the forwarder
    didn't deliver traffic to Node 5 (silent forwarding failure).
    """
    if direction not in ("15", "15_41"):
        return True  # "41" direction never sends to Node 5 — header-only is expected

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
    dry_run: bool,
    route_count: int | None = None,
) -> tuple[bool, bool]:
    """Run one experiment. Returns (ansible_ok, data_ok)."""
    print(f"\n  [1/5] Modifying pkt files ...", end=" ", flush=True)
    update_pkt_files(port_start, port_end, dry_run)
    if not dry_run:
        print("OK")

    print(f"  [2/5] Updating pktgen_config.json ...", end=" ", flush=True)
    update_pktgen_config(direction, dry_run)
    if not dry_run:
        print("OK")

    print(f"  [3/5] Running setup playbooks ...")
    for pb in SETUP_PLAYBOOKS:
        ok = run_playbook(pb, ansible_dir, inventory, pb, dry_run)
        if not ok:
            print(f"  ERROR: {pb} failed — skipping this experiment.")
            return False, False

    print(f"  [4/5] Running forwarder setup ({forwarder}) ...")
    if forwarder == "xdp" and route_count is not None:
        routes = build_xdp_routes(route_count)
        pb_path = ansible_dir / MULTIROUTE_PLAYBOOK
        if dry_run:
            print(f"    [dry-run] would generate {pb_path} with {route_count} routes "
                  f"({len(REAL_ROUTES)} real + {route_count - len(REAL_ROUTES)} dummy)")
        else:
            pb_path.write_text(render_xdp_multiroute_playbook(routes))
            print(f"    Generated {pb_path.name} ({route_count} entries)")
        forwarder_pb = MULTIROUTE_PLAYBOOK
    else:
        forwarder_pb = FORWARDER_PLAYBOOK[forwarder]
    ok = run_playbook(forwarder_pb, ansible_dir, inventory, forwarder_pb, dry_run)
    if not ok:
        print(f"  ERROR: forwarder setup failed — skipping this experiment.")
        return False, False

    print(f"  [5/5] Running pktgen experiment ...")

    port_label = f"{port_start}-{port_end}" if port_start != port_end else str(port_start)
    fw_label = (
        f"XDP_{route_count}r"
        if forwarder == "xdp" and route_count is not None
        else FORWARDER_LABEL[forwarder]
    )
    if dry_run:
        print(f"    [dry-run] would: launch 05_start_pktgen.yaml, wait {setup_wait}s,")
        print(f"              touch start signal, wait {duration}s, touch stop signal,")
        print(f"              wait for ansible, rename result dir to "
              f"{fw_label}_{port_label}_Port_No_Block_{direction}")
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

    # Wait for pktgen to initialize (playbook sleeps 5s internally)
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
            target_name = f"{fw_label}_{port_label}_Port_No_Block_{direction}"
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
        description="Automate pktgen experiments across port counts, forwarders, and traffic directions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--ports", required=True,
        help=(
            'Port range(s) for traffic (actual port numbers, not counts). '
            'Single: "1024". Range: "1024-1029". Multiple: "1024-1029,2000-2010".'
        ),
    )
    parser.add_argument(
        "--traffic", required=True,
        help='Traffic direction(s): "41", "15", "15_41", or comma-separated combination.',
    )
    parser.add_argument(
        "--duration", type=int, default=5,
        help="Seconds to run pktgen traffic per experiment (default: 5).",
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
        "--route-count", type=int, default=None, metavar="N",
        help=(
            "Total XDP forwarding-table entries (>= 4, only applies to 'xdp' forwarder). "
            "The 4 real routes are always first; remainder are dummy entries spread across "
            "192.168.0.0/16 (skipping .46.x and .56.x). "
            "Result dirs use XDP_<N>r_ prefix. Default: use the standard 2-entry playbook."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would happen without executing anything.",
    )
    args = parser.parse_args()

    if not args.dry_run and not args.inventory:
        parser.error("--inventory is required (or set ANSIBLE_INVENTORY env var)")

    if args.route_count is not None and args.route_count < len(REAL_ROUTES):
        parser.error(f"--route-count must be >= {len(REAL_ROUTES)} "
                     f"(the {len(REAL_ROUTES)} real routes are always included)")

    try:
        ports = parse_ports(args.ports)
    except ValueError as e:
        parser.error(str(e))

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
    print(f"  Port ranges: {port_labels}")
    print(f"  Forwarders:  {args.forwarder}")
    print(f"  Directions:  {directions}")
    print(f"  Duration:    {args.duration}s per run")
    print(f"  Setup wait:  {args.setup_wait}s")
    if args.dry_run:
        print("  [DRY RUN — no changes will be made]")

    failed = []
    data_warnings = []
    for idx, (pstart, pend, forwarder, direction) in enumerate(experiments, 1):
        port_label = f"{pstart}-{pend}" if pstart != pend else str(pstart)
        fw_display = (
            f"XDP({args.route_count}r)"
            if forwarder == "xdp" and args.route_count is not None
            else FORWARDER_LABEL[forwarder]
        )
        label = f"{fw_display} | Ports {port_label} | Direction: {direction}"
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
            dry_run=args.dry_run,
            route_count=args.route_count,
        )
        fw_label = (
            f"XDP_{args.route_count}r"
            if forwarder == "xdp" and args.route_count is not None
            else FORWARDER_LABEL[forwarder]
        )
        run_name = f"{fw_label}_{port_label}_Port_No_Block_{direction}"
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
