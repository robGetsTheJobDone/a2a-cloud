from __future__ import annotations

import io
import json
import subprocess
import tarfile
from pathlib import Path

import httpx
import pytest
import yaml

from control_plane import gitea
from control_plane.config import settings
from control_plane.k8s import render_manifests
from control_plane.scaffold import (
    _stamp_platform_files,
    commit_and_push_runtime,
    commit_and_push_runtime_from_repo,
    commit_and_push_source,
    read_runtime_availability_from_tarball,
)

def test_stamp_platform_files_uses_yaml_runtime_resources(tmp_path: Path) -> None:
    _write_agent_dsl(tmp_path)
    (tmp_path / "a2a.yaml").write_text(
        "\n".join(
            [
                "name: deck-builder",
                "version: 0.1.0",
                "entrypoint: agent:DeckBuilder",
                "runtime:",
                "  resources:",
                '    cpu: "2"',
                "    memory: 2Gi",
                "    max_runtime_seconds: 1800",
                "  apt_packages:",
                "    - ffmpeg",
            ]
        )
    )

    _stamp_platform_files(tmp_path, name="deck-builder", entrypoint="agent:DeckBuilder")

    deployment = (tmp_path / "deploy" / "20-deployment.yaml").read_text()
    assert "automountServiceAccountToken: false" in deployment
    assert "name: registry-pull-credentials" in deployment
    assert "a2a/workload-class: user" in deployment
    dockerfile = (tmp_path / "Dockerfile").read_text()
    assert "FROM registry.example.com/a2a/a2a-pack-base:latest" in dockerfile
    assert "apiVersion: serving.knative.dev/v1" in deployment
    assert "kind: Service" in deployment
    assert "kind: Deployment" not in deployment
    assert "kind: Ingress" not in deployment
    assert 'autoscaling.knative.dev/min-scale: "0"' in deployment
    assert 'autoscaling.knative.dev/max-scale: "1"' in deployment
    assert 'autoscaling.knative.dev/target: "100"' in deployment
    assert 'a2a/rebuild-id: "latest"' in deployment
    assert "containerConcurrency: 100" in deployment
    assert "timeoutSeconds: 1800" in deployment
    assert "responseStartTimeoutSeconds: 1800" in deployment
    assert "serving.knative.dev/rollout-duration" not in deployment
    assert "startupProbe:" in deployment
    assert 'requests: {cpu: "50m", memory: "2Gi"}' in deployment
    assert 'limits: {cpu: "2", memory: "2Gi"}' in deployment
    assert "A2A_AGENT_IMAGE" in deployment
    assert "A2A_RENDER_IMAGE" in deployment
    assert "A2A_GRANT_VERIFYING_KEY" in deployment
    assert "A2A_RECEIPT_SIGNING_KEY" not in deployment
    assert "A2A_REPLAY_SIGNING_KEY" not in deployment
    assert "A2A_MINIO_ACCESS_KEY" not in deployment
    assert "A2A_MINIO_SECRET_KEY" not in deployment
    assert "A2A_SANDBOX_TOKEN" not in deployment
    assert "apt-get install -y --no-install-recommends ffmpeg" in dockerfile


def test_stamp_platform_files_requires_admin_for_always_on(tmp_path: Path) -> None:
    _write_agent_dsl(tmp_path)
    (tmp_path / "a2a.yaml").write_text(
        "\n".join(
            [
                "name: invoice-bot",
                "version: 0.1.0",
                "entrypoint: agent:InvoiceBot",
                "runtime:",
                "  availability: always_on",
            ]
        )
    )

    with pytest.raises(PermissionError, match="site admin provisioning"):
        _stamp_platform_files(tmp_path, name="invoice-bot", entrypoint="agent:InvoiceBot")


def test_stamp_platform_files_renders_always_on_deployment(tmp_path: Path) -> None:
    _write_agent_dsl(tmp_path)
    (tmp_path / "a2a.yaml").write_text(
        "\n".join(
            [
                "name: invoice-bot",
                "version: 0.1.0",
                "entrypoint: agent:InvoiceBot",
                "runtime:",
                "  availability: always_on",
            ]
        )
    )

    _stamp_platform_files(
        tmp_path,
        name="invoice-bot",
        entrypoint="agent:InvoiceBot",
        allow_always_on=True,
    )

    deployment = (tmp_path / "deploy" / "20-deployment.yaml").read_text()
    assert "apiVersion: apps/v1" in deployment
    assert "kind: Deployment" in deployment
    assert "kind: Service" in deployment
    assert "kind: Ingress" in deployment
    assert "apiVersion: serving.knative.dev/v1" not in deployment
    assert "a2a/availability: always_on" in deployment
    assert "name: A2A_EVIDENCE_SIGNING_MODE" in deployment
    assert "name: agent-ingress-gateway" in deployment


def test_read_runtime_availability_from_tarball(tmp_path: Path) -> None:
    tarball = tmp_path / "source.tar.gz"
    tarball.write_bytes(
        _source_tarball(
            {
                "project/a2a.yaml": "\n".join(
                    [
                        "name: invoice-bot",
                        "version: 0.1.0",
                        "entrypoint: agent:InvoiceBot",
                        "runtime:",
                        "  availability: always_on",
                    ]
                ),
            }
        )
    )

    assert read_runtime_availability_from_tarball(str(tarball)) == "always_on"


def test_stamp_platform_files_uses_top_level_runtime_timeout(tmp_path: Path) -> None:
    _write_agent_dsl(tmp_path)
    (tmp_path / "a2a.yaml").write_text(
        "\n".join(
            [
                "name: animation-engineer",
                "version: 0.1.0",
                "entrypoint: agent:AnimationEngineer",
                "runtime:",
                "  max_runtime_seconds: 900",
            ]
        )
    )

    _stamp_platform_files(
        tmp_path,
        name="animation-engineer",
        entrypoint="agent:AnimationEngineer",
    )

    deployment = (tmp_path / "deploy" / "20-deployment.yaml").read_text()
    assert "timeoutSeconds: 900" in deployment
    assert "responseStartTimeoutSeconds: 900" in deployment


def test_stamp_platform_files_uses_skill_timeout_when_runtime_omits_it(
    tmp_path: Path,
) -> None:
    dsl = json.loads(_agent_dsl_text())
    dsl["skills"][0]["policy"]["timeout_seconds"] = 3600
    dsl["skills"][0]["policy"]["grant_run_timeout_seconds"] = 3540
    dsl_dir = tmp_path / ".a2a"
    dsl_dir.mkdir(parents=True)
    (dsl_dir / "agent.dsl.json").write_text(json.dumps(dsl))
    (tmp_path / "a2a.yaml").write_text(
        "\n".join(
            [
                "name: agent-studio",
                "version: 0.1.0",
                "entrypoint: agent:AgentStudio",
            ]
        )
    )

    _stamp_platform_files(tmp_path, name="agent-studio", entrypoint="agent:AgentStudio")

    deployment = (tmp_path / "deploy" / "20-deployment.yaml").read_text()
    assert "timeoutSeconds: 1800" in deployment
    assert "responseStartTimeoutSeconds: 1800" in deployment


