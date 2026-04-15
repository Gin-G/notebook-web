"""Kaniko-based session image builder with ConfigMap-backed cache."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import time
from pathlib import Path
from typing import Optional

from kubernetes import client as k8s

from .config import AppConfig, NotebookEntry

log = logging.getLogger(__name__)

MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "notebook-gallery"


class BuildManager:
    def __init__(self, config: AppConfig, api: k8s.CoreV1Api) -> None:
        self.config = config
        self._api = api
        self._local_cache: dict[str, str] = {}  # cache_key -> image ref

    # ── Cache ──────────────────────────────────────────────────────────────

    def _cache_key(self, notebook: NotebookEntry, env_content: str) -> str:
        digest = hashlib.sha256(
            f"{notebook.repo}@{notebook.ref}:{env_content}".encode()
        ).hexdigest()[:12]
        return f"{notebook.id}-{digest}"

    def _image_ref(self, cache_key: str) -> str:
        return f"{self.config.build.registry}/{cache_key}:latest"

    async def _lookup_cache(self, cache_key: str) -> Optional[str]:
        if cache_key in self._local_cache:
            return self._local_cache[cache_key]
        loop = asyncio.get_event_loop()
        try:
            cm = await loop.run_in_executor(
                None,
                lambda: self._api.read_namespaced_config_map(
                    self.config.build.cacheConfigMapName,
                    self.config.namespace,
                ),
            )
            if cm.data and cache_key in cm.data:
                image = cm.data[cache_key]
                self._local_cache[cache_key] = image
                return image
        except k8s.exceptions.ApiException as e:
            if e.status != 404:
                log.warning("Cache ConfigMap read error: %s", e)
        return None

    async def _write_cache(self, cache_key: str, image: str) -> None:
        self._local_cache[cache_key] = image
        loop = asyncio.get_event_loop()
        cm_name = self.config.build.cacheConfigMapName
        ns = self.config.namespace
        patch = k8s.V1ConfigMap(data={cache_key: image})
        try:
            await loop.run_in_executor(
                None,
                lambda: self._api.patch_namespaced_config_map(cm_name, ns, patch),
            )
        except k8s.exceptions.ApiException as e:
            if e.status == 404:
                await loop.run_in_executor(
                    None,
                    lambda: self._api.create_namespaced_config_map(
                        ns,
                        k8s.V1ConfigMap(
                            metadata=k8s.V1ObjectMeta(name=cm_name, namespace=ns),
                            data={cache_key: image},
                        ),
                    ),
                )
            else:
                log.error("Failed to write image cache: %s", e)

    # ── Build ──────────────────────────────────────────────────────────────

    async def _run_build(
        self,
        cache_key: str,
        env_content: str,
        env_filename: str,
        base_image: str,
    ) -> str:
        bc = self.config.build
        ns = self.config.namespace
        loop = asyncio.get_event_loop()
        image = self._image_ref(cache_key)
        pod_name = f"nb-build-{cache_key[:8]}"

        is_conda = env_filename.endswith((".yml", ".yaml"))
        if is_conda:
            install_cmd = (
                f"conda env update --name base --file /workspace/{env_filename} --prune"
                " && conda clean -afy"
            )
        else:
            install_cmd = (
                f"pip install --no-cache-dir -r /workspace/{env_filename}"
                " && pip cache purge"
            )

        dockerfile = (
            f"FROM {base_image}\n"
            f"COPY {env_filename} /workspace/{env_filename}\n"
            "USER root\n"
            f"RUN {install_cmd}\n"
            "USER $NB_UID\n"
        )

        # Base64-encode both files so the init container can write them safely
        # regardless of special characters in the content.
        env_b64 = base64.b64encode(env_content.encode()).decode()
        df_b64 = base64.b64encode(dockerfile.encode()).decode()
        write_ctx = (
            "mkdir -p /workspace"
            f" && echo '{df_b64}' | base64 -d > /workspace/Dockerfile"
            f" && echo '{env_b64}' | base64 -d > /workspace/{env_filename}"
        )

        # Volumes
        volumes = [k8s.V1Volume(name="ctx", empty_dir=k8s.V1EmptyDirVolumeSource())]
        kaniko_mounts = [k8s.V1VolumeMount(name="ctx", mount_path="/workspace")]
        kaniko_args = [
            "--context=dir:///workspace",
            f"--destination={image}",
            "--compressed-caching=false",
            "--single-snapshot",
            "--push-retry=2",
        ]

        if bc.pushSecretName:
            volumes.append(k8s.V1Volume(
                name="docker-cfg",
                secret=k8s.V1SecretVolumeSource(
                    secret_name=bc.pushSecretName,
                    items=[k8s.V1KeyToPath(key=".dockerconfigjson", path="config.json")],
                ),
            ))
            kaniko_mounts.append(
                k8s.V1VolumeMount(name="docker-cfg", mount_path="/kaniko/.docker")
            )

        pod = k8s.V1Pod(
            metadata=k8s.V1ObjectMeta(
                name=pod_name,
                namespace=ns,
                labels={MANAGED_BY_LABEL: MANAGED_BY_VALUE, "role": "image-builder"},
            ),
            spec=k8s.V1PodSpec(
                restart_policy="Never",
                init_containers=[
                    k8s.V1Container(
                        name="write-context",
                        image="busybox:latest",
                        command=["sh", "-c", write_ctx],
                        volume_mounts=[k8s.V1VolumeMount(name="ctx", mount_path="/workspace")],
                    )
                ],
                containers=[
                    k8s.V1Container(
                        name="kaniko",
                        image=bc.kanikoImage,
                        args=kaniko_args,
                        volume_mounts=kaniko_mounts,
                    )
                ],
                volumes=volumes,
            ),
        )

        log.info("Starting image build %s → %s", pod_name, image)
        await loop.run_in_executor(
            None, lambda: self._api.create_namespaced_pod(ns, pod)
        )

        try:
            start = time.monotonic()
            while time.monotonic() - start < 600:
                await asyncio.sleep(5)
                p = await loop.run_in_executor(
                    None,
                    lambda: self._api.read_namespaced_pod(pod_name, ns),
                )
                phase = p.status.phase
                if phase == "Succeeded":
                    log.info("Image build succeeded: %s", image)
                    await self._write_cache(cache_key, image)
                    return image
                if phase in ("Failed", "Unknown"):
                    raise RuntimeError(f"Build pod {pod_name} reached phase: {phase}")
            raise RuntimeError("Image build timed out after 600s")
        finally:
            try:
                await loop.run_in_executor(
                    None,
                    lambda: self._api.delete_namespaced_pod(
                        pod_name, ns,
                        body=k8s.V1DeleteOptions(grace_period_seconds=0),
                    ),
                )
            except Exception:
                pass

    # ── Public API ─────────────────────────────────────────────────────────

    async def get_or_build_image(
        self,
        notebook: NotebookEntry,
        env_path: Optional[Path],
        base_image: str,
    ) -> Optional[str]:
        """Return a session image with deps pre-installed, building it if needed.

        Returns None if the build feature is not configured or there are no deps.
        """
        if not self.config.build.registry:
            return None
        if env_path is None or not env_path.exists():
            return None

        env_content = env_path.read_text()
        cache_key = self._cache_key(notebook, env_content)

        cached = await self._lookup_cache(cache_key)
        if cached:
            log.info("Cache hit for %s → %s", notebook.id, cached)
            return cached

        return await self._run_build(cache_key, env_content, env_path.name, base_image)
