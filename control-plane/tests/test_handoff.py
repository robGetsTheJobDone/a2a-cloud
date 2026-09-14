from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
from typing import Any

import pytest

import main_agent.tools.handoff as handoff
from main_agent.grants import verify_grant
from main_agent.hooks import PlatformHooks
from main_agent.tools.handoff import build_handoff_tools


class _StreamResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        chunks: list[str] | None = None,
        body: bytes = b"",
    ) -> None:
        self.status_code = status_code
        self._chunks = chunks or []
        self._body = body

    async def __aenter__(self) -> "_StreamResponse":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def aiter_text(self):
        for chunk in self._chunks:
            yield chunk

    async def aread(self) -> bytes:
        return self._body


def _ctx(
    *,
    hooks: PlatformHooks | None = None,
    agent_card: dict[str, Any] | None = None,
) -> SimpleNamespace:
    card = agent_card or _builder_card()

    async def get_agent_card(_name: str) -> dict[str, Any]:
        return card

    if hooks is None:
        hooks = PlatformHooks(get_agent_card=get_agent_card)
    elif hooks.get_agent_card is None:
        hooks = replace(hooks, get_agent_card=get_agent_card)
    return SimpleNamespace(
        settings=SimpleNamespace(
            agents_namespace_dns="{name}.agents.svc.cluster.local",
            litellm_url="http://litellm:4000",
            litellm_model="platform-model",
            platform_llm_models=("platform-model",),
            platform_llm_max_budget_usd=1.0,
            platform_llm_rpm_limit=60,
            platform_llm_tpm_limit=200000,
        ),
        hooks=hooks,
        bucket="user-1-files",
        user_id=1,
        policy_controls={},
    )


def _builder_card() -> dict[str, Any]:
    return {
        "runtime": {"llm_provisioning": "agent_byok"},
        "skills": [
            {
                "name": "build",
                "stream": True,
                "input_schema": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {"name": {"type": "string"}},
                },
                "policy": {
                    "grant_mode": "read_write_overlay",
                    "grant_allow_patterns": ["agents/{name}/**"],
                    "grant_outputs_prefix": "agents/{name}/",
                    "grant_write_prefixes": ["agents/{name}/"],
                    "grant_ttl_seconds": 1200,
                    "grant_run_timeout_seconds": 900,
                },
            }
        ]
    }


def _platform_card() -> dict[str, Any]:
    card = _builder_card()
    card["runtime"] = {"llm_provisioning": "platform"}
    return card


def _dual_llm_card() -> dict[str, Any]:
    card = _builder_card()
    card["runtime"] = {"llm_provisioning": "platform_or_caller_provided"}
    return card


def _account_trial_card() -> dict[str, Any]:
    card = _platform_card()
    card["runtime"]["account_access"] = {
        "required": True,
        "platform_skill_calls": 2,
        "after_trial": "byok",
    }
    return card


@pytest.mark.asyncio
async def test_call_agent_mints_initial_grant_from_target_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, timeout: float) -> None:
            captured["timeout"] = timeout

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def stream(self, method: str, url: str, *, json: dict, headers: dict) -> _StreamResponse:
            captured["method"] = method
            captured["url"] = url
            captured["body"] = json
            captured["headers"] = headers
            return _StreamResponse(
                chunks=['data: {"type":"result","result":{"ok":true}}\n\n']
            )

    monkeypatch.setattr(handoff.httpx, "AsyncClient", FakeClient)

    call_agent = build_handoff_tools(_ctx())[0]
    raw = await call_agent.ainvoke({
        "name": "agent-builder",
        "skill": "build",
        "args_json": '{"name":"human-input-demo"}',
    })

    result = json.loads(raw)
    body = captured["body"]
    grant = verify_grant(body["grant"])  # type: ignore[index]

    assert result["ok"] is True
    assert captured["timeout"] == 930.0
    assert grant["mode"] == "read_write_overlay"
    assert grant["allow_patterns"] == ["agents/human-input-demo/**"]
    assert grant["outputs_prefix"] == "agents/human-input-demo/"
    assert grant["write_prefixes"] == ["agents/human-input-demo/"]
    assert grant["source_grants"] == [
        {"agent": "human-input-demo", "scope": "write"}
    ]
    assert grant["expires_at"] - grant["issued_at"] == 1200
    assert "llm_creds" not in body


