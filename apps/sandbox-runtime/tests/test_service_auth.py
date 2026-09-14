from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from a2a_pack.grants import mint_grant
from a2a_pack.workspace import WorkspaceMode
from sandbox_runtime import service


class _NoopClient:
    async def create(self, spec):  # pragma: no cover - invalid-bucket test must not call
        raise AssertionError("create should not be called")


class _RecordingClient:
    def __init__(self) -> None:
        self.created = []
        self.removed = []

    async def create(self, spec):
        self.created.append(spec)

    async def remove(self, name):
        self.removed.append(name)


@pytest.fixture(autouse=True)
def _reset_live_sessions(monkeypatch):
    monkeypatch.delenv("A2A_SANDBOX_MAX_LIVE_SESSIONS", raising=False)
    monkeypatch.delenv("A2A_SANDBOX_MAX_LIVE_PER_BUCKET", raising=False)
    monkeypatch.delenv("A2A_SB_VM_MEMORY_MIB", raising=False)
    monkeypatch.delenv("A2A_SB_VM_CPU_COUNT", raising=False)
    service._sandbox_buckets.clear()  # noqa: SLF001
    service._sandbox_grant_ids.clear()  # noqa: SLF001
    yield
    service._sandbox_buckets.clear()  # noqa: SLF001
    service._sandbox_grant_ids.clear()  # noqa: SLF001


def test_execution_endpoints_require_configured_auth(monkeypatch):
    monkeypatch.delenv("A2A_SANDBOX_TOKEN", raising=False)
    monkeypatch.delenv("A2A_SANDBOX_ALLOW_UNAUTH", raising=False)
    client = TestClient(service.app)

    res = client.post("/v1/run_shell", json={"bucket": "user-1-files", "script": "true"})

    assert res.status_code == 401
    assert "missing sandbox grant" in res.text


def test_execution_endpoints_reject_bad_bearer(monkeypatch):
    monkeypatch.setenv("A2A_SANDBOX_TOKEN", "secret-token")
    client = TestClient(service.app)

    res = client.post("/v1/run_shell", json={"bucket": "user-1-files", "script": "true"})

    assert res.status_code == 401


def test_bucket_validation_happens_before_sandbox_create(monkeypatch):
    monkeypatch.setenv("A2A_SANDBOX_TOKEN", "secret-token")
    monkeypatch.setattr(service, "_client", _NoopClient())
    client = TestClient(service.app)

    res = client.post(
        "/v1/run_shell",
        headers={"authorization": "Bearer secret-token"},
        json={"bucket": "../../other", "script": "true"},
    )

    assert res.status_code == 400
    assert "invalid workspace bucket" in res.text


def test_persistent_create_forwards_workspace_write_policy_label(monkeypatch):
    monkeypatch.setenv("A2A_SANDBOX_TOKEN", "secret-token")
    runtime = _RecordingClient()
    monkeypatch.setattr(service, "_client", runtime)
    client = TestClient(service.app)

    res = client.post(
        "/v1/sandboxes",
        headers={"authorization": "Bearer secret-token"},
        json={
            "name": "sb-1",
            "bucket": "user-1-files",
            "labels": {"workspace_write_policy": "workspace"},
        },
    )

    assert res.status_code == 200
    assert res.json()["name"].startswith("sb-")
    assert res.json()["name"] != "sb-1"
    assert runtime.created[0].name == res.json()["name"]
    assert runtime.created[0].labels == {
        "workspace_write_policy": "workspace",
        "network_disabled": "false",
    }


