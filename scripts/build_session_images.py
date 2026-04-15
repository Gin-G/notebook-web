#!/usr/bin/env python3
"""
Build pre-baked session images for every unique (repo, ref) pair in
chart/values.yaml using repo2docker, then write the resulting image
name back into each notebook entry's `image:` field.

Usage (called by GitHub Actions):
    python scripts/build_session_images.py

Requires:
    pip install ruamel.yaml jupyter-repo2docker

Environment variables:
    REGISTRY   — image name prefix, e.g. ghcr.io/gin-g/notebook-web/sessions
                 Defaults to ghcr.io/<GITHUB_REPOSITORY>/sessions
    GITHUB_REPOSITORY — set automatically by GHA
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parent.parent
VALUES_PATH = REPO_ROOT / "chart" / "values.yaml"


def image_name(registry: str, repo: str, ref: str) -> str:
    """Derive a stable, registry-compatible image tag from repo+ref."""
    # Strip scheme and special chars to get a short slug
    slug = repo.replace("https://", "").replace("http://", "")
    slug = slug.rstrip("/").rstrip(".git")
    # keep only alphanumeric, dots, dashes, underscores, forward-slashes
    import re
    slug = re.sub(r"[^a-zA-Z0-9._/-]", "-", slug)
    # collapse consecutive separators
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    # take the last two path components to keep it readable
    parts = [p for p in slug.split("/") if p]
    short = "-".join(parts[-2:]) if len(parts) >= 2 else parts[0]
    return f"{registry}/{short}:{ref}"


def run_repo2docker(repo: str, ref: str, image: str) -> None:
    cmd = [
        "jupyter-repo2docker",
        "--no-run",
        "--push",
        f"--image-name={image}",
        f"--ref={ref}",
        repo,
    ]
    print(f"\n==> Building {image}")
    print(f"    repo={repo}  ref={ref}")
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        print(f"ERROR: repo2docker failed for {repo}@{ref} (exit {result.returncode})")
        sys.exit(result.returncode)


def main() -> None:
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096  # prevent unwanted line-wrapping

    with VALUES_PATH.open() as f:
        data = yaml.load(f)

    registry = os.environ.get("REGISTRY", "")
    if not registry:
        gh_repo = os.environ.get("GITHUB_REPOSITORY", "")
        if not gh_repo:
            print("ERROR: set REGISTRY env var or run inside GitHub Actions")
            sys.exit(1)
        registry = f"ghcr.io/{gh_repo}/sessions"

    notebooks = data.get("notebooks") or []
    if not notebooks:
        print("No notebooks defined in values.yaml — nothing to build.")
        return

    # Group notebooks by (repo, ref) so we only build once per unique pair
    seen: dict[tuple[str, str], str] = {}  # (repo, ref) -> image name
    for nb in notebooks:
        repo = nb.get("repo", "")
        ref = nb.get("ref", "main")
        if not repo:
            continue
        key = (repo, ref)
        if key not in seen:
            seen[key] = image_name(registry, repo, ref)

    print(f"Found {len(seen)} unique repo+ref pair(s) to build:")
    for (repo, ref), img in seen.items():
        print(f"  {repo}@{ref} -> {img}")

    # Build each unique image
    for (repo, ref), img in seen.items():
        run_repo2docker(repo, ref, img)

    # Write image refs back into each notebook entry
    for nb in notebooks:
        repo = nb.get("repo", "")
        ref = nb.get("ref", "main")
        key = (repo, ref)
        if key in seen:
            nb["image"] = seen[key]

    with VALUES_PATH.open("w") as f:
        yaml.dump(data, f)

    print(f"\nUpdated {VALUES_PATH}")


if __name__ == "__main__":
    main()
