from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from control_plane import runtime_upgrade
from control_plane.runtime_upgrade import (
    bump_agent_runtime_repo,
    current_a2a_pack_version,
    runtime_upgrade_image_tag,
    runtime_upgrade_status,
)


@pytest.fixture(autouse=True)
def _reset_latest_a2a_pack_cache():
    """Keep the module-level latest-version memo from leaking across tests."""
    runtime_upgrade._latest_cache.update(version=None, at=0.0)
    yield
    runtime_upgrade._latest_cache.update(version=None, at=0.0)


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *_: object) -> bool:
        return False


def test_latest_a2a_pack_version_env_override_wins(monkeypatch) -> None:
    monkeypatch.setenv("A2A_CP_A2A_PACK_VERSION", "9.9.9")
    monkeypatch.setattr(
        runtime_upgrade, "_detect_latest_from_registry", lambda: "0.1.37"
    )
    assert runtime_upgrade.latest_a2a_pack_version() == "9.9.9"


def test_latest_a2a_pack_version_autodetects_from_registry(monkeypatch) -> None:
    monkeypatch.delenv("A2A_CP_A2A_PACK_VERSION", raising=False)
    monkeypatch.setattr(runtime_upgrade.settings, "a2a_pack_registry_auto_detect", True)
    runtime_upgrade._latest_cache.update(version=None, at=0.0)
    monkeypatch.setattr(
        runtime_upgrade, "_detect_latest_from_registry", lambda: "0.1.37"
    )
    assert runtime_upgrade.latest_a2a_pack_version() == "0.1.37"


def test_latest_a2a_pack_version_does_not_probe_registry_by_default(
    monkeypatch,
) -> None:
    monkeypatch.delenv("A2A_CP_A2A_PACK_VERSION", raising=False)
    monkeypatch.setattr(runtime_upgrade.settings, "a2a_pack_registry_auto_detect", False)
    monkeypatch.setattr(
        runtime_upgrade,
        "_detect_latest_from_registry",
        lambda: pytest.fail("authenticated registry must not be probed"),
    )

    assert (
        runtime_upgrade.latest_a2a_pack_version()
        == runtime_upgrade.DEFAULT_A2A_PACK_VERSION
    )


def test_latest_a2a_pack_version_falls_back_to_default(monkeypatch) -> None:
    monkeypatch.delenv("A2A_CP_A2A_PACK_VERSION", raising=False)
    runtime_upgrade._latest_cache.update(version=None, at=0.0)
    monkeypatch.setattr(runtime_upgrade, "_detect_latest_from_registry", lambda: None)
    assert (
        runtime_upgrade.latest_a2a_pack_version()
        == runtime_upgrade.DEFAULT_A2A_PACK_VERSION
    )


def test_default_a2a_pack_version_tracks_current_runtime() -> None:
    assert runtime_upgrade.DEFAULT_A2A_PACK_VERSION == "0.1.94"


def test_detect_latest_from_registry_picks_max_semver_ignoring_sha_tags(
    monkeypatch,
) -> None:
    payload = {"tags": ["0.1.6", "0.1.36", "deadbeefcafe", "0.1.9", "0.1.34", "latest"]}
    monkeypatch.setattr(
        runtime_upgrade.urllib.request, "urlopen", lambda *a, **k: _FakeResp(payload)
    )
    assert runtime_upgrade._detect_latest_from_registry() == "0.1.36"


def test_detect_latest_from_registry_returns_none_on_error(monkeypatch) -> None:
    def boom(*_a, **_k):
        raise OSError("registry unreachable")

    monkeypatch.setattr(runtime_upgrade.urllib.request, "urlopen", boom)
    assert runtime_upgrade._detect_latest_from_registry() is None


def test_current_a2a_pack_version_reads_card_capabilities() -> None:
    assert (
        current_a2a_pack_version(
            {"capabilities": {"a2a_pack": {"package": "a2a-pack", "version": "0.1.2"}}}
        )
        == "0.1.2"
    )


def test_runtime_upgrade_status_treats_missing_version_as_stale() -> None:
    status = runtime_upgrade_status(
        name="invoice-bot",
        image="registry.a2acloud.io/agents/invoice-bot:latest",
        card={},
        latest_version="0.1.2",
    )

    assert status["current_version"] is None
    assert status["update_available"] is True
    assert status["can_redeploy"] is True


def test_runtime_upgrade_status_blocks_external_images() -> None:
    status = runtime_upgrade_status(
        name="invoice-bot",
        image="ghcr.io/acme/invoice-bot:latest",
        card={},
        latest_version="0.1.2",
    )

    assert status["update_available"] is True
    assert status["can_redeploy"] is False


def test_runtime_upgrade_image_tag_is_unique_and_docker_safe() -> None:
    tag = runtime_upgrade_image_tag(
        "a" * 40,
        "0.1.57+build",
        nonce="nonce/value",
    )

    assert tag == f"{'a' * 40}-runtime-0.1.57-build-nonce-value"
    assert len(tag) <= 128


