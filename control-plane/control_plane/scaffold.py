"""Server-side scaffolding for user agent projects.

The user source repo only receives ``agent.py`` + ``a2a.yaml`` +
``requirements.txt`` and related source files. The control plane stamps
platform-internal pieces (Dockerfile, Gitea workflow, k8s manifests) into a
separate runtime repo that users do not edit.
"""
from __future__ import annotations

import os
import json
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import yaml

from .agent_secret_names import agent_runtime_secret_name
from .config import settings
from .database_resources import read_agent_database_declarations
from .gitea import is_user_source_path
from .k8s import KNATIVE_MAX_TIMEOUT_SECONDS, agent_min_scale, default_agent_timeout_seconds
from .resources import parse_cpu, parse_memory

AVAILABILITY_ON_DEMAND = "on_demand"
AVAILABILITY_ALWAYS_ON = "always_on"
STATIC_FRONTEND_KINDS = {"static", "static-spa", "spa"}
SERVER_RENDERED_FRONTEND_KIND = "server-rendered"
SERVER_RENDERED_FRONTEND_KINDS = {SERVER_RENDERED_FRONTEND_KIND, "server", "ssr"}
DEFAULT_FRONTEND_SERVER_PORT = 3000

DOCKERFILE = """\
{frontend_stage}\
FROM registry.a2acloud.io/a2a/a2a-pack-base:{base_image_tag}

{apt_block}WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

{feature_block}\
COPY . .
{frontend_runtime}

ENV A2A_ENTRYPOINT={entrypoint}
ENV PORT=8000
EXPOSE 8000

# Exec form (JSON array) so python/uvicorn is PID 1 and receives SIGTERM
# directly. Shell form leaves /bin/sh as PID 1, which doesn't forward the
# signal, so uvicorn never shuts down and the pod waits the full
# terminationGracePeriod (SIGKILL). Exec form: idle pods exit in ~1s; in-flight
# requests drain via uvicorn graceful shutdown instead of being cut.
CMD ["a2a", "run", "--entrypoint", "{entrypoint}", "--host", "0.0.0.0", "--port", "8000"]
"""

SIDECAR_DOCKERFILE = """\
{frontend_stage}\
FROM registry.a2acloud.io/a2a/{sidecar_base_image}:{base_image_tag}

{apt_block}WORKDIR /app

COPY . .
RUN {sidecar_build_command}

{feature_block}\
{frontend_runtime}
ENV A2A_DSL_PATH=/app/.a2a/agent.dsl.json
ENV A2A_WORKER_PORT=9001
ENV PORT=8000
EXPOSE 8000

RUN cat > /usr/local/bin/a2a-sidecar-entrypoint <<'EOF'
#!/bin/sh
set -eu
{worker_command} &
worker_pid=$!
trap 'kill "$worker_pid" 2>/dev/null || true; wait "$worker_pid" 2>/dev/null || true' INT TERM EXIT
a2a sidecar --dsl "$A2A_DSL_PATH" --worker-url "http://127.0.0.1:${{A2A_WORKER_PORT:-9001}}" --host 0.0.0.0 --port "${{PORT:-8000}}"
EOF
RUN chmod +x /usr/local/bin/a2a-sidecar-entrypoint

CMD ["a2a-sidecar-entrypoint"]
"""

_SIDECAR_BUILD_MATRIX = {
    "typescript": ("a2a-sidecar-node", "a2a-sidecar-build typescript"),
    "javascript": ("a2a-sidecar-node", "a2a-sidecar-build javascript"),
    "go": ("a2a-sidecar-go", "a2a-sidecar-build go"),
    "rust": ("a2a-sidecar-rust", "a2a-sidecar-build rust"),
    "java": ("a2a-sidecar-jvm", "a2a-sidecar-build java"),
    "dotnet": ("a2a-sidecar-dotnet", "a2a-sidecar-build dotnet"),
}


@dataclass(frozen=True)
class _BuildPlan:
    language: str
    sidecar: bool
    sidecar_base_image: str | None = None
    sidecar_build_command: str | None = None
    worker_command: str | None = None

# Belt + suspenders on the apt package allowlist. The card schema also
# enforces this; we revalidate here because the scaffold is the last hop
# before user-supplied strings land in a shell command inside the build
# job. Anything that doesn't match is dropped silently.
_APT_PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9.+\-]{1,63}$")
_CPU_RESOURCE_RE = re.compile(r"^\d+(?:\.\d+)?m?$")
_MEMORY_RESOURCE_RE = re.compile(
    r"^\d+(?:\.\d+)?(?:Ki|Mi|Gi|Ti|Pi|Ei|K|M|G|T|P|E)?$"
)
_SUPPORTED_RUNTIME_FEATURES = {"codegraph"}
_CODEGRAPH_APT_PACKAGES = ("ca-certificates", "curl")

CODEGRAPH_FEATURE_BLOCK = """\
RUN curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \\
      https://github.com/colbymchenry/codegraph/releases/download/v0.9.4/codegraph-linux-x64.tar.gz \\
      -o /tmp/codegraph.tar.gz \\
    && echo 'af4dfe25c17868d2260cec243e702de088738ba8bf379962bed8296362eb0c8a  /tmp/codegraph.tar.gz' | sha256sum -c - \\
    && mkdir -p /opt/codegraph \\
    && tar -xzf /tmp/codegraph.tar.gz -C /opt/codegraph --strip-components=1 \\
    && rm -f /tmp/codegraph.tar.gz \\
    && ln -sf /opt/codegraph/bin/codegraph /usr/local/bin/codegraph \\
    && codegraph --version

"""


