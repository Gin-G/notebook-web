"""Git clone/pull notebooks and render nbconvert HTML previews."""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import List, Optional

import git
import nbformat
from nbconvert import HTMLExporter
from nbconvert.preprocessors import ClearOutputPreprocessor

from .config import NotebookEntry, get_config

log = logging.getLogger(__name__)


def _repo_dir(notebook: NotebookEntry, cache_dir: str) -> Path:
    return Path(cache_dir) / (notebook.cacheId or notebook.id) / "repo"


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


def find_env_file(notebook: NotebookEntry, cache_dir: str) -> Optional[Path]:
    """Return the path to the env/requirements file on disk, or None if not found."""
    repo_dir = _repo_dir(notebook, cache_dir)
    nb_dir = str(notebook.path).rsplit("/", 1)[0] if "/" in notebook.path else "."

    if notebook.envFile:
        p = repo_dir / notebook.envFile
        return p if p.exists() else None

    candidates = [
        repo_dir / nb_dir / "requirements.txt",
        repo_dir / "requirements.txt",
        repo_dir / nb_dir / "environment.yml",
        repo_dir / "environment.yml",
        repo_dir / nb_dir / "environment.yaml",
        repo_dir / "environment.yaml",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def get_notebook_json(notebook: NotebookEntry, cache_dir: str) -> Optional[dict]:
    """Return the .ipynb as a plain dict (nbformat 4), or None if not yet synced."""
    nb_path = _notebook_path(notebook, cache_dir)
    if not nb_path.exists():
        return None
    with open(nb_path) as f:
        return nbformat.read(f, as_version=4)


def _notebook_title(nb_path: Path) -> str:
    """Extract a human-readable title from a notebook, falling back to the filename stem."""
    try:
        with open(nb_path) as f:
            nb = nbformat.read(f, as_version=4)
        title = nb.get("metadata", {}).get("title", "")
        if title:
            return title
        for cell in nb.cells:
            if cell.cell_type == "markdown":
                for line in cell.source.splitlines():
                    if line.startswith("#"):
                        return line.lstrip("#").strip()
                break
    except Exception:
        pass
    return nb_path.stem.replace("-", " ").replace("_", " ").title()


def discover_notebooks(notebook: NotebookEntry, cache_dir: str) -> List[NotebookEntry]:
    """If notebook.path is a directory, return one entry per .ipynb found. Otherwise return [notebook]."""
    repo_dir = _repo_dir(notebook, cache_dir)
    target = repo_dir / notebook.path

    if not target.exists() or not target.is_dir():
        return [notebook]

    children = []
    for nb_path in sorted(target.rglob("*.ipynb")):
        if ".ipynb_checkpoints" in nb_path.parts:
            continue
        rel_path = nb_path.relative_to(repo_dir)
        slug = re.sub(r"[^a-z0-9]+", "-", nb_path.stem.lower()).strip("-")
        children.append(NotebookEntry(
            id=f"{notebook.id}-{slug}",
            cacheId=notebook.id,
            name=_notebook_title(nb_path),
            repo=notebook.repo,
            ref=notebook.ref,
            path=str(rel_path),
            tags=notebook.tags,
            description=notebook.description,
            resources=notebook.resources,
        ))

    if not children:
        log.warning("Directory %s contains no .ipynb files", target)

    return children


async def sync_all(cache_dir: str) -> List[NotebookEntry]:
    config = get_config()
    tasks = [async_sync_notebook(nb, cache_dir) for nb in config.notebooks]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    ok = sum(1 for r in results if r is not None and not isinstance(r, Exception))
    log.info("Sync complete: %d/%d source(s) available", ok, len(config.notebooks))

    expanded: List[NotebookEntry] = []
    for nb in config.notebooks:
        expanded.extend(discover_notebooks(nb, cache_dir))
    log.info("Expanded to %d notebook(s) after directory discovery", len(expanded))
    return expanded
