from __future__ import annotations

import base64
import io
import json
import urllib.error
from types import SimpleNamespace
from typing import Any

from kubernetes.client.rest import ApiException

from control_plane import agent_secrets


class _FakeResponse:
    def __init__(self, payload: dict[str, Any] | None = None, *, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        if self._payload is None:
            return b""
        return json.dumps(self._payload).encode("utf-8")


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def test_ensure_agent_secret_projection_patches_deployment(monkeypatch) -> None:
    patches: list[dict[str, Any]] = []

    class FakeAppsV1Api:
        def read_namespaced_deployment(
            self,
            name: str,
            namespace: str,
        ) -> SimpleNamespace:
            assert name == "demo-agent"
            assert namespace == "agents"
            return SimpleNamespace(
                spec=SimpleNamespace(
                    template=SimpleNamespace(
                        spec=SimpleNamespace(
                            containers=[
                                SimpleNamespace(
                                    name="agent",
                                    env_from=[
                                        SimpleNamespace(
                                            config_map_ref=SimpleNamespace(
                                                name="shared",
                                                optional=False,
                                            ),
                                            secret_ref=None,
                                        )
                                    ],
                                ),
                            ],
                        ),
                    ),
                ),
            )

        def patch_namespaced_deployment(
            self,
            name: str,
            namespace: str,
            patch: dict[str, Any],
        ) -> None:
            assert name == "demo-agent"
            assert namespace == "agents"
            patches.append(patch)

    monkeypatch.setattr(agent_secrets, "_load_kube", lambda: None)
    monkeypatch.setattr(agent_secrets.client, "AppsV1Api", FakeAppsV1Api)
    monkeypatch.setattr(agent_secrets.settings, "in_cluster", False)
    monkeypatch.setattr(agent_secrets.settings, "agents_namespace", "agents")
    monkeypatch.setattr(agent_secrets.time, "time", lambda: 1234567890)

    result = agent_secrets.ensure_agent_secret_projection("demo-agent")

    assert result == {"ok": True, "secret_name": "demo-agent-agent-secrets"}
    assert patches == [
        {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {"a2a/secrets-revision": "1234567890"},
                    },
                    "spec": {
                        "containers": [
                            {
                                "name": "agent",
                                "envFrom": [
                                    {"configMapRef": {"name": "shared", "optional": False}},
                                    {
                                        "secretRef": {
                                            "name": "demo-agent-agent-secrets",
                                            "optional": True,
                                        }
                                    },
                                ],
                            }
                        ]
                    },
                }
            }
        }
    ]


def test_ensure_agent_secret_projection_patches_knative_service_when_deployment_missing(
    monkeypatch,
) -> None:
    patches: list[dict[str, Any]] = []

    class FakeAppsV1Api:
        def read_namespaced_deployment(
            self,
            name: str,
            namespace: str,
        ) -> None:
            assert name == "demo-agent"
            assert namespace == "agents"
            raise ApiException(status=404, reason="Not Found")

    class FakeCustomObjectsApi:
        def get_namespaced_custom_object(self, *args: object) -> dict[str, Any]:
            return {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "agent",
                                    "image": "registry.a2acloud.io/agents/demo:latest",
                                    "ports": [{"containerPort": 8000, "name": "http1"}],
                                    "envFrom": [{"configMapRef": {"name": "shared"}}],
                                }
                            ]
                        }
                    }
                }
            }

        def patch_namespaced_custom_object(self, *args: object) -> None:
            patches.append(args[-1])

    monkeypatch.setattr(agent_secrets, "_load_kube", lambda: None)
    monkeypatch.setattr(agent_secrets.client, "CustomObjectsApi", FakeCustomObjectsApi)
    monkeypatch.setattr(agent_secrets.client, "AppsV1Api", FakeAppsV1Api)
    monkeypatch.setattr(agent_secrets.settings, "in_cluster", False)
    monkeypatch.setattr(agent_secrets.settings, "agents_namespace", "agents")
    monkeypatch.setattr(agent_secrets.time, "time", lambda: 1234567890)

    result = agent_secrets.ensure_agent_secret_projection("demo-agent")

    assert result == {
        "ok": True,
        "secret_name": "demo-agent-agent-secrets",
        "runtime": "knative_service",
    }
    assert patches == [
        {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {"a2a/secrets-revision": "1234567890"},
                    },
                    "spec": {
                        "containers": [
                            {
                                "name": "agent",
                                "image": "registry.a2acloud.io/agents/demo:latest",
                                "ports": [{"containerPort": 8000, "name": "http1"}],
                                "envFrom": [
                                    {"configMapRef": {"name": "shared"}},
                                    {
                                        "secretRef": {
                                            "name": "demo-agent-agent-secrets",
                                            "optional": True,
                                        }
                                    }
                                ],
                            }
                        ]
                    },
                }
            }
        }
    ]


