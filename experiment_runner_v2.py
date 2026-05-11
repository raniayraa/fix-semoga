#!/usr/bin/env python3
"""
YAML-driven experiment runner for pktgen port-range sweeps.

Reads all configuration from two YAML files:
  infra.yaml      — stable testbed hardware, paths, ansible wiring
  experiment.yaml — per-run sweep parameters (ports, forwarders, directions,
                    and per-forwarder variable overrides)

Usage:
  python experiment_runner_v2.py
  python experiment_runner_v2.py --config my_sweep.yaml
  python experiment_runner_v2.py --config my_sweep.yaml --dry-run
  python experiment_runner_v2.py --config my_sweep.yaml --inventory hosts-lab.ini
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: pip install pyyaml")


# ---------------------------------------------------------------------------
# Config loading and validation
# ---------------------------------------------------------------------------

def _require(d: dict, *keys: str, label: str = "") -> None:
    for k in keys:
        if k not in d:
            ctx = f" in {label}" if label else ""
            raise ValueError(f"Missing required key '{k}'{ctx}")


def load_infra(path: Path) -> dict:
    with path.open() as f:
        d = yaml.safe_load(f)
    if not isinstance(d, dict):
        raise ValueError(f"{path}: expected a YAML mapping at top level")
    _validate_infra(d, path)
    return d


def _validate_infra(d: dict, path: Path) -> None:
    label = str(path)
    _require(d, "paths", "ansible", "traffic_directions", "validation", label=label)

    p = d["paths"]
    _require(p, "pkt_files_dir", "pktgen_config", "results_dir",
             "signal_start", "signal_stop", "pkt_nodes", label=f"{label}[paths]")
    if not isinstance(p["pkt_nodes"], list) or not p["pkt_nodes"]:
        raise ValueError(f"{label}[paths.pkt_nodes]: must be a non-empty list")

    a = d["ansible"]
    _require(a, "dir", "setup_playbooks", "pktgen_playbook",
             "forwarder_playbooks", "forwarder_labels",
             label=f"{label}[ansible]")

    td = d["traffic_directions"]
    if not isinstance(td, dict) or not td:
        raise ValueError(f"{label}[traffic_directions]: must be a non-empty mapping")
    for name, cfg in td.items():
        if "nodes" not in cfg:
            raise ValueError(f"{label}[traffic_directions.{name}]: missing 'nodes'")

    v = d["validation"]
    _require(v, "node5_csv_header_size", "directions_requiring_node5",
             label=f"{label}[validation]")


def load_experiment(path: Path) -> tuple[dict, dict]:
    """Load experiment YAML and its referenced infra YAML.
    Returns (infra_dict, experiment_dict).
    """
    with path.open() as f:
        d = yaml.safe_load(f)
    if not isinstance(d, dict):
        raise ValueError(f"{path}: expected a YAML mapping at top level")
    _require(d, "infra", "experiment", label=str(path))

    infra_path = Path(d["infra"])
    if not infra_path.is_absolute():
        infra_path = path.parent / infra_path

    infra = load_infra(infra_path)
    exp = d["experiment"]
    exp["forwarders_config"] = d.get("forwarders_config", {})
    _validate_experiment(exp, infra, path)
    return infra, exp


def _validate_experiment(exp: dict, infra: dict, path: Path) -> None:
    label = f"{path}[experiment]"
    _require(exp, "ports", "forwarders", "directions", label=label)

    if not isinstance(exp["ports"], list) or not exp["ports"]:
        raise ValueError(f"{label}.ports: must be a non-empty list")
    if not isinstance(exp["forwarders"], list) or not exp["forwarders"]:
        raise ValueError(f"{label}.forwarders: must be a non-empty list")

    valid_fwds = set(infra["ansible"]["forwarder_playbooks"].keys())
    for fw in exp["forwarders"]:
        if fw not in valid_fwds:
            raise ValueError(
                f"{label}.forwarders: '{fw}' is not defined in infra forwarder_playbooks "
                f"(valid: {sorted(valid_fwds)})"
            )

    valid_dirs = set(infra["traffic_directions"].keys())
    for direction in exp["directions"]:
        if str(direction) not in valid_dirs:
            raise ValueError(
                f"{label}.directions: '{direction}' is not defined in infra traffic_directions "
                f"(valid: {sorted(valid_dirs)})"
            )


# ---------------------------------------------------------------------------
# Port range parser
# ---------------------------------------------------------------------------

def parse_ports(spec_list: list) -> list[tuple[int, int]]:
    """Parse a YAML list of port specs into (start, end) tuples.

    Accepts integers and strings:
      1024          → (1024, 1024)
      "1024-1029"   → (1024, 1029)
    """
    result = []
    seen: set[tuple[int, int]] = set()
    for item in spec_list:
        spec = str(item).strip()
        if "-" in spec:
            lo_s, hi_s = spec.split("-", 1)
            lo, hi = int(lo_s.strip()), int(hi_s.strip())
            if lo > hi:
                raise ValueError(f"Invalid port range '{spec}': start > end")
        else:
            lo = hi = int(spec)
        key = (lo, hi)
        if key not in seen:
            seen.add(key)
            result.append(key)
    if not result:
        raise ValueError("ports list is empty — add at least one port or range")
    return result


# ---------------------------------------------------------------------------
# Pkt file modification
# ---------------------------------------------------------------------------

_PORT_LINE_RE = re.compile(
    r"^(range\s+0\s+(?:src|dst)\s+port\s+(?:start|min|max))\s+(\d+)\s*$"
)


def _set_port_range(content: str, port_start: int, port_end: int) -> str:
    value_map = {"start": port_start, "min": port_start, "max": port_end}
    lines = content.splitlines(keepends=True)
    out = []
    for line in lines:
        m = _PORT_LINE_RE.match(line)
        if m:
            prefix = m.group(1)
            keyword = prefix.split()[-1]
            value = value_map[keyword]
            gap = line[len(m.group(1)):line.index(m.group(2), len(m.group(1)))]
            out.append(f"{prefix}{gap}{value}\n")
        else:
            out.append(line)
    return "".join(out)


def _update_pkt_files(infra: dict, port_start: int, port_end: int, dry_run: bool) -> None:
    pkt_dir = Path(infra["paths"]["pkt_files_dir"])
    for fname in infra["paths"]["pkt_nodes"]:
        path = pkt_dir / fname
        if dry_run:
            print(f"    [dry-run] would write port range {port_start}-{port_end} to {path}")
            continue
        path.write_text(_set_port_range(path.read_text(), port_start, port_end))


def _update_pktgen_config(infra: dict, direction: str, dry_run: bool) -> None:
    config = infra["traffic_directions"][direction]["nodes"]
    pktgen_config_path = Path(infra["paths"]["pktgen_config"])
    if dry_run:
        print(f"    [dry-run] would write pktgen_config.json: {json.dumps(config)}")
        return
    pktgen_config_path.write_text(json.dumps(config, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Ansible runner
# ---------------------------------------------------------------------------

def _run_playbook(
    playbook: str,
    ansible_dir: Path,
    inventory: str,
    dry_run: bool,
    extra_vars: dict | None = None,
) -> bool:
    cmd = ["ansible-playbook", "-i", inventory, str(ansible_dir / playbook)]
    if extra_vars:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, prefix="ansible_evars_"
        )
        json.dump(extra_vars, tmp)
        tmp.close()
        cmd += ["--extra-vars", f"@{tmp.name}"]
    else:
        tmp = None

    if dry_run:
        ev_note = f" --extra-vars @<{len(extra_vars)} keys>" if extra_vars else ""
        print(f"    [dry-run] would run: {' '.join(cmd[:4])}{ev_note}")
        if tmp:
            os.unlink(tmp.name)
        return True

    print(f"    Running {playbook} ...", end=" ", flush=True)
    result = subprocess.run(cmd, capture_output=False)
    if tmp:
        os.unlink(tmp.name)
    if result.returncode != 0:
        print(f"FAILED (exit {result.returncode})")
        return False
    print("OK")
    return True


# ---------------------------------------------------------------------------
# XDP extra-vars builder
# ---------------------------------------------------------------------------

def _build_xdp_extra_vars(xdp_config: dict) -> dict:
    """Expand fwd_entries by adding action: redirect to each entry."""
    entries = [
        {
            "ip":      r["ip"],
            "dst_mac": r["dst_mac"],
            "src_mac": r["src_mac"],
            "action":  "redirect",
        }
        for r in xdp_config["fwd_entries"]
    ]
    return {
        "api_base":      xdp_config["api_base"],
        "iface_ingress": xdp_config["iface_ingress"],
        "iface_egress":  xdp_config["iface_egress"],
        "fwd_entries":   entries,
    }


# ---------------------------------------------------------------------------
# Result directory helpers
# ---------------------------------------------------------------------------

def _unique_name(base: Path) -> Path:
    if not base.exists():
        return base
    v = 2
    while True:
        candidate = base.parent / f"{base.name}_v{v}"
        if not candidate.exists():
            return candidate
        v += 1


def _validate_results(infra: dict, result_dir: Path, direction: str) -> bool:
    val = infra["validation"]
    if direction not in [str(d) for d in val["directions_requiring_node5"]]:
        return True

    node5_csv = result_dir / "node5.csv"
    if not node5_csv.exists():
        print(f"  WARNING [node5]: node5.csv not found in {result_dir.name}")
        return False

    size = node5_csv.stat().st_size
    threshold = val["node5_csv_header_size"]
    if size <= threshold:
        print(
            f"  WARNING [node5]: node5.csv is header-only ({size} bytes) in "
            f"{result_dir.name} — forwarder may not be reaching Node 5."
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Single experiment
# ---------------------------------------------------------------------------

def run_experiment(
    infra: dict,
    port_start: int,
    port_end: int,
    forwarder: str,
    direction: str,
    ansible_dir: Path,
    inventory: str,
    duration: int,
    setup_wait: int,
    dry_run: bool,
    forwarders_config: dict,
) -> tuple[bool, bool]:
    """Run one experiment. Returns (ansible_ok, data_ok)."""
    print("  [1/5] Modifying pkt files ...", end=" ", flush=True)
    _update_pkt_files(infra, port_start, port_end, dry_run)
    if not dry_run:
        print("OK")

    print("  [2/5] Updating pktgen_config.json ...", end=" ", flush=True)
    _update_pktgen_config(infra, direction, dry_run)
    if not dry_run:
        print("OK")

    print("  [3/5] Running setup playbooks ...")
    for pb in infra["ansible"]["setup_playbooks"]:
        if not _run_playbook(pb, ansible_dir, inventory, dry_run):
            print(f"  ERROR: {pb} failed — skipping this experiment.")
            return False, False

    print(f"  [4/5] Running forwarder setup ({forwarder}) ...")
    fw_config = forwarders_config.get(forwarder)
    extra_vars: dict | None = None
    if fw_config:
        if forwarder == "xdp":
            extra_vars = _build_xdp_extra_vars(fw_config)
        else:
            extra_vars = dict(fw_config)

    forwarder_pb = infra["ansible"]["forwarder_playbooks"][forwarder]
    if not _run_playbook(forwarder_pb, ansible_dir, inventory, dry_run, extra_vars):
        print("  ERROR: forwarder setup failed — skipping this experiment.")
        return False, False

    print("  [5/5] Running pktgen experiment ...")

    port_label = f"{port_start}-{port_end}" if port_start != port_end else str(port_start)
    fw_label = infra["ansible"]["forwarder_labels"][forwarder]

    if dry_run:
        pktgen_pb = infra["ansible"]["pktgen_playbook"]
        print(
            f"    [dry-run] would: launch {pktgen_pb}, wait {setup_wait}s,\n"
            f"              touch start signal, wait {duration}s, touch stop signal,\n"
            f"              wait for ansible, rename result dir to "
            f"{fw_label}_{port_label}_Port_No_Block_{direction}\n"
            f"              validate node5.csv for direction '{direction}'"
        )
        return True, True

    results_dir = Path(infra["paths"]["results_dir"])
    signal_start = Path(infra["paths"]["signal_start"])
    signal_stop  = Path(infra["paths"]["signal_stop"])

    existing = set(results_dir.iterdir()) if results_dir.exists() else set()
    signal_start.unlink(missing_ok=True)
    signal_stop.unlink(missing_ok=True)

    cmd = [
        "ansible-playbook", "-i", inventory,
        str(ansible_dir / infra["ansible"]["pktgen_playbook"]),
    ]
    proc = subprocess.Popen(cmd)

    print(f"        Waiting {setup_wait}s for pktgen to initialize ...", end=" ", flush=True)
    time.sleep(setup_wait)
    signal_start.touch()
    print("STARTED")

    print(f"        Running for {duration}s ...", end=" ", flush=True)
    time.sleep(duration)
    signal_stop.touch()
    print("STOPPED")

    print("        Waiting for ansible to collect results ...", end=" ", flush=True)
    proc.wait()
    print(f"done (exit {proc.returncode})")

    if proc.returncode != 0:
        print("  WARNING: pktgen playbook exited non-zero — results may be incomplete.")

    result_dir = None
    if results_dir.exists():
        new_dirs = set(results_dir.iterdir()) - existing
        if new_dirs:
            newest = max(new_dirs, key=lambda d: d.stat().st_mtime)
            target_name = f"{fw_label}_{port_label}_Port_No_Block_{direction}"
            target = _unique_name(results_dir / target_name)
            newest.rename(target)
            result_dir = target
            print(f"        Results saved as: {target.name}")
        else:
            print("  WARNING: No new result directory found to rename.")

    data_ok = _validate_results(infra, result_dir, direction) if result_dir else False
    return proc.returncode == 0, data_ok


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="YAML-driven pktgen experiment runner.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--config", default="experiment.yaml", metavar="FILE",
        help="Path to experiment YAML file (default: experiment.yaml).",
    )
    parser.add_argument(
        "--inventory", default=None, metavar="FILE",
        help="Override the ansible inventory from infra.yaml or ANSIBLE_INVENTORY env var.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would happen without executing anything.",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        sys.exit(f"Config file not found: {config_path}")

    try:
        infra, exp = load_experiment(config_path)
    except (ValueError, KeyError, FileNotFoundError) as e:
        sys.exit(f"Config error: {e}")

    dry_run    = args.dry_run or bool(exp.get("dry_run", False))
    duration   = int(exp.get("duration", 15))
    setup_wait = int(exp.get("setup_wait", 10))
    forwarders_config = exp.get("forwarders_config", {})

    inventory = (
        args.inventory
        or os.environ.get("ANSIBLE_INVENTORY", "")
        or infra["ansible"].get("inventory", "")
    )
    if not dry_run and not inventory:
        sys.exit(
            "Ansible inventory not configured. Provide it via one of:\n"
            "  1. --inventory flag\n"
            "  2. ANSIBLE_INVENTORY environment variable\n"
            "  3. ansible.inventory key in infra.yaml"
        )

    ansible_dir = Path(infra["ansible"]["dir"])
    if not dry_run and not ansible_dir.is_dir():
        sys.exit(f"Ansible directory not found: {ansible_dir}")

    try:
        ports = parse_ports(exp["ports"])
    except ValueError as e:
        sys.exit(f"Config error in ports: {e}")

    forwarders = exp["forwarders"]
    directions = [str(d) for d in exp["directions"]]

    experiments = [
        (pstart, pend, fw, direction)
        for pstart, pend in ports
        for fw in forwarders
        for direction in directions
    ]
    total = len(experiments)

    port_labels = [f"{s}-{e}" if s != e else str(s) for s, e in ports]
    print(
        f"\nExperiment sweep: {len(ports)} range(s) × {len(forwarders)} forwarder(s) "
        f"× {len(directions)} direction(s) = {total} runs"
    )
    print(f"  Port ranges: {port_labels}")
    print(f"  Forwarders:  {forwarders}")
    print(f"  Directions:  {directions}")
    print(f"  Duration:    {duration}s per run")
    print(f"  Setup wait:  {setup_wait}s")
    if dry_run:
        print("  [DRY RUN — no changes will be made]")

    failed: list[str] = []
    data_warnings: list[str] = []

    for idx, (pstart, pend, forwarder, direction) in enumerate(experiments, 1):
        port_label = f"{pstart}-{pend}" if pstart != pend else str(pstart)
        fw_display = infra["ansible"]["forwarder_labels"][forwarder]
        print(f"\n{'═' * 60}")
        print(f"[{idx}/{total}] {fw_display} | Ports {port_label} | Direction: {direction}")
        print(f"{'═' * 60}")

        ansible_ok, data_ok = run_experiment(
            infra=infra,
            port_start=pstart,
            port_end=pend,
            forwarder=forwarder,
            direction=direction,
            ansible_dir=ansible_dir,
            inventory=inventory,
            duration=duration,
            setup_wait=setup_wait,
            dry_run=dry_run,
            forwarders_config=forwarders_config,
        )

        fw_label = infra["ansible"]["forwarder_labels"][forwarder]
        run_name = f"{fw_label}_{port_label}_Port_No_Block_{direction}"
        if not ansible_ok:
            failed.append(run_name)
        if not data_ok and not dry_run:
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
