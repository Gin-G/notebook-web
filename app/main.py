"""FastAPI application — gallery UI, notebook API, session management, WebSocket proxy."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel

from .config import AppConfig, NotebookEntry, load_config
from .kernel_proxy import proxy_kernel_websocket
from .notebook_fetcher import get_notebook_json, render_preview, sync_all
from .session_manager import SessionManager

log = logging.getLogger(__name__)
BASE_DIR = os.path.dirname(__file__)

# ── Globals ───────────────────────────────────────────────────────────────────

config: AppConfig
session_mgr: SessionManager


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global config, session_mgr
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    )
    config = load_config()
    session_mgr = SessionManager(config)

    log.info("Starting notebook gallery — %d notebook(s) configured", len(config.notebooks))
    config.notebooks = await sync_all(config.cacheDir)

    # Pre-build session images for all notebooks (runs in background)
    if config.build.enabled and config.build.registry:
        from .build_manager import BuildManager
        from kubernetes import client as k8s_client, config as k8s_cfg
        try:
            k8s_cfg.load_incluster_config()
        except Exception:
            k8s_cfg.load_kube_config()
        build_mgr = BuildManager(config, k8s_client.CoreV1Api())
        session_mgr.build_mgr = build_mgr
        # Load the cache now so sessions created immediately after startup
        # use pre-built images rather than falling back to the base image.
        await build_mgr._load_cache()
        asyncio.create_task(build_mgr.build_all(config.notebooks))

    asyncio.create_task(session_mgr.reaper_task())
    asyncio.create_task(_periodic_sync())

    yield

    for s in session_mgr.list_sessions():
        # Leave pending/starting pods alive — they belong to users and will
        # be picked up by the new app instance via session resume.
        if s.status in ("running", "error"):
            try:
                await session_mgr.delete_session(s.session_id)
            except Exception:
                pass


async def _periodic_sync() -> None:
    while True:
        await asyncio.sleep(3600)
        try:
            config.notebooks = await sync_all(config.cacheDir)
            if session_mgr.build_mgr:
                asyncio.create_task(session_mgr.build_mgr.build_all(config.notebooks))
        except Exception as e:
            log.error("Periodic sync error: %s", e)


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Notebook Gallery", lifespan=lifespan)
app.mount(
    "/static",
    StaticFiles(directory=os.path.join(BASE_DIR, "static")),
    name="static",
)
def _build_jinja_env() -> Environment:
    loaders: list[FileSystemLoader] = []
    override_dir = os.environ.get("TEMPLATE_OVERRIDE_DIR")
    if override_dir and os.path.isdir(override_dir):
        present = sorted(os.listdir(override_dir))
        if present:
            log.info("Loading template overrides from %s: %s", override_dir, present)
            loaders.append(FileSystemLoader(override_dir))
    loaders.append(FileSystemLoader(os.path.join(BASE_DIR, "templates")))
    return Environment(loader=ChoiceLoader(loaders), autoescape=select_autoescape(["html", "xml"]))


templates = Jinja2Templates(env=_build_jinja_env())


def _find(notebook_id: str) -> Optional[NotebookEntry]:
    return next((nb for nb in config.notebooks if nb.id == notebook_id), None)


# ── Gallery pages ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def gallery(request: Request):
    notebooks = [
        {
            "id": nb.id,
            "name": nb.name,
            "description": nb.description,
            "tags": nb.tags,
            "thumbnail": nb.thumbnail,
        }
        for nb in config.notebooks
    ]
    return templates.TemplateResponse(
        request,
        "gallery.html",
        {"notebooks": notebooks, "theme": config.theme},
    )


@app.get("/notebook/{notebook_id}", response_class=HTMLResponse)
async def notebook_page(request: Request, notebook_id: str, preview: int = 0):
    nb = _find(notebook_id)
    if not nb:
        raise HTTPException(status_code=404, detail="Notebook not found")
    return templates.TemplateResponse(
        request,
        "notebook.html",
        {
            "notebook": {"id": nb.id, "name": nb.name, "tags": nb.tags},
            "theme": config.theme,
            "preview_mode": bool(preview),
        },
    )


# ── Notebook API ──────────────────────────────────────────────────────────────

@app.get("/api/notebooks")
async def list_notebooks():
    return [
        {"id": nb.id, "name": nb.name, "description": nb.description, "tags": nb.tags}
        for nb in config.notebooks
    ]


@app.get("/api/notebooks/{notebook_id}/ipynb")
async def get_ipynb(notebook_id: str):
    nb = _find(notebook_id)
    if not nb:
        raise HTTPException(404, "Notebook not found")
    data = get_notebook_json(nb, config.cacheDir)
    if data is None:
        raise HTTPException(503, "Notebook not yet synced — retry shortly")
    return JSONResponse(content=data)


@app.get("/api/notebooks/{notebook_id}/preview", response_class=HTMLResponse)
async def notebook_preview(notebook_id: str):
    nb = _find(notebook_id)
    if not nb:
        raise HTTPException(404, "Notebook not found")
    nb_path = Path(config.cacheDir) / nb.id / "repo" / nb.path
    if not nb_path.exists():
        raise HTTPException(503, "Notebook not yet synced")
    return render_preview(nb_path)


# ── Session API ───────────────────────────────────────────────────────────────

class CreateSessionRequest(BaseModel):
    notebook_id: str


@app.post("/api/sessions", status_code=202)
async def create_session(req: CreateSessionRequest):
    nb = _find(req.notebook_id)
    if not nb:
        raise HTTPException(404, "Notebook not found")
    image_ready = bool(nb.image) or bool(
        session_mgr.build_mgr and session_mgr.build_mgr.get_image(nb)
    )
    try:
        session = await session_mgr.create_session(nb)
    except RuntimeError as e:
        raise HTTPException(429, str(e))
    return {
        "session_id": session.session_id,
        "notebook_id": session.notebook_id,
        "status": session.status,
        "image_ready": image_ready,
    }


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    s = session_mgr.get_session(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    return {
        "session_id": s.session_id,
        "notebook_id": s.notebook_id,
        "notebook_name": s.notebook_name,
        "status": s.status,
        "kernel_id": s.kernel_id,
        "created_at": s.created_at.isoformat(),
        "last_activity": s.last_activity.isoformat(),
    }


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    if not await session_mgr.delete_session(session_id):
        raise HTTPException(404, "Session not found")
    return {"deleted": True}


@app.post("/api/sessions/{session_id}/interrupt")
async def interrupt_session(session_id: str):
    ok = await session_mgr.interrupt_kernel(session_id)
    if not ok:
        raise HTTPException(404, "Session not found or not running")
    return {"interrupted": True}


@app.get("/api/sessions")
async def list_sessions():
    return [
        {
            "session_id": s.session_id,
            "notebook_id": s.notebook_id,
            "notebook_name": s.notebook_name,
            "status": s.status,
            "created_at": s.created_at.isoformat(),
            "last_activity": s.last_activity.isoformat(),
        }
        for s in session_mgr.list_sessions()
    ]


# ── WebSocket kernel proxy ────────────────────────────────────────────────────

@app.websocket("/ws/kernel/{session_id}")
async def kernel_ws(websocket: WebSocket, session_id: str):
    session = session_mgr.get_session(session_id)
    if not session:
        await websocket.close(code=4004, reason="Session not found")
        return
    if session.status != "running":
        await websocket.close(code=4003, reason=f"Session not ready: {session.status}")
        return
    await proxy_kernel_websocket(websocket, session, session_mgr)
