# notebook-web

A self-hosted Jupyter notebook gallery. Notebooks are rendered in a custom web UI — no JupyterLab, no Binder. Users click a notebook, a Kubernetes pod starts with a pre-built environment, and they run cells directly in the browser.

## How it works

1. **Gallery** — `chart/values.yaml` defines the notebook catalog (repo, path, description, tags).
2. **Session pods** — clicking "Launch" creates a K8s pod: a `notebook-fetcher` init container clones the repo and copies the `.ipynb` file, then the main `jupyter` container starts with a pre-built image containing all dependencies.
3. **Pre-built images** — the `build-sessions` CI workflow runs [`repo2docker`](https://repo2docker.readthedocs.io/) for each unique `(repo, ref)` pair whenever `values.yaml` changes. The resulting image names are committed back into `values.yaml` automatically. Sessions start instantly without any runtime package installation.
4. **Kernel proxy** — the FastAPI app proxies WebSocket traffic from the browser to the Jupyter kernel running in the pod.

## Adding a notebook

Edit `chart/values.yaml`:

```yaml
notebooks:
  - name: "My Analysis"
    repo: "https://github.com/org/repo.git"
    ref: "main"
    path: "notebooks/analysis.ipynb"
    description: "What this notebook does"
    tags: ["python", "pandas"]
    # image: ""   # auto-populated by CI after first build
```

Commit and push. The `build-sessions` workflow will detect the change, build a session image via repo2docker, and write the image name back to `values.yaml`.

### Environment detection

repo2docker picks up dependencies automatically from standard files in the repo root or a `binder/` directory:

| File | Handled by |
|---|---|
| `environment.yml` | conda |
| `requirements.txt` | pip |
| `binder/environment.yml` | conda |
| `setup.py` / `pyproject.toml` | pip |

See the [repo2docker documentation](https://repo2docker.readthedocs.io/en/latest/usage/languages.html) for the full list.

## Deployment

```bash
helm upgrade --install notebook-web ./chart \
  --namespace notebook-web --create-namespace \
  -f chart/values.yaml
```

The app image is built and pushed to GHCR on every merge to `main`. The Helm chart `image.tag` is updated automatically by CI.

## Local development

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Config is loaded from `chart/values.yaml` when running locally.

## CI workflows

| Workflow | Trigger | What it does |
|---|---|---|
| `build.yaml` | push to `main` | Builds and pushes the app Docker image; updates `chart/values.yaml` image tag |
| `build-sessions.yaml` | `chart/values.yaml` changed | Runs repo2docker for each unique `(repo, ref)`; pushes session images to GHCR; commits image refs back to `values.yaml` |