@pytest.mark.asyncio
async def test_call_agent_uses_cached_card_without_waking_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    class FakeClient:
        def __init__(self, timeout: float) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def stream(self, method: str, url: str, *, json: dict, headers: dict) -> _StreamResponse:
            captured["body"] = json
            return _StreamResponse(
                chunks=['data: {"type":"result","result":{"ok":true}}\n\n']
            )

    monkeypatch.setattr(handoff.httpx, "AsyncClient", FakeClient)

    async def cached_card(name: str) -> dict[str, Any]:
        return _builder_card()

    call_agent = build_handoff_tools(
        _ctx(hooks=PlatformHooks(get_agent_card=cached_card))
    )[0]
    raw = await call_agent.ainvoke({
        "name": "agent-builder",
        "skill": "build",
        "args_json": '{"name":"demo"}',
    })

    assert json.loads(raw)["ok"] is True
    # A cached card is enough to reach the invoke POST; no discovery GET is
    # available on FakeClient, so an attempted live lookup would fail the test.


@pytest.mark.asyncio
async def test_call_agent_rejects_skill_missing_from_registered_card_without_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        def __init__(self, timeout: float) -> None:
            raise AssertionError("undeclared skill must not trigger network access")

    monkeypatch.setattr(handoff.httpx, "AsyncClient", FakeClient)

    async def stale_cached_card(name: str) -> dict[str, Any]:
        # Cache predates the skill the caller wants to invoke.
        return {"runtime": {"llm_provisioning": "agent_byok"}, "skills": []}

    call_agent = build_handoff_tools(
        _ctx(hooks=PlatformHooks(get_agent_card=stale_cached_card))
    )[0]
    raw = await call_agent.ainvoke({
        "name": "agent-builder",
        "skill": "build",
        "args_json": '{"name":"demo"}',
    })

    payload = json.loads(raw)
    assert payload["error"] == "skill is not declared by the registered agent card"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        "trusted@169.254.169.254",
        "trusted#@169.254.169.254",
        "trusted?target=169.254.169.254",
    ],
)
async def test_call_agent_rejects_url_control_characters_in_agent_name_before_io(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    card_calls = 0

    async def get_agent_card(_name: str) -> dict[str, Any]:
        nonlocal card_calls
        card_calls += 1
        return _builder_card()

    class NoNetworkClient:
        def __init__(self, timeout: float) -> None:
            raise AssertionError("invalid agent names must be rejected before network access")

    monkeypatch.setattr(handoff.httpx, "AsyncClient", NoNetworkClient)

    call_agent = build_handoff_tools(
        _ctx(hooks=PlatformHooks(get_agent_card=get_agent_card))
    )[0]
    raw = await call_agent.ainvoke({
        "name": name,
        "skill": "build",
        "args_json": '{"name":"demo"}',
    })

    payload = json.loads(raw)
    assert payload["error"].startswith("invalid agent name:")
    assert card_calls == 0


@pytest.mark.asyncio
async def test_call_agent_rejects_unregistered_agent_without_live_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def missing_card(_name: str) -> None:
        return None

    class NoNetworkClient:
        def __init__(self, timeout: float) -> None:
            raise AssertionError("unregistered agents must not trigger network access")

    monkeypatch.setattr(handoff.httpx, "AsyncClient", NoNetworkClient)

    call_agent = build_handoff_tools(
        _ctx(hooks=PlatformHooks(get_agent_card=missing_card))
    )[0]
    raw = await call_agent.ainvoke({
        "name": "unknown-agent",
        "skill": "build",
        "args_json": '{"name":"demo"}',
    })

    payload = json.loads(raw)
    assert payload["error"] == "agent is not registered or has no cached agent card"


@pytest.mark.asyncio
async def test_call_agent_quotes_declared_skill_as_one_url_path_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}
    skill = "build/@#?"
    card = _builder_card()
    card["skills"][0]["name"] = skill

    class FakeClient:
        def __init__(self, timeout: float) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def stream(
            self,
            method: str,
            url: str,
            *,
            json: dict,
            headers: dict,
        ) -> _StreamResponse:
            captured["url"] = url
            return _StreamResponse(
                chunks=['data: {"type":"result","result":{"ok":true}}\n\n']
            )

    monkeypatch.setattr(handoff.httpx, "AsyncClient", FakeClient)

    call_agent = build_handoff_tools(_ctx(agent_card=card))[0]
    raw = await call_agent.ainvoke({
        "name": "agent-builder",
        "skill": skill,
        "args_json": '{"name":"demo"}',
    })

    assert json.loads(raw)["ok"] is True
    assert captured["url"] == (
        "http://agent-builder.agents.svc.cluster.local/invoke/build%2F%40%23%3F"
    )


