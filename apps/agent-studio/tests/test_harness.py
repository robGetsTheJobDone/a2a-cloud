from __future__ import annotations

import json
import importlib.util
from pathlib import Path
import subprocess

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "agent_studio_harness",
    Path(__file__).parents[1] / "scripts" / "agent_studio_harness.py",
)
assert _SPEC and _SPEC.loader
harness = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(harness)


def _pod(name: str, container: str, *, ready: bool, terminating: bool = False) -> dict:
    metadata = {"name": name}
    if terminating:
        metadata["deletionTimestamp"] = "2026-07-12T12:00:00Z"
    return {
        "metadata": metadata,
        "spec": {"containers": [{"name": container}]},
        "status": {
            "conditions": [
                {"type": "Ready", "status": "True" if ready else "False"}
            ]
        },
    }


def test_exec_target_skips_terminating_worker_and_uses_api(monkeypatch) -> None:
    responses = iter(
        [
            {"items": [_pod("workers-old", "connector-mcp-worker", ready=True, terminating=True)]},
            {"items": [_pod("control-plane-current", "api", ready=True)]},
        ]
    )
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *args, **kwargs: json.dumps(next(responses)),
    )

    assert harness._ready_pod_target(
        namespace="control-plane",
        kubeconfig="/tmp/kubeconfig",
        deployment="control-plane-workers",
        container="connector-mcp-worker",
    ) == ("pod/control-plane-current", "api")


def test_exec_target_prefers_ready_worker(monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *args, **kwargs: json.dumps(
            {"items": [_pod("workers-current", "connector-mcp-worker", ready=True)]}
        ),
    )

    assert harness._ready_pod_target(
        namespace="control-plane",
        kubeconfig=None,
        deployment="control-plane-workers",
        container="connector-mcp-worker",
    ) == ("pod/workers-current", "connector-mcp-worker")


def test_mint_grant_keeps_delegated_smoke_outputs_agent_scoped(monkeypatch) -> None:
    calls: list[list[str]] = []

    monkeypatch.setattr(
        harness,
        "_ready_pod_target",
        lambda **_kwargs: ("pod/control-plane-current", "api"),
    )

    def fake_check_output(command: list[str], **_kwargs: object) -> str:
        calls.append(command)
        return json.dumps({"grant": "payload.signature", "payload": {}})

    monkeypatch.setattr(subprocess, "check_output", fake_check_output)

    result = harness.mint_grant(
        name="workspace-helper",
        user_id=2,
        kubeconfig="/tmp/kubeconfig",
    )

    assert result["grant"] == "payload.signature"
    embedded_code = calls[0][-2]
    assert 'allow_patterns=(f"agents/{name}/**",)' in embedded_code
    assert 'write_prefixes=(f"agents/{name}/",)' in embedded_code
    assert '"outputs/**"' not in embedded_code


def test_acceptance_does_not_require_disabled_code_editor() -> None:
    report = {
        "ok": True,
        "status": "succeeded",
        "agent_name": "demo-agent",
        "tests": [],
        "handoffs": [
            {"agent": "agent-builder", "skill": "build", "status": "ok"},
            {"agent": "agent-reviewer", "skill": "review", "status": "ok"},
        ],
    }

    harness._assert_acceptance(
        final={},
        report=report,
        subagent_runs=[],
        exercise_code_editor=False,
    )


def test_acceptance_requires_enabled_code_editor() -> None:
    report = {
        "ok": True,
        "status": "succeeded",
        "agent_name": "demo-agent",
        "tests": [],
        "handoffs": [
            {"agent": "agent-builder", "skill": "build", "status": "ok"},
            {"agent": "agent-reviewer", "skill": "review", "status": "ok"},
        ],
    }

    with pytest.raises(SystemExit, match="missing required handoff.*code-editor-agent"):
        harness._assert_acceptance(
            final={},
            report=report,
            subagent_runs=[],
            exercise_code_editor=True,
        )


def test_direct_args_preserve_app_spec_and_spend_cap() -> None:
    app_spec = json.dumps(
        {"profile": "full_stack", "product_ui": True},
        separators=(",", ":"),
        sort_keys=True,
    )

    args = harness._agent_studio_args(
        name="quote-judge",
        goal="Compare quotes",
        max_iterations=3,
        quality_bar="high",
        exercise_code_editor=True,
        app_spec_json=app_spec,
        max_spend_cents=2000,
    )

    assert args["app_spec_json"] == app_spec
    assert args["max_spend_cents"] == 2000


def test_expected_repo_guard_uses_code_editor_target_repo(monkeypatch) -> None:
    paths: list[str] = []

    def fake_http_json(method: str, url: str, **_kwargs: object) -> dict[str, str]:
        assert method == "GET"
        paths.append(url)
        if url.endswith("/code-editor"):
            return {
                "target_repo_url": "https://gitea.example/alice/quote-judge.git",
            }
        return {"name": "quote-judge"}

    monkeypatch.setattr(harness, "_http_json", fake_http_json)

    harness._assert_expected_repo(
        api_url="https://api.example",
        token="opaque-token",
        name="quote-judge",
        expected_repo_url="https://gitea.example/alice/quote-judge",
    )

    assert paths == [
        "https://api.example/v1/agents/quote-judge",
        "https://api.example/v1/agents/quote-judge/code-editor",
    ]