def test_caller_names_cannot_collide_across_buckets(monkeypatch):
    monkeypatch.delenv("A2A_SANDBOX_TOKEN", raising=False)
    runtime = _RecordingClient()
    monkeypatch.setattr(service, "_client", runtime)
    client = TestClient(service.app)
    _, first_token = mint_grant(
        issuer="control-plane",
        audience="agent-builder",
        bucket="user-1-files",
        mode=WorkspaceMode.READ_WRITE_OVERLAY,
    )
    _, second_token = mint_grant(
        issuer="control-plane",
        audience="agent-builder",
        bucket="user-2-files",
        mode=WorkspaceMode.READ_WRITE_OVERLAY,
    )

    first = client.post(
        "/v1/sandboxes",
        headers={"X-A2A-Grant": first_token},
        json={"name": "shared", "bucket": "user-1-files"},
    )
    second = client.post(
        "/v1/sandboxes",
        headers={"X-A2A-Grant": second_token},
        json={"name": "shared", "bucket": "user-2-files"},
    )

    assert first.status_code == second.status_code == 200
    assert first.json()["name"] != second.json()["name"]
    assert {spec.workspace for spec in runtime.created} == {
        "user-1-files",
        "user-2-files",
    }
    wrong_tenant = client.delete(
        f"/v1/sandboxes/{first.json()['name']}",
        headers={"X-A2A-Grant": second_token},
    )
    assert wrong_tenant.status_code == 403
    assert runtime.removed == []


def test_persistent_session_is_bound_to_creating_grant(monkeypatch):
    monkeypatch.delenv("A2A_SANDBOX_TOKEN", raising=False)
    runtime = _RecordingClient()
    monkeypatch.setattr(service, "_client", runtime)
    _, owner_token = mint_grant(
        issuer="control-plane",
        audience="agent-builder",
        bucket="user-1-files",
        mode=WorkspaceMode.READ_WRITE_OVERLAY,
    )
    _, sibling_token = mint_grant(
        issuer="control-plane",
        audience="agent-reviewer",
        bucket="user-1-files",
        mode=WorkspaceMode.READ_WRITE_OVERLAY,
    )
    client = TestClient(service.app)

    created = client.post(
        "/v1/sandboxes",
        headers={"X-A2A-Grant": owner_token},
        json={"bucket": "user-1-files"},
    )
    name = created.json()["name"]

    sibling_exec = client.post(
        f"/v1/sandboxes/{name}/exec",
        headers={"X-A2A-Grant": sibling_token},
        json={"script": "echo must-not-run"},
    )
    sibling_delete = client.delete(
        f"/v1/sandboxes/{name}",
        headers={"X-A2A-Grant": sibling_token},
    )

    assert sibling_exec.status_code == 403
    assert sibling_delete.status_code == 403
    assert runtime.removed == []
    assert client.delete(
        f"/v1/sandboxes/{name}",
        headers={"X-A2A-Grant": owner_token},
    ).status_code == 204


def test_global_and_per_bucket_live_session_caps(monkeypatch):
    monkeypatch.setenv("A2A_SANDBOX_TOKEN", "secret-token")
    monkeypatch.setenv("A2A_SANDBOX_MAX_LIVE_SESSIONS", "3")
    monkeypatch.setenv("A2A_SANDBOX_MAX_LIVE_PER_BUCKET", "1")
    runtime = _RecordingClient()
    monkeypatch.setattr(service, "_client", runtime)
    client = TestClient(service.app)
    headers = {"authorization": "Bearer secret-token"}

    assert client.post(
        "/v1/sandboxes", headers=headers, json={"bucket": "user-1-files"}
    ).status_code == 200
    same_bucket = client.post(
        "/v1/sandboxes", headers=headers, json={"bucket": "user-1-files"}
    )
    assert same_bucket.status_code == 429
    assert "bucket" in same_bucket.text
    assert client.post(
        "/v1/sandboxes", headers=headers, json={"bucket": "user-2-files"}
    ).status_code == 200
    assert client.post(
        "/v1/sandboxes", headers=headers, json={"bucket": "user-3-files"}
    ).status_code == 200
    global_limit = client.post(
        "/v1/sandboxes", headers=headers, json={"bucket": "user-4-files"}
    )
    assert global_limit.status_code == 429
    assert "capacity" in global_limit.text


def test_capacity_reservation_precedes_slow_vm_create(monkeypatch):
    class _BlockingClient(_RecordingClient):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def create(self, spec):
            self.created.append(spec)
            self.started.set()
            await self.release.wait()

    async def _run() -> None:
        runtime = _BlockingClient()
        monkeypatch.setattr(service, "_client", runtime)
        first = asyncio.create_task(
            service.create_sandbox(
                service._CreateIn(name="collision", bucket="user-1-files"),  # noqa: SLF001
                authorization="Bearer secret-token",
                x_a2a_grant=None,
            )
        )
        await runtime.started.wait()
        with pytest.raises(HTTPException) as exc_info:
            await service.create_sandbox(
                service._CreateIn(name="collision", bucket="user-2-files"),  # noqa: SLF001
                authorization="Bearer secret-token",
                x_a2a_grant=None,
            )
        assert exc_info.value.status_code == 429
        runtime.release.set()
        await first

    monkeypatch.setenv("A2A_SANDBOX_TOKEN", "secret-token")
    monkeypatch.setenv("A2A_SANDBOX_MAX_LIVE_SESSIONS", "1")
    monkeypatch.setenv("A2A_SANDBOX_MAX_LIVE_PER_BUCKET", "1")
    asyncio.run(_run())