@pytest.mark.asyncio
async def test_call_agent_platform_llm_uses_a2a_grant_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, timeout: float) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def stream(self, method: str, url: str, *, json: dict, headers: dict) -> _StreamResponse:
            captured["body"] = json
            return _StreamResponse(
                chunks=['data: {"type":"result","result":{"ok":true}}\n\n']
            )

    monkeypatch.setattr(handoff.httpx, "AsyncClient", FakeClient)

    call_agent = build_handoff_tools(_ctx(agent_card=_platform_card()))[0]
    raw = await call_agent.ainvoke({
        "name": "platform-agent",
        "skill": "build",
        "args_json": '{"name":"demo"}',
    })

    result = json.loads(raw)
    body = captured["body"]
    grant = verify_grant(body["grant"])  # type: ignore[index]

    assert result["ok"] is True
    assert grant["llm_models"] == ["platform-model"]
    assert grant["llm_max_budget_usd"] == 1.0
    llm_creds = body["llm_creds"]
    assert llm_creds == {
        "base_url": "http://litellm:4000/v1",
        "api_key": body["grant"],
        "model": "platform-model",
        "extra_body": {},
        "metadata": {
            "a2a_user_id": 1,
            "a2a_grant_id": grant["grant_id"],
            "a2a_agent_name": "platform-agent",
            "a2a_skill_name": "build",
            "a2a_llm_source": "handoff",
        },
    }


@pytest.mark.asyncio
async def test_call_agent_dual_llm_prefers_user_creds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_user_llm_creds() -> dict[str, Any]:
        return {
            "base_url": "https://user-provider.example/v1",
            "api_key": "user-key",
            "model": "user-model",
            "temperature_mode": "omit",
            "extra_body": {"reasoning": {"enabled": False}},
        }

    class FakeClient:
        def __init__(self, timeout: float) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def stream(self, method: str, url: str, *, json: dict, headers: dict) -> _StreamResponse:
            captured["body"] = json
            return _StreamResponse(
                chunks=['data: {"type":"result","result":{"ok":true}}\n\n']
            )

    monkeypatch.setattr(handoff.httpx, "AsyncClient", FakeClient)

    call_agent = build_handoff_tools(_ctx(
        hooks=PlatformHooks(get_user_llm_creds=fake_user_llm_creds),
        agent_card=_dual_llm_card(),
    ))[0]
    raw = await call_agent.ainvoke({
        "name": "agent-builder",
        "skill": "build",
        "args_json": '{"name":"demo"}',
    })

    result = json.loads(raw)
    body = captured["body"]
    grant = verify_grant(body["grant"])  # type: ignore[index]

    assert result["ok"] is True
    assert grant["llm_models"] == []
    assert grant["llm_max_budget_usd"] is None
    assert body["llm_creds"] == {
        "base_url": "https://user-provider.example/v1",
        "api_key": "user-key",
        "model": "user-model",
        "temperature_mode": "omit",
        "extra_body": {"reasoning": {"enabled": False}},
        "metadata": {
            "a2a_user_id": 1,
            "a2a_grant_id": grant["grant_id"],
            "a2a_agent_name": "agent-builder",
            "a2a_skill_name": "build",
            "a2a_llm_source": "handoff",
        },
    }