def _sanitize_apt_packages(raw: object) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, str):
            continue
        name = entry.strip().lower()
        if not _APT_PACKAGE_RE.match(name):
            continue
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
        if len(out) >= 32:
            break
    return out


def _read_runtime_config(workdir: Path) -> dict[str, object]:
    """Pull ``runtime`` out of the user's ``a2a.yaml``.

    Returning an empty dict when the file is missing / malformed is
    deliberate: we don't want a typo to break the build, just fall back
    to the stock base image."""
    yaml_path = workdir / "a2a.yaml"
    if not yaml_path.exists():
        return {}
    try:
        data = yaml.safe_load(yaml_path.read_text()) or {}
    except yaml.YAMLError:
        return {}
    rt = (data or {}).get("runtime") or {}
    return rt if isinstance(rt, dict) else {}


def _read_frontend_config(workdir: Path) -> dict[str, object] | None:
    """Read the optional ``frontend`` declaration from ``a2a.yaml``."""

    yaml_path = workdir / "a2a.yaml"
    if not yaml_path.exists():
        return None
    data = yaml.safe_load(yaml_path.read_text()) or {}
    raw = data.get("frontend") if isinstance(data, dict) else None
    if raw in (None, False):
        return None
    if raw is True:
        raw = {}
    if isinstance(raw, str):
        raw = {"path": raw}
    if not isinstance(raw, dict):
        raise ValueError("frontend must be an object, string path, true, or false")
    kind = _clean_frontend_kind(raw.get("type") or raw.get("kind") or "static")
    path = _require_frontend_relpath(raw.get("path") or "frontend")
    mount = _clean_mount(raw.get("mount") or "/")
    auth = str(raw.get("auth") or "inherit").strip().lower()
    if auth not in {"inherit", "platform", "public"}:
        auth = "inherit"
    if auth == "platform" and not settings.allow_platform_frontend_auth:
        raise ValueError(
            "frontend.auth=platform is disabled on hosted deployments until "
            "an origin-bound browser session gateway is configured"
        )
    build = _clean_build(raw.get("build"))
    docs_url = str(raw.get("docs_url") or raw.get("docsUrl") or "https://docs.a2acloud.io/").strip()
    cfg: dict[str, object] = {
        "type": kind,
        "path": path,
        "mount": mount,
        "auth": auth,
        "build": build,
        "docs_url": docs_url or "https://docs.a2acloud.io/",
    }
    if kind == SERVER_RENDERED_FRONTEND_KIND:
        framework = str(raw.get("framework") or "nextjs").strip().lower()
        if framework != "nextjs":
            raise ValueError("frontend.framework currently supports only nextjs")
        start = _clean_build(raw.get("start")) or "node server.js"
        if not build:
            raise ValueError("server-rendered frontend requires frontend.build")
        cfg.update({
            "framework": framework,
            "start": start,
            "port": _clean_frontend_port(raw.get("port")),
        })
        return cfg
    cfg["dist"] = _require_frontend_relpath(raw.get("dist") or "dist")
    return cfg


def _clean_frontend_kind(raw: object) -> str:
    value = str(raw or "static").strip().lower()
    if value in STATIC_FRONTEND_KINDS:
        return value
    if value in SERVER_RENDERED_FRONTEND_KINDS:
        return SERVER_RENDERED_FRONTEND_KIND
    raise ValueError("frontend.type must be static-spa or server-rendered")


def _clean_frontend_port(raw: object) -> int:
    if raw in (None, ""):
        return DEFAULT_FRONTEND_SERVER_PORT
    try:
        port = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("frontend.port must be an integer") from exc
    if port < 1 or port > 65535:
        raise ValueError("frontend.port must be between 1 and 65535")
    return port


def _require_frontend_relpath(raw: object) -> str:
    value = str(raw or "").strip().replace("\\", "/").strip("/")
    parts = Path(value).parts
    if not value or value == "." or ".." in parts:
        raise ValueError(f"unsafe frontend path: {raw!r}")
    return value


def _clean_relpath(raw: object, *, default: str) -> str:
    value = str(raw or "").strip().replace("\\", "/").strip("/")
    parts = Path(value).parts
    if not value or value == "." or ".." in parts:
        return default
    return value


def _clean_mount(raw: object) -> str:
    value = str(raw or "/").strip() or "/"
    if not value.startswith("/"):
        value = f"/{value}"
    return value.rstrip("/") if len(value) > 1 else value


def _clean_build(raw: object) -> str:
    value = str(raw or "").strip()
    return " ".join(value.splitlines())


def _read_apt_packages(workdir: Path) -> list[str]:
    """Pull ``runtime.apt_packages`` out of the user's ``a2a.yaml``."""
    rt = _read_runtime_config(workdir)
    return _sanitize_apt_packages(rt.get("apt_packages"))


def _read_runtime_features(workdir: Path) -> list[str]:
    """Pull supported ``runtime.features`` out of the user's ``a2a.yaml``."""
    rt = _read_runtime_config(workdir)
    raw = rt.get("features")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        name = item.strip().lower()
        if name in _SUPPORTED_RUNTIME_FEATURES and name not in out:
            out.append(name)
    return out


def _read_runtime_availability(workdir: Path) -> str:
    rt = _read_runtime_config(workdir)
    return _clean_runtime_availability(rt.get("availability"))


