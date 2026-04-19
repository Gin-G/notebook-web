"""Kubernetes pod lifecycle management for per-session Jupyter kernels."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

import aiohttp
from kubernetes import client as k8s, config as k8s_config

from .config import AppConfig, NotebookEntry
from .notebook_fetcher import find_env_file

log = logging.getLogger(__name__)

MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "notebook-gallery"


def _conda_env_name(env_file) -> str:
    """Return the conda env name from an environment.yml, or 'base' as fallback."""
    if env_file and env_file.suffix in (".yml", ".yaml"):
        try:
            import yaml
            with open(env_file) as f:
                data = yaml.safe_load(f)
            name = (data or {}).get("name", "")
            if name:
                return name
        except Exception:
            pass
    return "base"


@dataclass
class Session:
    session_id: str
    notebook_id: str
    notebook_name: str
    pod_name: str
    pod_ip: str = ""
    kernel_id: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: str = "pending"  # pending | starting | running | error | terminating


class SessionManager:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._sessions: Dict[str, Session] = {}
        self._lock = asyncio.Lock()
        self._k8s_api: Optional[k8s.CoreV1Api] = None
        self.build_mgr = None  # set by main.py after startup

    # ── Kubernetes client ──────────────────────────────────────────────────

    def _api(self) -> k8s.CoreV1Api:
        if self._k8s_api is None:
            try:
                k8s_config.load_incluster_config()
            except k8s_config.ConfigException:
                k8s_config.load_kube_config()
            self._k8s_api = k8s.CoreV1Api()
        return self._k8s_api

    def _ns(self) -> str:
        return self.config.namespace

    # ── Pod spec ──────────────────────────────────────────────────────────

    def _pod_spec(self, session_id: str, notebook: NotebookEntry) -> k8s.V1Pod:
        import re
        sd = self.config.sessionDefaults
        res = notebook.resources or sd.resources

        pod_name = f"nb-{session_id[:8]}"

        def _label_safe(value: str, max_len: int = 63) -> str:
            slug = re.sub(r"[^A-Za-z0-9_.-]", "-", value).strip("-")
            return slug[:max_len].rstrip("-")

        # Pre-built image from build manager / values.yaml, or fall back to default
        jupyter_image = notebook.image or sd.image
        using_prebuilt = jupyter_image != sd.image
        pull_secrets = (
            [k8s.V1LocalObjectReference(name=self.config.build.pushSecretName)]
            if self.config.build.pushSecretName and using_prebuilt
            else []
        )

        # For pre-built repo2docker images, ENV_NAME is already baked into the
        # image — overriding it would activate the wrong conda env. For the
        # fallback base image, read the name from the repo's environment.yml so
        # at least the right env name is communicated (packages still won't be
        # present, but activation won't mask it). Fall back to "base".
        if using_prebuilt:
            env_vars = []
        else:
            env_name = _conda_env_name(find_env_file(notebook, self.config.cacheDir))
            env_vars = [k8s.V1EnvVar(name="ENV_NAME", value=env_name)]

        # Fetch the notebook file from git into /notebook
        fetch_cmd = (
            "git clone --depth=1 --single-branch"
            f" --branch {notebook.ref}"
            f" {notebook.repo} /tmp/repo"
            " && mkdir -p /notebook"
            f" && cp /tmp/repo/{notebook.path} /notebook/notebook.ipynb"
        )

        init_containers = [
            k8s.V1Container(
                name="notebook-fetcher",
                image="alpine/git:latest",
                command=["sh", "-c", fetch_cmd],
                volume_mounts=[k8s.V1VolumeMount(name="notebook-data", mount_path="/notebook")],
            )
        ]

        return k8s.V1Pod(
            metadata=k8s.V1ObjectMeta(
                name=pod_name,
                namespace=self._ns(),
                labels={
                    MANAGED_BY_LABEL: MANAGED_BY_VALUE,
                    "session-id": session_id,
                    "notebook-id": _label_safe(notebook.id),
                    "notebook-name": _label_safe(notebook.name),
                },
            ),
            spec=k8s.V1PodSpec(
                restart_policy="Never",
                image_pull_secrets=pull_secrets or None,
                init_containers=init_containers,
                containers=[
                    k8s.V1Container(
                        name="jupyter",
                        image=jupyter_image,
                        command=[
                            "jupyter",
                            "server",
                            "--ServerApp.token=",
                            "--ServerApp.password=",
                            "--ServerApp.allow_origin=*",
                            "--ServerApp.disable_check_xsrf=True",
                            "--no-browser",
                            "--ip=0.0.0.0",
                            "--port=8888",
                            "--ServerApp.root_dir=/notebook",
                        ],
                        env=env_vars or None,
                        ports=[k8s.V1ContainerPort(container_port=8888, name="http")],
                        resources=k8s.V1ResourceRequirements(
                            limits={"cpu": res.limits.cpu, "memory": res.limits.memory},
                            requests={"cpu": res.requests.cpu, "memory": res.requests.memory},
                        ),
                        volume_mounts=[
                            k8s.V1VolumeMount(name="notebook-data", mount_path="/notebook")
                        ],
                    )
                ],
                volumes=[
                    k8s.V1Volume(
                        name="notebook-data",
                        empty_dir=k8s.V1EmptyDirVolumeSource(),
                    )
                ],
            ),
        )

    # ── Public API ────────────────────────────────────────────────────────

    async def create_session(self, notebook: NotebookEntry) -> Session:
        async with self._lock:
            active = [
                s for s in self._sessions.values()
                if s.status in ("pending", "starting", "running")
            ]
            if len(active) >= self.config.sessionDefaults.maxSessions:
                raise RuntimeError(
                    f"Max sessions ({self.config.sessionDefaults.maxSessions}) reached"
                )

        session_id = str(uuid.uuid4())

        # Use pre-built image if the build manager has one ready
        if self.build_mgr:
            prebuilt = self.build_mgr.get_image(notebook)
            if prebuilt:
                log.info("Using pre-built image %s for notebook '%s'", prebuilt, notebook.name)
                # Temporarily set image on notebook for pod spec generation
                notebook = notebook.model_copy(update={"image": prebuilt})

        pod_spec = self._pod_spec(session_id, notebook)

        session = Session(
            session_id=session_id,
            notebook_id=notebook.id,
            notebook_name=notebook.name,
            pod_name=pod_spec.metadata.name,
        )
        self._sessions[session_id] = session

        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: self._api().create_namespaced_pod(self._ns(), pod_spec),
            )
            log.info("Created pod %s for session %s", session.pod_name, session_id)
        except Exception as e:
            session.status = "error"
            log.error("Pod creation failed: %s", e)
            raise

        asyncio.create_task(self._await_ready(session))
        return session

    async def delete_session(self, session_id: str) -> bool:
        session = self._sessions.get(session_id)
        if not session:
            return False
        session.status = "terminating"
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: self._api().delete_namespaced_pod(
                    session.pod_name,
                    self._ns(),
                    body=k8s.V1DeleteOptions(grace_period_seconds=5),
                ),
            )
            log.info("Deleted pod %s", session.pod_name)
        except Exception as e:
            log.warning("Error deleting pod %s: %s", session.pod_name, e)
        self._sessions.pop(session_id, None)
        return True

    def get_session(self, session_id: str) -> Optional[Session]:
        return self._sessions.get(session_id)

    def list_sessions(self) -> List[Session]:
        return list(self._sessions.values())

    def touch_session(self, session_id: str) -> None:
        s = self._sessions.get(session_id)
        if s:
            s.last_activity = datetime.now(timezone.utc)

    async def interrupt_kernel(self, session_id: str) -> bool:
        s = self._sessions.get(session_id)
        if not s or s.status != "running":
            return False
        try:
            async with aiohttp.ClientSession() as http:
                async with http.post(
                    f"http://{s.pod_ip}:8888/api/kernels/{s.kernel_id}/interrupt",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    return resp.status == 204
        except Exception as e:
            log.warning("Interrupt failed for session %s: %s", session_id, e)
            return False

    # ── Background tasks ─────────────────────────────────────────────────

    async def reap_idle_sessions(self) -> int:
        timeout_secs = self.config.sessionDefaults.idleTimeoutMinutes * 60
        now = datetime.now(timezone.utc)
        to_delete = []
        for sid, s in list(self._sessions.items()):
            if s.status == "running":
                idle = (now - s.last_activity).total_seconds()
                if idle > timeout_secs:
                    log.info("Reaping idle session %s (idle %.0fs)", sid, idle)
                    to_delete.append(sid)
            elif s.status == "error":
                age = (now - s.created_at).total_seconds()
                if age > 1800:  # keep error pods for 30 min so logs stay readable
                    to_delete.append(sid)
        for sid in to_delete:
            await self.delete_session(sid)
        return len(to_delete)

    async def reaper_task(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                n = await self.reap_idle_sessions()
                if n:
                    log.info("Reaped %d session(s)", n)
            except Exception as e:
                log.error("Reaper error: %s", e)

    # ── Internal: wait for pod + kernel ──────────────────────────────────

    async def _await_ready(self, session: Session) -> None:
        timeout = self.config.sessionDefaults.kernelStartupTimeoutSeconds
        start = time.monotonic()
        session.status = "starting"
        loop = asyncio.get_event_loop()

        # 1. Wait for pod Running phase
        while time.monotonic() - start < timeout:
            try:
                pod = await loop.run_in_executor(
                    None,
                    lambda: self._api().read_namespaced_pod(session.pod_name, self._ns()),
                )
                phase = pod.status.phase
                pod_ip = pod.status.pod_ip
                if phase == "Running" and pod_ip:
                    session.pod_ip = pod_ip
                    log.info("Pod %s running at %s", session.pod_name, pod_ip)
                    break
                if phase in ("Failed", "Succeeded", "Unknown"):
                    session.status = "error"
                    log.error("Pod %s reached unexpected phase: %s", session.pod_name, phase)
                    return
            except Exception as e:
                log.debug("Pod status check error: %s", e)
            await asyncio.sleep(2)
        else:
            session.status = "error"
            log.error("Timeout waiting for pod %s to start", session.pod_name)
            return

        # 2. Wait for Jupyter server to accept requests
        jupyter_url = f"http://{session.pod_ip}:8888"
        async with aiohttp.ClientSession() as http:
            while time.monotonic() - start < timeout:
                try:
                    async with http.get(
                        f"{jupyter_url}/api",
                        timeout=aiohttp.ClientTimeout(total=3),
                    ) as resp:
                        if resp.status == 200:
                            log.info("Jupyter server ready on %s", session.pod_name)
                            break
                except Exception:
                    pass
                await asyncio.sleep(2)
            else:
                session.status = "error"
                log.error("Timeout waiting for Jupyter on pod %s", session.pod_name)
                return

            # 3. Start a Python kernel
            try:
                async with http.post(
                    f"{jupyter_url}/api/kernels",
                    json={"name": "python3"},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status not in (200, 201):
                        raise RuntimeError(f"Kernel create returned {resp.status}")
                    data = await resp.json()
                    session.kernel_id = data["id"]
                    log.info("Kernel %s started on pod %s", session.kernel_id, session.pod_name)
            except Exception as e:
                session.status = "error"
                log.error("Kernel start failed: %s", e)
                return

        session.status = "running"
        session.last_activity = datetime.now(timezone.utc)