@pytest.mark.asyncio
async def test_call_agent_dual_llm_falls_back_to_platform_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def no_user_llm_creds() -> dict[str, Any] | None:
        return None

    class FakeClient:
        def __init__(self, timeout: float) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def stream(self, method: str, url: str, *, json: dict, headers: dict) -> _StreamResponse:
            captured["body"] = json
            return _StreamResponse(
                chunks=['data: {"type":"result","result":{"ok":true}}\n\n']
            )

    monkeypatch.setattr(handoff.httpx, "AsyncClient", FakeClient)

    call_agent = build_handoff_tools(_ctx(
        hooks=PlatformHooks(get_user_llm_creds=no_user_llm_creds),
        agent_card=_dual_llm_card(),
    ))[0]
    raw = await call_agent.ainvoke({
        "name": "agent-builder",
        "skill": "build",
        "args_json": '{"name":"demo"}',
    })

    result = json.loads(raw)
    body = captured["body"]
    grant = verify_grant(body["grant"])  # type: ignore[index]

    assert result["ok"] is True
    assert grant["llm_models"] == ["platform-model"]
    assert body["llm_creds"] == {
        "base_url": "http://litellm:4000/v1",
        "api_key": body["grant"],
        "model": "platform-model",
        "extra_body": {},
        "metadata": {
            "a2a_user_id": 1,
            "a2a_grant_id": grant["grant_id"],
            "a2a_agent_name": "agent-builder",
            "a2a_skill_name": "build",
            "a2a_llm_source": "handoff",
        },
    }


@pytest.mark.asyncio
async def test_call_agent_account_trial_claims_before_platform_funding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def no_user_llm_creds() -> None:
        return None

    async def claim_platform_trial(agent: str, skill: str) -> dict[str, Any]:
        assert (agent, skill) == ("trial-agent", "build")
        return {
            "ok": True,
            "llm_source": "platform_trial",
            "platform_skill_calls_remaining": 1,
        }

    class FakeClient:
        def __init__(self, timeout: float) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def stream(self, method: str, url: str, *, json: dict, headers: dict) -> _StreamResponse:
            captured["body"] = json
            return _StreamResponse(
                chunks=['data: {"type":"result","result":{"ok":true}}\n\n']
            )

    monkeypatch.setattr(handoff.httpx, "AsyncClient", FakeClient)
    call_agent = build_handoff_tools(_ctx(
        hooks=PlatformHooks(
            get_user_llm_creds=no_user_llm_creds,
            claim_platform_trial=claim_platform_trial,
        ),
        agent_card=_account_trial_card(),
    ))[0]

    result = json.loads(await call_agent.ainvoke({
        "name": "trial-agent",
        "skill": "build",
        "args_json": '{"name":"demo"}',
    }))

    assert result["ok"] is True
    grant = verify_grant(captured["body"]["grant"])  # type: ignore[index]
    assert grant["llm_models"] == ["platform-model"]


@pytest.mark.asyncio
async def test_call_agent_account_trial_exhaustion_requires_byok() -> None:
    async def no_user_llm_creds() -> None:
        return None

    async def exhausted(_agent: str, _skill: str) -> dict[str, Any]:
        return {
            "ok": False,
            "error": "llm_credentials_required",
            "reason": "platform_trial_exhausted",
            "setup_url": "https://app.example.com/llm-keys",
        }

    call_agent = build_handoff_tools(_ctx(
        hooks=PlatformHooks(
            get_user_llm_creds=no_user_llm_creds,
            claim_platform_trial=exhausted,
        ),
        agent_card=_account_trial_card(),
    ))[0]
    result = json.loads(await call_agent.ainvoke({
        "name": "trial-agent",
        "skill": "build",
        "args_json": '{"name":"demo"}',
    }))

    assert result["error"] == "llm_credentials_required"
    assert result["reason"] == "platform_trial_exhausted"
    assert result["setup_url"].endswith("/llm-keys")



@pytest.mark.asyncio
async def test_call_agent_returns_input_schema_for_bad_handoff_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        def __init__(self, timeout: float) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def stream(self, method: str, url: str, *, json: dict, headers: dict) -> _StreamResponse:
            return _StreamResponse(status_code=422, body=b"missing name")

    monkeypatch.setattr(handoff.httpx, "AsyncClient", FakeClient)

    call_agent = build_handoff_tools(_ctx())[0]
    raw = await call_agent.ainvoke({
        "name": "agent-builder",
        "skill": "build",
        "args_json": "{}",
    })

    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["result"]["error"] == "agent 422"
    assert payload["result"]["input_schema"]["required"] == ["name"]


