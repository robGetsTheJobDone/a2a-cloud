from __future__ import annotations

import pytest

from control_plane import devbox, gitea


def test_devbox_names() -> None:
    assert devbox.devbox_service_name("revenue-agent") == "revenue-agent-devbox"
    assert devbox.devbox_secret_name("revenue-agent") == "devbox-revenue-agent"


def test_render_devbox_service_is_scale_to_zero_knative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(devbox, "_resolve_devbox_image", lambda: devbox.DEVBOX_IMAGE)
    doc = devbox.render_devbox_service("revenue-agent", owner_id=7)

    assert doc["apiVersion"] == "serving.knative.dev/v1"
    assert doc["kind"] == "Service"
    assert doc["metadata"]["name"] == "revenue-agent-devbox"

    tmpl = doc["spec"]["template"]
    ann = tmpl["metadata"]["annotations"]
    assert ann["autoscaling.knative.dev/min-scale"] == "0"  # scales to zero
    assert ann["autoscaling.knative.dev/max-scale"] == "1"  # one box

    spec = tmpl["spec"]
    # Long sessions are one long-lived request — needs the raised ceiling.
    assert spec["timeoutSeconds"] == devbox.DEVBOX_TIMEOUT_SECONDS == 28800
    assert spec["responseStartTimeoutSeconds"] == 28800

    container = spec["containers"][0]
    assert container["image"] == devbox.DEVBOX_IMAGE
    assert container["ports"][0]["containerPort"] == 8000
    # The per-session secret (authorized key + access token) is projected in.
    assert container["envFrom"][0]["secretRef"]["name"] == "devbox-revenue-agent"
    env = {e["name"]: e for e in container["env"]}
    assert env["A2A_DEVBOX_PORT"]["value"] == "8000"
    assert env["A2A_AGENT_NAME"]["value"] == "revenue-agent"
    assert container["readinessProbe"]["httpGet"]["path"] == "/healthz"


def test_build_repo_clone_url_uses_scoped_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gitea, "create_repo_clone_token", lambda user, *, name_hint: ("tok123", "n")
    )
    url = devbox.build_repo_clone_url("revenue-agent", "gitea_admin")
    assert url == (
        "http://gitea_admin:tok123@"
        "gitea-http.gitea.svc.cluster.local:3000/gitea_admin/revenue-agent.git"
    )
    # The credential is the scoped token, not a password.
    assert ":tok123@" in url


def test_build_repo_clone_url_is_best_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(user, *, name_hint):  # noqa: ANN001, ANN202
        raise RuntimeError("gitea down")

    monkeypatch.setattr(gitea, "create_repo_clone_token", _boom)
    # Clone is a convenience — failure must not raise, just yields no repo URL.
    assert devbox.build_repo_clone_url("revenue-agent", "gitea_admin") == ""


def test_ensure_devbox_uses_scoped_grant_not_shared_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    monkeypatch.setattr(devbox, "_apply_devbox_service", lambda *a, **k: None)
    monkeypatch.setattr(devbox, "_devbox_host_key", lambda agent: "HOSTKEY")
    monkeypatch.setattr(devbox, "build_repo_clone_url", lambda *a, **k: "")

    def _fake_secret(agent_name, *, data, owner_id):  # noqa: ANN001, ANN202
        captured["data"] = data

    monkeypatch.setattr(devbox, "_upsert_devbox_secret", _fake_secret)
    monkeypatch.setattr(
        devbox.grants,
        "mint_grant_token",
        lambda **kw: ("grant-token", {"audience": kw["audience"]}),
    )

    info = devbox.ensure_devbox(
        agent_name="rev", owner_id=1, gitea_owner=None, public_key="ssh-ed25519 AAA"
    )

    # Transport credential is the minted grant, returned to the caller.
    assert info["access_token"] == "grant-token"
    assert info["wss_url"].endswith("/ssh")
    # The box secret carries the authorized key but NEVER a shared access token.
    assert captured["data"]["A2A_DEVBOX_AUTHORIZED_KEYS"] == "ssh-ed25519 AAA"
    assert "A2A_DEVBOX_ACCESS_TOKEN" not in captured["data"]

def test_ensure_devbox_injects_credentials_when_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    monkeypatch.setattr(devbox, "_apply_devbox_service", lambda *a, **k: None)
    monkeypatch.setattr(devbox, "_devbox_host_key", lambda agent: "HOSTKEY")
    monkeypatch.setattr(devbox, "build_repo_clone_url", lambda *a, **k: "")
    monkeypatch.setattr(
        devbox, "_upsert_devbox_secret",
        lambda agent_name, *, data, owner_id: captured.update(data=data),
    )
    monkeypatch.setattr(
        devbox.grants, "mint_grant_token", lambda **kw: ("t", {})
    )

    devbox.ensure_devbox(
        agent_name="rev", owner_id=1, gitea_owner=None,
        public_key="ssh-ed25519 AAA", credentials_json='{"token": "x"}',
    )
    assert captured["data"]["A2A_CREDENTIALS_JSON"] == '{"token": "x"}'

    devbox.ensure_devbox(
        agent_name="rev", owner_id=1, gitea_owner=None, public_key="ssh-ed25519 AAA"
    )
    assert "A2A_CREDENTIALS_JSON" not in captured["data"]

def test_resolve_devbox_image_accepts_operator_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = "registry.example.com/a2a/a2a-devbox@sha256:" + "a" * 64
    monkeypatch.setattr(devbox, "DEVBOX_IMAGE", image)

    assert devbox._resolve_devbox_image() == image


def test_resolve_devbox_image_rejects_mutable_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        devbox,
        "DEVBOX_IMAGE",
        "registry.example.com/a2a/a2a-devbox:latest",
    )

    with pytest.raises(RuntimeError, match="independently verified"):
        devbox._resolve_devbox_image()


def test_devbox_host_key_reused_from_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    import base64 as b64

    class _Secret:
        data = {"A2A_DEVBOX_HOST_KEY_ED25519": b64.b64encode(b"EXISTING").decode()}

    class _Core:
        def read_namespaced_secret(self, name, ns):  # noqa: ANN001, ANN202
            return _Secret()

    monkeypatch.setattr(devbox, "_load_kube", lambda: None)
    monkeypatch.setattr(devbox.client, "CoreV1Api", lambda: _Core())
    assert devbox._devbox_host_key("rev") == "EXISTING"


def test_devbox_host_key_generated_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from kubernetes.client.rest import ApiException

    class _Core:
        def read_namespaced_secret(self, name, ns):  # noqa: ANN001, ANN202
            raise ApiException(status=404)

    monkeypatch.setattr(devbox, "_load_kube", lambda: None)
    monkeypatch.setattr(devbox.client, "CoreV1Api", lambda: _Core())
    key = devbox._devbox_host_key("rev")
    assert key.startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert key.rstrip().endswith("-----END OPENSSH PRIVATE KEY-----")
