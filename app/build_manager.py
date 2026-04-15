"""
Pre-builds session images for every notebook in the catalog.

On startup, BuildManager.build_all() launches one Kubernetes Job per unique
(repo, ref) pair. Each job runs jupyter-repo2docker inside a Docker-in-Docker
container (requires privileged: true). Built image names are cached in a
ConfigMap so a pod restart does not re-trigger builds unnecessarily.

Requires chart/values.yaml:
    build:
      registry: ghcr.io/org/repo/sessions   # where to push images
      pushSecretName: ghcr-push             # K8s secret with .dockerconfigjson
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


class BuildManager:
    def __init__(self, config: AppConfig, core_api: k8s.CoreV1Api) -> None:
        self.config = config
        self._core = core_api
        self._batch = k8s.BatchV1Api(core_api.api_client)
        self._cache: Dict[str, str] = {}  # cache_key -> image name

    def _ns(self) -> str:
        return self.config.namespace

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
            log.info("Loaded %d cached session image(s) from ConfigMap", len(self._cache))
        except k8s.exceptions.ApiException as e:
            if e.status == 404:
                await self._ensure_cache_cm()
            else:
                log.warning("Could not read image cache ConfigMap: %s", e)

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
            if e.status != 409:  # 409 = already exists, fine
                log.warning("Could not create cache ConfigMap: %s", e)

    async def _save_cache(self) -> None:
        loop = asyncio.get_event_loop()
        payload = k8s.V1ConfigMap(data={"cache": json.dumps(self._cache)})
        try:
            await loop.run_in_executor(
                None,
                lambda: self._core.patch_namespaced_config_map(CACHE_CM_NAME, self._ns(), payload),
            )
        except k8s.exceptions.ApiException as e:
            log.warning("Could not save image cache: %s", e)

    # ── Public: build all ──────────────────────────────────────────────────────

    async def build_all(self, notebooks: List[NotebookEntry]) -> None:
        """
        Called at startup. Launches one build Job per unique (repo, ref) that
        isn't already cached. Runs in background — does not block app startup.
        """
        if not self.config.build.registry:
            log.info("build.registry not set — skipping session image pre-builds")
            return

        await self._load_cache()

        # Collect unique (repo, ref) pairs that need a build
        pending: dict[tuple[str, str], str] = {}
        for nb in notebooks:
            if nb.image:
                continue  # explicitly pinned, nothing to do
            key = self._cache_key(nb.repo, nb.ref)
            if key in self._cache:
                log.info("Notebook '%s': cached image %s", nb.name, self._cache[key])
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

        log.info("Building %s from %s @ %s", image, repo, ref)

        # Start dockerd, wait for it, install repo2docker, build + push, then exit
        build_cmd = " && ".join([
            "dockerd-entrypoint.sh &",
            "echo 'Waiting for Docker daemon...'",
            "for i in $(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 2; done",
            "docker info >/dev/null 2>&1 || (echo 'Docker failed to start' && exit 1)",
            "apk add --no-cache python3 py3-pip",
            "pip install --quiet jupyter-repo2docker",
            f"jupyter-repo2docker --no-run --push --image-name={image} --ref={ref} {repo}",
        ])

        volumes: list[k8s.V1Volume] = []
        volume_mounts: list[k8s.V1VolumeMount] = []

        if self.config.build.pushSecretName:
            volumes.append(k8s.V1Volume(
                name="push-secret",
                secret=k8s.V1SecretVolumeSource(secret_name=self.config.build.pushSecretName),
            ))
            volume_mounts.append(k8s.V1VolumeMount(
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
                        containers=[
                            k8s.V1Container(
                                name="builder",
                                image="docker:dind",
                                command=["sh", "-c", build_cmd],
                                security_context=k8s.V1SecurityContext(privileged=True),
                                volume_mounts=volume_mounts,
                            )
                        ],
                        volumes=volumes,
                    )
                ),
            ),
        )

        # Clean up any stale job with the same name
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
                    None,
                    lambda: self._batch.read_namespaced_job(job_name, self._ns()),
                )
                if j.status.succeeded:
                    log.info("Build succeeded: %s", image)
                    self._cache[key] = image
                    await self._save_cache()
                    return
                if j.status.failed and j.status.failed >= 2:
                    log.error("Build job %s failed — check pod logs in namespace %s", job_name, self._ns())
                    return
            except Exception as e:
                log.warning("Error polling build job %s: %s", job_name, e)

        log.error("Build job %s timed out after 1 hour", job_name)