@pytest.mark.asyncio
async def test_call_agent_compacts_oversized_results_for_model_and_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[dict[str, Any]] = []
    huge_text = "x" * 50_000
    large_result = {
        "path": "outputs/report.md",
        "content": huge_text,
        "task": {
            "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [
                {
                    "artifactId": "artifact-1",
                    "uri": "s3://bucket/outputs/report.md",
                    "parts": [{"text": huge_text}],
                }
            ],
        },
    }

    async def emit(event: dict[str, Any]) -> None:
        emitted.append(event)

    class FakeClient:
        def __init__(self, timeout: float) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def stream(self, method: str, url: str, *, json: dict, headers: dict) -> _StreamResponse:
            payload = {"type": "result", "result": large_result}
            return _StreamResponse(chunks=[f"data: {handoff.json.dumps(payload)}\n\n"])

    monkeypatch.setattr(handoff.httpx, "AsyncClient", FakeClient)

    call_agent = build_handoff_tools(_ctx(hooks=PlatformHooks(emit=emit)))[0]
    raw = await call_agent.ainvoke({
        "name": "agent-builder",
        "skill": "build",
        "args_json": '{"name":"demo"}',
    })

    payload = json.loads(raw)
    compact = payload["result"]
    complete = [event for event in emitted if event["type"] == "handoff_complete"][-1]

    assert payload["ok"] is True
    assert compact["path"] == "outputs/report.md"
    assert compact["_truncated"] is True
    assert len(json.dumps(compact)) < 10_000
    assert huge_text not in raw
    assert complete["summary"] == "completed, 1 artifact"
    assert complete["result"] == compact


def test_compact_handoff_result_overrides_stale_truncated_marker() -> None:
    compact = handoff._compact_handoff_result({
        "_truncated": False,
        "_truncated_reason": "upstream marker",
        "content": "x" * 50_000,
    })

    assert compact["_truncated"] is True
    assert "callee result exceeded" in compact["_truncated_reason"]
    assert len(json.dumps(compact)) < 10_000


@pytest.mark.asyncio
async def test_call_agent_blocks_before_grant_when_consumer_setup_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        emitted.append(event)

    async def resolve_setup(
        agent_name: str,
        agent_card: dict[str, Any] | None,
    ) -> dict[str, Any]:
        assert agent_name == "agent-builder"
        assert agent_card == _builder_card()
        return {
            "ok": False,
            "setup": {
                "declaration": {
                    "fields": [
                        {
                            "name": "GITHUB_TOKEN",
                            "kind": "secret",
                            "label": "GitHub token",
                            "description": "",
                            "required": True,
                            "input_type": "password",
                            "options": [],
                        }
                    ]
                },
                "values": [],
                "missing_required": ["GITHUB_TOKEN"],
                "complete": False,
                "organization": None,
                "can_manage_org": False,
            },
            "missing_required": ["GITHUB_TOKEN"],
        }

    class FakeClient:
        def __init__(self, timeout: float) -> None:
            raise AssertionError("agent invoke should not be attempted")

    monkeypatch.setattr(handoff.httpx, "AsyncClient", FakeClient)

    call_agent = build_handoff_tools(
        _ctx(hooks=PlatformHooks(emit=emit, resolve_consumer_setup=resolve_setup))
    )[0]
    raw = await call_agent.ainvoke({
        "name": "agent-builder",
        "skill": "build",
        "args_json": '{"name":"needs-token"}',
    })

    payload = json.loads(raw)

    assert payload["error"] == "agent_setup_required"
    assert payload["missing_required"] == ["GITHUB_TOKEN"]
    assert emitted == [
        {
            "type": "agent_setup_required",
            "agent": "agent-builder",
            "skill": "build",
            "setup": payload["setup"],
            "missing_required": ["GITHUB_TOKEN"],
        }
    ]


