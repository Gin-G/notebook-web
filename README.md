# notebook-web

A self-hosted Jupyter notebook gallery. Notebooks are rendered in a custom web UI — no JupyterLab, no Binder. Users click a notebook, a Kubernetes pod starts with a pre-built environment, and they run cells directly in the browser.

## How it works

1. **Gallery** — `chart/values.yaml` defines the notebook catalog (repo, path, description, tags).
2. **Pre-built images** — on startup the app launches one Kubernetes Job per unique `(repo, ref)` pair. Each job runs [`repo2docker`](https://repo2docker.readthedocs.io/) inside a Docker-in-Docker container, builds an image with all notebook dependencies baked in, and pushes it to your registry. Results are cached in a ConfigMap so restarts don't rebuild unnecessarily.
3. **Session pods** — clicking "Launch" creates a K8s pod: a `notebook-fetcher` init container clones the repo, then the main `jupyter` container starts instantly from the pre-built image.
4. **Kernel proxy** — the FastAPI app proxies WebSocket traffic from the browser to the Jupyter kernel running in the pod.

## Adding a notebook

Edit `chart/values.yaml` and configure a push registry so the app can build and cache images:

```yaml
build:
  registry: ghcr.io/org/notebook-web/sessions
  pushSecretName: ghcr-push   # kubectl create secret docker-registry ...

notebooks:
  - name: "My Analysis"
    repo: "https://github.com/org/repo.git"
    ref: "main"
    path: "notebooks/analysis.ipynb"
    description: "What this notebook does"
    tags: ["python", "pandas"]
```

Deploy or restart the app. It will detect the new notebook, launch a repo2docker build Job, and start serving fast sessions once the image is ready. You can watch build progress with:

```bash
kubectl logs -n <namespace> -l app.kubernetes.io/managed-by=notebook-gallery-builder -f
```

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
| `build-sessions.yaml` | `chart/values.yaml` changed | Optional: pre-builds session images in CI rather than on first app startup. Useful when forking this repo to manage your own notebooks. |