def _clean_runtime_availability(raw: object) -> str:
    value = str(raw or AVAILABILITY_ON_DEMAND).strip().lower()
    if value not in {AVAILABILITY_ON_DEMAND, AVAILABILITY_ALWAYS_ON}:
        raise ValueError(
            "runtime.availability must be 'on_demand' or 'always_on'"
        )
    return value


def read_runtime_availability_from_tarball(tar_path: str) -> str:
    with tarfile.open(tar_path, "r:gz") as tar:
        members = tar.getmembers()
        roots = {m.name.split("/")[0] for m in members if m.name}
        common_root = next(iter(roots)) if len(roots) == 1 else None
        for member in members:
            name = _source_member_name(member.name, common_root)
            if name != "a2a.yaml" or not member.isfile():
                continue
            src = tar.extractfile(member)
            if src is None:
                return AVAILABILITY_ON_DEMAND
            data = yaml.safe_load(src.read().decode("utf-8")) or {}
            runtime = data.get("runtime") if isinstance(data, dict) else None
            raw = runtime.get("availability") if isinstance(runtime, dict) else None
            return _clean_runtime_availability(raw)
    return AVAILABILITY_ON_DEMAND


def _apt_packages_with_feature_deps(
    packages: list[str],
    features: list[str],
) -> list[str]:
    out = list(packages)
    seen = set(out)
    if "codegraph" in features:
        for package in _CODEGRAPH_APT_PACKAGES:
            if package not in seen:
                out.append(package)
                seen.add(package)
    return out


def _feature_block(features: list[str]) -> str:
    if "codegraph" in features:
        return CODEGRAPH_FEATURE_BLOCK
    return ""