def test_create_failure_and_remove_clean_bucket_reservations(monkeypatch):
    class _FailingClient(_RecordingClient):
        async def create(self, spec):
            self.created.append(spec)
            raise RuntimeError("vm failed")

    async def _failed_create() -> None:
        runtime = _FailingClient()
        monkeypatch.setattr(service, "_client", runtime)
        with pytest.raises(RuntimeError, match="vm failed"):
            await service.create_sandbox(
                service._CreateIn(bucket="user-1-files"),  # noqa: SLF001
                authorization="Bearer secret-token",
                x_a2a_grant=None,
            )
        assert service._sandbox_buckets == {}  # noqa: SLF001
        assert len(runtime.removed) == 1

    monkeypatch.setenv("A2A_SANDBOX_TOKEN", "secret-token")
    asyncio.run(_failed_create())

    runtime = _RecordingClient()
    monkeypatch.setattr(service, "_client", runtime)
    client = TestClient(service.app)
    headers = {"authorization": "Bearer secret-token"}
    created = client.post(
        "/v1/sandboxes", headers=headers, json={"bucket": "user-1-files"}
    )
    name = created.json()["name"]
    assert client.delete(f"/v1/sandboxes/{name}", headers=headers).status_code == 204
    assert service._sandbox_buckets == {}  # noqa: SLF001


def test_operator_resource_caps_reject_oversized_vm_before_create(monkeypatch):
    monkeypatch.setenv("A2A_SANDBOX_TOKEN", "secret-token")
    monkeypatch.setenv("A2A_SB_VM_MEMORY_MIB", "256")
    monkeypatch.setenv("A2A_SB_VM_CPU_COUNT", "1")
    runtime = _RecordingClient()
    monkeypatch.setattr(service, "_client", runtime)
    client = TestClient(service.app)

    memory = client.post(
        "/v1/sandboxes",
        headers={"authorization": "Bearer secret-token"},
        json={"bucket": "user-1-files", "memory_mib": 512},
    )
    cpu = client.post(
        "/v1/sandboxes",
        headers={"authorization": "Bearer secret-token"},
        json={"bucket": "user-1-files", "memory_mib": 256, "cpus": 2},
    )

    assert memory.status_code == 400
    assert cpu.status_code == 400
    assert runtime.created == []
    assert service._sandbox_buckets == {}  # noqa: SLF001


def test_persistent_create_preserves_network_disabled_label(monkeypatch):
    monkeypatch.setenv("A2A_SANDBOX_TOKEN", "secret-token")
    runtime = _RecordingClient()
    monkeypatch.setattr(service, "_client", runtime)
    client = TestClient(service.app)

    response = client.post(
        "/v1/sandboxes",
        headers={"authorization": "Bearer secret-token"},
        json={
            "bucket": "user-1-files",
            "labels": {"network_disabled": "true"},
        },
    )

    assert response.status_code == 200
    assert runtime.created[0].labels["network_disabled"] == "true"


