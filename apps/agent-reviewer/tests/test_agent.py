"""Smoke tests for agent-reviewer: card, schema, validation behaviors."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from a2a_pack import LocalRunContext, NoAuth  # noqa: E402
from agent import AgentReviewer  # noqa: E402
from agent_reviewer import builder as reviewer_builder  # noqa: E402
from agent_reviewer.builder import ReviewerContext, build_reviewer_graph  # noqa: E402
from agent_reviewer.tools import Finding, ReviewReport, ToolContext, build_tools  # noqa: E402
from agent_reviewer.config import load_settings  # noqa: E402


def test_class_metadata() -> None:
    assert AgentReviewer.name == "agent-reviewer"
    assert AgentReviewer.version == "0.1.2"
    assert AgentReviewer.wants_cp_jwt is True


def test_review_skill_registered() -> None:
    spec = AgentReviewer._skills["review"]  # type: ignore[attr-defined]
    assert spec.stream is True
    assert spec.policy.timeout_seconds == 900
    # Reviewer is read-only — no write grants declared.
    assert not spec.policy.grant_write_prefixes
    assert spec.policy.grant_outputs_prefix is None


def test_review_report_round_trips() -> None:
    report = ReviewReport(
        ok=False,
        agent_name="hello",
        ref="main",
        summary="one critical finding",
        findings=[
            Finding(
                severity="critical",
                category="security",
                message="hardcoded API key",
                file="agent.py",
                line=42,
                suggestion="read ctx.llm.api_key instead",
            )
        ],
    )
    dumped = report.model_dump()
    assert dumped["ok"] is False
    again = ReviewReport.model_validate(dumped)
    assert again.findings[0].severity == "critical"


@pytest.mark.asyncio
async def test_review_returns_structured_error_without_llm_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENT_LLM_KEY", raising=False)
    monkeypatch.delenv("A2A_LITELLM_KEY", raising=False)
    ctx = LocalRunContext(auth=NoAuth())
    ctx._cp_jwt = "jwt-123"  # noqa: SLF001
    ctx._cp_url = "http://control-plane.local"  # noqa: SLF001

    async def fail_mint(*_args: object, **_kwargs: object) -> dict[str, str]:
        raise AssertionError("review should not mint a Gitea token without LLM creds")

    monkeypatch.setattr(ctx, "mint_gitea_token", fail_mint)

    result = await AgentReviewer().review(ctx, agent_name="hello-agent")

    assert result["error"] == "llm_credentials_missing"
    assert result["agent_name"] == "hello-agent"
    assert result["ref"] == "main"


def test_reviewer_graph_uses_forwarded_llm_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def fake_create_a2a_deep_agent(ctx, *, creds, **kwargs) -> dict[str, object]:
        captured["creds"] = creds
        captured["graph_kwargs"] = kwargs
        return kwargs

    monkeypatch.setattr(reviewer_builder, "create_a2a_deep_agent", fake_create_a2a_deep_agent)

    graph = build_reviewer_graph(
        ReviewerContext(
            agent_name="hello-agent",
            ref="main",
            gitea_token="gitea-read-token",
            gitea_owner="gitea_admin",
            llm_base_url="http://litellm.test/v1",
            llm_api_key="scoped-grant-token",
            llm_model="platform-model",
            llm_temperature_mode="omit",
            llm_extra_body={"extra_body": {"thinking": {"type": "disabled"}}},
            llm_metadata={"a2a_grant_id": "grant-1"},
        ),
        completion_box={},
    )

    assert graph is captured["graph_kwargs"]
    creds = captured["creds"]
    assert getattr(creds, "model") == "platform-model"
    assert getattr(creds, "base_url") == "http://litellm.test/v1"
    assert getattr(creds, "api_key") == "scoped-grant-token"
    assert getattr(creds, "temperature_mode") == "omit"
    assert getattr(creds, "extra_body") == {"extra_body": {"thinking": {"type": "disabled"}}}
    assert getattr(creds, "metadata") == {"a2a_grant_id": "grant-1"}


def test_reviewer_graph_rejects_missing_forwarded_llm_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("A2A_LITELLM_KEY", "env-key-should-not-be-used")

    with pytest.raises(ValueError, match="forwarded ctx\\.llm credentials"):
        build_reviewer_graph(
            ReviewerContext(
                agent_name="hello-agent",
                ref="main",
                gitea_token="gitea-read-token",
                gitea_owner="gitea_admin",
            ),
            completion_box={},
        )


def test_invalid_severity_rejected() -> None:
    with pytest.raises(Exception):
        Finding(
            severity="catastrophic",  # type: ignore[arg-type]
            category="security",
            message="x",
        )


@pytest.mark.asyncio
async def test_submit_review_report_writes_completion_box() -> None:
    box: dict = {}
    tools = build_tools(
        ToolContext(
            settings=load_settings(),
            agent_name="hello",
            ref="main",
            fetch_source=lambda: {},  # unused for submit
            completion_box=box,
        )
    )
    submit = next(t for t in tools if t.name == "submit_review_report")
    result = await submit.ainvoke(
        {
            "report_json": json.dumps(
                {
                    "ok": True,
                    "agent_name": "ignored",
                    "ref": "ignored",
                    "summary": "clean",
                    "findings": [],
                }
            )
        }
    )
    assert result == {"accepted": True}
    assert "report" in box
    # Reviewer overrides agent_name / ref from ctx, not from the LLM.
    assert box["report"].agent_name == "hello"
    assert box["report"].ref == "main"


@pytest.mark.asyncio
async def test_submit_review_report_rejects_bad_json() -> None:
    box: dict = {}
    tools = build_tools(
        ToolContext(
            settings=load_settings(),
            agent_name="hello",
            ref="main",
            fetch_source=lambda: {},
            completion_box=box,
        )
    )
    submit = next(t for t in tools if t.name == "submit_review_report")
    result = await submit.ainvoke({"report_json": "{not json"})
    assert result["accepted"] is False
    assert "error" in result
    assert "report" not in box


@pytest.mark.asyncio
async def test_submit_review_report_rejects_bad_schema() -> None:
    box: dict = {}
    tools = build_tools(
        ToolContext(
            settings=load_settings(),
            agent_name="hello",
            ref="main",
            fetch_source=lambda: {},
            completion_box=box,
        )
    )
    submit = next(t for t in tools if t.name == "submit_review_report")
    result = await submit.ainvoke(
        {
            "report_json": json.dumps(
                {"ok": True, "summary": "missing agent_name"}
            )
        }
    )
    assert result["accepted"] is False
    assert "report" not in box