def test_stamp_platform_files_installs_supported_runtime_features(
    tmp_path: Path,
) -> None:
    _write_agent_dsl(tmp_path)
    (tmp_path / "a2a.yaml").write_text(
        "\n".join(
            [
                "name: code-editor-agent",
                "version: 0.2.0",
                "entrypoint: agent:CodeEditorAgent",
                "runtime:",
                "  features:",
                "    - codegraph",
                "    - 'bad;feature'",
            ]
        )
    )

    _stamp_platform_files(
        tmp_path,
        name="code-editor-agent",
        entrypoint="agent:CodeEditorAgent",
    )

    dockerfile = (tmp_path / "Dockerfile").read_text()
    assert (
        "apt-get install -y --no-install-recommends ca-certificates curl"
        in dockerfile
    )
    assert "codegraph/releases/download/v0.9.4/codegraph-linux-x64.tar.gz" in dockerfile
    assert (
        "af4dfe25c17868d2260cec243e702de088738ba8bf379962bed8296362eb0c8a"
        in dockerfile
    )
    assert "sha256sum -c -" in dockerfile
    assert "ln -sf /opt/codegraph/bin/codegraph /usr/local/bin/codegraph" in dockerfile
    assert "codegraph --version" in dockerfile
    assert "CODEGRAPH_INSTALL_URL" not in dockerfile
    assert "install.sh" not in dockerfile
    assert "bad;feature" not in dockerfile


def test_stamp_platform_files_never_projects_platform_signers(tmp_path: Path) -> None:
    _write_agent_dsl(tmp_path)
    (tmp_path / "a2a.yaml").write_text(
        "\n".join(
            [
                "name: agent-studio",
                "version: 0.1.0",
                "entrypoint: agent:AgentStudio",
                "runtime:",
                "  grant_signing: true",
            ]
        )
    )

    _stamp_platform_files(tmp_path, name="agent-studio", entrypoint="agent:AgentStudio")

    deployment = (tmp_path / "deploy" / "20-deployment.yaml").read_text()
    assert "A2A_GRANT_SIGNING_KEY" not in deployment
    assert "A2A_RECEIPT_SIGNING_KEY" not in deployment
    assert "A2A_REPLAY_SIGNING_KEY" not in deployment


def test_stamp_platform_files_builds_packed_frontend(tmp_path: Path) -> None:
    _write_agent_dsl(tmp_path)
    (tmp_path / "a2a.yaml").write_text(
        "\n".join(
            [
                "name: chart-agent",
                "version: 0.1.0",
                "entrypoint: agent:ChartAgent",
                "frontend:",
                "  path: frontend",
                "  build: npm run build",
                "  dist: dist",
                "  mount: /app",
                "  auth: inherit",
            ]
        )
    )

    _stamp_platform_files(tmp_path, name="chart-agent", entrypoint="agent:ChartAgent")

    dockerfile = (tmp_path / "Dockerfile").read_text()
    dockerignore = (tmp_path / ".dockerignore").read_text()
    assert "FROM node:20-bookworm-slim AS frontend-build" in dockerfile
    assert "COPY frontend/ ./" in dockerfile
    assert "RUN npm run build" in dockerfile
    assert "COPY --from=frontend-build /frontend/dist frontend/dist" in dockerfile
    assert "COPY --from=frontend-build /frontend/dist /app/.a2a/frontend" in dockerfile
    assert "ENV A2A_FRONTEND_MOUNT=/app" in dockerfile
    assert "node_modules" in dockerignore


def test_stamp_platform_files_rejects_platform_frontend_auth_when_gateway_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The agent-session gateway ships enabled; operators can still turn it off.
    monkeypatch.setattr(settings, "allow_platform_frontend_auth", False)
    _write_agent_dsl(tmp_path)
    dist = tmp_path / "frontend" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<main>private app</main>")
    (tmp_path / "a2a.yaml").write_text(
        "\n".join(
            [
                "name: private-app",
                "version: 0.1.0",
                "entrypoint: agent:PrivateApp",
                "frontend:",
                "  path: frontend",
                "  dist: dist",
                "  mount: /app",
                "  auth: platform",
            ]
        )
    )

    with pytest.raises(ValueError, match="origin-bound browser session gateway"):
        _stamp_platform_files(
            tmp_path,
            name="private-app",
            entrypoint="agent:PrivateApp",
        )


def test_stamp_platform_files_allows_platform_frontend_auth_with_gateway_opt_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "allow_platform_frontend_auth", True)
    _write_agent_dsl(tmp_path)
    dist = tmp_path / "frontend" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<main>private app</main>")
    (tmp_path / "a2a.yaml").write_text(
        "\n".join(
            [
                "name: private-app",
                "version: 0.1.0",
                "entrypoint: agent:PrivateApp",
                "frontend:",
                "  path: frontend",
                "  dist: dist",
                "  mount: /app",
                "  auth: platform",
            ]
        )
    )

    _stamp_platform_files(tmp_path, name="private-app", entrypoint="agent:PrivateApp")

    dockerfile = (tmp_path / "Dockerfile").read_text()
    assert "ENV A2A_FRONTEND_AUTH=platform" in dockerfile


def test_stamp_platform_files_rejects_invalid_frontend_config(tmp_path: Path) -> None:
    _write_agent_dsl(tmp_path)
    (tmp_path / "a2a.yaml").write_text(
        "\n".join(
            [
                "name: bad-frontend",
                "version: 0.1.0",
                "entrypoint: agent:BadFrontend",
                "frontend:",
                "  type: native",
            ]
        )
    )

    with pytest.raises(ValueError, match="frontend.type"):
        _stamp_platform_files(
            tmp_path,
            name="bad-frontend",
            entrypoint="agent:BadFrontend",
        )


def test_stamp_platform_files_rejects_missing_prebuilt_frontend_dist(
    tmp_path: Path,
) -> None:
    _write_agent_dsl(tmp_path)
    (tmp_path / "a2a.yaml").write_text(
        "\n".join(
            [
                "name: missing-frontend",
                "version: 0.1.0",
                "entrypoint: agent:MissingFrontend",
                "frontend:",
                "  path: frontend",
                "  dist: dist",
            ]
        )
    )

    with pytest.raises(ValueError, match="frontend dist is missing index.html"):
        _stamp_platform_files(
            tmp_path,
            name="missing-frontend",
            entrypoint="agent:MissingFrontend",
        )


