import asyncio
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import cpu_metrics as cpu_metrics_module
import metrics as metrics_module
import node_registry as node_reg
import pkt_editor
import pktgen_config as cfg_module
import runner
from models import (
    DescriptionRequest,
    ExperimentSummary,
    JobStatus,
    LatencyMetrics,
    MetricsSummary,
    NodeEntry,
    NodeRegistryResponse,
    NodeUpdateRequest,
    PlaybookInfo,
    PktFileContent,
    PktFileInfo,
    PktgenConfig,
    RenameRequest,
    RunOptions,
    SignalRequest,
)
from ws_manager import manager

FRONTEND_DIST = Path(__file__).parent.parent / "frontend" / "dist"
RESULTS_DIR = Path(__file__).parent.parent.parent / "results"


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="Ansible Dashboard", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Playbooks ───────────────────────────────────────────────────────────────

@app.get("/api/playbooks", response_model=list[PlaybookInfo])
def list_playbooks():
    return [PlaybookInfo(id=pb["id"], filename=pb["filename"], description=pb["description"])
            for pb in runner.PLAYBOOKS]


@app.post("/api/playbooks/{playbook_id}/run")
async def run_playbook(playbook_id: str, opts: RunOptions = Body(default=RunOptions())):
    try:
        job = await runner.launch_playbook(playbook_id, variant=opts.variant)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"job_id": job.job_id}


@app.post("/api/jobs/run-all")
async def run_all(opts: RunOptions = Body(default=RunOptions())):
    seq_id = await runner.run_all(variant=opts.variant)
    return {"job_id": seq_id}


# ─── Jobs ────────────────────────────────────────────────────────────────────

@app.get("/api/jobs/{job_id}", response_model=JobStatus)
def get_job(job_id: str):
    job = runner.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobStatus(
        job_id=job.job_id,
        playbook_id=job.playbook_id,
        status=job.status,
        pause_state=job.pause_state,
        exit_code=job.exit_code,
    )


@app.post("/api/jobs/{job_id}/signal")
async def send_signal(job_id: str, req: SignalRequest):
    job = runner.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    if req.signal == "abort":
        ok = await runner.abort_job(job_id)
    else:
        ok = await runner.inject_enter(job_id)

    if not ok:
        raise HTTPException(status_code=409, detail="Cannot send signal in current state")
    return {"ok": True}


# ─── Node Registry ───────────────────────────────────────────────────────────

def _registry_to_response(data: dict) -> NodeRegistryResponse:
    nodes = [NodeEntry(ip=ip, **entry) for ip, entry in data.items()]
    return NodeRegistryResponse(nodes=nodes)


@app.get("/api/node-registry", response_model=NodeRegistryResponse)
def get_node_registry():
    return _registry_to_response(node_reg.read_registry())


@app.patch("/api/node-registry/{ip}", response_model=NodeRegistryResponse)
def update_node(ip: str, body: NodeUpdateRequest):
    data = node_reg.read_registry()
    if ip not in data:
        raise HTTPException(status_code=404, detail=f"Unknown node: {ip}")
    if body.enabled is not None:
        data[ip]["enabled"] = body.enabled
    if body.pkt_file is not None:
        data[ip]["pkt_file"] = body.pkt_file
    node_reg.write_registry(data)
    return _registry_to_response(data)


# ─── PKT files ───────────────────────────────────────────────────────────────

@app.get("/api/pkt-files", response_model=list[PktFileInfo])
def list_pkt_files():
    return pkt_editor.list_pkt_files()


@app.get("/api/pkt-files/{name}")
def get_pkt_file(name: str):
    try:
        content = pkt_editor.read_pkt_file(name)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"name": name, "content": content}


@app.put("/api/pkt-files/{name}")
def update_pkt_file(name: str, body: PktFileContent):
    try:
        pkt_editor.write_pkt_file(name, body.content)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


# ─── Pktgen config ───────────────────────────────────────────────────────────

@app.get("/api/pktgen-config", response_model=PktgenConfig)
def get_pktgen_config():
    data = cfg_module.read_config()
    return PktgenConfig(nodes=data)


@app.put("/api/pktgen-config")
def update_pktgen_config(body: PktgenConfig):
    cfg_module.write_config(body.nodes)
    return {"ok": True}


# ─── Results ─────────────────────────────────────────────────────────────────

@app.get("/api/results", response_model=list[ExperimentSummary])
def list_results():
    if not RESULTS_DIR.exists():
        return []
    dirs = sorted(
        [d for d in RESULTS_DIR.iterdir()
         if d.is_dir() and (d.name.startswith("pktgen_stats_") or (d / "node1.csv").exists())],
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    result = []
    for d in dirs:
        display_name = None
        description = None
        meta_path = d / "meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                display_name = meta.get("display_name")
                description = meta.get("description")
            except Exception:
                pass
        result.append(ExperimentSummary(
            name=d.name,
            mtime=d.stat().st_mtime,
            files=[f.name for f in sorted(d.iterdir())],
            display_name=display_name,
            description=description,
        ))
    return result


@app.get("/api/results/{exp_name}/metrics", response_model=MetricsSummary)
def get_metrics(exp_name: str):
    if ".." in exp_name or "/" in exp_name:
        raise HTTPException(status_code=400, detail="Invalid experiment name")
    exp_dir = RESULTS_DIR / exp_name
    if not exp_dir.exists() or not exp_dir.is_dir():
        raise HTTPException(status_code=404, detail="Experiment not found")
    try:
        data = metrics_module.get_or_compute_metrics(exp_dir)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=f"CSV missing: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Metrics computation failed: {e}")
    return MetricsSummary(**data)


