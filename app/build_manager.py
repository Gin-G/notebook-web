"""
Pre-builds session images for every notebook in the catalog.

On startup, BuildManager.build_all() launches one Kubernetes Job per unique
(repo, ref) pair. Each job uses BuildKit (packaged as a chart dependency) to
build images without requiring privileged containers or a Docker daemon.

Job structure:
  init container (alpine/git)
    - clones the repo
    - detects environment files (environment.yml / requirements.txt)
    - writes a Dockerfile + build context to /workspace

  main container (alpine + buildctl)
    - connects to the in-cluster BuildKit service
    - builds the image and pushes it to the configured registry

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

# buildctl is downloaded at job time from the official release.
# Pin a version matching the packaged buildkit-service chart (1.4.0 -> v0.28.1).
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

        # ── Init container: clone repo and write Dockerfile ──────────────────
        #
        # Detects environment files in order of preference:
        #   1. environment.yml / environment.yaml  → conda install
        #   2. requirements.txt                    → pip install
        #   3. neither                             → base image only (no deps)
        #
        # Writes the Dockerfile and any needed files to /workspace/context.

        prepare_cmd = rf"""
set -e
git clone --depth=1 --single-branch --branch {ref} {repo} /tmp/repo

REPO=/tmp/repo
CTX=/workspace/context
mkdir -p $CTX

# Find env file (repo-root takes precedence over subdir)
ENV_FILE=""
for f in \
    $REPO/environment.yml \
    $REPO/environment.yaml \
    $REPO/binder/environment.yml \
    $REPO/binder/environment.yaml; do
  if [ -f "$f" ]; then ENV_FILE="$f"; break; fi
done

REQ_FILE=""
for f in $REPO/requirements.txt $REPO/binder/requirements.txt; do
  if [ -f "$f" ]; then REQ_FILE="$f"; break; fi
done

if [ -n "$ENV_FILE" ]; then
  cp "$ENV_FILE" $CTX/environment.yml
  # Strip 'name:' so conda always installs into base
  sed '/^name:/d' $CTX/environment.yml > $CTX/env-base.yml
  cat > $CTX/Dockerfile << 'DOCKERFILE'
FROM jupyter/minimal-notebook:latest
USER root
COPY env-base.yml /tmp/env-base.yml
RUN conda env update --name base --file /tmp/env-base.yml \
    && conda clean -afy
USER ${{NB_USER}}
DOCKERFILE
elif [ -n "$REQ_FILE" ]; then
  cp "$REQ_FILE" $CTX/requirements.txt
  cat > $CTX/Dockerfile << 'DOCKERFILE'
FROM jupyter/minimal-notebook:latest
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
DOCKERFILE
else
  echo "No environment file found — using base image"
  cat > $CTX/Dockerfile << 'DOCKERFILE'
FROM jupyter/minimal-notebook:latest
DOCKERFILE
fi

echo "=== Dockerfile ===" && cat $CTX/Dockerfile
""".strip()

        # ── Main container: build with buildctl ──────────────────────────────
        #
        # Downloads the buildctl binary (matching the packaged buildkit-service
        # version) and runs the build against the in-cluster BuildKit daemon.

        buildkit_addr = self._buildkit_addr()
        build_cmd = rf"""
set -e
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

        # Registry credentials volume
        volumes: list[k8s.V1Volume] = [
            k8s.V1Volume(name="workspace", empty_dir=k8s.V1EmptyDirVolumeSource())
        ]
        init_mounts = [k8s.V1VolumeMount(name="workspace", mount_path="/workspace")]
        main_mounts = [k8s.V1VolumeMount(name="workspace", mount_path="/workspace")]

        if self.config.build.pushSecretName:
            volumes.append(k8s.V1Volume(
                name="push-secret",
                secret=k8s.V1SecretVolumeSource(secret_name=self.config.build.pushSecretName),
            ))
            main_mounts.append(k8s.V1VolumeMount(
                name="push-secret",
                mount_path="/root/.docker",
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
                                name="prepare",
                                image="alpine/git:latest",
                                command=["sh", "-c", prepare_cmd],
                                volume_mounts=init_mounts,
                            )
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