def test_upsert_agent_secret_value_uses_raw_in_cluster_client(
    monkeypatch,
    tmp_path,
) -> None:
    token_path = tmp_path / "token"
    token_path.write_text("service-account-token", encoding="utf-8")
    calls: list[tuple[str, str]] = []
    patch_body: dict[str, Any] | None = None

    def fake_urlopen(request: Any, *, context: Any, timeout: int) -> _FakeResponse:
        nonlocal patch_body
        assert context == "ssl-context"
        assert timeout == 15
        assert request.get_header("Authorization") == "Bearer service-account-token"
        calls.append((request.get_method(), request.full_url))
        if request.get_method() == "GET":
            return _FakeResponse({"data": {"OLD": _b64("old")}})
        if request.get_method() == "PATCH":
            assert request.get_header("Content-type") == "application/merge-patch+json"
            patch_body = json.loads(request.data.decode("utf-8"))
            return _FakeResponse({})
        raise AssertionError(f"unexpected method {request.get_method()}")

    monkeypatch.setattr(agent_secrets.settings, "in_cluster", True)
    monkeypatch.setattr(agent_secrets.settings, "agents_namespace", "agents")
    monkeypatch.setattr(agent_secrets, "SERVICE_ACCOUNT_TOKEN_PATH", str(token_path))
    monkeypatch.setattr(
        agent_secrets.ssl,
        "create_default_context",
        lambda *, cafile: "ssl-context",
    )
    monkeypatch.setattr(agent_secrets.urllib.request, "urlopen", fake_urlopen)

    agent_secrets.upsert_agent_secret_value(
        agent_name="demo-agent",
        key="NEW",
        value="new",
        owner_id=42,
    )

    assert calls == [
        (
            "GET",
            "https://kubernetes.default.svc"
            "/api/v1/namespaces/agents/secrets/demo-agent-agent-secrets",
        ),
        (
            "PATCH",
            "https://kubernetes.default.svc"
            "/api/v1/namespaces/agents/secrets/demo-agent-agent-secrets",
        ),
    ]
    assert patch_body is not None
    assert patch_body["metadata"]["annotations"]["a2a/owner-id"] == "42"
    assert patch_body["data"] == {"OLD": _b64("old"), "NEW": _b64("new")}