@app.put("/api/results/{exp_name}/description")
def update_description(exp_name: str, body: DescriptionRequest):
    if ".." in exp_name or "/" in exp_name:
        raise HTTPException(status_code=400, detail="Invalid experiment name")
    exp_dir = RESULTS_DIR / exp_name
    if not exp_dir.exists() or not exp_dir.is_dir():
        raise HTTPException(status_code=404, detail="Experiment not found")
    meta_path = exp_dir / "meta.json"
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            pass
    meta["description"] = body.description
    meta_path.write_text(json.dumps(meta, indent=2))
    return {"ok": True}


@app.get("/api/results/{exp_name}/cpu/{node}")
def get_cpu_timeseries(exp_name: str, node: str):
    if ".." in exp_name or "/" in exp_name:
        raise HTTPException(status_code=400, detail="Invalid experiment name")
    if node not in ("node1", "node4", "node5", "node6"):
        raise HTTPException(status_code=400, detail="Invalid node")
    exp_dir = RESULTS_DIR / exp_name
    if not exp_dir.exists() or not exp_dir.is_dir():
        raise HTTPException(status_code=404, detail="Experiment not found")
    try:
        csv_path = cpu_metrics_module.get_or_parse_cpu_csv(exp_dir, node)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"CPU CSV parse failed: {e}")
    if csv_path is None:
        raise HTTPException(status_code=404, detail="CPU data not available for this node")
    return FileResponse(csv_path, media_type="text/csv")


@app.get("/api/results/{exp_name}/latency", response_model=LatencyMetrics)
def get_latency(exp_name: str):
    if ".." in exp_name or "/" in exp_name:
        raise HTTPException(status_code=400, detail="Invalid experiment name")
    exp_dir = RESULTS_DIR / exp_name
    if not exp_dir.exists() or not exp_dir.is_dir():
        raise HTTPException(status_code=404, detail="Experiment not found")
    latency_path = exp_dir / "node5_latency.log"
    if not latency_path.exists():
        raise HTTPException(status_code=404, detail="Latency data not available")
    try:
        rows = latency_path.read_text().splitlines()
        # skip header; CSV format: port,min_us,avg_us,max_us,num_pkts
        for line in rows[1:]:
            parts = line.strip().split(",")
            if len(parts) == 5 and parts[0] == "0":
                return LatencyMetrics(
                    min_ns=float(parts[1]) * 1000,
                    avg_ns=float(parts[2]) * 1000,
                    max_ns=float(parts[3]) * 1000,
                    jitter_ns=0,
                )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Latency parse failed: {e}")
    raise HTTPException(status_code=404, detail="Latency data not available")


def _slugify(name: str) -> str:
    slug = re.sub(r"[^\w\-.]", "_", name.strip())
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug[:100] or "experiment"


@app.put("/api/results/{exp_name}/rename")
def rename_experiment(exp_name: str, body: RenameRequest):
    if ".." in exp_name or "/" in exp_name:
        raise HTTPException(status_code=400, detail="Invalid experiment name")
    exp_dir = RESULTS_DIR / exp_name
    if not exp_dir.exists() or not exp_dir.is_dir():
        raise HTTPException(status_code=404, detail="Experiment not found")
    display_name = body.display_name.strip()
    if not display_name:
        raise HTTPException(status_code=422, detail="display_name must not be empty")

    new_dir_name = _slugify(display_name)
    new_dir = RESULTS_DIR / new_dir_name
    if new_dir != exp_dir and new_dir.exists():
        raise HTTPException(status_code=409, detail=f"A folder named '{new_dir_name}' already exists")

    # Preserve existing description before renaming
    meta: dict = {}
    meta_path = exp_dir / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            pass
    meta["display_name"] = display_name

    if new_dir != exp_dir:
        exp_dir.rename(new_dir)

    (new_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    return {"ok": True, "display_name": display_name, "new_name": new_dir_name}


@app.get("/api/results/{exp_name}/{node_file}")
def get_result_file(exp_name: str, node_file: str):
    if ".." in exp_name or ".." in node_file or "/" in exp_name or "/" in node_file:
        raise HTTPException(status_code=400, detail="Invalid path")
    path = RESULTS_DIR / exp_name / node_file
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if path.suffix == ".pkt":
        return {"filename": node_file, "content": path.read_text()}
    rows = []
    lines = path.read_text().splitlines()
    for line in lines[1:]:
        parts = line.split(",", 3)
        if len(parts) == 4:
            rows.append({"time": parts[0], "port": parts[1], "metric": parts[2], "value": parts[3]})
    return {"filename": node_file, "rows": rows}


# ─── WebSocket ───────────────────────────────────────────────────────────────

@app.websocket("/ws/jobs/{job_id}")
async def ws_job(job_id: str, websocket: WebSocket):
    await websocket.accept()
    await manager.subscribe(job_id, websocket)
    try:
        # Send current state immediately on connect
        job = runner.get_job(job_id)
        if job:
            await websocket.send_json({
                "type": "state",
                "status": job.status,
                "pause_state": job.pause_state,
            })
        while True:
            # Keep connection alive; all messages come via manager.broadcast
            await asyncio.sleep(30)
            await websocket.send_json({"type": "ping"})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        await manager.unsubscribe(job_id, websocket)


# ─── Serve frontend in production ────────────────────────────────────────────

if FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="static")
