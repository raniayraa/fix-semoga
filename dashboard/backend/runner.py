import asyncio
import os
import pty
import signal
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from ws_manager import manager

ANSIBLE_DIR = Path("/home/telmat/final_t40/ansible")
INVENTORY = str(ANSIBLE_DIR / "inventory.ini")

PLAYBOOKS = [
    {"id": "00", "filename": "00_check_node_connection.yaml",  "description": "Check SSH connectivity to all nodes"},
    {"id": "01", "filename": "01_basic_setup.yaml",            "description": "Configure network interfaces and IP addresses"},
    {"id": "02", "filename": "02_setup_route.yaml",            "description": "Set up static routing and validate connectivity"},
    {"id": "03", "filename": "03_setup_scripts.yaml",          "description": "Deploy pktgen scripts and bind NICs to DPDK"},
    {"id": "04", "filename": "04_setup_kernel_node6.yaml",     "description": "Set up Node 6 forwarder (XDP / VPP / Kernel)"},
    {"id": "05", "filename": "05_start_pktgen.yaml",           "description": "Launch pktgen and control traffic generation"},
]

VARIANTS = {
    "04": {
        "kernel": "04_setup_kernel_node6.yaml",
        "xdp":    "04_setup_xdp_node6.yaml",
        "vpp":    "04_setup_vpp_node6.yaml",
    }
}

SIGNAL_START_MARKER = "DASHBOARD_SIGNAL: waiting_for_start"
SIGNAL_STOP_MARKER  = "DASHBOARD_SIGNAL: waiting_for_stop"

SIGNAL_FILE_START = Path("/tmp/ansible_pktgen_start")
SIGNAL_FILE_STOP  = Path("/tmp/ansible_pktgen_stop")


@dataclass
class Job:
    job_id: str
    playbook_id: str
    status: str = "running"
    pause_state: Optional[str] = None
    exit_code: Optional[int] = None
    master_fd: int = -1
    pid: int = -1
    forward_to: Optional[str] = None
    active_child: Optional['Job'] = None
    _done_event: asyncio.Event = field(default_factory=asyncio.Event)


_registry: Dict[str, Job] = {}
_lock = asyncio.Lock()


def get_job(job_id: str) -> Optional[Job]:
    return _registry.get(job_id)


def get_playbook_path(playbook_id: str) -> Optional[str]:
    for pb in PLAYBOOKS:
        if pb["id"] == playbook_id:
            return str(ANSIBLE_DIR / pb["filename"])
    return None


async def launch_playbook(playbook_id: str, variant: str | None = None) -> Job:
    if variant and playbook_id in VARIANTS:
        filename = VARIANTS[playbook_id].get(variant)
        if filename is None:
            raise ValueError(f"Unknown variant '{variant}' for playbook {playbook_id}")
        path = str(ANSIBLE_DIR / filename)
    else:
        path = get_playbook_path(playbook_id)
    if path is None:
        raise ValueError(f"Unknown playbook id: {playbook_id}")

    job_id = str(uuid.uuid4())
    job = Job(job_id=job_id, playbook_id=playbook_id)

    master_fd, slave_fd = pty.openpty()
    job.master_fd = master_fd

    import subprocess
    proc = subprocess.Popen(
        ["ansible-playbook", "-i", INVENTORY, path],
        stdout=slave_fd,
        stderr=slave_fd,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        env={**os.environ, "ANSIBLE_FORCE_COLOR": "1"},
    )
    os.close(slave_fd)
    job.pid = proc.pid

    async with _lock:
        _registry[job_id] = job

    asyncio.create_task(_read_loop(job, proc))
    return job


async def _read_loop(job: Job, proc):
    loop = asyncio.get_event_loop()
    buf = b""
    while True:
        try:
            chunk = await loop.run_in_executor(None, _safe_read, job.master_fd)
        except OSError:
            break
        if not chunk:
            break

        buf += chunk
        while b"\n" in buf or b"\r" in buf:
            # split on \r\n, \n, \r
            for sep in (b"\r\n", b"\n", b"\r"):
                if sep in buf:
                    line_bytes, buf = buf.split(sep, 1)
                    line = line_bytes.decode("utf-8", errors="replace").rstrip()
                    await _process_line(job, line)
                    break
            else:
                break

    # flush remainder
    if buf:
        line = buf.decode("utf-8", errors="replace").rstrip()
        if line:
            await _process_line(job, line)

    proc.wait()
    exit_code = proc.returncode
    try:
        os.close(job.master_fd)
    except OSError:
        pass

    job.exit_code = exit_code
    job.status = "done" if exit_code == 0 else "error"
    job.pause_state = None
    await manager.broadcast(job.job_id, {
        "type": "done",
        "exit_code": exit_code,
        "status": job.status,
    })
    job._done_event.set()


def _safe_read(fd: int) -> bytes:
    try:
        return os.read(fd, 4096)
    except OSError:
        return b""


async def _process_line(job: Job, line: str):
    msg = {"type": "log", "line": line}
    await manager.broadcast(job.job_id, msg)
    if job.forward_to:
        await manager.broadcast(job.forward_to, msg)

    old_pause = job.pause_state
    if SIGNAL_START_MARKER in line:
        job.pause_state = "paused_start"
    elif SIGNAL_STOP_MARKER in line:
        job.pause_state = "paused_stop"

    if job.pause_state != old_pause:
        state_msg = {"type": "state", "status": job.status, "pause_state": job.pause_state}
        await manager.broadcast(job.job_id, state_msg)
        if job.forward_to:
            await manager.broadcast(job.forward_to, state_msg)


async def inject_enter(job_id: str) -> bool:
    """Create the appropriate signal file based on current pause_state."""
    job = get_job(job_id)
    if job is None or job.status != "running":
        return False
    # Sequence job: delegate to the currently active child
    if job.active_child is not None:
        return await inject_enter(job.active_child.job_id)
    if job.pause_state == "paused_start":
        SIGNAL_FILE_START.touch()
        return True
    elif job.pause_state == "paused_stop":
        SIGNAL_FILE_STOP.touch()
        return True
    return False


async def abort_job(job_id: str) -> bool:
    job = get_job(job_id)
    if job is None or job.status != "running":
        return False
    try:
        os.kill(job.pid, signal.SIGTERM)
        job.status = "aborted"
        return True
    except ProcessLookupError:
        return False


async def run_all(variant: str | None = None) -> str:
    """Run playbooks 00-05 sequentially. Returns a synthetic job_id for the sequence."""
    seq_id = str(uuid.uuid4())
    seq_job = Job(job_id=seq_id, playbook_id="__all__")
    async with _lock:
        _registry[seq_id] = seq_job

    async def _sequence():
        for pb in PLAYBOOKS:
            pb_variant = variant if pb["id"] == "04" else None
            job = await launch_playbook(pb["id"], variant=pb_variant)
            job.forward_to = seq_id
            seq_job.active_child = job
            await job._done_event.wait()
            seq_job.active_child = None
            if job.exit_code != 0:
                seq_job.status = "error"
                await manager.broadcast(seq_id, {
                    "type": "done",
                    "exit_code": job.exit_code,
                    "status": "error",
                })
                return
        seq_job.status = "done"
        await manager.broadcast(seq_id, {"type": "done", "exit_code": 0, "status": "done"})

    asyncio.create_task(_sequence())
    return seq_id