def test_one_shot_uses_and_releases_live_session_reservation(monkeypatch):
    class _Handle:
        stopped = False

        async def shell(self, _script, *, timeout):
            return types.SimpleNamespace(
                stdout="ok",
                stderr="",
                exit_code=0,
                files=(),
            )

        async def stop(self):
            self.stopped = True

    class _OneShotClient(_RecordingClient):
        def __init__(self):
            super().__init__()
            self.handle = _Handle()

        async def create(self, spec):
            self.created.append(spec)
            return self.handle

    monkeypatch.setenv("A2A_SANDBOX_TOKEN", "secret-token")
    monkeypatch.setenv("A2A_SANDBOX_MAX_LIVE_SESSIONS", "1")
    runtime = _OneShotClient()
    monkeypatch.setattr(service, "_client", runtime)
    client = TestClient(service.app)

    response = client.post(
        "/v1/run_shell",
        headers={"authorization": "Bearer secret-token"},
        json={"bucket": "user-1-files", "script": "true"},
    )

    assert response.status_code == 200
    assert runtime.created[0].name.startswith("sb-")
    assert runtime.handle.stopped is True
    assert runtime.removed == [runtime.created[0].name]
    assert service._sandbox_buckets == {}  # noqa: SLF001
    assert service._sandbox_grant_ids == {}  # noqa: SLF001


def test_lifespan_cleanup_clears_handles_and_bucket_mappings(monkeypatch):
    runtime = _RecordingClient()

    class _LifecycleClient(_RecordingClient):
        async def list(self):
            return ["sb-live"]

    runtime = _LifecycleClient()
    monkeypatch.setattr(service, "LocalMicrosandboxClient", lambda **kwargs: runtime)

    async def _run() -> None:
        async with service.lifespan(service.app):
            service._sandbox_buckets["sb-live"] = "user-1-files"  # noqa: SLF001
        assert runtime.removed == ["sb-live"]
        assert service._sandbox_buckets == {}  # noqa: SLF001
        assert service._sandbox_grant_ids == {}  # noqa: SLF001
        assert service._client is None  # noqa: SLF001

    asyncio.run(_run())


@pytest.mark.parametrize(
    "image",
    [
        "http://127.0.0.1:5000/private:latest",
        "https://169.254.169.254/metadata:latest",
        "169.254.169.254/repository:latest",
        "localhost:5000/repository:latest",
        "registry.example/repository:latest",
        "python:3.11-slim?redirect=http://127.0.0.1",
        "python:3.11-slim#fragment",
        "python:3.11-slim/../private:latest",
    ],
)
def test_create_rejects_caller_selected_images_outside_allowlist(monkeypatch, image):
    monkeypatch.setenv("A2A_SANDBOX_TOKEN", "secret-token")
    monkeypatch.setenv("A2A_SANDBOX_DEFAULT_IMAGE", "python:3.11-slim")
    monkeypatch.setenv("A2A_SANDBOX_ALLOWED_IMAGES", "python:3.11-slim")
    runtime = _RecordingClient()
    monkeypatch.setattr(service, "_client", runtime)
    client = TestClient(service.app)

    res = client.post(
        "/v1/sandboxes",
        headers={"authorization": "Bearer secret-token"},
        json={"name": "sb-image", "bucket": "user-1-files", "image": image},
    )

    assert res.status_code == 400
    assert "operator allowlist" in res.text
    assert runtime.created == []


def test_create_resolves_operator_default_image(monkeypatch):
    image = "python:3.11-slim@sha256:" + "a" * 64
    monkeypatch.setenv("A2A_SANDBOX_TOKEN", "secret-token")
    monkeypatch.setenv("A2A_SANDBOX_DEFAULT_IMAGE", image)
    monkeypatch.setenv("A2A_SANDBOX_ALLOWED_IMAGES", image)
    monkeypatch.setenv("A2A_SANDBOX_REQUIRE_IMAGE_DIGEST", "true")
    runtime = _RecordingClient()
    monkeypatch.setattr(service, "_client", runtime)
    client = TestClient(service.app)

    res = client.post(
        "/v1/sandboxes",
        headers={"authorization": "Bearer secret-token"},
        json={"name": "sb-default", "bucket": "user-1-files"},
    )

    assert res.status_code == 200
    assert runtime.created[0].image == image


def test_approved_mutable_name_is_resolved_to_operator_digest(monkeypatch):
    image = "python:3.11-slim@sha256:" + "a" * 64
    monkeypatch.setenv("A2A_SANDBOX_TOKEN", "secret-token")
    monkeypatch.setenv("A2A_SANDBOX_DEFAULT_IMAGE", image)
    monkeypatch.setenv("A2A_SANDBOX_ALLOWED_IMAGES", image)
    monkeypatch.setenv(
        "A2A_SANDBOX_IMAGE_ALIASES",
        json.dumps({"python:3.11-slim": image}),
    )
    monkeypatch.setenv("A2A_SANDBOX_REQUIRE_IMAGE_DIGEST", "true")
    runtime = _RecordingClient()
    monkeypatch.setattr(service, "_client", runtime)
    client = TestClient(service.app)

    res = client.post(
        "/v1/sandboxes",
        headers={"authorization": "Bearer secret-token"},
        json={
            "name": "sb-alias",
            "bucket": "user-1-files",
            "image": "python:3.11-slim",
        },
    )

    assert res.status_code == 200
    assert runtime.created[0].image == image