def test_stamp_platform_files_builds_server_rendered_frontend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "allow_platform_frontend_auth", True)
    _write_agent_dsl(tmp_path)
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text("{}")
    (tmp_path / "a2a.yaml").write_text(
        "\n".join(
            [
                "name: ssr-agent",
                "version: 0.1.0",
                "entrypoint: agent:SsrAgent",
                "frontend:",
                "  type: server-rendered",
                "  framework: nextjs",
                "  path: frontend",
                "  build: npm run build",
                "  start: node server.js",
                "  port: 3000",
                "  mount: /",
                "  auth: platform",
            ]
        )
    )

    _stamp_platform_files(tmp_path, name="ssr-agent", entrypoint="agent:SsrAgent")

    dockerfile = (tmp_path / "Dockerfile").read_text()
    assert "FROM node:20-bookworm-slim AS frontend-build" in dockerfile
    assert "RUN npm run build" in dockerfile
    assert "COPY --from=frontend-build /usr/local/bin/node /usr/local/bin/node" in dockerfile
    assert (
        "COPY --from=frontend-build /frontend/.next/standalone "
        "/app/.a2a/frontend-server"
    ) in dockerfile
    assert 'ENV A2A_FRONTEND_KIND="server-rendered"' in dockerfile
    assert 'ENV A2A_FRONTEND_AUTH="platform"' in dockerfile
    assert 'ENV A2A_FRONTEND_PROXY_URL="http://127.0.0.1:3000"' in dockerfile
    assert 'ENV A2A_FRONTEND_START="node server.js"' in dockerfile
    assert "ENV A2A_FRONTEND_START=node server.js" not in dockerfile


def test_stamp_platform_files_rejects_server_rendered_without_build(
    tmp_path: Path,
) -> None:
    _write_agent_dsl(tmp_path)
    (tmp_path / "frontend").mkdir()
    (tmp_path / "a2a.yaml").write_text(
        "\n".join(
            [
                "name: ssr-agent",
                "version: 0.1.0",
                "entrypoint: agent:SsrAgent",
                "frontend:",
                "  type: server-rendered",
                "  framework: nextjs",
                "  path: frontend",
                "  start: node server.js",
            ]
        )
    )

    with pytest.raises(ValueError, match="requires frontend.build"):
        _stamp_platform_files(tmp_path, name="ssr-agent", entrypoint="agent:SsrAgent")


def test_stamp_platform_files_falls_back_for_invalid_runtime_resources(
    tmp_path: Path,
) -> None:
    _write_agent_dsl(tmp_path)
    (tmp_path / "a2a.yaml").write_text(
        "\n".join(
            [
                "name: bad-agent",
                "version: 0.1.0",
                "entrypoint: agent:BadAgent",
                "runtime:",
                "  resources:",
                "    cpu: '2; rm -rf /'",
                "    memory: '2Gi # nope'",
            ]
        )
    )

    _stamp_platform_files(tmp_path, name="bad-agent", entrypoint="agent:BadAgent")

    deployment = (tmp_path / "deploy" / "20-deployment.yaml").read_text()
    assert 'requests: {cpu: "50m", memory: "256Mi"}' in deployment
    assert 'limits: {cpu: "1", memory: "512Mi"}' in deployment


def test_stamp_platform_files_canonicalizes_cpu_for_kubernetes(
    tmp_path: Path,
) -> None:
    _write_agent_dsl(tmp_path)
    (tmp_path / "a2a.yaml").write_text(
        "\n".join(
            [
                "name: canonical-agent",
                "version: 0.1.0",
                "entrypoint: agent:CanonicalAgent",
                "runtime:",
                "  resources:",
                '    cpu: "2000m"',
                "    memory: 1Gi",
            ]
        )
    )

    _stamp_platform_files(
        tmp_path,
        name="canonical-agent",
        entrypoint="agent:CanonicalAgent",
    )

    deployment = (tmp_path / "deploy" / "20-deployment.yaml").read_text()
    assert 'limits: {cpu: "2", memory: "1Gi"}' in deployment


def test_render_manifests_exposes_runtime_image_to_agent() -> None:
    image = "registry.example.com/agents/demo-agent:abc123"
    docs = render_manifests(
        "demo-agent",
        image,
        public=False,
        card={"runtime": {"resources": {"cpu": "1", "memory": "1Gi"}, "concurrency": 4}},
    )
    assert len(docs) == 1
    svc = docs[0]
    assert svc["apiVersion"] == "serving.knative.dev/v1"
    assert svc["kind"] == "Service"
    assert svc["metadata"]["annotations"] == {
        "argocd.argoproj.io/sync-options": "ServerSideApply=true",
    }
    # Private agent (public=False) stays cluster-local.
    assert svc["metadata"]["labels"]["networking.knative.dev/visibility"] == "cluster-local"
    tmpl = svc["spec"]["template"]
    assert tmpl["metadata"]["annotations"] == {
        "autoscaling.knative.dev/min-scale": "0",
        "autoscaling.knative.dev/max-scale": "1",
        "autoscaling.knative.dev/target": "4",
    }
    assert tmpl["spec"]["containerConcurrency"] == 4
    assert tmpl["spec"]["timeoutSeconds"] == 1800
    assert tmpl["spec"]["responseStartTimeoutSeconds"] == 1800
    assert tmpl["spec"]["nodeSelector"] == {"a2a/worker": "true"}
    assert tmpl["metadata"]["labels"]["a2a/workload-class"] == "user"
    container = tmpl["spec"]["containers"][0]
    assert container["resources"]["requests"] == {"cpu": "50m", "memory": "1Gi"}
    assert container["resources"]["limits"] == {"cpu": "1", "memory": "2Gi"}
    assert container["startupProbe"] == {
        "httpGet": {"path": "/healthz", "port": 8000},
        "periodSeconds": 5,
        "timeoutSeconds": 3,
        "failureThreshold": 60,
    }
    env = {item["name"]: item for item in container["env"]}

    assert env["A2A_AGENT_IMAGE"]["value"] == image
    assert env["A2A_RENDER_IMAGE"]["value"] == image
    assert env["A2A_LOGIN_URL"]["value"]
    # Agent pods get their own origin-bound cookie. The dashboard's
    # ``__Host-a2a_session`` is host-locked to the dashboard and can never
    # arrive here, so expecting it was the frontend-auth bug.
    assert env["A2A_SESSION_COOKIE_NAME"]["value"] == "__Host-a2a_agent_session"
    assert env["A2A_AGENT_SESSION_AUTHORIZE_URL"]["value"].endswith(
        "/v1/auth/agent-session/authorize"
    )
    assert env["A2A_EVIDENCE_SIGNING_MODE"]["value"] == "gateway"
    assert env["A2A_GRANT_VERIFYING_KEY"]["valueFrom"]["secretKeyRef"] == {
        "name": "platform-secrets",
        "key": "grant_verifying_key",
    }
    assert "A2A_GRANT_SIGNING_KEY" not in env
    assert "A2A_RECEIPT_SIGNING_KEY" not in env
    assert "A2A_REPLAY_SIGNING_KEY" not in env
    assert "A2A_MINIO_ACCESS_KEY" not in env
    assert "A2A_MINIO_SECRET_KEY" not in env
    assert "A2A_SANDBOX_TOKEN" not in env


