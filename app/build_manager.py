"""
Pre-builds session images for every notebook in the catalog.

On startup, BuildManager.build_all() launches one Kubernetes Job per unique
(repo, ref) pair.

Job structure:
  init container 1 (alpine/git)
    - clones the repo to /workspace/repo

  init container 2 (quay.io/jupyter/repo2docker)
    - runs repo2docker in dry-run mode to generate a complete Dockerfile +
      build context from the repo's environment files (environment.yml,
      requirements.txt, etc.)
    - writes the context to /workspace/context and exits before building

  main container (alpine + buildctl)
    - connects to the in-cluster BuildKit service
    - builds the image from /workspace/context and pushes to the registry

Results are cached in a ConfigMap so restarts skip already-built images.

Requires values.yaml:
  build:
    enabled: true
    registry: ghcr.io/org/repo/sessions
    pushSecretName: ghcr-push          # K8s Secret with .dockerconfigjson
    buildkitServiceName: ""            # defaults to <release>-buildkit-service
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from typing import Dict, List

from kubernetes import client as k8s

from .config import AppConfig, NotebookEntry

log = logging.getLogger(__name__)

CACHE_CM_NAME = "notebook-image-cache"
JOB_LABEL = "app.kubernetes.io/managed-by"
JOB_VALUE = "notebook-gallery-builder"

# Pin buildctl version to match the packaged buildkit-service chart (1.4.0 → v0.28.1).
BUILDCTL_VERSION = "v0.28.1"
BUILDCTL_URL = (
    f"https://github.com/moby/buildkit/releases/download/{BUILDCTL_VERSION}"
    f"/buildkit-{BUILDCTL_VERSION}.linux-amd64.tar.gz"
)


class BuildManager:
    def __init__(self, config: AppConfig, core_api: k8s.CoreV1Api) -> None:
        self.config = config
        self._core = core_api
        self._batch = k8s.BatchV1Api(core_api.api_client)
        self._cache: Dict[str, str] = {}  # cache_key -> image name

    def _ns(self) -> str:
        return self.config.namespace

    def _buildkit_addr(self) -> str:
        svc = self.config.build.buildkitServiceName or "buildkit-service"
        return f"tcp://{svc}:1234"

    @staticmethod
    def _cache_key(repo: str, ref: str) -> str:
        return hashlib.sha256(f"{repo}@{ref}".encode()).hexdigest()[:16]

    def _image_name(self, repo: str, ref: str) -> str:
        slug = repo.replace("https://", "").replace("http://", "").rstrip("/").rstrip(".git")
        slug = re.sub(r"[^a-zA-Z0-9._/-]", "-", slug)
        slug = re.sub(r"-{2,}", "-", slug).strip("-")
        parts = [p for p in slug.split("/") if p]
        short = "-".join(parts[-2:]) if len(parts) >= 2 else parts[0]
        safe_ref = re.sub(r"[^a-zA-Z0-9._-]", "-", ref)
        return f"{self.config.build.registry}/{short}:{safe_ref}"

    def get_image(self, notebook: NotebookEntry) -> str:
        """Return pre-built image for this notebook, or empty string if not ready."""
        if notebook.image:
            return notebook.image
        key = self._cache_key(notebook.repo, notebook.ref)
        return self._cache.get(key, "")

    # ── Cache persistence ─────────────────────────────────────────────────────

    async def _load_cache(self) -> None:
        loop = asyncio.get_event_loop()
        try:
            cm = await loop.run_in_executor(
                None, lambda: self._core.read_namespaced_config_map(CACHE_CM_NAME, self._ns())
            )
            self._cache = json.loads(cm.data.get("cache", "{}"))
            log.info("Loaded %d cached session image(s)", len(self._cache))
        except k8s.exceptions.ApiException as e:
            if e.status == 404:
                await self._ensure_cache_cm()
            else:
                log.warning("Could not read image cache: %s", e)

    async def _ensure_cache_cm(self) -> None:
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: self._core.create_namespaced_config_map(
                    self._ns(),
                    k8s.V1ConfigMap(
                        metadata=k8s.V1ObjectMeta(name=CACHE_CM_NAME, namespace=self._ns()),
                        data={"cache": "{}"},
                    ),
                ),
            )
        except k8s.exceptions.ApiException as e:
            if e.status != 409:
                log.warning("Could not create cache ConfigMap: %s", e)

    async def _save_cache(self) -> None:
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: self._core.patch_namespaced_config_map(
                    CACHE_CM_NAME, self._ns(),
                    k8s.V1ConfigMap(data={"cache": json.dumps(self._cache)}),
                ),
            )
        except k8s.exceptions.ApiException as e:
            log.warning("Could not save image cache: %s", e)

    # ── Public: build all ──────────────────────────────────────────────────────

    async def build_all(self, notebooks: List[NotebookEntry]) -> None:
        """
        Called at startup (and after periodic sync). Launches one build Job
        per unique (repo, ref) that isn't already cached. Non-blocking.
        """
        if not self.config.build.registry:
            log.info("build.registry not set — skipping session image pre-builds")
            return

        await self._load_cache()

        pending: dict[tuple[str, str], str] = {}
        for nb in notebooks:
            if nb.image:
                continue
            key = self._cache_key(nb.repo, nb.ref)
            if key in self._cache:
                log.info("Notebook '%s': using cached image %s", nb.name, self._cache[key])
                continue
            pair = (nb.repo, nb.ref)
            if pair not in pending:
                pending[pair] = self._image_name(nb.repo, nb.ref)

        if not pending:
            log.info("All session images are up to date")
            return

        log.info("Starting %d session image build(s)", len(pending))
        for (repo, ref), image in pending.items():
            asyncio.create_task(self._build(repo, ref, image))

    # ── Internal: single build job ─────────────────────────────────────────────

    async def _build(self, repo: str, ref: str, image: str) -> None:
        loop = asyncio.get_event_loop()
        key = self._cache_key(repo, ref)
        job_name = f"nb-build-{key}"

        log.info("Building %s  ←  %s @ %s", image, repo, ref)

        buildkit_addr = self._buildkit_addr()

        # ── Init 1: clone repo ────────────────────────────────────────────────
        clone_cmd = (
            f"git clone --depth=1 --single-branch --branch {ref} {repo} /workspace/repo"
        )

        # ── Init 2: generate build context via repo2docker ───────────────────
        #
        # repo2docker detects all environment specs (environment.yml,
        # requirements.txt, apt.txt, postBuild, etc.) and generates a complete
        # Dockerfile + build context in a temp dir. We intercept mkdtemp so we
        # can copy the context to /workspace/context before repo2docker cleans
        # it up, then exit before it tries to call docker build.
        generate_cmd = rf"""