def test_production_image_policy_rejects_mutable_tags(monkeypatch):
    monkeypatch.setenv("A2A_SANDBOX_TOKEN", "secret-token")
    monkeypatch.setenv("A2A_SANDBOX_DEFAULT_IMAGE", "python:3.11-slim")
    monkeypatch.setenv("A2A_SANDBOX_ALLOWED_IMAGES", "python:3.11-slim")
    monkeypatch.setenv("A2A_SANDBOX_REQUIRE_IMAGE_DIGEST", "true")
    monkeypatch.setattr(service, "_client", _RecordingClient())
    client = TestClient(service.app)

    res = client.post(
        "/v1/sandboxes",
        headers={"authorization": "Bearer secret-token"},
        json={"name": "sb-tag", "bucket": "user-1-files"},
    )

    assert res.status_code == 503
    assert "misconfigured" in res.text


def test_grant_auth_forwards_path_policy_labels(monkeypatch):
    monkeypatch.delenv("A2A_SANDBOX_TOKEN", raising=False)
    runtime = _RecordingClient()
    monkeypatch.setattr(service, "_client", runtime)
    _, token = mint_grant(
        issuer="control-plane",
        audience="agent-builder",
        bucket="user-1-files",
        mode=WorkspaceMode.READ_WRITE_OVERLAY,
        allow_patterns=("agents/demo/**",),
        outputs_prefix="agents/demo",
        write_prefixes=("agents/demo",),
    )
    client = TestClient(service.app)

    res = client.post(
        "/v1/sandboxes",
        headers={"X-A2A-Grant": token},
        json={"name": "sb-grant", "bucket": "user-1-files"},
    )

    assert res.status_code == 200
    assert runtime.created[0].labels["workspace_allow_patterns"] == '["agents/demo/**"]'
    assert runtime.created[0].labels["workspace_mode"] == "read_write_overlay"
    assert runtime.created[0].labels["workspace_outputs_prefix"] == "agents/demo"
    assert runtime.created[0].labels["workspace_write_prefixes"] == '["agents/demo/"]'


def test_grant_auth_accepts_current_source_grants_payload(monkeypatch):
    monkeypatch.delenv("A2A_SANDBOX_TOKEN", raising=False)
    runtime = _RecordingClient()
    monkeypatch.setattr(service, "_client", runtime)
    _, token = mint_grant(
        issuer="control-plane",
        audience="agent-builder",
        bucket="user-1-files",
        mode=WorkspaceMode.READ_WRITE_OVERLAY,
        allow_patterns=("agents/demo/**",),
        outputs_prefix="agents/demo",
        write_prefixes=("agents/demo",),
        source_grants=({"agent": "code-editor-agent", "scope": "read"},),
    )
    client = TestClient(service.app)

    res = client.post(
        "/v1/sandboxes",
        headers={"X-A2A-Grant": token},
        json={"name": "sb-grant-source", "bucket": "user-1-files"},
    )

    assert res.status_code == 200
    assert runtime.created[0].labels["workspace_mode"] == "read_write_overlay"


def test_healthz_does_not_require_auth(monkeypatch):
    monkeypatch.delenv("A2A_SANDBOX_TOKEN", raising=False)
    client = TestClient(service.app)

    assert client.get("/healthz").status_code == 200


def test_run_configures_uvicorn_concurrency_limit(monkeypatch):
    calls = {}

    def fake_run(*args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs

    monkeypatch.setitem(sys.modules, "uvicorn", types.SimpleNamespace(run=fake_run))
    monkeypatch.setenv("A2A_SANDBOX_LIMIT_CONCURRENCY", "17")

    service.run()

    assert calls["kwargs"]["limit_concurrency"] == 17