@pytest.mark.asyncio
async def test_call_agent_forwards_a2a_task_status_as_typed_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        emitted.append(event)

    class FakeClient:
        def __init__(self, timeout: float) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def stream(self, method: str, url: str, *, json: dict, headers: dict) -> _StreamResponse:
            return _StreamResponse(
                chunks=[
                    'data: {"type":"event","kind":"task_status","payload":{"task":{"status":{"state":"TASK_STATE_AUTH_REQUIRED","message":{"parts":[{"text":"Connect Google"}]}}}}}\n\n',
                    'data: {"type":"result","result":{"task":{"status":{"state":"TASK_STATE_AUTH_REQUIRED"}}}}\n\n',
                ]
            )

    monkeypatch.setattr(handoff.httpx, "AsyncClient", FakeClient)

    call_agent = build_handoff_tools(_ctx(hooks=PlatformHooks(emit=emit)))[0]
    raw = await call_agent.ainvoke({
        "name": "agent-builder",
        "skill": "build",
        "args_json": '{"name":"needs-auth"}',
    })

    assert json.loads(raw)["ok"] is True
    assert any(event["type"] == "agent_auth_required" for event in emitted)
    complete = [event for event in emitted if event["type"] == "handoff_complete"][-1]
    assert complete["summary"] == "auth required"


@pytest.mark.asyncio
async def test_scope_request_with_write_prefixes_asks_and_posts_superseding_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    posts: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        emitted.append(event)

    async def audit_grant(
        payload: dict[str, Any],
        decision: str,
        decided_by: str,
        reason: str | None,
        parent: str | None,
    ) -> None:
        audits.append({
            "payload": payload,
            "decision": decision,
            "decided_by": decided_by,
            "reason": reason,
            "parent": parent,
        })

    async def approve_scope(approval_id: str, timeout: float) -> str:
        return "approve"

    class FakeClient:
        def __init__(self, timeout: float) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def stream(self, method: str, url: str, *, json: dict, headers: dict) -> _StreamResponse:
            return _StreamResponse(
                chunks=[
                    'data: {"type":"event","kind":"scope_request","payload":{"request_id":"sr_1","reason":"Need reports folder","read_patterns":["reference/**"],"write_prefixes":["reports/"],"mode":"read_write_overlay","ttl_seconds":120}}\n\n',
                    'data: {"type":"result","result":{"ok":true}}\n\n',
                ]
            )

        async def post(self, url: str, *, json: dict) -> SimpleNamespace:
            posts.append({"url": url, "json": json})
            return SimpleNamespace(status_code=200)

    monkeypatch.setattr(handoff.httpx, "AsyncClient", FakeClient)

    call_agent = build_handoff_tools(
        _ctx(hooks=PlatformHooks(
            emit=emit,
            audit_grant=audit_grant,
            wait_for_scope_approval=approve_scope,
        ))
    )[0]
    raw = await call_agent.ainvoke({
        "name": "agent-builder",
        "skill": "build",
        "args_json": '{"name":"demo"}',
    })
    assert json.loads(raw)["ok"] is True

    for _ in range(100):
        if any(event["type"] == "scope_grant" for event in emitted):
            break
        await asyncio.sleep(0.01)

    request_event = [event for event in emitted if event["type"] == "scope_request"][0]
    approval_event = [
        event for event in emitted if event["type"] == "scope_approval_required"
    ][0]
    grant_event = [event for event in emitted if event["type"] == "scope_grant"][0]

    assert request_event["requested"]["write_prefixes"] == ["reports/"]
    assert approval_event["proposed_grant"]["write_prefixes"] == [
        "agents/demo/",
        "reports/",
    ]
    assert grant_event["scopes"]["write_prefixes"] == ["agents/demo/", "reports/"]
    assert grant_event["scopes"]["source_grants"] == [
        {"agent": "demo", "scope": "write"}
    ]
    assert posts and posts[0]["url"].endswith("/scope-grants/sr_1")
    new_grant = verify_grant(posts[0]["json"]["grant"])
    assert new_grant["allow_patterns"] == ["agents/demo/**", "reference/**"]
    assert new_grant["write_prefixes"] == ["agents/demo/", "reports/"]
    assert new_grant["source_grants"] == [{"agent": "demo", "scope": "write"}]
    assert audits[-1]["decision"] == "user_approve"