python3 - << 'PYEOF'
import sys, os, shutil, tempfile, logging

logging.getLogger("repo2docker").setLevel(logging.WARNING)

_orig = tempfile.mkdtemp
_dirs = []
def _capture(*a, **kw):
    d = _orig(*a, **kw)
    _dirs.append(d)
    return d
tempfile.mkdtemp = _capture

from repo2docker import Repo2Docker

r2d = Repo2Docker()
r2d.repo = "/workspace/repo"
r2d.output_image_spec = "{image}"
r2d.dry_run = True

try:
    r2d.initialize([])
    r2d.start()
except (SystemExit, Exception):
    pass

for d in reversed(_dirs):
    if os.path.isfile(os.path.join(d, "Dockerfile")):
        shutil.copytree(d, "/workspace/context", dirs_exist_ok=True)
        print(f"context written to /workspace/context (from {{d}})", flush=True)
        sys.exit(0)

sys.exit("repo2docker did not produce a Dockerfile")
PYEOF
""".strip()
        # The push secret is a generic secret with a raw token under key 'api'
        # (e.g. synced from OpenBao via ExternalSecrets). We mount it and
        # construct a docker config.json at build time so buildctl can push.
        push_auth_cmd = ""
        if self.config.build.pushSecretName:
            registry_host = self.config.build.registry.split("/")[0]
            push_auth_cmd = rf"""