def test_bump_agent_runtime_repo_restamps_and_pushes(tmp_path: Path) -> None:
    source_bare = tmp_path / "source.git"
    runtime_bare = tmp_path / "runtime.git"
    seed = tmp_path / "seed"
    runtime_clone = tmp_path / "runtime-clone"
    _git(tmp_path, "init", "--bare", str(source_bare))
    _git(tmp_path, "init", "--bare", str(runtime_bare))
    seed.mkdir()
    (seed / ".a2a").mkdir()
    (seed / "a2a.yaml").write_text(
        "name: invoice-bot\nversion: 0.1.0\nentrypoint: agent:InvoiceBot\n"
    )
    (seed / ".a2a" / "agent.dsl.json").write_text(
        json.dumps(
            {
                "schema_version": "2026-06-04",
                "language": "python",
                "name": "invoice-bot",
                "description": "Invoice bot",
                "version": "0.1.0",
                "entrypoint": {
                    "module": "agent",
                    "class_name": "InvoiceBot",
                    "function": None,
                    "command": None,
                },
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
            }
        )
    )
    (seed / "agent.py").write_text("class InvoiceBot:\n    pass\n")
    (seed / "requirements.txt").write_text("")
    _git(seed, "init", "-b", "main")
    _git(seed, "config", "user.email", "test@example.com")
    _git(seed, "config", "user.name", "test")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(source_bare))
    _git(seed, "push", "-u", "origin", "main")
    source_sha = _git(seed, "rev-parse", "HEAD").strip()

    image_tag = f"{source_sha}-runtime-0.1.2-testnonce"
    sha = bump_agent_runtime_repo(
        name="invoice-bot",
        source_repo_url=str(source_bare),
        source_sha=source_sha,
        runtime_repo_url=str(runtime_bare),
        latest_version="0.1.2",
        image_tag=image_tag,
    )

    _git(tmp_path, "clone", "--branch", "main", str(runtime_bare), str(runtime_clone))
    marker = json.loads((runtime_clone / ".a2acloud" / "runtime-upgrade.json").read_text())
    workflow = (runtime_clone / ".gitea" / "workflows" / "build.yml").read_text()
    dockerfile = (runtime_clone / "Dockerfile").read_text()
    assert marker["target_version"] == "0.1.2"
    assert marker["base_image_tag"] == "0.1.2"
    assert marker["source_sha"] == source_sha
    assert marker["image_tag"] == image_tag
    assert f'SOURCE_SHA: "{source_sha}"' in workflow
    assert f'IMAGE_TAG: "{image_tag}"' in workflow
    assert "docker build --pull -f Dockerfile" in workflow
    assert 'docker push "$IMG:$IMAGE_TAG"' in workflow
    assert "FROM registry.a2acloud.io/a2a/a2a-pack-base:0.1.2" in dockerfile
    assert sha == _git(seed, "ls-remote", str(runtime_bare), "refs/heads/main").split()[0]


def test_bump_agent_runtime_repo_supports_legacy_source_without_dsl(
    tmp_path: Path,
) -> None:
    source_bare = tmp_path / "source.git"
    runtime_bare = tmp_path / "runtime.git"
    seed = tmp_path / "seed"
    runtime_clone = tmp_path / "runtime-clone"
    _git(tmp_path, "init", "--bare", str(source_bare))
    _git(tmp_path, "init", "--bare", str(runtime_bare))
    seed.mkdir()
    (seed / "a2a.yaml").write_text(
        "name: legacy-bot\nversion: 0.1.0\nentrypoint: agent:LegacyBot\n"
    )
    (seed / "agent.py").write_text("class LegacyBot:\n    pass\n")
    (seed / "requirements.txt").write_text("")
    _git(seed, "init", "-b", "main")
    _git(seed, "config", "user.email", "test@example.com")
    _git(seed, "config", "user.name", "test")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(source_bare))
    _git(seed, "push", "-u", "origin", "main")
    source_sha = _git(seed, "rev-parse", "HEAD").strip()

    sha = bump_agent_runtime_repo(
        name="legacy-bot",
        source_repo_url=str(source_bare),
        source_sha=source_sha,
        runtime_repo_url=str(runtime_bare),
        latest_version="0.1.2",
        image_tag="legacy-runtime-test",
    )

    _git(tmp_path, "clone", "--branch", "main", str(runtime_bare), str(runtime_clone))
    dockerfile = (runtime_clone / "Dockerfile").read_text()
    deployment = (runtime_clone / "deploy" / "20-deployment.yaml").read_text()
    assert "FROM registry.a2acloud.io/a2a/a2a-pack-base:0.1.2" in dockerfile
    assert "ENV A2A_ENTRYPOINT=agent:LegacyBot" in dockerfile
    assert "A2A_DSL_PATH" not in dockerfile
    assert "timeoutSeconds: 1800" in deployment
    assert sha == _git(seed, "ls-remote", str(runtime_bare), "refs/heads/main").split()[0]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _git_show(repo: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "--git-dir", str(repo), "show", ref],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