@pytest.mark.asyncio
async def test_auto_approve_bypasses_handoff_approval_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        emitted.append(event)

    async def audit_grant(
        payload: dict[str, Any],
        decision: str,
        decided_by: str,
        reason: str | None,
        parent: str | None,
    ) -> None:
        audits.append({
            "payload": payload,
            "decision": decision,
            "decided_by": decided_by,
            "reason": reason,
            "parent": parent,
        })

    async def fail_handoff_wait(approval_id: str, timeout: float) -> str:
        raise AssertionError("handoff approval should be auto-approved")

    class FakeClient:
        def __init__(self, timeout: float) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def stream(self, method: str, url: str, *, json: dict, headers: dict) -> _StreamResponse:
            return _StreamResponse(
                chunks=['data: {"type":"result","result":{"ok":true}}\n\n']
            )

    monkeypatch.setattr(handoff.httpx, "AsyncClient", FakeClient)

    call_agent = build_handoff_tools(
        _ctx(hooks=PlatformHooks(
            emit=emit,
            approval_mode=True,
            auto_approve=True,
            wait_for_handoff_approval=fail_handoff_wait,
            audit_grant=audit_grant,
        ))
    )[0]
    raw = await call_agent.ainvoke({
        "name": "agent-builder",
        "skill": "build",
        "args_json": '{"name":"demo"}',
    })

    assert json.loads(raw)["ok"] is True
    assert not any(event["type"] == "approval_required" for event in emitted)
    assert any(event["type"] == "agent_handoff" for event in emitted)
    assert audits[-1]["decision"] == "auto_approve"
    assert audits[-1]["decided_by"] == "auto"


@pytest.mark.asyncio
async def test_auto_approve_converts_scope_approval_into_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    posts: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        emitted.append(event)

    async def audit_grant(
        payload: dict[str, Any],
        decision: str,
        decided_by: str,
        reason: str | None,
        parent: str | None,
    ) -> None:
        audits.append({
            "payload": payload,
            "decision": decision,
            "decided_by": decided_by,
            "reason": reason,
            "parent": parent,
        })

    async def fail_scope_wait(approval_id: str, timeout: float) -> str:
        raise AssertionError("scope approval should be auto-approved")

    class FakeClient:
        def __init__(self, timeout: float) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def stream(self, method: str, url: str, *, json: dict, headers: dict) -> _StreamResponse:
            return _StreamResponse(
                chunks=[
                    'data: {"type":"event","kind":"scope_request","payload":{"request_id":"sr_1","reason":"Need reports folder","read_patterns":["reference/**"],"write_prefixes":["reports/"],"mode":"read_write_overlay","ttl_seconds":120}}\n\n',
                    'data: {"type":"result","result":{"ok":true}}\n\n',
                ]
            )

        async def post(self, url: str, *, json: dict) -> SimpleNamespace:
            posts.append({"url": url, "json": json})
            return SimpleNamespace(status_code=200)

    monkeypatch.setattr(handoff.httpx, "AsyncClient", FakeClient)

    call_agent = build_handoff_tools(
        _ctx(hooks=PlatformHooks(
            emit=emit,
            approval_mode=True,
            auto_approve=True,
            wait_for_scope_approval=fail_scope_wait,
            audit_grant=audit_grant,
        ))
    )[0]
    raw = await call_agent.ainvoke({
        "name": "agent-builder",
        "skill": "build",
        "args_json": '{"name":"demo"}',
    })
    assert json.loads(raw)["ok"] is True

    for _ in range(100):
        if any(event["type"] == "scope_grant" for event in emitted):
            break
        await asyncio.sleep(0.01)

    assert not any(event["type"] == "scope_approval_required" for event in emitted)
    request_event = [event for event in emitted if event["type"] == "scope_request"][0]
    grant_event = [event for event in emitted if event["type"] == "scope_grant"][0]
    assert request_event["decision"] == "auto_approve"
    assert grant_event["scopes"]["write_prefixes"] == ["agents/demo/", "reports/"]
    assert grant_event["scopes"]["source_grants"] == [
        {"agent": "demo", "scope": "write"}
    ]
    assert posts and posts[0]["url"].endswith("/scope-grants/sr_1")
    new_grant = verify_grant(posts[0]["json"]["grant"])
    assert new_grant["write_prefixes"] == ["agents/demo/", "reports/"]
    assert new_grant["source_grants"] == [{"agent": "demo", "scope": "write"}]
    assert audits[-1]["decision"] == "auto_approve"
    assert audits[-1]["decided_by"] == "auto"