def _sanitize_cpu_resource(raw: object) -> str | None:
    value = str(raw or "").strip()
    if not value or not _CPU_RESOURCE_RE.match(value):
        return None
    millicores = parse_cpu(value)
    if millicores <= 0:
        return None
    # Match Kubernetes' canonical quantity representation so Argo CD does not
    # report semantic-only drift (for example, ``2000m`` versus ``2``).
    if millicores.is_integer():
        whole_millicores = int(millicores)
        if whole_millicores % 1000 == 0:
            return str(whole_millicores // 1000)
        return f"{whole_millicores}m"
    return value


def _sanitize_memory_resource(raw: object) -> str | None:
    value = str(raw or "").strip()
    if not value or not _MEMORY_RESOURCE_RE.match(value):
        return None
    return value if parse_memory(value) > 0 else None


def _sanitize_positive_int(raw: object, *, default: int, maximum: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value < 1:
        return default
    return min(value, maximum)


def _positive_int_or_none(raw: object) -> int | None:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _declared_runtime_timeout(
    workdir: Path,
    rt: dict[str, object],
    resources: dict[str, object],
) -> int:
    candidates: list[int] = []
    for raw in (
        rt.get("max_runtime_seconds"),
        resources.get("max_runtime_seconds"),
    ):
        value = _positive_int_or_none(raw)
        if value is not None:
            candidates.append(value)

    try:
        dsl = _read_agent_dsl(workdir)
    except ValueError:
        dsl = {}
    skills = dsl.get("skills") if isinstance(dsl, dict) else None
    if isinstance(skills, list):
        for skill in skills:
            if not isinstance(skill, dict):
                continue
            policy = skill.get("policy")
            if not isinstance(policy, dict):
                continue
            for key in ("timeout_seconds", "grant_run_timeout_seconds"):
                value = _positive_int_or_none(policy.get(key))
                if value is not None:
                    candidates.append(value)

    if not candidates:
        return default_agent_timeout_seconds()
    return min(max(candidates), KNATIVE_MAX_TIMEOUT_SECONDS)


def _read_k8s_resources(workdir: Path) -> dict[str, str]:
    """Read deployment resources from ``runtime.resources`` in ``a2a.yaml``.

    The card's Python ``Resources`` declaration is not available at scaffold
    time without importing user code before its image is built, so resource
    sizing has to be mirrored into ``a2a.yaml`` for Gitea/Argo deployments.
    """
    resources = _read_runtime_config(workdir).get("resources")
    if not isinstance(resources, dict):
        resources = {}
    cpu = _sanitize_cpu_resource(resources.get("cpu"))
    memory = _sanitize_memory_resource(resources.get("memory"))
    rt = _read_runtime_config(workdir)
    concurrency = _sanitize_positive_int(rt.get("concurrency"), default=100, maximum=100)
    timeout_seconds = _declared_runtime_timeout(workdir, rt, resources)
    lifecycle = str(rt.get("lifecycle") or "ephemeral").strip().lower()
    return {
        # CPU request is a small fixed RESERVATION (agents are I/O-bound and
        # idle ~0 CPU); the declared cpu becomes the burst LIMIT, not a
        # reservation. Keeps the single node from filling on reservations no
        # one uses (real cluster CPU use ~21% vs ~98% requested before this).
        "request_cpu": "50m",
        "request_memory": memory or "256Mi",
        "limit_cpu": cpu or "1",
        "limit_memory": memory or "512Mi",
        "container_concurrency": str(concurrency),
        "timeout_seconds": str(timeout_seconds),
        "response_start_timeout_seconds": str(timeout_seconds),
        "min_scale": "1" if lifecycle == "warm" else "0",
    }


def _apt_block(packages: list[str]) -> str:
    if not packages:
        return ""
    joined = " ".join(packages)
    return (
        "RUN apt-get update \\\n"
        "    && apt-get install -y --no-install-recommends "
        f"{joined} \\\n"
        "    && rm -rf /var/lib/apt/lists/*\n\n"
    )


def _docker_env(name: str, value: object) -> str:
    return f"ENV {name}={json.dumps(str(value))}\n"


def _frontend_blocks(frontend: dict[str, object] | None) -> tuple[str, str]:
    if frontend is None:
        return "", ""
    if frontend.get("type") == SERVER_RENDERED_FRONTEND_KIND:
        server_dir = "/app/.a2a/frontend-server"
        port = int(frontend.get("port") or DEFAULT_FRONTEND_SERVER_PORT)
        runtime_env = (
            _docker_env("A2A_FRONTEND_KIND", "server-rendered")
            + _docker_env("A2A_FRONTEND_FRAMEWORK", frontend.get("framework") or "nextjs")
            + _docker_env("A2A_FRONTEND_MOUNT", frontend["mount"])
            + _docker_env("A2A_FRONTEND_AUTH", frontend["auth"])
            + _docker_env("A2A_FRONTEND_DOCS_URL", frontend["docs_url"])
            + _docker_env("A2A_FRONTEND_PROXY_URL", f"http://127.0.0.1:{port}")
            + _docker_env("A2A_FRONTEND_START", frontend.get("start") or "node server.js")
            + _docker_env("A2A_FRONTEND_WORKDIR", server_dir)
            + _docker_env("A2A_FRONTEND_PORT", port)
        )
        stage = (
            "FROM node:20-bookworm-slim AS frontend-build\n"
            "WORKDIR /frontend\n"
            f"COPY {frontend['path']}/ ./\n"
            "RUN if [ -f package-lock.json ]; then npm ci; "
            "elif [ -f yarn.lock ]; then corepack enable && yarn install --frozen-lockfile; "
            "elif [ -f pnpm-lock.yaml ]; then corepack enable && pnpm install --frozen-lockfile; "
            "else npm install; fi\n"
            f"RUN {frontend['build']}\n\n"
        )
        runtime = (
            "\nCOPY --from=frontend-build /usr/local/bin/node /usr/local/bin/node\n"
            f"COPY --from=frontend-build /frontend/.next/standalone {server_dir}\n"
            f"COPY --from=frontend-build /frontend/.next/static {server_dir}/.next/static\n"
            f"COPY --from=frontend-build /frontend/public {server_dir}/public\n"
            f"{runtime_env}"
        )
        return stage, runtime
    dist_env = "/app/.a2a/frontend"
    runtime_env = (
        f"ENV A2A_FRONTEND_DIST={dist_env}\n"
        f"ENV A2A_FRONTEND_MOUNT={frontend['mount']}\n"
        f"ENV A2A_FRONTEND_AUTH={frontend['auth']}\n"
        f"ENV A2A_FRONTEND_DOCS_URL={frontend['docs_url']}\n"
    )
    build = frontend.get("build") or ""
    if build:
        source_dist = f"{frontend['path']}/{frontend['dist']}".strip("/")
        stage = (
            "FROM node:20-bookworm-slim AS frontend-build\n"
            "WORKDIR /frontend\n"
            f"COPY {frontend['path']}/ ./\n"
            "RUN if [ -f package-lock.json ]; then npm ci; "
            "elif [ -f yarn.lock ]; then corepack enable && yarn install --frozen-lockfile; "
            "elif [ -f pnpm-lock.yaml ]; then corepack enable && pnpm install --frozen-lockfile; "
            "else npm install; fi\n"
            f"RUN {build}\n\n"
        )
        runtime = (
            f"\nCOPY --from=frontend-build /frontend/{frontend['dist']} {source_dist}\n"
            f"\nCOPY --from=frontend-build /frontend/{frontend['dist']} {dist_env}\n"
            f"{runtime_env}"
        )
        return stage, runtime
    runtime = (
        f"\nCOPY {frontend['path']}/{frontend['dist']}/ {dist_env}/\n"
        f"{runtime_env}"
    )
    return "", runtime


def _validate_frontend_source(source: Path, frontend: dict[str, object] | None) -> None:
    if frontend is None:
        return
    if frontend.get("type") == SERVER_RENDERED_FRONTEND_KIND:
        if not (source / str(frontend["path"])).is_dir():
            raise ValueError(f"frontend.path does not exist: {frontend['path']}")
        return
    if frontend.get("build"):
        return
    index = source / str(frontend["path"]) / str(frontend["dist"]) / "index.html"
    if not index.is_file():
        raise ValueError(
            "frontend dist is missing index.html; run the frontend build "
            "or set frontend.build"
        )

WORKFLOW = """\
name: build
on:
  push:
    branches: [main]
    paths-ignore:
      - 'deploy/**'

jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    env:
      SOURCE_REPO_URL: {source_repo_url}
      SOURCE_SHA: "{source_sha}"
      IMAGE_TAG: "{image_tag}"
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683
        with:
          fetch-depth: 0

      - name: checkout source
        env:
          SOURCE_REPO_USERNAME: ${{{{ secrets.SOURCE_REPO_USERNAME }}}}
          SOURCE_REPO_PASSWORD: ${{{{ secrets.SOURCE_REPO_PASSWORD }}}}
        run: |
          set -e
          test -n "$SOURCE_REPO_USERNAME"
          test -n "$SOURCE_REPO_PASSWORD"
          rm -rf src
          SOURCE_AUTH="$(printf '%s:%s' "$SOURCE_REPO_USERNAME" "$SOURCE_REPO_PASSWORD" | base64 | tr -d '\\n')"
          git -c http.extraHeader="Authorization: Basic $SOURCE_AUTH" clone --quiet "$SOURCE_REPO_URL" src
          unset SOURCE_AUTH SOURCE_REPO_PASSWORD
          git -C src checkout --quiet "$SOURCE_SHA"
          cp .dockerignore src/.dockerignore

      - name: build image
        env:
          REGISTRY_USERNAME: ${{{{ secrets.REGISTRY_USERNAME }}}}
          REGISTRY_PASSWORD: ${{{{ secrets.REGISTRY_PASSWORD }}}}
        run: |
          set -e
          test "$REGISTRY_USERNAME" = registry-push
          test -n "$REGISTRY_PASSWORD"
          trap 'docker logout registry.a2acloud.io >/dev/null 2>&1 || true' EXIT
          printf '%s' "$REGISTRY_PASSWORD" | docker login registry.a2acloud.io \
            --username "$REGISTRY_USERNAME" --password-stdin
          IMG=registry.a2acloud.io/agents/{name}
          docker build --pull -f Dockerfile -t "$IMG:$IMAGE_TAG" -t "$IMG:latest" src
          push_output="$(docker push "$IMG:$IMAGE_TAG" 2>&1 | tee /dev/stderr)"
          docker push "$IMG:latest"
          digest="$(printf '%s\\n' "$push_output" \
            | sed -n 's/.*digest: \\(sha256:[0-9a-f]*\\).*/\\1/p' \
            | tail -1)"
          [[ "$digest" =~ ^sha256:[0-9a-f]{{64}}$ ]]
          PINNED_IMAGE="$IMG:$IMAGE_TAG@$digest"
          docker manifest inspect --verbose "$PINNED_IMAGE" >/dev/null
          printf '%s\\n' "$PINNED_IMAGE" >.a2a-pushed-image

      - name: bump deploy manifest
        run: |
          IMG=registry.a2acloud.io/agents/{name}
          PINNED_IMAGE="$(cat .a2a-pushed-image)"
          rm -f .a2a-pushed-image
          [[ "$PINNED_IMAGE" =~ ^$IMG:[^@[:space:]]+@sha256:[0-9a-f]{{64}}$ ]]
          sed -i "s|image: $IMG:.*|image: $PINNED_IMAGE|" deploy/20-deployment.yaml
          sed -i "s|value: $IMG:.*|value: $PINNED_IMAGE|" deploy/20-deployment.yaml
          sed -i "s|a2a/rebuild-id: .*|a2a/rebuild-id: \\"$GITHUB_SHA\\"|" deploy/20-deployment.yaml
          git config user.email "ci@a2a.local"
          git config user.name "ci"
          git add deploy/20-deployment.yaml
          if git diff --staged --quiet; then
            echo "no manifest changes"
          else
            git commit -m "ci: bump image to $SOURCE_SHA"
            for attempt in 1 2 3; do
              git fetch origin main
              git rebase -X theirs FETCH_HEAD
              if git push origin HEAD:main; then
                exit 0
              fi
              git rebase --abort || true
              sleep "$((attempt * 2))"
            done
            exit 1
          fi
"""

DEPLOYMENT = """\
apiVersion: serving.knative.dev/v1
kind: Service
metadata:
  name: {name}
  namespace: agents
  annotations:
    argocd.argoproj.io/sync-options: ServerSideApply=true
    argocd.argoproj.io/tracking-id: {name}:serving.knative.dev/Service:agents/{name}
  labels:
    app: {name}
    a2a/managed-by: control-plane
spec:
  template:
    metadata:
      labels:
        app: {name}
        a2a/workload-class: user
      annotations:
        autoscaling.knative.dev/min-scale: "{min_scale}"
        autoscaling.knative.dev/max-scale: "{max_scale}"
        autoscaling.knative.dev/target: "{container_concurrency}"
        a2a/rebuild-id: "{image_tag}"
    spec:
      automountServiceAccountToken: false
      enableServiceLinks: false
      imagePullSecrets:
        - name: registry-pull-credentials
      containerConcurrency: {container_concurrency}
      timeoutSeconds: {timeout_seconds}
      responseStartTimeoutSeconds: {response_start_timeout_seconds}
      containers:
        - name: agent
          image: registry.a2acloud.io/agents/{name}:{image_tag}
          imagePullPolicy: Always
          env:
            - name: A2A_AGENT_NAME
              value: "{name}"
            - name: A2A_AGENT_IMAGE
              value: registry.a2acloud.io/agents/{name}:{image_tag}
            - name: A2A_RENDER_IMAGE
              value: registry.a2acloud.io/agents/{name}:{image_tag}
            - name: A2A_CP_URL
              value: "{cp_url}"
            - name: A2A_LOGIN_URL
              value: "{login_url}"
            - name: A2A_SESSION_COOKIE_NAME
              value: "{session_cookie_name}"
            - name: A2A_AGENT_SESSION_AUTHORIZE_URL
              value: "{agent_session_authorize_url}"
            - name: A2A_EVIDENCE_SIGNING_MODE
              value: gateway
            - name: A2A_GRANT_VERIFYING_KEY
              valueFrom:
                secretKeyRef: {{name: platform-secrets, key: grant_verifying_key}}
            - name: A2A_SANDBOX_URL
              value: http://sandbox.sandbox.svc.cluster.local:8000
            - name: A2A_SANDBOX_TIMEOUT_S
              value: "180"
          envFrom:
            - secretRef:
                name: {secret_name}
                optional: true
          ports:
            - containerPort: 8000
              name: http1
              protocol: TCP
          startupProbe:
            httpGet: {{path: /healthz, port: 8000}}
            periodSeconds: 5
            timeoutSeconds: 3
            failureThreshold: 60
          readinessProbe:
            httpGet: {{path: /healthz, port: 8000}}
            periodSeconds: 5
            timeoutSeconds: 3
            failureThreshold: 6
            successThreshold: 1
          resources:
            requests: {{cpu: "{request_cpu}", memory: "{request_memory}"}}
            limits: {{cpu: "{limit_cpu}", memory: "{limit_memory}"}}
"""

ALWAYS_ON_DEPLOYMENT = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {name}
  namespace: agents
  annotations:
    argocd.argoproj.io/tracking-id: {name}:apps/Deployment:agents/{name}
  labels:
    app: {name}
    a2a/managed-by: control-plane
    a2a/availability: always_on
    a2a/workload-class: user
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {name}
  template:
    metadata:
      labels:
        app: {name}
        a2a/availability: always_on
        a2a/workload-class: user
      annotations:
        a2a/rebuild-id: "{image_tag}"
    spec:
      automountServiceAccountToken: false
      enableServiceLinks: false
      nodeSelector:
        a2a/worker: "true"
      containers:
        - name: agent
          image: registry.a2acloud.io/agents/{name}:{image_tag}
          imagePullPolicy: Always
          env:
            - name: A2A_AGENT_NAME
              value: "{name}"
            - name: A2A_AGENT_PUBLIC
              value: "true"
            - name: A2A_AGENT_IMAGE
              value: registry.a2acloud.io/agents/{name}:{image_tag}
            - name: A2A_RENDER_IMAGE
              value: registry.a2acloud.io/agents/{name}:{image_tag}
            - name: A2A_CP_URL
              value: "{cp_url}"
            - name: A2A_LOGIN_URL
              value: "{login_url}"
            - name: A2A_SESSION_COOKIE_NAME
              value: "{session_cookie_name}"
            - name: A2A_AGENT_SESSION_AUTHORIZE_URL
              value: "{agent_session_authorize_url}"
            - name: A2A_EVIDENCE_SIGNING_MODE
              value: gateway
            - name: A2A_GRANT_VERIFYING_KEY
              valueFrom:
                secretKeyRef: {{name: platform-secrets, key: grant_verifying_key}}
            - name: A2A_SANDBOX_URL
              value: http://sandbox.sandbox.svc.cluster.local:8000
            - name: A2A_SANDBOX_TIMEOUT_S
              value: "180"
          envFrom:
            - secretRef:
                name: {secret_name}
                optional: true
          ports:
            - containerPort: 8000
              name: http
              protocol: TCP
          readinessProbe:
            httpGet: {{path: /healthz, port: http}}
            periodSeconds: 5
            timeoutSeconds: 3
            failureThreshold: 6
            successThreshold: 1
          resources:
            requests: {{cpu: "{request_cpu}", memory: "{request_memory}"}}
            limits: {{cpu: "{limit_cpu}", memory: "{limit_memory}"}}
---
apiVersion: v1
kind: Service
metadata:
  name: {name}
  namespace: agents
  annotations:
    argocd.argoproj.io/tracking-id: {name}:/Service:agents/{name}
  labels:
    app: {name}
    a2a/managed-by: control-plane
    a2a/availability: always_on
spec:
  selector:
    app: {name}
  ports:
    - name: http
      port: 80
      targetPort: http
      protocol: TCP
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {name}
  namespace: agents
  annotations:
    argocd.argoproj.io/tracking-id: {name}:networking.k8s.io/Ingress:agents/{name}
  labels:
    app: {name}
    a2a/managed-by: control-plane
    a2a/availability: always_on
spec:
  rules:
    - host: {host}
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: agent-ingress-gateway
                port:
                  number: 80
"""

DOCKERIGNORE = "__pycache__\n*.pyc\n.venv\n.git\n.pytest_cache\nnode_modules\n.env\n.env.local\n"


def _credential_free_repo_url(value: str) -> str:
    """Return a clone URL that is safe to commit to a runtime repository."""
    parsed = urlsplit(str(value or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return str(value or "").strip()
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    if parsed.port is not None:
        netloc += f":{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _read_agent_dsl(source: Path) -> dict[str, object]:
    dsl_path = source / ".a2a" / "agent.dsl.json"
    if not dsl_path.exists():
        raise ValueError("source is missing .a2a/agent.dsl.json")
    try:
        raw = json.loads(dsl_path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid .a2a/agent.dsl.json: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(".a2a/agent.dsl.json must be a JSON object")
    return raw


def _entrypoint_command(dsl: dict[str, object]) -> str:
    entrypoint = dsl.get("entrypoint")
    command = entrypoint.get("command") if isinstance(entrypoint, dict) else None
    if not isinstance(command, list) or not command:
        raise ValueError("sidecar languages require agent_dsl.entrypoint.command")
    parts: list[str] = []
    for item in command:
        if not isinstance(item, str) or not item:
            raise ValueError("agent_dsl.entrypoint.command values must be non-empty strings")
        parts.append(shlex.quote(item))
    return " ".join(parts)


def _build_plan(source: Path) -> _BuildPlan:
    if not (source / ".a2a" / "agent.dsl.json").exists():
        return _BuildPlan(language="python", sidecar=False)
    dsl = _read_agent_dsl(source)
    language = str(dsl.get("language") or "").strip().lower()
    if language == "python":
        return _BuildPlan(language=language, sidecar=False)
    if language in _SIDECAR_BUILD_MATRIX:
        base_image, build_command = _SIDECAR_BUILD_MATRIX[language]
        return _BuildPlan(
            language=language,
            sidecar=True,
            sidecar_base_image=base_image,
            sidecar_build_command=build_command,
            worker_command=_entrypoint_command(dsl),
        )
    raise ValueError(f"unsupported agent_dsl.language: {language or '<missing>'}")


def _stamp_platform_files(
    workdir: Path,
    *,
    name: str,
    entrypoint: str,
    source_dir: Path | None = None,
    source_repo_url: str = "",
    source_sha: str = "latest",
    image_tag: str | None = None,
    base_image_tag: str = "latest",
    allow_always_on: bool = False,
) -> None:
    source = source_dir or workdir
    build_plan = _build_plan(source)
    runtime_features = _read_runtime_features(source)
    read_agent_database_declarations(source)
    apt_packages = _apt_packages_with_feature_deps(
        _read_apt_packages(source),
        runtime_features,
    )
    k8s_resources = _read_k8s_resources(source)
    availability = _read_runtime_availability(source)
    if availability == AVAILABILITY_ALWAYS_ON and not allow_always_on:
        raise PermissionError(
            "runtime.availability=always_on requires site admin provisioning"
        )
    # Author signal (warm lifecycle) and the platform always-on override both
    # raise the floor; ordinary user agents stay at min-scale 0 (scale-to-zero).
    k8s_resources["min_scale"] = str(max(int(k8s_resources["min_scale"]), agent_min_scale(name)))
    k8s_resources["max_scale"] = str(settings.agents_max_scale)
    frontend = _read_frontend_config(source)
    _validate_frontend_source(source, frontend)
    frontend_stage, frontend_runtime = _frontend_blocks(frontend)
    deploy_image_tag = image_tag or source_sha
    dockerfile_template = SIDECAR_DOCKERFILE if build_plan.sidecar else DOCKERFILE
    (workdir / "Dockerfile").write_text(
        dockerfile_template.format(
            entrypoint=entrypoint,
            base_image_tag=base_image_tag,
            apt_block=_apt_block(apt_packages),
            feature_block=_feature_block(runtime_features),
            frontend_stage=frontend_stage,
            frontend_runtime=frontend_runtime,
            sidecar_base_image=build_plan.sidecar_base_image or "",
            sidecar_build_command=build_plan.sidecar_build_command or "",
            worker_command=build_plan.worker_command or "",
        )
    )
    (workdir / ".dockerignore").write_text(DOCKERIGNORE)
    wf_dir = workdir / ".gitea" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / "build.yml").write_text(
        WORKFLOW.format(
            name=name,
            source_repo_url=json.dumps(_credential_free_repo_url(source_repo_url)),
            source_sha=source_sha,
            image_tag=deploy_image_tag,
        )
    )
    deploy_dir = workdir / "deploy"
    deploy_dir.mkdir(parents=True, exist_ok=True)
    deployment_template = (
        ALWAYS_ON_DEPLOYMENT
        if availability == AVAILABILITY_ALWAYS_ON
        else DEPLOYMENT
    )
    (deploy_dir / "20-deployment.yaml").write_text(
        deployment_template.format(
            name=name,
            image_tag=deploy_image_tag,
            host=settings.ingress_host_template.format(name=name),
            secret_name=agent_runtime_secret_name(name),
            cp_url=settings.public_cp_url,
            login_url=settings.dashboard_url,
            session_cookie_name=settings.agent_session_cookie_name,
            agent_session_authorize_url=(
                f"{settings.dashboard_url.rstrip('/')}/v1/auth/agent-session/authorize"
            ),
            **k8s_resources,
        )
    )


def _git(workdir: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(workdir), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )
    return result.stdout


def _clone_source_repo(push_url: str, workdir: Path) -> None:
    """Clone the managed source repo, preferring its main branch."""
    try:
        subprocess.run(
            ["git", "clone", "--quiet", "--branch", "main", push_url, str(workdir)],
            check=True,
            capture_output=True,
            text=True,
        )
        return
    except subprocess.CalledProcessError:
        if workdir.exists():
            shutil.rmtree(workdir)
    subprocess.run(
        ["git", "clone", "--quiet", push_url, str(workdir)],
        check=True,
        capture_output=True,
        text=True,
    )


def _checkout_main(workdir: Path) -> None:
    try:
        _git(workdir, "checkout", "-B", "main", "origin/main")
    except subprocess.CalledProcessError:
        _git(workdir, "checkout", "-B", "main")


def _clear_worktree(workdir: Path) -> None:
    for child in workdir.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def _validate_runtime_source(source: Path) -> None:
    build_plan = _build_plan(source)
    if not build_plan.sidecar and not (source / "requirements.txt").is_file():
        raise ValueError("Python agent source is missing requirements.txt")


def _commit_and_push(workdir: Path, message: str) -> str:
    _git(workdir, "config", "user.email", "platform@a2a.local")
    _git(workdir, "config", "user.name", "a2a-platform")
    _git(workdir, "add", "-A")
    if _git(workdir, "status", "--porcelain").strip():
        _git(workdir, "commit", "-q", "-m", message)
    else:
        return _git(workdir, "rev-parse", "HEAD").strip()

    try:
        _git(workdir, "push", "-u", "origin", "main")
    except subprocess.CalledProcessError:
        _git(workdir, "fetch", "origin", "main")
        _git(workdir, "rebase", "origin/main")
        _git(workdir, "push", "-u", "origin", "main")
    return _git(workdir, "rev-parse", "HEAD").strip()


def _source_member_name(member_name: str, common_root: str | None) -> str:
    name = member_name
    if common_root:
        if name == common_root:
            return ""
        if name.startswith(common_root + "/"):
            name = name[len(common_root) + 1 :]
    return name


def _extract_source_tarball(tar_path: str, workdir: Path) -> None:
    root = workdir.resolve()
    with tarfile.open(tar_path, "r:gz") as tar:
        members = tar.getmembers()
        roots = {m.name.split("/")[0] for m in members if m.name}
        common_root = next(iter(roots)) if len(roots) == 1 else None
        for member in members:
            name = _source_member_name(member.name, common_root)
            if not name or name == ".git" or name.startswith(".git/"):
                continue
            source_name = f"{name.rstrip('/')}/" if member.isdir() else name
            if not is_user_source_path(source_name):
                continue
            target = (workdir / name).resolve()
            if root != target and root not in target.parents:
                raise ValueError(f"tarball member escapes source root: {member.name}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(member)
            if src is None:
                continue
            with src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
            os.chmod(target, member.mode & 0o777 or 0o644)


def commit_and_push_source(
    *,
    tarball_bytes: bytes | None = None,
    tarball_path: str | None = None,
    name: str,
    entrypoint: str,
    push_url: str,
) -> str:
    """Untar user source, commit it to ``push_url`` without platform files.

    Returns the head commit SHA.
    """
    with tempfile.TemporaryDirectory(prefix="a2a-prov-") as tmp:
        workdir = Path(tmp) / "src"
        _clone_source_repo(push_url, workdir)
        _checkout_main(workdir)
        _clear_worktree(workdir)

        if tarball_path is None:
            if tarball_bytes is None:
                raise ValueError("tarball_bytes or tarball_path is required")
            with tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz") as tf:
                tf.write(tarball_bytes)
                tarball_path = tf.name
            remove_tarball = True
        else:
            remove_tarball = False
        try:
            _extract_source_tarball(tarball_path, workdir)
        finally:
            if remove_tarball:
                os.unlink(tarball_path)

        return _commit_and_push(workdir, "deploy")


def commit_and_push_runtime(
    *,
    tarball_bytes: bytes | None = None,
    tarball_path: str | None = None,
    name: str,
    entrypoint: str,
    source_repo_url: str,
    source_sha: str,
    push_url: str,
    extra_files: dict[str, str] | None = None,
    image_tag: str | None = None,
    base_image_tag: str = "latest",
    allow_always_on: bool = False,
) -> str:
    """Stamp hidden build/deploy plumbing into the platform runtime repo."""
    with tempfile.TemporaryDirectory(prefix="a2a-runtime-") as tmp:
        root = Path(tmp)
        source_dir = root / "source"
        runtime_dir = root / "runtime"
        source_dir.mkdir()
        _clone_source_repo(push_url, runtime_dir)
        _checkout_main(runtime_dir)
        _clear_worktree(runtime_dir)

        if tarball_path is None:
            if tarball_bytes is None:
                raise ValueError("tarball_bytes or tarball_path is required")
            with tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz") as tf:
                tf.write(tarball_bytes)
                tarball_path = tf.name
            remove_tarball = True
        else:
            remove_tarball = False
        try:
            _extract_source_tarball(tarball_path, source_dir)
        finally:
            if remove_tarball:
                os.unlink(tarball_path)

        _validate_runtime_source(source_dir)
        _stamp_platform_files(
            runtime_dir,
            name=name,
            entrypoint=entrypoint,
            source_dir=source_dir,
            source_repo_url=source_repo_url,
            source_sha=source_sha,
            image_tag=image_tag,
            base_image_tag=base_image_tag,
            allow_always_on=allow_always_on,
        )
        _write_extra_files(runtime_dir, extra_files)
        return _commit_and_push(runtime_dir, "deploy runtime")


def commit_and_push_runtime_from_repo(
    *,
    name: str,
    entrypoint: str,
    source_repo_url: str,
    source_sha: str,
    push_url: str,
    extra_files: dict[str, str] | None = None,
    image_tag: str | None = None,
    base_image_tag: str = "latest",
    allow_always_on: bool = False,
) -> str:
    """Restamp the runtime repo from source already committed to Gitea."""
    with tempfile.TemporaryDirectory(prefix="a2a-runtime-") as tmp:
        root = Path(tmp)
        source_dir = root / "source"
        runtime_dir = root / "runtime"
        _clone_source_repo(source_repo_url, source_dir)
        _git(source_dir, "checkout", "--quiet", source_sha)
        _clone_source_repo(push_url, runtime_dir)
        _checkout_main(runtime_dir)
        _clear_worktree(runtime_dir)
        _validate_runtime_source(source_dir)
        _stamp_platform_files(
            runtime_dir,
            name=name,
            entrypoint=entrypoint,
            source_dir=source_dir,
            source_repo_url=source_repo_url,
            source_sha=source_sha,
            image_tag=image_tag,
            base_image_tag=base_image_tag,
            allow_always_on=allow_always_on,
        )
        _write_extra_files(runtime_dir, extra_files)
        return _commit_and_push(runtime_dir, "deploy runtime")


def _write_extra_files(workdir: Path, extra_files: dict[str, str] | None) -> None:
    if not extra_files:
        return
    root = workdir.resolve()
    for relpath, content in extra_files.items():
        target = (workdir / relpath).resolve()
        if root != target and root not in target.parents:
            raise ValueError(f"extra file escapes runtime root: {relpath}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
