"""Load and validate the notebook catalog + settings from ConfigMap JSON or values.yaml."""
from __future__ import annotations

import json
import os
import re
from typing import List, Optional

import yaml
from pydantic import BaseModel


class ResourceSpec(BaseModel):
    cpu: str = "1"
    memory: str = "2Gi"


class Resources(BaseModel):
    limits: ResourceSpec = ResourceSpec()
    requests: ResourceSpec = ResourceSpec(cpu="250m", memory="512Mi")


class NotebookEntry(BaseModel):
    name: str
    repo: str
    ref: str = "main"
    path: str
    envFile: str = ""   # explicit path to requirements.txt or environment.yml; auto-discovered if empty
    image: str = ""     # pre-built session image; skips env install entirely when set
    tags: List[str] = []
    description: str = ""
    resources: Optional[Resources] = None
    id: str = ""

    def model_post_init(self, __context) -> None:
        if not self.id:
            self.id = re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-")


class SessionDefaults(BaseModel):
    image: str = "jupyter/minimal-notebook:latest"
    resources: Resources = Resources()
    idleTimeoutMinutes: int = 30
    maxSessions: int = 20
    kernelStartupTimeoutSeconds: int = 60


class Theme(BaseModel):
    title: str = "Notebook Gallery"
    primaryColor: str = "#1a5276"
    fontFamily: str = "Inter, system-ui, sans-serif"
    customCssPath: str = ""
    customJsPath: str = ""
    codeTheme: str = "one-dark"


class IngressConfig(BaseModel):
    enabled: bool = True
    host: str = "notebooks.example.com"


class ImageConfig(BaseModel):
    repository: str = "ghcr.io/org/notebook-gallery"
    tag: str = "latest"


class BuildConfig(BaseModel):
    enabled: bool = False
    registry: str = ""            # image name prefix, e.g. ghcr.io/org/repo/sessions
    pushSecretName: str = ""      # K8s Secret with .dockerconfigjson for pushing images
    buildkitServiceName: str = "" # defaults to <release-name>-buildkit-service


class AppConfig(BaseModel):
    notebooks: List[NotebookEntry] = []
    sessionDefaults: SessionDefaults = SessionDefaults()
    theme: Theme = Theme()
    ingress: IngressConfig = IngressConfig()
    image: ImageConfig = ImageConfig()
    build: BuildConfig = BuildConfig()
    namespace: str = "default"
    cacheDir: str = "/tmp/notebook-cache"


_config: Optional[AppConfig] = None


def load_config(path: Optional[str] = None) -> AppConfig:
    global _config

    if path is None:
        path = os.environ.get("CONFIG_PATH", "/etc/notebook-gallery/config.json")

    if os.path.isfile(path):
        with open(path) as f:
            data = yaml.safe_load(f) if path.endswith((".yaml", ".yml")) else json.load(f)
    else:
        # Local dev fallback: read chart/values.yaml
        local = os.path.join(os.path.dirname(__file__), "..", "chart", "values.yaml")
        if os.path.isfile(local):
            with open(local) as f:
                data = yaml.safe_load(f)
        else:
            data = {}

    _config = AppConfig(**(data or {}))
    return _config


def get_config() -> AppConfig:
    global _config
    if _config is None:
        _config = load_config()
    return _config