TOKEN=$(cat /run/secrets/push/api)
mkdir -p /root/.docker
printf '{{"auths":{{"{registry_host}":{{"auth":"%s"}}}}}}' \
  "$(printf 'x-token:%s' "$TOKEN" | base64 -w0)" \
  > /root/.docker/config.json
""".strip()

        build_cmd = rf"""
set -e
{push_auth_cmd}
echo "Downloading buildctl {BUILDCTL_VERSION}..."
wget -qO- {BUILDCTL_URL} | tar xz -C /usr/local/bin --strip-components=1 bin/buildctl

echo "Building {image} via {buildkit_addr}..."
buildctl \
  --addr {buildkit_addr} \
  build \
  --frontend dockerfile.v0 \
  --local context=/workspace/context \
  --local dockerfile=/workspace/context \
  --output type=image,name={image},push=true
""".strip()

        ws_mount = k8s.V1VolumeMount(name="workspace", mount_path="/workspace")
        volumes: list[k8s.V1Volume] = [
            k8s.V1Volume(name="workspace", empty_dir=k8s.V1EmptyDirVolumeSource())
        ]
        main_mounts = [ws_mount]

        if self.config.build.pushSecretName:
            volumes.append(k8s.V1Volume(
                name="push-secret",
                secret=k8s.V1SecretVolumeSource(
                    secret_name=self.config.build.pushSecretName,
                    optional=True,
                ),
            ))
            main_mounts.append(k8s.V1VolumeMount(
                name="push-secret",
                mount_path="/run/secrets/push",
                read_only=True,
            ))

        job = k8s.V1Job(
            metadata=k8s.V1ObjectMeta(
                name=job_name,
                namespace=self._ns(),
                labels={JOB_LABEL: JOB_VALUE},
            ),
            spec=k8s.V1JobSpec(
                backoff_limit=1,
                ttl_seconds_after_finished=7200,
                template=k8s.V1PodTemplateSpec(
                    spec=k8s.V1PodSpec(
                        restart_policy="Never",
                        init_containers=[
                            k8s.V1Container(
                                name="clone",
                                image="alpine/git:latest",
                                command=["sh", "-c", clone_cmd],
                                volume_mounts=[ws_mount],
                            ),
                            k8s.V1Container(
                                name="generate-context",
                                image="quay.io/jupyterhub/repo2docker:latest",
                                command=["sh", "-c", generate_cmd],
                                volume_mounts=[ws_mount],
                            ),
                        ],
                        containers=[
                            k8s.V1Container(
                                name="build",
                                image="alpine:latest",
                                command=["sh", "-c", build_cmd],
                                volume_mounts=main_mounts,
                            )
                        ],
                        volumes=volumes,
                    )
                ),
            ),
        )

        # Clean up stale job if present
        try:
            await loop.run_in_executor(
                None,
                lambda: self._batch.delete_namespaced_job(
                    job_name, self._ns(),
                    body=k8s.V1DeleteOptions(propagation_policy="Foreground"),
                ),
            )
            await asyncio.sleep(5)
        except k8s.exceptions.ApiException:
            pass

        try:
            await loop.run_in_executor(
                None, lambda: self._batch.create_namespaced_job(self._ns(), job)
            )
        except Exception as e:
            log.error("Failed to create build job %s: %s", job_name, e)
            return

        # Poll for completion (up to 1 hour)
        elapsed = 0
        while elapsed < 3600:
            await asyncio.sleep(20)
            elapsed += 20
            try:
                j = await loop.run_in_executor(
                    None, lambda: self._batch.read_namespaced_job(job_name, self._ns())
                )
                if j.status.succeeded:
                    log.info("Build succeeded: %s", image)
                    self._cache[key] = image
                    await self._save_cache()
                    return
                if j.status.failed and j.status.failed >= 2:
                    log.error("Build job %s failed — check pod logs in ns %s", job_name, self._ns())
                    return
            except Exception as e:
                log.warning("Error polling build job %s: %s", job_name, e)

        log.error("Build job %s timed out", job_name)
