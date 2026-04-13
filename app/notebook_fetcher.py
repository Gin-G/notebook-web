"""Git clone/pull notebooks and render nbconvert HTML previews."""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Optional

import git
import nbformat
from nbconvert import HTMLExporter
from nbconvert.preprocessors import ClearOutputPreprocessor

from .config import NotebookEntry, get_config

log = logging.getLogger(__name__)


def _repo_dir(notebook: NotebookEntry, cache_dir: str) -> Path:
    return Path(cache_dir) / notebook.id / "repo"


def _notebook_path(notebook: NotebookEntry, cache_dir: str) -> Path:
    return _repo_dir(notebook, cache_dir) / notebook.path


def sync_notebook(notebook: NotebookEntry, cache_dir: str) -> Optional[Path]:
    """Clone or pull a notebook repo. Returns path to the .ipynb, or None on failure."""
    repo_dir = _repo_dir(notebook, cache_dir)
    try:
        if repo_dir.exists():
            try:
                repo = git.Repo(repo_dir)
                origin = repo.remote("origin")
                origin.fetch()
                repo.git.checkout(notebook.ref)
                repo.git.reset("--hard", f"origin/{notebook.ref}")
                log.info("Pulled %s @ %s", notebook.name, notebook.ref)
            except git.GitCommandError as e:
                # Branch may be a tag or commit SHA — fall back to full clone
                log.warning("Reset failed for %s, re-cloning: %s", notebook.name, e)
                import shutil
                shutil.rmtree(repo_dir, ignore_errors=True)
                raise
        else:
            repo_dir.parent.mkdir(parents=True, exist_ok=True)
            git.Repo.clone_from(
                notebook.repo,
                repo_dir,
                depth=1,
                single_branch=True,
                branch=notebook.ref,
            )
            log.info("Cloned %s @ %s", notebook.name, notebook.ref)
    except Exception as e:
        log.error("Failed to sync notebook '%s': %s", notebook.name, e)
        return None

    nb_path = _notebook_path(notebook, cache_dir)
    if not nb_path.exists():
        log.error("Notebook path not found after sync: %s", nb_path)
        return None
    return nb_path


async def async_sync_notebook(notebook: NotebookEntry, cache_dir: str) -> Optional[Path]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, sync_notebook, notebook, cache_dir)


def render_preview(nb_path: Path) -> str:
    """Convert .ipynb to HTML body fragment (outputs stripped) for gallery preview."""
    with open(nb_path) as f:
        nb = nbformat.read(f, as_version=4)

    exporter = HTMLExporter()
    exporter.exclude_input_prompt = True
    exporter.exclude_output_prompt = True
    exporter.preprocessors = [ClearOutputPreprocessor]
    exporter.template_name = "basic"

    body, _ = exporter.from_notebook_node(nb)
    # Strip full HTML wrapper, return just the body fragment
    match = re.search(r"<body[^>]*>(.*)</body>", body, re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else body


def get_notebook_json(notebook: NotebookEntry, cache_dir: str) -> Optional[dict]:
    """Return the .ipynb as a plain dict (nbformat 4), or None if not yet synced."""
    nb_path = _notebook_path(notebook, cache_dir)
    if not nb_path.exists():
        return None
    with open(nb_path) as f:
        return nbformat.read(f, as_version=4)


async def sync_all(cache_dir: str) -> None:
    config = get_config()
    tasks = [async_sync_notebook(nb, cache_dir) for nb in config.notebooks]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    ok = sum(1 for r in results if r is not None and not isinstance(r, Exception))
    log.info("Sync complete: %d/%d notebooks available", ok, len(config.notebooks))