def test_render_manifests_uses_deployment_for_always_on() -> None:
    docs = render_manifests(
        "always-agent",
        "registry.example.com/agents/always-agent:abc123",
        public=True,
        card={"runtime": {"availability": "always_on"}},
        owner_id=123,
    )

    kinds = {(doc["apiVersion"], doc["kind"]) for doc in docs}
    assert ("apps/v1", "Deployment") in kinds
    assert ("v1", "Service") in kinds
    assert ("networking.k8s.io/v1", "Ingress") in kinds
    assert ("serving.knative.dev/v1", "Service") not in kinds
    deployment = next(doc for doc in docs if doc["kind"] == "Deployment")
    assert deployment["spec"]["template"]["spec"]["imagePullSecrets"] == [
        {"name": "registry-pull-credentials"}
    ]
    ingress = next(doc for doc in docs if doc["kind"] == "Ingress")
    backend = ingress["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]
    assert backend == {"name": "agent-ingress-gateway", "port": {"number": 80}}


def test_render_manifests_uses_runtime_and_skill_timeouts() -> None:
    docs = render_manifests(
        "agent-studio",
        "registry.example.com/agents/agent-studio:abc123",
        public=False,
        card={
            "runtime": {"max_runtime_seconds": 900},
            "skills": [
                {
                    "name": "create_agent",
                    "policy": {
                        "timeout_seconds": 3600,
                        "grant_run_timeout_seconds": 3540,
                    },
                }
            ],
        },
    )

    spec = docs[0]["spec"]["template"]["spec"]
    assert spec["timeoutSeconds"] == 1800
    assert spec["responseStartTimeoutSeconds"] == 1800


def test_render_manifests_never_projects_platform_signers() -> None:
    docs = render_manifests(
        "meta-agent",
        "registry.example.com/agents/meta-agent:abc123",
        public=False,
        card={"runtime": {"grant_signing": True}},
    )
    env = {
        item["name"]: item
        for item in docs[0]["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert docs[0]["spec"]["template"]["spec"]["automountServiceAccountToken"] is False
    assert docs[0]["spec"]["template"]["spec"]["imagePullSecrets"] == [
        {"name": "registry-pull-credentials"}
    ]

    assert "A2A_GRANT_SIGNING_KEY" not in env
    assert "A2A_RECEIPT_SIGNING_KEY" not in env
    assert "A2A_REPLAY_SIGNING_KEY" not in env
    assert env["A2A_EVIDENCE_SIGNING_MODE"]["value"] == "gateway"


def test_repo_urls_never_embed_admin_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gitea, "GITEA_PUBLIC", "https://gitea.example")
    monkeypatch.setattr(gitea, "GITEA_INTERNAL", "http://gitea.internal:3000")
    monkeypatch.setattr(gitea, "GITEA_USER", "admin")
    monkeypatch.setattr(gitea, "GITEA_PASS", "secret")

    public_url, internal_url = gitea.repo_urls("demo-agent")
    auth_url = gitea.authenticated_repo_url("demo-agent")
    org_public_url, org_internal_url = gitea.repo_urls("demo-agent", owner="a2a-acme")
    org_auth_url = gitea.authenticated_repo_url("demo-agent", owner="a2a-acme")

    assert public_url == "https://gitea.example/admin/demo-agent.git"
    assert internal_url == "http://gitea.internal:3000/admin/demo-agent.git"
    assert org_public_url == "https://gitea.example/a2a-acme/demo-agent.git"
    assert org_internal_url == "http://gitea.internal:3000/a2a-acme/demo-agent.git"
    assert "secret" not in public_url
    assert "secret" not in internal_url
    assert "secret" not in org_public_url
    assert "secret" not in org_internal_url
    assert "secret" in auth_url
    assert "admin:secret@" in org_auth_url
    assert "/a2a-acme/demo-agent.git" in org_auth_url


def test_ensure_repo_falls_back_to_user_owned_repo_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def get(self, url: str, **kwargs: object):
            calls.append(("GET", url))
            return httpx.Response(404, request=httpx.Request("GET", url))

        def post(self, url: str, **kwargs: object):
            calls.append(("POST", url))
            if "/api/v1/org/" in url:
                return httpx.Response(422, request=httpx.Request("POST", url))
            if "/api/v1/orgs/" in url:
                return httpx.Response(404, request=httpx.Request("POST", url))
            return httpx.Response(
                201,
                json={"name": "demo-agent"},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(gitea, "GITEA_INTERNAL", "http://gitea.internal:3000")
    monkeypatch.setattr(gitea.httpx, "Client", FakeClient)

    push_url, internal_url = gitea.ensure_repo("demo-agent", owner="owner-1")

    assert push_url.endswith("@gitea.internal:3000/owner-1/demo-agent.git")
    assert internal_url == "http://gitea.internal:3000/owner-1/demo-agent.git"
    assert calls == [
        ("GET", "http://gitea.internal:3000/api/v1/repos/owner-1/demo-agent"),
        ("POST", "http://gitea.internal:3000/api/v1/orgs/owner-1/repos"),
        ("POST", "http://gitea.internal:3000/api/v1/org/owner-1/repos"),
        ("POST", "http://gitea.internal:3000/api/v1/admin/users/owner-1/repos"),
    ]


def test_ensure_repo_creates_public_source_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads: list[dict[str, object]] = []
    public_owners: list[str | None] = []

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def get(self, url: str, **kwargs: object):
            return httpx.Response(404, request=httpx.Request("GET", url))

        def post(self, url: str, **kwargs: object):
            payload = dict(kwargs.get("json") or {})
            payloads.append(payload)
            return httpx.Response(
                201,
                json={"name": "demo-agent", "private": payload["private"]},
                request=httpx.Request("POST", url),
            )

        def patch(self, url: str, **kwargs: object):
            raise AssertionError("new repo already has the requested visibility")

    monkeypatch.setattr(gitea, "GITEA_INTERNAL", "http://gitea.internal:3000")
    monkeypatch.setattr(gitea.httpx, "Client", FakeClient)
    monkeypatch.setattr(
        gitea,
        "ensure_repo_owner_public",
        public_owners.append,
    )

    gitea.ensure_repo("demo-agent", owner="admin", public=True)

    assert payloads == [
        {
            "name": "demo-agent",
            "description": "agent demo-agent",
            "auto_init": False,
            "private": False,
            "default_branch": "main",
        }
    ]
    assert public_owners == ["admin"]


def test_ensure_repo_updates_existing_source_visibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patches: list[dict[str, object]] = []
    public_owners: list[str | None] = []

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def get(self, url: str, **kwargs: object):
            return httpx.Response(
                200,
                json={"name": "demo-agent", "private": True},
                request=httpx.Request("GET", url),
            )

        def patch(self, url: str, **kwargs: object):
            patches.append(dict(kwargs.get("json") or {}))
            return httpx.Response(
                200,
                json={"name": "demo-agent", "private": False},
                request=httpx.Request("PATCH", url),
            )

    monkeypatch.setattr(gitea, "GITEA_INTERNAL", "http://gitea.internal:3000")
    monkeypatch.setattr(gitea.httpx, "Client", FakeClient)
    monkeypatch.setattr(
        gitea,
        "ensure_repo_owner_public",
        public_owners.append,
    )

    gitea.ensure_repo("demo-agent", owner="admin", public=True)

    assert patches == [{"private": False}]
    assert public_owners == ["admin"]


def test_ensure_repo_owner_public_updates_private_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patches: list[tuple[str, dict[str, object]]] = []

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def get(self, url: str, **kwargs: object):
            if "/api/v1/orgs/" in url:
                return httpx.Response(404, request=httpx.Request("GET", url))
            return httpx.Response(
                200,
                json={"username": "owner-1", "visibility": "private"},
                request=httpx.Request("GET", url),
            )

        def patch(self, url: str, **kwargs: object):
            patches.append((url, dict(kwargs.get("json") or {})))
            return httpx.Response(200, request=httpx.Request("PATCH", url))

    monkeypatch.setattr(gitea, "GITEA_INTERNAL", "http://gitea.internal:3000")
    monkeypatch.setattr(gitea.httpx, "Client", FakeClient)

    gitea.ensure_repo_owner_public("owner-1")

    assert patches == [
        (
            "http://gitea.internal:3000/api/v1/admin/users/owner-1",
            {"login_name": "owner-1", "visibility": "public"},
        )
    ]


def test_ensure_repo_owner_public_updates_private_organization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patches: list[tuple[str, dict[str, object]]] = []

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def get(self, url: str, **kwargs: object):
            return httpx.Response(
                200,
                json={"username": "a2a-acme", "visibility": "private"},
                request=httpx.Request("GET", url),
            )

        def patch(self, url: str, **kwargs: object):
            patches.append((url, dict(kwargs.get("json") or {})))
            return httpx.Response(200, request=httpx.Request("PATCH", url))

    monkeypatch.setattr(gitea, "GITEA_INTERNAL", "http://gitea.internal:3000")
    monkeypatch.setattr(gitea.httpx, "Client", FakeClient)

    gitea.ensure_repo_owner_public("a2a-acme")

    assert patches == [
        (
            "http://gitea.internal:3000/api/v1/orgs/a2a-acme",
            {"visibility": "public"},
        )
    ]


def test_set_repo_visibility_keeps_missing_external_repo_unmodified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    public_owners: list[str | None] = []
    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def get(self, url: str, **kwargs: object):
            return httpx.Response(404, request=httpx.Request("GET", url))

        def patch(self, url: str, **kwargs: object):
            raise AssertionError("missing repositories must not be edited")

    monkeypatch.setattr(gitea, "GITEA_INTERNAL", "http://gitea.internal:3000")
    monkeypatch.setattr(gitea.httpx, "Client", FakeClient)
    monkeypatch.setattr(
        gitea,
        "ensure_repo_owner_public",
        public_owners.append,
    )

    assert gitea.set_repo_visibility("external-agent", public=True) is False
    assert public_owners == []


def test_ensure_repo_truncates_long_descriptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads: list[dict[str, object]] = []

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def get(self, url: str, **kwargs: object):
            return httpx.Response(404, request=httpx.Request("GET", url))

        def post(self, url: str, **kwargs: object):
            payloads.append(dict(kwargs.get("json") or {}))
            return httpx.Response(
                201,
                json={"name": "demo-agent"},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(gitea, "GITEA_INTERNAL", "http://gitea.internal:3000")
    monkeypatch.setattr(gitea.httpx, "Client", FakeClient)

    gitea.ensure_repo("demo-agent", "Long OpenAPI description.\n" * 100, owner="admin")

    description = str(payloads[0]["description"])
    assert len(description) <= gitea.GITEA_REPO_DESCRIPTION_MAX
    assert description.endswith("...")
    assert "\n" not in description


def test_ensure_repo_treats_concurrent_create_failure_as_existing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    get_count = 0

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def get(self, url: str, **kwargs: object):
            nonlocal get_count
            calls.append(("GET", url))
            get_count += 1
            status = 404 if get_count == 1 else 200
            return httpx.Response(
                status,
                json={"name": "demo-agent"},
                request=httpx.Request("GET", url),
            )

        def post(self, url: str, **kwargs: object):
            calls.append(("POST", url))
            return httpx.Response(
                500,
                text="duplicate",
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(gitea, "GITEA_INTERNAL", "http://gitea.internal:3000")
    monkeypatch.setattr(gitea.httpx, "Client", FakeClient)

    push_url, internal_url = gitea.ensure_repo("demo-agent", owner="owner-1")

    assert push_url.endswith("@gitea.internal:3000/owner-1/demo-agent.git")
    assert internal_url == "http://gitea.internal:3000/owner-1/demo-agent.git"
    assert calls == [
        ("GET", "http://gitea.internal:3000/api/v1/repos/owner-1/demo-agent"),
        ("POST", "http://gitea.internal:3000/api/v1/orgs/owner-1/repos"),
        ("GET", "http://gitea.internal:3000/api/v1/repos/owner-1/demo-agent"),
    ]


def test_runtime_repo_receives_scoped_build_actions_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def put(self, url: str, **kwargs: object):
            calls.append((url, dict(kwargs.get("json") or {})))
            return httpx.Response(204, request=httpx.Request("PUT", url))

    monkeypatch.setattr(gitea, "GITEA_INTERNAL", "http://gitea.internal:3000")
    monkeypatch.setattr(gitea, "GITEA_USER", "gitea_admin")
    monkeypatch.setattr(gitea, "GITEA_PASS", "source-clone-secret")
    monkeypatch.setattr(gitea.settings, "registry_actions_username", "registry-push")
    monkeypatch.setattr(gitea.settings, "registry_actions_password", "push-secret")
    monkeypatch.setattr(gitea.httpx, "Client", FakeClient)

    gitea.ensure_runtime_repo_actions_secrets("demo-agent-runtime")

    assert calls == [
        (
            "http://gitea.internal:3000/api/v1/repos/gitea_admin/"
            "demo-agent-runtime/actions/secrets/REGISTRY_USERNAME",
            {"data": "registry-push"},
        ),
        (
            "http://gitea.internal:3000/api/v1/repos/gitea_admin/"
            "demo-agent-runtime/actions/secrets/REGISTRY_PASSWORD",
            {"data": "push-secret"},
        ),
        (
            "http://gitea.internal:3000/api/v1/repos/gitea_admin/"
            "demo-agent-runtime/actions/secrets/SOURCE_REPO_USERNAME",
            {"data": "gitea_admin"},
        ),
        (
            "http://gitea.internal:3000/api/v1/repos/gitea_admin/"
            "demo-agent-runtime/actions/secrets/SOURCE_REPO_PASSWORD",
            {"data": "source-clone-secret"},
        ),
    ]


def test_registry_actions_secrets_reject_source_repos() -> None:
    with pytest.raises(ValueError, match="restricted to runtime repos"):
        gitea.ensure_runtime_repo_actions_secrets("demo-agent")


def test_generated_workflow_does_not_reference_admin_password(tmp_path: Path) -> None:
    workdir = tmp_path / "src"
    workdir.mkdir()
    _write_agent_dsl(workdir)
    _stamp_platform_files(
        workdir,
        name="demo-agent",
        entrypoint="agent:Demo",
        source_repo_url="http://gitea/source.git",
        source_sha="abc123",
    )
    workflow = (workdir / ".gitea" / "workflows" / "build.yml").read_text()

    assert "ADMIN_PW" not in workflow
    assert "gitea_admin:" not in workflow
    assert "SOURCE_REPO_USERNAME: ${{ secrets.SOURCE_REPO_USERNAME }}" in workflow
    assert "SOURCE_REPO_PASSWORD: ${{ secrets.SOURCE_REPO_PASSWORD }}" in workflow
    assert 'http.extraHeader="Authorization: Basic $SOURCE_AUTH"' in workflow
    assert "git clone --quiet \"$SOURCE_REPO_URL\" src" not in workflow
    assert "cp .dockerignore src/.dockerignore" in workflow
    assert "docker build --pull -f Dockerfile" in workflow
    assert "a2a/rebuild-id:" in workflow
    assert 'IMAGE_TAG: "abc123"' in workflow


def test_generated_workflow_strips_credentials_from_source_url(tmp_path: Path) -> None:
    _write_agent_dsl(tmp_path)

    _stamp_platform_files(
        tmp_path,
        name="demo-agent",
        entrypoint="agent:Demo",
        source_repo_url=(
            "http://gitea_admin:source-clone-secret@"
            "gitea-http.gitea.svc.cluster.local:3000/alice/demo-agent.git"
        ),
        source_sha="abc123",
    )

    workflow = (tmp_path / ".gitea" / "workflows" / "build.yml").read_text()
    assert "source-clone-secret" not in workflow
    assert "gitea_admin:" not in workflow
    assert (
        "http://gitea-http.gitea.svc.cluster.local:3000/alice/demo-agent.git"
        in workflow
    )


def test_stamp_platform_files_generates_sidecar_node_build_for_typescript(
    tmp_path: Path,
) -> None:
    _write_agent_dsl(
        tmp_path,
        language="typescript",
        command=["node", "dist/worker.js"],
    )
    (tmp_path / "package.json").write_text('{"scripts":{"build":"tsc"}}\n')

    _stamp_platform_files(tmp_path, name="ts-agent", entrypoint="unused")

    dockerfile = (tmp_path / "Dockerfile").read_text()
    dockerignore = (tmp_path / ".dockerignore").read_text()
    assert "FROM registry.example.com/a2a/a2a-sidecar-node:latest" in dockerfile
    assert "RUN a2a-sidecar-build typescript" in dockerfile
    assert "node dist/worker.js &" in dockerfile
    assert "a2a sidecar --dsl" in dockerfile
    assert 'CMD ["a2a-sidecar-entrypoint"]' in dockerfile
    assert "a2a run" not in dockerfile
    assert ".a2a\n" not in dockerignore


def test_stamp_platform_files_generates_sidecar_build_for_compiled_languages(
    tmp_path: Path,
) -> None:
    cases = {
        "javascript": "a2a-sidecar-node",
        "go": "a2a-sidecar-go",
        "rust": "a2a-sidecar-rust",
        "java": "a2a-sidecar-jvm",
        "dotnet": "a2a-sidecar-dotnet",
    }
    for language, image in cases.items():
        workdir = tmp_path / language
        workdir.mkdir()
        _write_agent_dsl(workdir, language=language, command=["./worker"])

        _stamp_platform_files(workdir, name=f"{language}-agent", entrypoint="unused")

        dockerfile = (workdir / "Dockerfile").read_text()
        assert f"FROM registry.example.com/a2a/{image}:latest" in dockerfile
        assert f"RUN a2a-sidecar-build {language}" in dockerfile
        assert "./worker &" in dockerfile


def test_stamp_platform_files_supports_legacy_python_without_dsl(
    tmp_path: Path,
) -> None:
    (tmp_path / "a2a.yaml").write_text(
        "name: legacy-agent\nversion: 0.1.0\nentrypoint: agent:LegacyAgent\n"
    )

    _stamp_platform_files(tmp_path, name="legacy-agent", entrypoint="agent:LegacyAgent")

    dockerfile = (tmp_path / "Dockerfile").read_text()
    deployment = (tmp_path / "deploy" / "20-deployment.yaml").read_text()
    assert "FROM registry.example.com/a2a/a2a-pack-base:latest" in dockerfile
    assert "ENV A2A_ENTRYPOINT=agent:LegacyAgent" in dockerfile
    assert "timeoutSeconds: 1800" in deployment


def test_stamp_platform_files_rejects_sidecar_language_without_command(
    tmp_path: Path,
) -> None:
    _write_agent_dsl(tmp_path, language="go", command=None)

    with pytest.raises(ValueError, match="entrypoint.command"):
        _stamp_platform_files(tmp_path, name="go-agent", entrypoint="unused")


def test_stamp_platform_files_rejects_unsupported_dsl_language(
    tmp_path: Path,
) -> None:
    _write_agent_dsl(tmp_path, language="elixir", command=["mix", "run"])

    with pytest.raises(ValueError, match="unsupported agent_dsl.language"):
        _stamp_platform_files(tmp_path, name="beam-agent", entrypoint="unused")



def test_source_changed_paths_since_ignores_platform_owned_commits(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bare = tmp_path / "repo.git"
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "--bare", str(bare))
    _git(tmp_path, "init", "-b", "main", str(seed))
    _git(seed, "config", "user.email", "test@example.com")
    _git(seed, "config", "user.name", "test")
    (seed / "deploy").mkdir()
    (seed / ".gitea" / "workflows").mkdir(parents=True)
    (seed / "agent.py").write_text("print('v1')\n")
    (seed / "deploy" / "20-deployment.yaml").write_text("image: v1\n")
    (seed / ".gitea" / "workflows" / "build.yml").write_text("name: build\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(bare))
    _git(seed, "push", "-u", "origin", "main")
    base = _git(seed, "rev-parse", "HEAD").strip()

    (seed / "deploy" / "20-deployment.yaml").write_text("image: ci-bump\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "ci bump")
    _git(seed, "push", "origin", "main")

    monkeypatch.setattr(gitea, "authenticated_repo_url", lambda name: str(bare))
    assert gitea.source_changed_paths_since("demo-agent", base) == []

    (seed / "agent.py").write_text("print('v2')\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "user source change")
    _git(seed, "push", "origin", "main")

    assert gitea.source_changed_paths_since("demo-agent", base) == ["agent.py"]


def test_source_changed_paths_since_reports_missing_base(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bare = tmp_path / "repo.git"
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "--bare", str(bare))
    _git(tmp_path, "init", "-b", "main", str(seed))
    _git(seed, "config", "user.email", "test@example.com")
    _git(seed, "config", "user.name", "test")
    (seed / "agent.py").write_text("print('ok')\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(bare))
    _git(seed, "push", "-u", "origin", "main")
    _git(bare, "symbolic-ref", "HEAD", "refs/heads/main")

    monkeypatch.setattr(gitea, "authenticated_repo_url", lambda name: str(bare))
    with pytest.raises(gitea.RepoBaseMissingError):
        gitea.source_changed_paths_since(
            "demo-agent",
            "9852150a3cb0fce5b826affc0fedd694fcce1043",
        )


def test_commit_and_push_source_preserves_repo_history(tmp_path: Path) -> None:
    bare = tmp_path / "repo.git"
    clone = tmp_path / "clone"
    _git(tmp_path, "init", "--bare", str(bare))

    first = commit_and_push_source(
        tarball_bytes=_source_tarball(
            {
                "agent.py": "print('v1')\n",
                "a2a.yaml": "name: demo-agent\nentrypoint: agent:DemoAgent\n",
                "requirements.txt": "",
            }
        ),
        name="demo-agent",
        entrypoint="agent:DemoAgent",
        push_url=str(bare),
    )
    second = commit_and_push_source(
        tarball_bytes=_source_tarball(
            {
                "agent.py": "print('v2')\n",
                "a2a.yaml": "name: demo-agent\nentrypoint: agent:DemoAgent\n",
                "requirements.txt": "",
            }
        ),
        name="demo-agent",
        entrypoint="agent:DemoAgent",
        push_url=str(bare),
    )

    _git(tmp_path, "clone", "--branch", "main", str(bare), str(clone))
    assert _git(clone, "cat-file", "-e", f"{first}^{{commit}}") == ""
    assert _git(clone, "rev-parse", "HEAD").strip() == second
    assert "agent.py" in _git(clone, "diff", "--name-only", first, "HEAD").splitlines()
    assert not (clone / ".gitea").exists()
    assert not (clone / "deploy").exists()
    assert not (clone / "Dockerfile").exists()


def test_commit_and_push_source_filters_platform_files_from_upload(tmp_path: Path) -> None:
    bare = tmp_path / "repo.git"
    clone = tmp_path / "clone"
    _git(tmp_path, "init", "--bare", str(bare))

    commit_and_push_source(
        tarball_bytes=_source_tarball(
            {
                "agent.py": "print('ok')\n",
                "a2a.yaml": "name: demo-agent\nentrypoint: agent:DemoAgent\n",
                "requirements.txt": "",
                ".gitea/workflows/build.yml": "name: user-owned-build\n",
                "deploy/20-deployment.yaml": "apiVersion: v1\n",
                "Dockerfile": "FROM bad\n",
            }
        ),
        name="demo-agent",
        entrypoint="agent:DemoAgent",
        push_url=str(bare),
    )

    _git(tmp_path, "clone", "--branch", "main", str(bare), str(clone))
    assert (clone / "agent.py").exists()
    assert not (clone / ".gitea").exists()
    assert not (clone / "deploy").exists()
    assert not (clone / "Dockerfile").exists()


def test_commit_and_push_runtime_writes_hidden_build_repo(tmp_path: Path) -> None:
    bare = tmp_path / "runtime.git"
    clone = tmp_path / "clone"
    _git(tmp_path, "init", "--bare", str(bare))

    sha = commit_and_push_runtime(
        tarball_bytes=_source_tarball(
            {
                "agent.py": "print('ok')\n",
                "a2a.yaml": "name: demo-agent\nentrypoint: agent:DemoAgent\n",
                ".a2a/agent.dsl.json": _agent_dsl_text(),
                "requirements.txt": "",
            }
        ),
        name="demo-agent",
        entrypoint="agent:DemoAgent",
        source_repo_url="http://gitea.internal/source.git",
        source_sha="abc123",
        push_url=str(bare),
    )

    _git(tmp_path, "clone", "--branch", "main", str(bare), str(clone))
    workflow = (clone / ".gitea" / "workflows" / "build.yml").read_text()
    deployment = (clone / "deploy" / "20-deployment.yaml").read_text()
    yaml.safe_load(workflow)
    assert "\x01" not in workflow
    assert _git(clone, "rev-parse", "HEAD").strip() == sha
    assert "SOURCE_REPO_URL" in workflow
    assert "abc123" in workflow
    assert 'IMAGE_TAG: "abc123"' in workflow
    assert "cp .dockerignore src/.dockerignore" in workflow
    assert "docker build --pull -f Dockerfile" in workflow
    assert "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683" in workflow
    assert "REGISTRY_USERNAME: ${{ secrets.REGISTRY_USERNAME }}" in workflow
    assert 'test "$REGISTRY_USERNAME" = registry-push' in workflow
    assert "--password-stdin" in workflow
    assert "docker logout registry.example.com" in workflow
    assert 'docker push "$IMG:$IMAGE_TAG"' in workflow
    assert "docker manifest inspect --verbose \"$PINNED_IMAGE\"" in workflow
    assert "@sha256:[0-9a-f]{64}" in workflow
    assert 'image: $PINNED_IMAGE' in workflow
    assert 'value: $PINNED_IMAGE' in workflow
    assert "a2a/rebuild-id:" in workflow
    assert "A2A_AGENT_IMAGE" in deployment
    assert "image: registry.example.com/agents/demo-agent:abc123" in deployment
    assert "value: registry.example.com/agents/demo-agent:abc123" in deployment
    assert 'a2a/rebuild-id: "abc123"' in deployment


def test_commit_and_push_runtime_from_repo_uses_exact_source_sha(
    tmp_path: Path,
) -> None:
    source_bare = tmp_path / "source.git"
    runtime_bare = tmp_path / "runtime.git"
    seed = tmp_path / "seed"
    clone = tmp_path / "runtime-clone"
    _git(tmp_path, "init", "--bare", str(source_bare))
    _git(tmp_path, "init", "--bare", str(runtime_bare))
    _git(tmp_path, "init", "-b", "main", str(seed))
    _git(seed, "config", "user.email", "test@example.com")
    _git(seed, "config", "user.name", "test")
    (seed / ".gitea" / "workflows").mkdir(parents=True)
    (seed / "deploy").mkdir()
    (seed / ".a2a").mkdir()
    (seed / "a2a.yaml").write_text(
        "name: demo-agent\nentrypoint: agent:DemoAgent\n"
    )
    (seed / ".a2a" / "agent.dsl.json").write_text(_agent_dsl_text())
    (seed / "agent.py").write_text("print('ok')\n")
    (seed / "requirements.txt").write_text("")
    (seed / ".gitea" / "workflows" / "build.yml").write_text("bad workflow\n")
    (seed / "deploy" / "20-deployment.yaml").write_text("bad deploy\n")
    (seed / "Dockerfile").write_text("FROM bad\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(source_bare))
    _git(seed, "push", "-u", "origin", "main")
    source_sha = _git(seed, "rev-parse", "HEAD").strip()

    runtime_sha = commit_and_push_runtime_from_repo(
        name="demo-agent",
        entrypoint="agent:DemoAgent",
        source_repo_url=str(source_bare),
        source_sha=source_sha,
        push_url=str(runtime_bare),
    )

    _git(tmp_path, "clone", "--branch", "main", str(runtime_bare), str(clone))
    workflow = (clone / ".gitea" / "workflows" / "build.yml").read_text()
    deployment = (clone / "deploy" / "20-deployment.yaml").read_text()
    dockerfile = (clone / "Dockerfile").read_text()
    assert _git(clone, "rev-parse", "HEAD").strip() == runtime_sha
    assert f'SOURCE_SHA: "{source_sha}"' in workflow
    assert f'IMAGE_TAG: "{source_sha}"' in workflow
    assert "bad workflow" not in workflow
    assert "bad deploy" not in deployment
    assert f"image: registry.example.com/agents/demo-agent:{source_sha}" in deployment
    assert f"value: registry.example.com/agents/demo-agent:{source_sha}" in deployment
    assert f'a2a/rebuild-id: "{source_sha}"' in deployment
    assert "FROM bad" not in dockerfile


def test_commit_and_push_runtime_from_repo_requires_python_requirements(
    tmp_path: Path,
) -> None:
    source_bare = tmp_path / "source.git"
    runtime_bare = tmp_path / "runtime.git"
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "--bare", str(source_bare))
    _git(tmp_path, "init", "--bare", str(runtime_bare))
    _git(tmp_path, "init", "-b", "main", str(seed))
    _git(seed, "config", "user.email", "test@example.com")
    _git(seed, "config", "user.name", "test")
    (seed / "a2a.yaml").write_text(
        "name: demo-agent\nentrypoint: agent:DemoAgent\n"
    )
    (seed / "agent.py").write_text("print('ok')\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(source_bare))
    _git(seed, "push", "-u", "origin", "main")
    source_sha = _git(seed, "rev-parse", "HEAD").strip()

    with pytest.raises(ValueError, match="requirements.txt"):
        commit_and_push_runtime_from_repo(
            name="demo-agent",
            entrypoint="agent:DemoAgent",
            source_repo_url=str(source_bare),
            source_sha=source_sha,
            push_url=str(runtime_bare),
        )


def test_source_tarball_from_repo_exports_only_user_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bare = tmp_path / "repo.git"
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "--bare", str(bare))
    _git(tmp_path, "init", "-b", "main", str(seed))
    _git(seed, "config", "user.email", "test@example.com")
    _git(seed, "config", "user.name", "test")
    (seed / "deploy").mkdir()
    (seed / ".gitea" / "workflows").mkdir(parents=True)
    (seed / "agent.py").write_text("print('ok')\n")
    (seed / "a2a.yaml").write_text("name: demo-agent\n")
    (seed / "deploy" / "20-deployment.yaml").write_text("image: latest\n")
    (seed / ".gitea" / "workflows" / "build.yml").write_text("name: build\n")
    (seed / "Dockerfile").write_text("FROM base\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(bare))
    _git(seed, "push", "-u", "origin", "main")
    _git(bare, "symbolic-ref", "HEAD", "refs/heads/main")

    monkeypatch.setattr(gitea, "authenticated_repo_url", lambda name: str(bare))
    bundle, head_sha = gitea.source_tarball_from_repo("demo-agent")

    assert head_sha == _git(seed, "rev-parse", "HEAD").strip()
    with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as tf:
        assert tf.getnames() == ["a2a.yaml", "agent.py"]


def _source_tarball(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, body in files.items():
            data = body.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _write_agent_dsl(
    root: Path,
    *,
    language: str = "python",
    command: list[str] | None = None,
) -> None:
    dsl_dir = root / ".a2a"
    dsl_dir.mkdir(parents=True, exist_ok=True)
    (dsl_dir / "agent.dsl.json").write_text(
        _agent_dsl_text(language=language, command=command)
    )


def _agent_dsl_text(
    *,
    language: str = "python",
    command: list[str] | None = None,
) -> str:
    entrypoint = (
        {"module": "agent", "class_name": "DemoAgent", "function": None, "command": None}
        if command is None
        else {"module": None, "class_name": None, "function": None, "command": command}
    )
    return json.dumps(
        {
            "schema_version": "2026-06-04",
            "language": language,
            "name": "demo-agent",
            "description": "Demo agent",
            "version": "0.1.0",
            "entrypoint": entrypoint,
            "skills": [
                {
                    "name": "run",
                    "description": "Run",
                    "handler": "run",
                    "tags": [],
                    "scopes": [],
                    "stream": False,
                    "policy": {
                        "timeout_seconds": None,
                        "idempotent": False,
                        "max_retries": 0,
                        "cost_class": None,
                        "allow_scope_expansion": False,
                        "grant_mode": None,
                        "grant_allow_patterns": [],
                        "grant_deny_patterns": [],
                        "grant_outputs_prefix": None,
                        "grant_write_prefixes": [],
                        "grant_ttl_seconds": None,
                        "grant_run_timeout_seconds": None,
                        "grant_approval_timeout_seconds": None,
                        "grant_scope_approval_timeout_seconds": None,
                    },
                    "input_schema": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                        "additionalProperties": False,
                    },
                    "output_schema": {"type": "object"},
                }
            ],
            "capabilities": {},
            "input_modes": ["application/json"],
            "output_modes": ["application/json"],
            "required_secrets": [],
            "required_env": [],
            "consumer_setup": {"fields": []},
            "runtime": {},
            "template_lineage": None,
            "meta_agent_manifest": None,
            "state_schema": None,
            "workspace_access": {"enabled": False},
            "config_schema": None,
            "auth": {
                "model": "NoAuth",
                "strategy": "public",
                "principal_schema": {
                    "title": "NoAuth",
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                "resolver": None,
                "required": False,
            },
            "metadata": {},
        },
        indent=2,
    )


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_both_deployment_templates_define_the_agent_identity_env() -> None:
    """On-demand and always-on agents must agree on identity env vars.

    ``DEPLOYMENT`` silently lacked ``A2A_AGENT_NAME`` while
    ``ALWAYS_ON_DEPLOYMENT`` had it, so on-demand agents could not build their
    own sign-in URL and their packed UI stayed unauthenticated.
    """
    import re

    from control_plane.scaffold import ALWAYS_ON_DEPLOYMENT, DEPLOYMENT

    def env_names(template: str) -> set[str]:
        return set(re.findall(r"- name: (A2A_[A-Z0-9_]+)", template))

    required = {
        "A2A_AGENT_NAME",
        "A2A_CP_URL",
        "A2A_LOGIN_URL",
        "A2A_SESSION_COOKIE_NAME",
        "A2A_AGENT_SESSION_AUTHORIZE_URL",
    }
    assert required <= env_names(DEPLOYMENT)
    assert required <= env_names(ALWAYS_ON_DEPLOYMENT)