def test_ensure_agent_secret_projection_uses_raw_knative_in_cluster(
    monkeypatch,
    tmp_path,
) -> None:
    token_path = tmp_path / "token"
    token_path.write_text("service-account-token", encoding="utf-8")
    calls: list[tuple[str, str]] = []
    patch_body: dict[str, Any] | None = None

    def fake_urlopen(request: Any, *, context: Any, timeout: int) -> _FakeResponse:
        nonlocal patch_body
        assert context == "ssl-context"
        assert timeout == 15
        assert request.get_header("Authorization") == "Bearer service-account-token"
        calls.append((request.get_method(), request.full_url))
        if "/apis/apps/v1/" in request.full_url:
            raise urllib.error.HTTPError(
                request.full_url,
                404,
                "Not Found",
                {},
                io.BytesIO(b'{"message":"not found"}'),
            )
        if request.get_method() == "GET":
            return _FakeResponse(
                {
                    "spec": {
                        "template": {
                            "spec": {
                                "containers": [
                                    {
                                        "name": "agent",
                                        "image": "demo:latest",
                                        "envFrom": [{"configMapRef": {"name": "shared"}}],
                                    }
                                ]
                            }
                        }
                    }
                }
            )
        if request.get_method() == "PATCH":
            assert request.get_header("Content-type") == "application/merge-patch+json"
            patch_body = json.loads(request.data.decode("utf-8"))
            return _FakeResponse({})
        raise AssertionError(f"unexpected method {request.get_method()}")

    monkeypatch.setattr(agent_secrets.settings, "in_cluster", True)
    monkeypatch.setattr(agent_secrets.settings, "agents_namespace", "agents")
    monkeypatch.setattr(agent_secrets.time, "time", lambda: 1234567890)
    monkeypatch.setattr(agent_secrets, "SERVICE_ACCOUNT_TOKEN_PATH", str(token_path))
    monkeypatch.setattr(
        agent_secrets.ssl,
        "create_default_context",
        lambda *, cafile: "ssl-context",
    )
    monkeypatch.setattr(agent_secrets.urllib.request, "urlopen", fake_urlopen)

    result = agent_secrets.ensure_agent_secret_projection("demo-agent")

    assert result == {
        "ok": True,
        "secret_name": "demo-agent-agent-secrets",
        "runtime": "knative_service",
    }
    assert calls == [
        (
            "GET",
            "https://kubernetes.default.svc"
            "/apis/apps/v1/namespaces/agents/deployments/demo-agent",
        ),
        (
            "GET",
            "https://kubernetes.default.svc"
            "/apis/serving.knative.dev/v1/namespaces/agents/services/demo-agent",
        ),
        (
            "PATCH",
            "https://kubernetes.default.svc"
            "/apis/serving.knative.dev/v1/namespaces/agents/services/demo-agent",
        ),
    ]
    assert patch_body is not None
    container = patch_body["spec"]["template"]["spec"]["containers"][0]
    assert container["envFrom"] == [
        {"configMapRef": {"name": "shared"}},
        {"secretRef": {"name": "demo-agent-agent-secrets", "optional": True}},
    ]


def test_delete_agent_runtime_secret_ignores_in_cluster_http_404(
    monkeypatch,
    tmp_path,
) -> None:
    token_path = tmp_path / "token"
    token_path.write_text("service-account-token", encoding="utf-8")
    opened_urls: list[str] = []

    def fake_urlopen(request: Any, *, context: Any, timeout: int) -> None:
        assert context == "ssl-context"
        assert timeout == 15
        opened_urls.append(request.full_url)
        raise urllib.error.HTTPError(
            request.full_url,
            404,
            "Not Found",
            {},
            io.BytesIO(b'{"message":"not found"}'),
        )

    monkeypatch.setattr(agent_secrets, "_load_kube", lambda: None)
    monkeypatch.setattr(agent_secrets.client, "CoreV1Api", lambda: object())
    monkeypatch.setattr(agent_secrets.settings, "in_cluster", True)
    monkeypatch.setattr(agent_secrets.settings, "agents_namespace", "agents")
    monkeypatch.setattr(agent_secrets, "SERVICE_ACCOUNT_TOKEN_PATH", str(token_path))
    monkeypatch.setattr(
        agent_secrets.ssl,
        "create_default_context",
        lambda *, cafile: "ssl-context",
    )
    monkeypatch.setattr(agent_secrets.urllib.request, "urlopen", fake_urlopen)

    agent_secrets.delete_agent_runtime_secret("demo-agent")

    assert opened_urls == [
        "https://kubernetes.default.svc"
        "/api/v1/namespaces/agents/secrets/demo-agent-agent-secrets"
    ]
