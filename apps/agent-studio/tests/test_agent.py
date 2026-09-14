from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent as agent_module  # noqa: E402
import agent_studio.specialists as specialists_module  # noqa: E402
from agent import AgentStudio  # noqa: E402
from a2a_pack import CallResult  # noqa: E402
from agent_studio import (  # noqa: E402
    AgentStudioCoordinator,
    AgentStudioSpecialists,
    PlatformHelperClient,
)
from agent_studio.specialists import (  # noqa: E402
    BUILDER_HANDOFF_TIMEOUT_SECONDS,
    BUILDER_WORKSPACE_TTL_SECONDS,
    CODE_EDITOR_AGENT_TARGET,
    _child_progress_message,
    _delegate_target_workspace_if_needed,
    _editable_ref,
    _exception_summary,
    _sample_args_from_schema,
)
from agent_studio.coordinator import _redact_report  # noqa: E402
from agent_studio.models import (  # noqa: E402
    AcceptanceCall,
    AppSpec,
    AgentStudioReport,
    BrowserJourney,
    BrowserStep,
    BuildBrief,
    DistributionSpec,
    EvaluationResult,
    HandoffRecord,
    MutationRecord,
    OutputExpectation,
    ReviewSummary,
)
from agent_studio.planning import build_launch_plan  # noqa: E402


class FakeContext:
    def __init__(self, *, cp_url: str | None = None, cp_jwt: str | None = None) -> None:
        self.progress: list[str] = []
        self.cp_url = cp_url
        self.cp_jwt = cp_jwt

    async def emit_progress(self, message: str) -> None:
        self.progress.append(message)


def test_child_progress_message_redacts_tool_arguments_and_results() -> None:
    assert (
        _child_progress_message(
            {
                "payload": {
                    "summary": "→ write_agent_file(name=secret, content=very-secret)"
                }
            }
        )
        == "running write_agent_file"
    )
    assert (
        _child_progress_message(
            {"payload": {"summary": "  write_agent_file ← content='secret bytes'"}}
        )
        == "write_agent_file completed"
    )
    assert (
        _child_progress_message(
            {"payload": {"summary": "deployed: https://demo.a2acloud.io"}}
        )
        == "deployed: https://demo.a2acloud.io"
    )
    assert (
        _child_progress_message({"payload": {"summary": "receipt token abc.def"}})
        is None
    )


def test_exception_summary_strips_signed_a2a_evidence() -> None:
    token = "eyJhbGciOiJIUzI1NiJ9.abcdefghijk12345.lmnopqrstuv98765"
    exc = RuntimeError(
        'request failed: {"detail":"database unavailable",'
        f'"a2a_evidence":{{"receipt":{{"signed_token":"{token}"}}}}}}'
    )

    summary = _exception_summary("acceptance call failed", exc)

    assert "database unavailable" in summary
    assert "A2A evidence redacted" in summary
    assert token not in summary
    assert "signed_token" not in summary


def test_launch_plan_infers_recipe_and_platform_capabilities() -> None:
    spec = AppSpec(
        profile="full_stack",
        product_ui=True,
        auth="platform",
        uploads=True,
        persistence=True,
        integrations=["Gmail", "Stripe"],
        requires_mcp=True,
        account_trial_calls=3,
        output_expectations=[OutputExpectation(kind="artifact", filename="report.md")],
    )

    plan = build_launch_plan(
        "Email a daily invoice report and require approval before refunds.",
        spec,
    )

    assert plan.recipe == "document_generator"
    required = {item.capability for item in plan.capabilities if item.required}
    assert {
        "frontend",
        "database",
        "files",
        "email",
        "schedules",
        "payments",
        "browser",
        "mcp",
        "auth",
        "artifacts",
    }.issubset(required)
    assert plan.account_trial_calls == 3
    assert plan.after_trial == "byok"


class FakeWorkspace:
    def __init__(self) -> None:
        self.delegations: list[dict[str, Any]] = []
        self.current_grant = SimpleNamespace(
            llm_models=("platform-model",),
            llm_max_budget_usd=20.0,
            llm_rpm_limit=30,
            llm_tpm_limit=60_000,
        )

    async def delegate(self, **kwargs: Any) -> str:
        self.delegations.append(dict(kwargs))
        return "delegated-builder-grant"


class MockSpecialists:
    async def build_agent(
        self,
        *,
        name: str,
        brief: BuildBrief,
        public: bool,
        version: str,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "name": name,
            "version": version,
            "url": f"https://{name}.a2acloud.io",
            "workspace_dir": f"agents/{name}/",
        }

    async def load_live_state(self, *, name: str) -> dict[str, Any]:
        return {
            "ok": True,
            "url": f"https://{name}.a2acloud.io",
            "head_sha": "abc123",
            "deployment_id": "deploy-123",
            "card": {
                "name": name,
                "version": "0.1.0",
                "skills": [{"name": "run", "description": "Run the agent"}],
            },
        }

    async def evaluate(
        self,
        *,
        name: str,
        brief: BuildBrief,
        live_state: dict[str, Any],
    ) -> list[EvaluationResult]:
        return [
            EvaluationResult(
                name="agent-card",
                status="pass",
                summary="live card is present",
            )
        ]

    async def review_agent(
        self,
        *,
        name: str,
        ref: str,
        owner: str | None,
    ) -> ReviewSummary:
        return ReviewSummary(status="passed", summary="clean")

    async def patch_agent(
        self,
        *,
        name: str,
        prompt: str,
        ref: str,
        owner: str | None,
        session_name: str,
        target_finding_hash: str | None = None,
        target_review_id: str | None = None,
    ) -> MutationRecord:
        return MutationRecord(status="skipped", summary="not needed")

    async def refresh_agent(self, *, name: str) -> dict[str, Any]:
        return await self.load_live_state(name=name)

    async def publish_agent(self, *, name: str) -> dict[str, Any]:
        return {"ok": True, "name": name, "public": True}

    async def verify_distribution(self, *, name, brief, live_state):
        del brief, live_state
        return {
            "ok": True,
            "public_url": f"https://a2acloud.io/a/{name}",
            "checks": {"agent_card": {"ok": True}},
            "failures": [],
        }

    async def exercise_code_editor(
        self,
        *,
        name: str,
        ref: str,
        owner: str | None,
        session_name: str,
    ) -> MutationRecord:
        return MutationRecord(
            status="dry_run",
            summary="code-editor dry-run completed",
            base_sha=ref,
            exit_code=0,
        )


class FakePlatform:
    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.state = state or {
            "ok": True,
            "url": "https://invoice-helper.a2acloud.io",
            "head_sha": "abc123",
            "deployment_id": "deploy-123",
            "card": {
                "name": "invoice-helper",
                "version": "0.1.0",
                "skills": [
                    {
                        "name": "run",
                        "description": "Run",
                        "input_schema": {"type": "object", "properties": {}},
                    }
                ],
            },
        }
        self.enabled: list[str] = []
        self.source_deploys: list[str] = []

    async def refresh_agent(self, name: str) -> dict[str, Any]:
        return dict(self.state)

    async def enable_code_editor(self, name: str) -> dict[str, Any]:
        self.enabled.append(name)
        return {
            "ok": True,
            "name": name,
            "code_editor": {"enabled": True, "status": "enabled"},
        }

    async def deployment_status(self, name: str) -> dict[str, Any]:
        return {
            "ok": True,
            "deployments": [
                {
                    "deploy_id": "deploy-123",
                    "source_repo_url": f"https://gitea.example/alice/{name}",
                }
            ],
        }

    async def deploy_source(self, name: str) -> dict[str, Any]:
        self.source_deploys.append(name)
        return {
            "ok": True,
            "deployment_id": "source-deploy-head",
            "head_sha": "head",
            "status": "queued",
        }

    async def publish_agent(self, name: str) -> dict[str, Any]:
        self.state["public"] = True
        return {"ok": True, "name": name, "public": True}

    async def verify_distribution(self, *, name, url, repo_url, spec):
        del repo_url, spec
        return {
            "ok": True,
            "public_url": f"https://a2acloud.io/a/{name}",
            "live_app_url": f"{url}/app/",
            "checks": {"agent_card": {"ok": True}},
            "failures": [],
        }


class FakeCallContext(FakeContext):
    def __init__(
        self,
        *,
        result: Any | None = None,
        error: BaseException | None = None,
        workspace: Any | None = None,
    ) -> None:
        super().__init__(cp_url="https://api.example.test", cp_jwt="cp-token")
        self.result = result
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self.workspace = workspace if workspace is not None else FakeWorkspace()

    async def call(
        self,
        target: str,
        skill: str,
        *,
        args: dict[str, Any] | None = None,
        grant: str | None = None,
        timeout: float | None = None,
        **_: Any,
    ) -> CallResult:
        self.calls.append(
            {
                "target": target,
                "skill": skill,
                "args": args or {},
                "grant": grant,
                "timeout": timeout,
            }
        )
        if self.error is not None:
            raise self.error
        return CallResult(result=self.result, events=({"kind": "progress"},))


class LoopSpecialists(MockSpecialists):
    def __init__(
        self,
        *,
        evaluations: list[list[EvaluationResult]],
        reviews: list[ReviewSummary],
        mutations: list[MutationRecord] | None = None,
    ) -> None:
        self.evaluations = list(evaluations)
        self.reviews = list(reviews)
        self.mutations = list(mutations or [])
        self.patch_prompts: list[str] = []
        self.refreshes = 0
        self.live_head = "sha-0"

    async def load_live_state(self, *, name: str) -> dict[str, Any]:
        deployment_id = f"deploy-{self.refreshes}"
        return {
            "ok": True,
            "url": f"https://{name}.a2acloud.io",
            "head_sha": self.live_head,
            "deployment_id": deployment_id,
            "latest_deployment": {
                "deploy_id": deployment_id,
                "head_sha": self.live_head,
                "status": "live",
            },
            "card": {
                "name": name,
                "version": "0.1.0",
                "skills": [{"name": "run", "input_schema": {"type": "object"}}],
            },
        }

    async def refresh_agent(self, *, name: str) -> dict[str, Any]:
        self.refreshes += 1
        return await self.load_live_state(name=name)

    async def evaluate(
        self,
        *,
        name: str,
        brief: BuildBrief,
        live_state: dict[str, Any],
    ) -> list[EvaluationResult]:
        if self.evaluations:
            return self.evaluations.pop(0)
        return [
            EvaluationResult(
                name="agent-card",
                status="pass",
                summary="live card is present",
            )
        ]

    async def review_agent(
        self,
        *,
        name: str,
        ref: str,
        owner: str | None,
    ) -> ReviewSummary:
        if self.reviews:
            return self.reviews.pop(0)
        return ReviewSummary(status="passed", summary="clean")

    async def patch_agent(
        self,
        *,
        name: str,
        prompt: str,
        ref: str,
        owner: str | None,
        session_name: str,
        target_finding_hash: str | None = None,
        target_review_id: str | None = None,
    ) -> MutationRecord:
        self.patch_prompts.append(prompt)
        if self.mutations:
            mutation = self.mutations.pop(0)
            mutation.target_finding_hash = (
                mutation.target_finding_hash or target_finding_hash
            )
            mutation.target_review_id = mutation.target_review_id or target_review_id
            if mutation.head_sha:
                self.live_head = mutation.head_sha
            return mutation
        mutation = MutationRecord(
            status="pushed",
            summary="patched",
            base_sha=ref,
            head_sha="sha-patched",
            target_finding_hash=target_finding_hash,
            target_review_id=target_review_id,
            changed_files=["agent.py"],
            exit_code=0,
        )
        self.live_head = mutation.head_sha or self.live_head
        return mutation

    async def exercise_code_editor(
        self,
        *,
        name: str,
        ref: str,
        owner: str | None,
        session_name: str,
    ) -> MutationRecord:
        return MutationRecord(
            status="dry_run",
            summary="code-editor dry-run completed",
            base_sha=ref,
            exit_code=0,
        )


def test_class_metadata_and_skill_policy() -> None:
    assert AgentStudio.name == "agent-studio"
    assert AgentStudio.version == "0.1.31"
    assert "version: 0.1.31" in (ROOT / "a2a.yaml").read_text(encoding="utf-8")
    assert AgentStudio.wants_cp_jwt is True
    meta = AgentStudio().card().capabilities["meta_agent"]
    assert meta["composition"]["planning"] == "deterministic_dag"
    assert [item["name"] for item in meta["composition"]["sub_agents"]] == [
        "agent-builder",
        "agent-reviewer",
        "code-editor-agent",
    ]
    assert meta["memory"]["tiers"] == ["files"]

    spec = AgentStudio._skills["create_agent"]  # type: ignore[attr-defined]
    assert spec.stream is True
    assert spec.policy.timeout_seconds == 3600
    assert spec.policy.grant_outputs_prefix == "agents/{name}/.agent-studio/"
    assert spec.policy.grant_allow_patterns == ("agents/{name}/**",)
    assert spec.policy.grant_write_prefixes == (
        "agents/{name}/",
        "agents/{name}/.agent-studio/",
    )
    upgrade = AgentStudio._skills["upgrade_agent"]  # type: ignore[attr-defined]
    assert upgrade.stream is True
    assert upgrade.policy.timeout_seconds == 3600
    assert upgrade.policy.grant_allow_patterns == ("agents/{name}/**",)


def test_report_schema_contains_stable_fields() -> None:
    report_fields = {
        "ok",
        "status",
        "run_id",
        "agent_name",
        "agent_url",
        "version",
        "head_sha",
        "deployment_id",
        "live_card",
        "iterations",
        "tests",
        "review",
        "residual_risks",
        "ledger_path",
    }
    from agent_studio.models import AgentStudioReport

    assert report_fields.issubset(AgentStudioReport.model_fields)


@pytest.mark.asyncio
async def test_mocked_happy_path_succeeds() -> None:
    ctx = FakeContext()
    coordinator = AgentStudioCoordinator(MockSpecialists())

    report = await coordinator.run(
        ctx,
        name="invoice-helper",
        goal="Read invoice text and return structured JSON.",
        public=False,
        max_iterations=3,
        quality_bar="high",
    )

    assert report.ok is True
    assert report.status == "succeeded"
    assert report.agent_name == "invoice-helper"
    assert report.public is False
    assert report.agent_url == "https://invoice-helper.a2acloud.io"
    assert report.head_sha == "abc123"
    assert report.deployment_id == "deploy-123"
    assert report.review.status == "passed"
    assert report.tests[0].status == "pass"
    assert report.handoffs[0].agent == "agent-builder"
    assert report.handoffs[0].args_summary["public"] is False
    assert report.ledger_path == "agents/invoice-helper/.agent-studio/report.json"
    assert any("agent-builder.build" in event for event in ctx.progress)


@pytest.mark.asyncio
async def test_existing_agent_version_is_forwarded_to_builder_without_downgrade() -> (
    None
):
    class ExistingVersionSpecialists(MockSpecialists):
        def __init__(self) -> None:
            self.build_versions: list[str] = []

        async def load_live_state(self, *, name: str) -> dict[str, Any]:
            state = await super().load_live_state(name=name)
            state["card"]["version"] = "0.9.4"
            return state

        async def build_agent(
            self,
            *,
            name: str,
            brief: BuildBrief,
            public: bool,
            version: str,
        ) -> dict[str, Any]:
            self.build_versions.append(version)
            result = await super().build_agent(
                name=name,
                brief=brief,
                public=public,
                version=version,
            )
            return result

    specialists = ExistingVersionSpecialists()

    report = await AgentStudioCoordinator(specialists).run(
        FakeContext(),
        name="invoice-helper",
        goal="Read invoice text and return structured JSON.",
    )

    assert report.ok is True
    assert report.version == "0.9.4"
    assert specialists.build_versions == ["0.9.4"]
    assert report.handoffs[0].args_summary["version"] == "0.9.4"


@pytest.mark.asyncio
async def test_create_agent_normalizes_verbose_quality_bar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeCoordinator:
        def __init__(self, specialists: Any) -> None:
            self.specialists = specialists

        async def run(self, ctx: Any, **kwargs: Any) -> AgentStudioReport:
            del ctx
            captured.update(kwargs)
            return AgentStudioReport(
                ok=False,
                status="partial",
                run_id="agent-studio-test",
                agent_name=kwargs["name"],
                started_at="2026-06-06T00:00:00+00:00",
                stop_reason="builder_failed",
            )

    monkeypatch.setattr(agent_module, "AgentStudioCoordinator", FakeCoordinator)

    result = await AgentStudio().create_agent(
        FakeContext(),
        name="invoice-helper",
        goal="Build invoices.",
        quality_bar="Make it very robust with high quality validation",
        max_spend_cents=99999,
    )

    assert captured["quality_bar"] == "standard"
    assert captured["max_spend_cents"] == 3000
    assert result["status"] == "partial"


@pytest.mark.asyncio
async def test_mocked_happy_path_can_exercise_code_editor() -> None:
    ctx = FakeContext()
    coordinator = AgentStudioCoordinator(MockSpecialists())

    report = await coordinator.run(
        ctx,
        name="invoice-helper",
        goal="Read invoice text and return structured JSON.",
        exercise_code_editor=True,
    )

    assert report.ok is True
    assert report.status == "succeeded"
    code_handoff = next(
        handoff for handoff in report.handoffs if handoff.agent == "code-editor-agent"
    )
    assert code_handoff.skill == "turn"
    assert code_handoff.status == "ok"
    assert code_handoff.args_summary["dry_run"] is True
    assert code_handoff.result_summary["status"] == "dry_run"


@pytest.mark.asyncio
async def test_budget_stop_before_child_call() -> None:
    ctx = FakeContext()
    coordinator = AgentStudioCoordinator(MockSpecialists())

    report = await coordinator.run(
        ctx,
        name="invoice-helper",
        goal="Read invoice text and return structured JSON.",
        max_child_calls=0,
    )

    assert report.ok is False
    assert report.status == "blocked"
    assert report.stop_reason == "max_child_calls_exhausted_before_build"
    assert report.handoffs == []
    assert report.budgets["max_child_calls"] == 0


@pytest.mark.asyncio
async def test_invalid_goal_stops_before_child_call() -> None:
    ctx = FakeContext()
    coordinator = AgentStudioCoordinator(MockSpecialists())

    report = await coordinator.run(
        ctx,
        name="invoice-helper",
        goal="",
    )

    assert report.ok is False
    assert report.status == "failed"
    assert report.stop_reason == "invalid_goal"
    assert report.handoffs == []
    assert report.ledger_path == "agents/invoice-helper/.agent-studio/report.json"


@pytest.mark.asyncio
async def test_public_request_publishes_only_after_acceptance() -> None:
    ctx = FakeContext()
    coordinator = AgentStudioCoordinator(MockSpecialists())

    report = await coordinator.run(
        ctx,
        name="invoice-helper",
        goal="Read invoice text and return structured JSON.",
        public=True,
    )

    assert report.public is True
    assert report.handoffs[0].args_summary["public"] is False
    assert report.publish_next_step == "published_and_verified"
    assert report.distribution["ok"] is True
    assert report.residual_risks == []


@pytest.mark.asyncio
async def test_report_redacts_token_like_values() -> None:
    class LeakySpecialists(MockSpecialists):
        async def build_agent(
            self,
            *,
            name: str,
            brief: BuildBrief,
            public: bool,
            version: str,
        ) -> dict[str, Any]:
            out = await super().build_agent(
                name=name,
                brief=brief,
                public=public,
                version=version,
            )
            out["token"] = "sk-test-secret"
            out["error"] = "bearer abc"
            return out

    report = await AgentStudioCoordinator(LeakySpecialists()).run(
        FakeContext(),
        name="invoice-helper",
        goal="Read invoice text and return structured JSON.",
    )

    summary = report.handoffs[0].result_summary
    assert summary["error"] == "[redacted]"
    assert "token" not in summary


@pytest.mark.asyncio
async def test_coordinator_preserves_structured_full_stack_brief() -> None:
    class CapturingSpecialists(MockSpecialists):
        brief: BuildBrief | None = None

        async def build_agent(self, *, name, brief, public, version):
            self.brief = brief
            return await super().build_agent(
                name=name,
                brief=brief,
                public=public,
                version=version,
            )

    specialists = CapturingSpecialists()
    app_spec = AppSpec(
        profile="full_stack",
        product_ui=True,
        auth="platform",
        uploads=True,
        persistence=True,
        requires_mcp=True,
        requires_receipt=True,
        primary_workflow="Compare two quotes and save the result",
        acceptance_calls=[
            AcceptanceCall(
                purpose="reload",
                tool="get_comparison",
                arguments={"comparison_id": "cmp-1"},
            )
        ],
    )

    report = await AgentStudioCoordinator(specialists).run(
        FakeContext(),
        name="quote-judge",
        goal="Compare vendor quotes in a useful one-page app.",
        app_spec_json=app_spec.model_dump_json(),
        max_spend_cents=1000,
    )

    assert report.ok is True
    assert specialists.brief is not None
    assert specialists.brief.app_spec == app_spec
    prompt = specialists.brief.to_prompt()
    assert "Application contract (authoritative JSON)" in prompt
    assert '"profile": "full_stack"' in prompt
    assert "packed frontend" in prompt
    assert "Managed Postgres" in prompt
    assert "MCP tools/list" in prompt
    assert "Maximum build spend: 1000 cents" in prompt
    assert "Lean-build mode is mandatory" in prompt


@pytest.mark.asyncio
async def test_coordinator_rejects_invalid_app_spec() -> None:
    report = await AgentStudioCoordinator(MockSpecialists()).run(
        FakeContext(),
        name="quote-judge",
        goal="Compare vendor quotes in a useful one-page app.",
        app_spec_json="[]",
    )

    assert report.status == "failed"
    assert report.stop_reason == "invalid_app_spec"


@pytest.mark.asyncio
async def test_agent_studio_specialists_call_agent_builder_success() -> None:
    ctx = FakeCallContext(
        result={
            "ok": True,
            "name": "invoice-helper",
            "version": "0.1.0",
            "url": "https://invoice-helper.a2acloud.io",
            "workspace_dir": "agents/invoice-helper/",
            "reply": "built it",
        }
    )
    coordinator = AgentStudioCoordinator(
        AgentStudioSpecialists(ctx, platform=FakePlatform())
    )

    report = await coordinator.run(
        ctx,
        name="invoice-helper",
        goal="Read invoice text and return structured JSON.",
        public=True,
        quality_bar="high",
    )

    assert report.ok is True
    assert ctx.calls[0]["target"] == "agent-builder"
    assert ctx.calls[0]["skill"] == "build"
    assert ctx.calls[0]["args"]["public"] is False
    assert ctx.calls[0]["grant"] == "delegated-builder-grant"
    assert ctx.workspace.delegations == [
        {
            "audience": "agent-builder",
            "allow_patterns": ("agents/invoice-helper/**",),
            "mode": "read_write_overlay",
            "outputs_prefix": "agents/invoice-helper/",
            "llm_models": ("platform-model",),
            "llm_max_budget_usd": 20.0,
            "llm_rpm_limit": 30,
            "llm_tpm_limit": 60_000,
            "source_grants": ({"agent": "invoice-helper", "scope": "write"},),
            "write_prefixes": ("agents/invoice-helper/",),
            "ttl_seconds": BUILDER_WORKSPACE_TTL_SECONDS,
        }
    ]
    assert ctx.calls[0]["timeout"] == BUILDER_HANDOFF_TIMEOUT_SECONDS
    assert "Read invoice text" in ctx.calls[0]["args"]["prompt"]
    assert report.agent_url == "https://invoice-helper.a2acloud.io"
    assert report.workspace_dir == "agents/invoice-helper/"
    assert (
        report.handoffs[0].result_summary["url"] == "https://invoice-helper.a2acloud.io"
    )
    assert report.handoffs[0].result_summary["reply"] == "built it"
    assert report.public is True
    assert report.publish_next_step == "published_and_verified"


@pytest.mark.asyncio
async def test_agent_studio_specialists_builder_schema_error_becomes_partial_report() -> (
    None
):
    ctx = FakeCallContext(error=RuntimeError("a2a build -> 400: invalid input"))
    report = await AgentStudioCoordinator(
        AgentStudioSpecialists(ctx, platform=FakePlatform())
    ).run(ctx, name="invoice-helper", goal="Build invoices.")

    assert report.ok is False
    assert report.status == "partial"
    assert report.stop_reason == "builder_failed"
    assert "agent-builder handoff failed" in report.residual_risks[0]
    assert any("agent-builder failed:" in message for message in ctx.progress)


@pytest.mark.asyncio
async def test_agent_studio_specialists_builder_without_url_stops_partial() -> None:
    ctx = FakeCallContext(
        result={
            "ok": True,
            "name": "invoice-helper",
            "version": "0.1.0",
            "warning": "no deploy URL",
        }
    )
    report = await AgentStudioCoordinator(
        AgentStudioSpecialists(ctx, platform=FakePlatform())
    ).run(ctx, name="invoice-helper", goal="Build invoices.")

    assert report.ok is False
    assert report.status == "partial"
    assert report.stop_reason == "builder_failed"
    assert report.handoffs[0].status == "failed"
    assert report.handoffs[0].result_summary["warning"] == "no deploy URL"


@pytest.mark.asyncio
async def test_agent_studio_specialists_builder_timeout_stops_partial() -> None:
    ctx = FakeCallContext(error=TimeoutError())
    report = await AgentStudioCoordinator(
        AgentStudioSpecialists(ctx, platform=FakePlatform())
    ).run(ctx, name="invoice-helper", goal="Build invoices.")

    assert report.ok is False
    assert report.status == "partial"
    assert report.stop_reason == "builder_failed"
    assert report.residual_risks == ["agent-builder timed out"]


@pytest.mark.asyncio
async def test_agent_studio_specialists_missing_a2a_client_is_structured() -> None:
    ctx = FakeCallContext(
        error=PermissionError(
            "no A2A client attached; runtime must provision one before "
            "ctx.call(...) can be used"
        )
    )
    report = await AgentStudioCoordinator(
        AgentStudioSpecialists(ctx, platform=FakePlatform())
    ).run(ctx, name="invoice-helper", goal="Build invoices.")

    assert report.ok is False
    assert report.status == "partial"
    assert report.stop_reason == "builder_failed"
    assert "missing an outbound A2A client" in report.residual_risks[0]


@pytest.mark.asyncio
async def test_agent_studio_evaluation_smoke_pass_uses_schema_args() -> None:
    ctx = FakeCallContext(result={"ok": True, "summary": "ran"})
    specialists = AgentStudioSpecialists(ctx, platform=FakePlatform())
    brief = BuildBrief(
        agent_name="invoice-helper",
        goal="Read invoice text.",
        target_user="tester",
    )

    results = await specialists.evaluate(
        name="invoice-helper",
        brief=brief,
        live_state={
            "ok": True,
            "card": {
                "version": "0.1.0",
                "skills": [
                    {
                        "name": "extract",
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string"},
                                "max_items": {"type": "integer", "minimum": 2},
                                "controls": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                            "required": ["text", "max_items", "controls"],
                        },
                    }
                ],
            },
        },
    )

    assert [result.status for result in results] == ["pass", "pass"]
    assert ctx.calls[0]["target"] == "invoice-helper"
    assert ctx.calls[0]["skill"] == "extract"
    assert ctx.calls[0]["args"] == {
        "text": "smoke-text",
        "max_items": 2,
        "controls": ["smoke-controls"],
    }


@pytest.mark.asyncio
async def test_agent_studio_evaluation_gates_full_stack_contract() -> None:
    class FullStackContext(FakeCallContext):
        async def call(
            self, target, skill, *, args=None, grant=None, timeout=None, **kwargs
        ):
            self.calls.append(
                {
                    "target": target,
                    "skill": skill,
                    "args": args or {},
                    "grant": grant,
                    "timeout": timeout,
                }
            )
            if args and args.get("quotes") == []:
                return CallResult(
                    result={"ok": False, "errors": ["two quotes required"]}
                )
            return CallResult(result={"ok": True, "comparison_id": "cmp-1"})

    class FullStackPlatform(FakePlatform):
        def __init__(self):
            super().__init__()
            self.receipt_reads = 0

        async def probe_frontend(self, url):
            assert url == "https://quote-judge.a2acloud.io"
            return {
                "ok": True,
                "statuses": {
                    "/app/": 200,
                    "/app/config.json": 200,
                    "/app/a2a-client.js": 200,
                },
                "auth_mode": "platform",
            }

        async def list_mcp_tools(self, url):
            return {
                "ok": True,
                "tools": [{"name": "compare_quotes"}, {"name": "get_comparison"}],
            }

        async def call_mcp_tool(self, url, *, tool, arguments):
            assert tool == "get_comparison"
            assert arguments == {"comparison_id": "cmp-1"}
            return {"ok": True, "result": {"comparison_id": "cmp-1"}}

        async def list_receipts(self, name, *, limit=50):
            self.receipt_reads += 1
            if self.receipt_reads == 1:
                return {"ok": True, "receipts": []}
            return {
                "ok": True,
                "receipts": [
                    {
                        "receipt_id": "receipt-1",
                        "skill_name": "compare_quotes",
                        "status": "ok",
                    }
                ],
            }

    ctx = FullStackContext()
    platform = FullStackPlatform()
    specialists = AgentStudioSpecialists(ctx, platform=platform)
    brief = BuildBrief(
        agent_name="quote-judge",
        goal="Compare and save vendor quotes.",
        target_user="buyer",
        app_spec=AppSpec(
            profile="full_stack",
            product_ui=True,
            auth="platform",
            uploads=True,
            persistence=True,
            requires_mcp=True,
            requires_receipt=True,
            acceptance_calls=[
                AcceptanceCall(
                    purpose="success",
                    tool="compare_quotes",
                    arguments={"quotes": [{"vendor": "A"}, {"vendor": "B"}]},
                    expected={"ok": True, "comparison_id": "cmp-1"},
                ),
                AcceptanceCall(
                    purpose="reload",
                    tool="get_comparison",
                    arguments={"comparison_id": "cmp-1"},
                    expected={"comparison_id": "cmp-1"},
                ),
                AcceptanceCall(
                    purpose="failure",
                    tool="compare_quotes",
                    arguments={"quotes": []},
                    expect_error=True,
                    expected={"errors": ["two quotes required"]},
                ),
                AcceptanceCall(
                    purpose="mcp",
                    tool="get_comparison",
                    arguments={"comparison_id": "cmp-1"},
                    expected={"comparison_id": "cmp-1"},
                ),
            ],
        ),
    )
    skills = [
        {
            "name": "compare_quotes",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_comparison",
            "input_schema": {"type": "object", "properties": {}},
        },
    ]

    results = await specialists.evaluate(
        name="quote-judge",
        brief=brief,
        live_state={
            "ok": True,
            "url": "https://quote-judge.a2acloud.io",
            "card": {
                "version": "0.1.0",
                "skills": skills,
                "runtime": {
                    "platform_resources": {"databases": [{"name": "quote-data"}]}
                },
            },
        },
    )

    by_name = {result.name: result.status for result in results}
    assert by_name["managed-database"] == "pass"
    assert by_name["acceptance:success:compare_quotes"] == "pass"
    assert by_name["acceptance:reload:get_comparison"] == "pass"
    assert by_name["acceptance:failure:compare_quotes"] == "pass"
    assert by_name["packed-frontend"] == "pass"
    assert by_name["mcp:tools-list"] == "pass"
    assert by_name["mcp:tools-call"] == "pass"
    assert by_name["execution-receipt"] == "pass"
    assert all(result.status != "fail" for result in results)


@pytest.mark.asyncio
async def test_evaluation_proves_first_class_artifact_and_account_trial() -> None:
    class ArtifactContext(FakeCallContext):
        async def call(
            self, target, skill, *, args=None, grant=None, timeout=None, **kwargs
        ):
            del target, skill, args, grant, timeout, kwargs
            return CallResult(
                result={
                    "ok": True,
                    "document": {
                        "name": "report.md",
                        "media_type": "text/markdown",
                        "uri": "artifact://report.md",
                        "content": "# Report",
                    },
                },
                artifacts=(
                    {
                        "name": "report.md",
                        "mime_type": "text/markdown",
                        "uri": "artifact://report.md",
                    },
                ),
            )

    specialists = AgentStudioSpecialists(ArtifactContext(), platform=FakePlatform())
    brief = BuildBrief(
        agent_name="report-maker",
        goal="Generate a report artifact.",
        target_user="operator",
        app_spec=AppSpec(
            account_trial_calls=3,
            acceptance_calls=[
                AcceptanceCall(purpose="success", tool="generate", arguments={})
            ],
            output_expectations=[
                OutputExpectation(
                    kind="artifact",
                    filename="report.md",
                    media_type="text/markdown",
                ),
                OutputExpectation(
                    kind="preview",
                    filename="report.md",
                    media_type="text/markdown",
                ),
                OutputExpectation(
                    kind="download",
                    filename="report.md",
                    media_type="text/markdown",
                ),
            ],
        ),
    )

    results = await specialists.evaluate(
        name="report-maker",
        brief=brief,
        live_state={
            "ok": True,
            "url": "https://report-maker.a2acloud.io",
            "card": {
                "skills": [{"name": "generate", "input_schema": {"type": "object"}}],
                "runtime": {
                    "account_access": {
                        "required": True,
                        "platform_skill_calls": 3,
                        "after_trial": "byok",
                    }
                },
            },
        },
    )

    by_name = {result.name: result.status for result in results}
    assert by_name["account-funded-trial"] == "pass"
    assert by_name["output:artifact:report.md"] == "pass"
    assert by_name["output:preview:report.md"] == "pass"
    assert by_name["output:download:report.md"] == "pass"


@pytest.mark.asyncio
async def test_evaluation_runs_declared_real_browser_journey(monkeypatch) -> None:
    seen: list[BrowserJourney] = []

    class FakeBrowserProofRunner:
        def __init__(self, ctx):
            del ctx

        async def run(self, *, base_url, journey, authorization=""):
            assert base_url == "https://browser-app.a2acloud.io"
            assert authorization == "Bearer cp-token"
            seen.append(journey)
            return {
                "ok": True,
                "completed_steps": ["1:click", "2:assert_visible"],
                "screenshot": {"name": "browser-proof-primary.png"},
                "console_errors": [],
            }

    class BrowserPlatform(FakePlatform):
        async def probe_frontend(self, url):
            assert url == "https://browser-app.a2acloud.io"
            return {"ok": True, "statuses": {"/app/": 200}, "auth_mode": "platform"}

    monkeypatch.setattr(
        specialists_module, "BrowserProofRunner", FakeBrowserProofRunner
    )
    ctx = FakeCallContext(result={"ok": True})
    journey = BrowserJourney(
        name="primary",
        steps=[
            BrowserStep(action="click", selector="[data-testid='agent-submit']"),
            BrowserStep(
                action="assert_visible", selector="[data-testid='agent-result']"
            ),
        ],
    )
    results = await AgentStudioSpecialists(ctx, platform=BrowserPlatform()).evaluate(
        name="browser-app",
        brief=BuildBrief(
            agent_name="browser-app",
            goal="Run a browser app.",
            target_user="operator",
            app_spec=AppSpec(
                profile="full_stack",
                product_ui=True,
                auth="platform",
                browser_journeys=[journey],
            ),
        ),
        live_state={
            "ok": True,
            "url": "https://browser-app.a2acloud.io",
            "card": {
                "skills": [
                    {
                        "name": "run",
                        "input_schema": {"type": "object", "properties": {}},
                    }
                ]
            },
        },
    )

    browser_result = next(
        result for result in results if result.name == "browser:primary"
    )
    assert browser_result.status == "pass"
    assert browser_result.details["screenshot"]["name"] == "browser-proof-primary.png"
    assert seen == [journey]


@pytest.mark.asyncio
async def test_agent_studio_generic_smoke_skips_file_upload_schema() -> None:
    ctx = FakeCallContext(result={"ok": True})
    specialists = AgentStudioSpecialists(ctx, platform=FakePlatform())

    results = await specialists.evaluate(
        name="upload-helper",
        brief=BuildBrief(
            agent_name="upload-helper", goal="Upload files", target_user="tester"
        ),
        live_state={
            "ok": True,
            "card": {
                "skills": [
                    {
                        "name": "ingest",
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "documents": {
                                    "type": "array",
                                    "x-a2a-file-upload": {"multiple": True},
                                }
                            },
                        },
                    }
                ]
            },
        },
    )

    assert [result.status for result in results] == ["pass", "warning"]
    assert "FileUpload" in results[1].summary
    assert results[1].details["actionable"] is False
    assert ctx.calls == []


@pytest.mark.asyncio
async def test_agent_studio_generic_smoke_skips_non_idempotent_skill() -> None:
    ctx = FakeCallContext(result={"ok": True})
    specialists = AgentStudioSpecialists(ctx, platform=FakePlatform())

    results = await specialists.evaluate(
        name="decision-helper",
        brief=BuildBrief(
            agent_name="decision-helper",
            goal="Record a decision",
            target_user="tester",
        ),
        live_state={
            "ok": True,
            "card": {
                "skills": [
                    {
                        "name": "record_decision",
                        "policy": {"idempotent": False},
                        "input_schema": {
                            "type": "object",
                            "properties": {"case_id": {"type": "string"}},
                            "required": ["case_id"],
                        },
                    }
                ]
            },
        },
    )

    assert [result.status for result in results] == ["pass", "warning"]
    assert "non-idempotent" in results[1].summary
    assert results[1].details["actionable"] is False
    assert ctx.calls == []


@pytest.mark.asyncio
async def test_agent_studio_generic_smoke_skips_browser_base64_upload_schema() -> None:
    ctx = FakeCallContext(result={"ok": True})
    specialists = AgentStudioSpecialists(ctx, platform=FakePlatform())

    results = await specialists.evaluate(
        name="upload-helper",
        brief=BuildBrief(
            agent_name="upload-helper", goal="Upload files", target_user="tester"
        ),
        live_state={
            "ok": True,
            "card": {
                "skills": [
                    {
                        "name": "ingest_browser",
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "documents": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "data_base64": {"type": "string"},
                                        },
                                    },
                                }
                            },
                        },
                    }
                ]
            },
        },
    )

    assert [result.status for result in results] == ["pass", "warning"]
    assert "browser base64" in results[1].summary
    assert results[1].details["actionable"] is False
    assert ctx.calls == []


@pytest.mark.asyncio
async def test_agent_studio_evaluation_delegates_workspace_for_workspace_agent() -> (
    None
):
    workspace = FakeWorkspace()
    ctx = FakeCallContext(result={"ok": True, "summary": "ran"}, workspace=workspace)
    specialists = AgentStudioSpecialists(ctx, platform=FakePlatform())

    results = await specialists.evaluate(
        name="workspace-helper",
        brief=BuildBrief(agent_name="workspace-helper", goal="x", target_user="tester"),
        live_state={
            "ok": True,
            "url": "https://workspace-helper.a2acloud.io",
            "card": {
                "workspace_access": {
                    "enabled": True,
                    "allowed_modes": ["read_only", "read_write_overlay"],
                },
                "skills": [
                    {
                        "name": "run",
                        "input_schema": {"type": "object", "properties": {}},
                    }
                ],
            },
        },
    )

    assert [result.status for result in results] == ["pass", "pass"]
    assert ctx.calls[0]["grant"] == "delegated-builder-grant"
    assert workspace.delegations[0]["audience"] == "workspace-helper"
    assert workspace.delegations[0]["allow_patterns"] == ("agents/workspace-helper/**",)
    assert workspace.delegations[0]["outputs_prefix"] == (
        "agents/workspace-helper/.agent-studio/smoke/"
    )
    assert workspace.delegations[0]["write_prefixes"] == (
        "agents/workspace-helper/.agent-studio/smoke/",
    )


@pytest.mark.asyncio
async def test_target_workspace_delegation_uses_control_plane_signer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"grant": "platform-signed-child-grant"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_: client)
    ctx = FakeCallContext(result={"ok": True})
    ctx.llm = SimpleNamespace(api_key="parent-grant")

    token = await _delegate_target_workspace_if_needed(
        ctx,
        name="workspace-helper",
        card={
            "workspace_access": {
                "enabled": True,
                "allowed_modes": ["read_only", "read_write_overlay"],
            }
        },
    )

    assert token == "platform-signed-child-grant"
    assert ctx.workspace.delegations == []
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://api.example.test/v1/workspace-grants/delegate"
    assert request.headers["X-A2A-Grant"] == "parent-grant"
    assert json.loads(request.content) == {
        "audience": "workspace-helper",
        "allow_patterns": ["agents/workspace-helper/**"],
        "mode": "read_write_overlay",
        "outputs_prefix": "agents/workspace-helper/.agent-studio/smoke/",
        "write_prefixes": ["agents/workspace-helper/.agent-studio/smoke/"],
        "source_grants": [{"agent": "workspace-helper", "scope": "write"}],
        "ttl_seconds": 900,
    }


@pytest.mark.asyncio
async def test_agent_studio_evaluation_samples_nested_refs_and_patterns() -> None:
    ctx = FakeCallContext(result={"ok": True})
    specialists = AgentStudioSpecialists(ctx, platform=FakePlatform())
    schema = {
        "type": "object",
        "properties": {
            "request": {
                "type": "object",
                "$defs": {
                    "Request": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "tenant_id": {"type": "string", "minLength": 3},
                            "plan_digest": {
                                "type": "string",
                                "pattern": r"^sha256:[a-f0-9]{64}$",
                            },
                        },
                        "required": ["tenant_id", "plan_digest"],
                    }
                },
                "additionalProperties": False,
                "properties": {
                    "payload": {"$ref": "#/$defs/Request"},
                },
                "required": ["payload"],
            },
        },
        "required": ["request"],
    }

    results = await specialists.evaluate(
        name="nested-helper",
        brief=BuildBrief(agent_name="nested-helper", goal="x", target_user="tester"),
        live_state={
            "ok": True,
            "card": {"skills": [{"name": "run", "input_schema": schema}]},
        },
    )

    assert [result.status for result in results] == ["pass", "pass"]
    assert ctx.calls[0]["args"] == {
        "request": {
            "payload": {
                "tenant_id": "smoke-tenant-id",
                "plan_digest": "sha256:" + ("0" * 64),
            }
        }
    }


@pytest.mark.asyncio
async def test_agent_studio_evaluation_samples_regex_safe_slug() -> None:
    ctx = FakeCallContext(result={"ok": True})
    specialists = AgentStudioSpecialists(ctx, platform=FakePlatform())

    results = await specialists.evaluate(
        name="incident-helper",
        brief=BuildBrief(agent_name="incident-helper", goal="x", target_user="tester"),
        live_state={
            "ok": True,
            "card": {
                "skills": [
                    {
                        "name": "run",
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "incident_id": {
                                    "type": "string",
                                    "pattern": r"^[a-z0-9][a-z0-9-]{2,63}$",
                                }
                            },
                            "required": ["incident_id"],
                        },
                    }
                ]
            },
        },
    )

    assert [result.status for result in results] == ["pass", "pass"]
    assert ctx.calls[0]["args"] == {"incident_id": "smoke-incident-id"}


@pytest.mark.asyncio
async def test_agent_studio_evaluation_private_cloud_agent_uses_canonical_url() -> None:
    ctx = FakeCallContext(result={"ok": True, "summary": "ran"})
    ctx.cp_url = "https://api.a2acloud.io"
    specialists = AgentStudioSpecialists(ctx, platform=FakePlatform())

    results = await specialists.evaluate(
        name="private-helper",
        brief=BuildBrief(agent_name="private-helper", goal="x", target_user="tester"),
        live_state={
            "ok": True,
            "url": None,
            "card": {
                "skills": [
                    {
                        "name": "run",
                        "input_schema": {"type": "object", "properties": {}},
                    }
                ]
            },
        },
    )

    assert [result.status for result in results] == ["pass", "pass"]
    assert ctx.calls[0]["target"] == "https://private-helper.a2acloud.io"


@pytest.mark.asyncio
async def test_agent_studio_evaluation_schema_mismatch_fails_before_call() -> None:
    ctx = FakeCallContext(result={"ok": True})
    specialists = AgentStudioSpecialists(ctx, platform=FakePlatform())
    results = await specialists.evaluate(
        name="bad-agent",
        brief=BuildBrief(agent_name="bad-agent", goal="x", target_user="tester"),
        live_state={
            "ok": True,
            "card": {
                "skills": [
                    {
                        "name": "run",
                        "input_schema": {
                            "type": "object",
                            "properties": {},
                            "required": ["missing"],
                        },
                    }
                ]
            },
        },
    )

    assert results[1].status == "fail"
    assert "schema mismatch" in results[1].summary
    assert ctx.calls == []


@pytest.mark.asyncio
async def test_agent_studio_evaluation_skill_400_fails() -> None:
    ctx = FakeCallContext(error=RuntimeError("a2a invoke -> 400: bad input"))
    specialists = AgentStudioSpecialists(ctx, platform=FakePlatform())
    results = await specialists.evaluate(
        name="bad-agent",
        brief=BuildBrief(agent_name="bad-agent", goal="x", target_user="tester"),
        live_state={
            "ok": True,
            "card": {
                "skills": [
                    {
                        "name": "run",
                        "input_schema": {"type": "object", "properties": {}},
                    }
                ]
            },
        },
    )

    assert results[1].status == "fail"
    assert "400" in results[1].summary


@pytest.mark.asyncio
async def test_agent_studio_evaluation_timeout_fails() -> None:
    ctx = FakeCallContext(error=TimeoutError())
    specialists = AgentStudioSpecialists(ctx, platform=FakePlatform())
    results = await specialists.evaluate(
        name="slow-agent",
        brief=BuildBrief(agent_name="slow-agent", goal="x", target_user="tester"),
        live_state={
            "ok": True,
            "card": {
                "skills": [
                    {
                        "name": "run",
                        "input_schema": {"type": "object", "properties": {}},
                    }
                ]
            },
        },
    )

    assert results[1].status == "fail"
    assert results[1].summary == "smoke call timed out"


@pytest.mark.asyncio
async def test_agent_studio_evaluation_none_result_warns() -> None:
    ctx = FakeCallContext(result=None)
    specialists = AgentStudioSpecialists(ctx, platform=FakePlatform())
    results = await specialists.evaluate(
        name="quiet-agent",
        brief=BuildBrief(agent_name="quiet-agent", goal="x", target_user="tester"),
        live_state={
            "ok": True,
            "card": {
                "skills": [
                    {
                        "name": "run",
                        "input_schema": {"type": "object", "properties": {}},
                    }
                ]
            },
        },
    )

    assert results[1].status == "warning"
    assert results[1].summary == "smoke call returned no result"


@pytest.mark.asyncio
async def test_agent_studio_loop_patches_critical_review_then_passes() -> None:
    specialists = LoopSpecialists(
        evaluations=[
            [
                EvaluationResult(
                    name="smoke:run",
                    status="fail",
                    summary="run returned 500",
                )
            ],
            [
                EvaluationResult(
                    name="smoke:run",
                    status="pass",
                    summary="run succeeded",
                )
            ],
        ],
        reviews=[
            ReviewSummary(
                status="critical",
                summary="unsafe runtime call",
                critical_count=1,
                findings=[
                    {
                        "severity": "critical",
                        "category": "security",
                        "message": "removes sandbox boundary",
                        "file": "agent.py",
                        "line": 42,
                    }
                ],
            ),
            ReviewSummary(status="passed", summary="clean"),
        ],
    )

    report = await AgentStudioCoordinator(specialists).run(
        FakeContext(),
        name="invoice-helper",
        goal="Read invoice text and return structured JSON.",
        max_iterations=2,
        quality_bar="high",
    )

    assert report.ok is True
    assert report.stop_reason == "acceptance_passed"
    assert [item.status for item in report.iterations] == ["patched", "passed"]
    assert report.iterations[0].mutation is not None
    assert report.iterations[0].mutation.head_sha == "sha-patched"
    assert report.iterations[0].review is not None
    target_hash = report.iterations[0].review.finding_hashes[0]
    assert report.iterations[0].mutation.target_finding_hash == target_hash
    assert report.handoffs[-1].agent == "agent-reviewer"
    code_handoff = next(
        handoff for handoff in report.handoffs if handoff.agent == "code-editor-agent"
    )
    assert code_handoff.correlation["target_finding_hash"] == target_hash
    assert code_handoff.correlation["produced_head_sha"] == "sha-patched"
    assert report.evidence_refs["target_agent"] == "invoice-helper"
    assert report.evidence_refs["target_finding_hash"] == target_hash
    assert "smoke:run" in report.evidence_refs["smoke_test_ids"]
    assert specialists.patch_prompts
    prompt = specialists.patch_prompts[0]
    assert "run returned 500" in prompt
    assert "removes sandbox boundary" in prompt
    assert target_hash in prompt
    assert "Bump the generated agent version" in prompt
    assert "Do not make unrelated platform" in prompt


@pytest.mark.asyncio
async def test_agent_studio_loop_blocks_unresolved_critical_at_max_iterations() -> None:
    specialists = LoopSpecialists(
        evaluations=[
            [
                EvaluationResult(
                    name="smoke:run",
                    status="pass",
                    summary="run succeeded",
                )
            ]
        ],
        reviews=[
            ReviewSummary(
                status="critical",
                summary="critical remains",
                critical_count=1,
                findings=[{"severity": "critical", "message": "bad"}],
            )
        ],
    )

    report = await AgentStudioCoordinator(specialists).run(
        FakeContext(),
        name="invoice-helper",
        goal="Read invoice text and return structured JSON.",
        max_iterations=0,
    )

    assert report.ok is False
    assert report.status == "blocked"
    assert report.stop_reason == "reviewer_critical_findings"
    assert specialists.patch_prompts == []


@pytest.mark.asyncio
async def test_agent_studio_loop_stops_on_code_editor_failure() -> None:
    specialists = LoopSpecialists(
        evaluations=[
            [
                EvaluationResult(
                    name="smoke:run",
                    status="fail",
                    summary="run failed",
                )
            ]
        ],
        reviews=[ReviewSummary(status="passed", summary="clean")],
        mutations=[
            MutationRecord(
                status="failed",
                summary="code-editor crashed",
                exit_code=1,
            )
        ],
    )

    report = await AgentStudioCoordinator(specialists).run(
        FakeContext(),
        name="invoice-helper",
        goal="Read invoice text and return structured JSON.",
        max_iterations=2,
    )

    assert report.ok is False
    assert report.status == "partial"
    assert report.stop_reason == "code_editor_failed"
    assert report.residual_risks == ["code-editor crashed"]


@pytest.mark.asyncio
async def test_agent_studio_accepts_live_agent_when_optional_patch_agent_fails() -> (
    None
):
    specialists = LoopSpecialists(
        evaluations=[
            [
                EvaluationResult(
                    name="smoke:email_workflow",
                    status="warning",
                    summary="preview-only smoke skipped SMTP send",
                )
            ]
        ],
        reviews=[
            ReviewSummary(
                status="skipped",
                summary="agent-reviewer handoff failed: not registered",
            )
        ],
        mutations=[
            MutationRecord(
                status="failed",
                summary="code-editor handoff failed: resolved without a URL",
            )
        ],
    )

    report = await AgentStudioCoordinator(specialists).run(
        FakeContext(),
        name="invoice-helper",
        goal="Read invoice text and return structured JSON.",
        max_iterations=2,
        quality_bar="high",
    )

    assert report.ok is True
    assert report.status == "succeeded"
    assert report.stop_reason == "acceptance_passed_with_residual_risks"
    assert report.residual_risks == [
        "preview-only smoke skipped SMTP send",
        "agent-reviewer handoff failed: not registered",
        "code-editor handoff failed: resolved without a URL",
    ]


@pytest.mark.asyncio
async def test_agent_studio_loop_stops_before_patch_when_child_budget_exhausted() -> (
    None
):
    specialists = LoopSpecialists(
        evaluations=[
            [
                EvaluationResult(
                    name="smoke:run",
                    status="fail",
                    summary="run failed",
                )
            ]
        ],
        reviews=[ReviewSummary(status="passed", summary="clean")],
    )

    report = await AgentStudioCoordinator(specialists).run(
        FakeContext(),
        name="invoice-helper",
        goal="Read invoice text and return structured JSON.",
        max_iterations=2,
        max_child_calls=2,
    )

    assert report.ok is False
    assert report.status == "blocked"
    assert report.stop_reason == "child_call_budget_exhausted_before_patch"
    assert specialists.patch_prompts == []


@pytest.mark.asyncio
async def test_agent_studio_loop_stops_when_refreshed_live_head_is_stale() -> None:
    class StaleRefreshSpecialists(LoopSpecialists):
        async def patch_agent(
            self,
            *,
            name: str,
            prompt: str,
            ref: str,
            owner: str | None,
            session_name: str,
            target_finding_hash: str | None = None,
            target_review_id: str | None = None,
        ) -> MutationRecord:
            self.patch_prompts.append(prompt)
            return MutationRecord(
                status="pushed",
                summary="patched",
                base_sha=ref,
                head_sha="new-head",
                target_finding_hash=target_finding_hash,
                target_review_id=target_review_id,
                changed_files=["agent.py"],
                exit_code=0,
            )

    specialists = StaleRefreshSpecialists(
        evaluations=[
            [
                EvaluationResult(
                    name="smoke:run",
                    status="fail",
                    summary="run failed",
                )
            ]
        ],
        reviews=[ReviewSummary(status="passed", summary="clean")],
    )

    report = await AgentStudioCoordinator(
        specialists,
        post_patch_refresh_attempts=1,
        post_patch_refresh_delay_seconds=0,
    ).run(
        FakeContext(),
        name="invoice-helper",
        goal="Read invoice text and return structured JSON.",
        max_iterations=1,
    )

    assert report.ok is False
    assert report.status == "partial"
    assert report.stop_reason == "live_version_not_fresh_after_patch"
    assert "new-head" in report.residual_risks[0]


@pytest.mark.asyncio
async def test_agent_studio_rejects_initial_deployment_that_never_becomes_live() -> (
    None
):
    class InitialBuildingSpecialists(LoopSpecialists):
        async def load_live_state(self, *, name: str) -> dict[str, Any]:
            state = await super().load_live_state(name=name)
            state["latest_deployment"] = {
                "deploy_id": "deploy-initial",
                "head_sha": "initial-head",
                "status": "verifying",
            }
            return state

        async def refresh_agent(self, *, name: str) -> dict[str, Any]:
            self.refreshes += 1
            return await self.load_live_state(name=name)

    specialists = InitialBuildingSpecialists(
        evaluations=[
            [EvaluationResult(name="smoke:run", status="pass", summary="must not run")]
        ],
        reviews=[ReviewSummary(status="passed", summary="must not run")],
    )

    report = await AgentStudioCoordinator(
        specialists,
        post_patch_refresh_attempts=2,
        post_patch_refresh_delay_seconds=0,
    ).run(
        FakeContext(),
        name="launch-check",
        goal="Audit a public launch URL and persist a structured report.",
    )

    assert report.ok is False
    assert report.status == "partial"
    assert report.stop_reason == "initial_deployment_not_live"
    assert specialists.refreshes == 2
    assert specialists.evaluations


@pytest.mark.asyncio
async def test_agent_studio_waits_for_refreshed_live_head_after_patch() -> None:
    class EventuallyFreshSpecialists(LoopSpecialists):
        async def patch_agent(
            self,
            *,
            name: str,
            prompt: str,
            ref: str,
            owner: str | None,
            session_name: str,
            target_finding_hash: str | None = None,
            target_review_id: str | None = None,
        ) -> MutationRecord:
            self.patch_prompts.append(prompt)
            return MutationRecord(
                status="pushed",
                summary="patched",
                base_sha=ref,
                head_sha="new-head",
                target_finding_hash=target_finding_hash,
                target_review_id=target_review_id,
                changed_files=["agent.py"],
                exit_code=0,
            )

        async def refresh_agent(self, *, name: str) -> dict[str, Any]:
            self.refreshes += 1
            if self.refreshes >= 2:
                self.live_head = "new-head"
            return await self.load_live_state(name=name)

    specialists = EventuallyFreshSpecialists(
        evaluations=[
            [
                EvaluationResult(
                    name="smoke:run",
                    status="fail",
                    summary="run failed",
                )
            ],
            [
                EvaluationResult(
                    name="smoke:run",
                    status="pass",
                    summary="run passed",
                )
            ],
        ],
        reviews=[
            ReviewSummary(status="passed", summary="clean"),
            ReviewSummary(status="passed", summary="clean"),
        ],
    )
    ctx = FakeContext()

    report = await AgentStudioCoordinator(
        specialists,
        post_patch_refresh_attempts=3,
        post_patch_refresh_delay_seconds=0,
    ).run(
        ctx,
        name="invoice-helper",
        goal="Read invoice text and return structured JSON.",
        max_iterations=1,
    )

    assert report.ok is True
    assert report.status == "succeeded"
    assert report.stop_reason == "acceptance_passed"
    assert report.head_sha == "new-head"
    assert specialists.refreshes == 2
    assert any(
        "waiting for exact repaired deployment to become live head=new-head" in item
        for item in ctx.progress
    )


@pytest.mark.asyncio
async def test_agent_studio_waits_when_repaired_head_is_still_building() -> None:
    class BuildingThenLiveSpecialists(LoopSpecialists):
        async def patch_agent(
            self,
            *,
            name: str,
            prompt: str,
            ref: str,
            owner: str | None,
            session_name: str,
            target_finding_hash: str | None = None,
            target_review_id: str | None = None,
        ) -> MutationRecord:
            self.patch_prompts.append(prompt)
            self.live_head = "new-head"
            return MutationRecord(
                status="pushed",
                summary="patched",
                base_sha=ref,
                head_sha="new-head",
                deployment_id="deploy-repaired",
                target_finding_hash=target_finding_hash,
                target_review_id=target_review_id,
                changed_files=["agent.py"],
                exit_code=0,
            )

        async def refresh_agent(self, *, name: str) -> dict[str, Any]:
            self.refreshes += 1
            deployment_status = "live" if self.refreshes >= 2 else "building"
            state = await self.load_live_state(name=name)
            state["deployment_id"] = "deploy-repaired"
            state["latest_deployment"] = {
                "deploy_id": "deploy-repaired",
                "head_sha": "new-head",
                "status": deployment_status,
            }
            return state

    specialists = BuildingThenLiveSpecialists(
        evaluations=[
            [EvaluationResult(name="smoke:run", status="fail", summary="run failed")],
            [EvaluationResult(name="smoke:run", status="pass", summary="run passed")],
        ],
        reviews=[
            ReviewSummary(status="passed", summary="clean"),
            ReviewSummary(status="passed", summary="clean"),
        ],
    )

    report = await AgentStudioCoordinator(
        specialists,
        post_patch_refresh_attempts=3,
        post_patch_refresh_delay_seconds=0,
    ).run(
        FakeContext(),
        name="invoice-helper",
        goal="Read invoice text and return structured JSON.",
        max_iterations=1,
    )

    assert report.ok is True
    assert report.stop_reason == "acceptance_passed"
    assert specialists.refreshes == 2


@pytest.mark.asyncio
async def test_agent_studio_rejects_repaired_head_that_never_becomes_live() -> None:
    class BuildingSpecialists(LoopSpecialists):
        async def patch_agent(
            self,
            *,
            name: str,
            prompt: str,
            ref: str,
            owner: str | None,
            session_name: str,
            target_finding_hash: str | None = None,
            target_review_id: str | None = None,
        ) -> MutationRecord:
            self.patch_prompts.append(prompt)
            self.live_head = "new-head"
            return MutationRecord(
                status="pushed",
                summary="patched",
                base_sha=ref,
                head_sha="new-head",
                deployment_id="deploy-repaired",
                target_finding_hash=target_finding_hash,
                target_review_id=target_review_id,
                changed_files=["agent.py"],
                exit_code=0,
            )

        async def refresh_agent(self, *, name: str) -> dict[str, Any]:
            self.refreshes += 1
            state = await self.load_live_state(name=name)
            state["deployment_id"] = "deploy-repaired"
            state["latest_deployment"] = {
                "deploy_id": "deploy-repaired",
                "head_sha": "new-head",
                "status": "building",
            }
            return state

    specialists = BuildingSpecialists(
        evaluations=[
            [EvaluationResult(name="smoke:run", status="fail", summary="run failed")],
            [EvaluationResult(name="smoke:run", status="pass", summary="must not run")],
        ],
        reviews=[ReviewSummary(status="passed", summary="clean")],
    )

    report = await AgentStudioCoordinator(
        specialists,
        post_patch_refresh_attempts=2,
        post_patch_refresh_delay_seconds=0,
    ).run(
        FakeContext(),
        name="invoice-helper",
        goal="Read invoice text and return structured JSON.",
        max_iterations=1,
    )

    assert report.ok is False
    assert report.status == "partial"
    assert report.stop_reason == "live_version_not_fresh_after_patch"
    assert "status=building" in report.residual_risks[0]
    assert specialists.refreshes == 2
    assert len(specialists.evaluations) == 1


@pytest.mark.asyncio
async def test_agent_studio_specialists_review_parses_structured_findings() -> None:
    ctx = FakeCallContext(
        result={
            "ok": False,
            "agent_name": "invoice-helper",
            "ref": "abc123",
            "summary": "one critical",
            "findings": [
                {
                    "severity": "critical",
                    "category": "security",
                    "message": "token leak",
                    "file": "agent.py",
                    "line": 7,
                },
                {"severity": "warning", "category": "policy", "message": "broad scope"},
            ],
        }
    )
    specialists = AgentStudioSpecialists(ctx, platform=FakePlatform())

    review = await specialists.review_agent(
        name="invoice-helper",
        ref="abc123",
        owner="alice",
    )

    assert ctx.calls[0]["target"] == "agent-reviewer"
    assert ctx.calls[0]["skill"] == "review"
    assert ctx.calls[0]["args"] == {
        "agent_name": "invoice-helper",
        "ref": "abc123",
        "owner": "alice",
    }
    assert review.status == "critical"
    assert review.critical_count == 1
    assert review.warning_count == 1


@pytest.mark.asyncio
async def test_agent_studio_specialists_patch_enables_opt_in_and_never_pushes_on_failure() -> (
    None
):
    platform = FakePlatform()
    ctx = FakeCallContext(
        result={
            "ok": True,
            "exit_code": 0,
            "timed_out": False,
            "sync": {"head_sha": "base"},
            "changes": {"files": ["agent.py"]},
            "push": {
                "ok": True,
                "attempted": True,
                "head_sha": "head",
                "deployment_id": "deploy-head",
                "review_id": "review-head",
                "proof_id": "proof-head",
            },
        }
    )
    specialists = AgentStudioSpecialists(ctx, platform=platform)

    mutation = await specialists.patch_agent(
        name="invoice-helper",
        prompt="Fix smoke failure.",
        ref="base",
        owner="alice",
        session_name="studio-1",
        target_finding_hash="finding-123",
        target_review_id="review-base",
    )

    assert platform.enabled == ["invoice-helper"]
    assert platform.source_deploys == ["invoice-helper"]
    assert ctx.calls[0]["target"] == CODE_EDITOR_AGENT_TARGET
    assert ctx.calls[0]["skill"] == "turn"
    assert ctx.calls[0]["args"]["dry_run"] is False
    assert ctx.calls[0]["args"]["push_on_failure"] is False
    assert "target_finding_hash" not in ctx.calls[0]["args"]
    assert "target_review_id" not in ctx.calls[0]["args"]
    assert ctx.calls[0]["args"]["prompt"] == "Fix smoke failure."
    assert mutation.status == "pushed"
    assert mutation.base_sha == "base"
    assert mutation.head_sha == "head"
    assert mutation.target_finding_hash == "finding-123"
    assert mutation.target_review_id == "review-base"
    assert mutation.deployment_id == "source-deploy-head"
    assert mutation.review_id == "review-head"
    assert mutation.proof_id == "proof-head"
    assert mutation.changed_files == ["agent.py"]


def test_code_editor_uses_managed_branch_for_deployment_sha() -> None:
    assert _editable_ref("a" * 40) == "main"
    assert _editable_ref("main") == "main"
    assert _editable_ref("release/v1") == "release/v1"


def test_smoke_sampler_uses_positive_values_inside_zero_based_numeric_bounds() -> None:
    args = _sample_args_from_schema(
        {
            "type": "object",
            "required": ["weights"],
            "properties": {
                "weights": {
                    "type": "object",
                    "required": ["price", "quality", "risk"],
                    "properties": {
                        "price": {"type": "number", "minimum": 0, "maximum": 10},
                        "quality": {"type": "integer", "minimum": 0, "maximum": 10},
                        "risk": {"type": "number", "minimum": 0, "maximum": 0.5},
                    },
                }
            },
        }
    )

    assert args == {"weights": {"price": 1.0, "quality": 1, "risk": 0.25}}


def test_agent_studio_report_redacts_secret_and_raw_log_fields() -> None:
    report = AgentStudioReport(
        ok=False,
        status="partial",
        run_id="agent-studio-redact",
        agent_name="invoice-helper",
        started_at="2026-06-02T00:00:00+00:00",
        residual_risks=["bearer sk-live-secret1234567890"],
        handoffs=[
            HandoffRecord(
                agent="code-editor-agent",
                skill="turn",
                status="failed",
                started_at="2026-06-02T00:00:00+00:00",
                result_summary={
                    "stdout": "x" * 5000,
                    "cp_jwt": "eyJhbGciOiJIUzI1NiJ9.abcdefghijk12345.lmnopqrstuv98765",
                    "summary": "safe",
                },
            )
        ],
    )

    encoded = _redact_report(report).model_dump_json()

    assert "sk-live-secret1234567890" not in encoded
    assert "eyJhbGciOiJIUzI1NiJ9" not in encoded
    assert "x" * 100 not in encoded
    assert "[redacted]" in encoded


@pytest.mark.asyncio
async def test_platform_helper_requires_cp_jwt() -> None:
    helper = PlatformHelperClient.from_context(
        FakeContext(cp_url="https://api.example.test")
    )

    result = await helper.get_agent("invoice-helper")

    assert result == {
        "ok": False,
        "status_code": 401,
        "error": "control-plane bearer required",
    }


@pytest.mark.asyncio
async def test_platform_helper_fetches_safe_agent_state_and_refresh_warning() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        assert request.headers["authorization"] == "Bearer cp-token"
        return httpx.Response(
            200,
            json={
                "name": "invoice-helper",
                "status": "running",
                "public": False,
                "url": "https://invoice-helper.a2acloud.io",
                "version": "0.1.0",
                "repo_url": "https://gitea.example/user/invoice-helper",
                "card": {
                    "name": "invoice-helper",
                    "version": "0.1.0",
                    "skills": [],
                    "capabilities": {"meta_agent": {"jwt": "eyJsecret"}},
                },
                "latest_deployment": {
                    "deploy_id": "deploy-123",
                    "agent_name": "invoice-helper",
                    "status": "succeeded",
                    "head_sha": "abc123",
                    "verification": {"token": "sk-secret"},
                },
                "code_editor": {
                    "target_agent_name": "invoice-helper",
                    "enabled": False,
                    "status": "disabled",
                    "shared_agent_name": "code-editor-agent",
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    helper = PlatformHelperClient.from_context(
        FakeContext(cp_url="https://api.example.test", cp_jwt="cp-token"),
        client_factory=lambda: client,
    )

    result = await helper.refresh_agent("invoice-helper")

    assert seen == ["https://api.example.test/v1/agents/invoice-helper?refresh=true"]
    assert result["ok"] is True
    assert result["head_sha"] == "abc123"
    assert result["deployment_id"] == "deploy-123"
    assert result["owner"] == "user"
    assert result["warnings"] == ["live_card_missing_or_stale"]
    assert result["card"]["capabilities"]["meta_agent"]["jwt"] == "[redacted]"
    assert result["latest_deployment"]["verification"]["token"] == "[redacted]"


@pytest.mark.asyncio
async def test_platform_helper_refresh_loads_latest_deployment_when_agent_omits_it() -> (
    None
):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.endswith("/deployments"):
            return httpx.Response(
                200,
                json=[
                    {
                        "deploy_id": "deploy-456",
                        "agent_name": "invoice-helper",
                        "status": "live",
                        "head_sha": "source-head-456",
                        "source_repo_url": "https://gitea.example/user/invoice-helper",
                    }
                ],
            )
        return httpx.Response(
            200,
            json={
                "name": "invoice-helper",
                "status": "running",
                "public": False,
                "url": "https://invoice-helper.a2acloud.io",
                "version": "0.1.0",
                "card": {
                    "name": "invoice-helper",
                    "version": "0.1.0",
                    "skills": [{"name": "run"}],
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    helper = PlatformHelperClient(
        cp_url="https://api.example.test",
        cp_jwt="cp-token",
        client_factory=lambda: client,
    )

    result = await helper.refresh_agent("invoice-helper")

    assert seen == [
        "https://api.example.test/v1/agents/invoice-helper?refresh=true",
        "https://api.example.test/v1/agents/invoice-helper/deployments",
    ]
    assert result["head_sha"] == "source-head-456"
    assert result["deployment_id"] == "deploy-456"
    assert result["repo_url"] == "https://gitea.example/user/invoice-helper"


@pytest.mark.asyncio
async def test_platform_helper_preserves_live_workspace_access() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == (
            "https://workspace-helper.a2acloud.io/.well-known/agent-card"
        )
        return httpx.Response(
            200,
            json={
                "name": "workspace-helper",
                "version": "0.1.0",
                "skills": [{"name": "run", "input_schema": {"type": "object"}}],
                "workspace_access": {
                    "enabled": True,
                    "allowed_modes": ["read_only", "read_write_overlay"],
                    "deny_patterns": ["**/.env"],
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    helper = PlatformHelperClient(
        cp_url="https://api.example.test",
        cp_jwt="cp-token",
        client_factory=lambda: client,
    )

    result = await helper.fetch_live_card("https://workspace-helper.a2acloud.io")

    assert result["ok"] is True
    assert result["card"]["workspace_access"] == {
        "enabled": True,
        "allowed_modes": ["read_only", "read_write_overlay"],
        "deny_patterns": ["**/.env"],
    }


@pytest.mark.asyncio
async def test_platform_helper_probes_frontend_mcp_and_receipts_without_tokens() -> (
    None
):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url}")
        assert request.headers["authorization"] == "Bearer cp-token"
        path = request.url.path
        if path == "/app/":
            return httpx.Response(200, text="<main>Quote Judge</main>")
        if path == "/app/config.json":
            return httpx.Response(
                200,
                json={
                    "endpoints": {"invoke": "https://quote-judge.a2acloud.io/invoke"},
                    "auth": {"mode": "platform"},
                    "ui": {"type": "static-spa"},
                },
            )
        if path == "/app/a2a-client.js":
            return httpx.Response(
                200,
                text=(
                    "export function createA2AClient() {}\n"
                    "export function unwrapInvokeResponse(payload) {}"
                ),
            )
        if path == "/mcp":
            body = json.loads(request.content)
            if body["method"] == "tools/list":
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {
                            "tools": [
                                {
                                    "name": "get_comparison",
                                    "inputSchema": {"type": "object"},
                                }
                            ]
                        },
                    },
                )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream; charset=utf-8"},
                text=(
                    "event: message\n"
                    "data: "
                    + json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": body["id"],
                            "result": {"structuredContent": {"comparison_id": "cmp-1"}},
                        }
                    )
                    + "\n\n"
                    + "event: message\n"
                    + "data: "
                    + json.dumps(
                        {
                            "type": "a2a.evidence",
                            "evidence": {
                                "receipt": {"signed_token": "must-not-leak"},
                            },
                        }
                    )
                    + "\n\n"
                    + "data: [DONE]\n\n"
                ),
            )
        if path == "/v1/agents/quote-judge/receipts":
            return httpx.Response(
                200,
                json=[
                    {
                        "receipt_id": "receipt-1",
                        "skill_name": "get_comparison",
                        "status": "ok",
                        "signed_token": "must-not-leak",
                    }
                ],
            )
        raise AssertionError(path)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    helper = PlatformHelperClient(
        cp_url="https://api.example.test",
        cp_jwt="cp-token",
        client_factory=lambda: client,
    )

    frontend = await helper.probe_frontend("https://quote-judge.a2acloud.io")
    tools = await helper.list_mcp_tools("https://quote-judge.a2acloud.io")
    called = await helper.call_mcp_tool(
        "https://quote-judge.a2acloud.io",
        tool="get_comparison",
        arguments={"comparison_id": "cmp-1"},
    )
    receipts = await helper.list_receipts("quote-judge")

    assert frontend["ok"] is True
    assert frontend["auth_mode"] == "platform"
    assert frontend["invoke_result_contract"] == "unwrapped"
    assert tools["tools"] == [
        {"name": "get_comparison", "input_schema": {"type": "object"}}
    ]
    assert called["ok"] is True
    assert called["result"] == {"comparison_id": "cmp-1"}
    assert receipts == {
        "ok": True,
        "receipts": [
            {"receipt_id": "receipt-1", "skill_name": "get_comparison", "status": "ok"}
        ],
    }
    assert "must-not-leak" not in repr(receipts)
    assert len(seen) == 6


@pytest.mark.asyncio
async def test_platform_helper_publishes_and_returns_distribution_package() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PATCH"
        assert request.url.path == "/v1/agents/mine/report-maker/visibility"
        assert json.loads(request.content) == {"public": True}
        return httpx.Response(
            200,
            json={
                "name": "report-maker",
                "status": "ready",
                "public": True,
                "url": "https://report-maker.a2acloud.io",
                "repo_url": "https://git.a2acloud.io/alice/report-maker",
                "card": {"name": "report-maker", "skills": [{"name": "generate"}]},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    helper = PlatformHelperClient(
        cp_url="https://api.example.test",
        cp_jwt="cp-token",
        client_factory=lambda: client,
    )

    published = await helper.publish_agent("report-maker")
    package = await helper.verify_distribution(
        name="report-maker",
        url="https://report-maker.a2acloud.io",
        repo_url="https://git.a2acloud.io/alice/report-maker",
        spec=DistributionSpec(
            public_page=False,
            live_app=False,
            agent_card=False,
            mcp=False,
            seo=False,
            source=False,
            shareable_demo=False,
        ),
    )

    assert published["ok"] is True
    assert published["public"] is True
    assert package["ok"] is True
    assert package["cli"] == "a2a call report-maker <skill> --json '{}'"
    assert package["checks"]["cli"]["ok"] is True


@pytest.mark.asyncio
async def test_platform_helper_frontend_probe_blocks_internal_markers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/config.json":
            return httpx.Response(
                200,
                json={
                    "endpoints": {
                        "invoke": "http://agent.agents.svc.cluster.local/invoke"
                    },
                    "auth": {"mode": "platform"},
                },
            )
        return httpx.Response(200, text="safe")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    helper = PlatformHelperClient(
        cp_url="https://api.example.test",
        cp_jwt="cp-token",
        client_factory=lambda: client,
    )

    result = await helper.probe_frontend("https://leaky-app.a2acloud.io")

    assert result["ok"] is False
    assert result["markers"] == ["internal-service-host"]


@pytest.mark.asyncio
async def test_platform_helper_frontend_probe_requires_unwrapped_result_contract() -> (
    None
):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/config.json":
            return httpx.Response(
                200,
                json={
                    "endpoints": {"invoke": "https://broken-app.a2acloud.io/invoke"},
                    "auth": {"mode": "platform"},
                },
            )
        return httpx.Response(200, text="safe but old browser client")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    helper = PlatformHelperClient(
        cp_url="https://api.example.test",
        cp_jwt="cp-token",
        client_factory=lambda: client,
    )

    result = await helper.probe_frontend("https://broken-app.a2acloud.io")

    assert result["ok"] is False
    assert result["status_code"] == 422
    assert "does not unwrap" in result["error"]


@pytest.mark.asyncio
async def test_platform_helper_enable_code_editor_summarizes_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert (
            str(request.url)
            == "https://api.example.test/v1/agents/invoice-helper/code-editor"
        )
        return httpx.Response(
            200,
            json={
                "name": "invoice-helper",
                "status": "running",
                "public": False,
                "url": "https://invoice-helper.a2acloud.io",
                "version": "0.1.0",
                "card": {
                    "name": "invoice-helper",
                    "version": "0.1.0",
                    "skills": [{"name": "run", "input_schema": {"type": "object"}}],
                },
                "latest_deployment": {"deploy_id": "deploy-1", "head_sha": "sha1"},
                "code_editor": {
                    "target_agent_name": "invoice-helper",
                    "enabled": True,
                    "status": "enabled",
                    "shared_agent_name": "code-editor-agent",
                    "workspace_key": "agents/invoice-helper",
                    "last_error": "bearer should not leak",
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    helper = PlatformHelperClient(
        cp_url="https://api.example.test",
        cp_jwt="cp-token",
        client_factory=lambda: client,
    )

    result = await helper.enable_code_editor("invoice-helper")

    assert result["ok"] is True
    assert result["card"]["skills"][0]["name"] == "run"
    assert result["code_editor"]["enabled"] is True
    assert result["code_editor"]["last_error"] == "[redacted]"


@pytest.mark.asyncio
async def test_platform_helper_returns_redacted_non_owned_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="not owned; bearer cp-token")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    helper = PlatformHelperClient(
        cp_url="https://api.example.test",
        cp_jwt="cp-token",
        client_factory=lambda: client,
    )

    result = await helper.get_code_editor("someone-elses-agent")

    assert result == {"ok": False, "status_code": 403, "error": "[redacted]"}


@pytest.mark.asyncio
async def test_platform_helper_refresh_failure_is_structured() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="card fetch failed")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    helper = PlatformHelperClient(
        cp_url="https://api.example.test",
        cp_jwt="cp-token",
        client_factory=lambda: client,
    )

    result = await helper.refresh_agent("invoice-helper")

    assert result == {"ok": False, "status_code": 502, "error": "card fetch failed"}


class UpgradeSpecialists(MockSpecialists):
    def __init__(self) -> None:
        self.patch_calls: list[dict[str, Any]] = []

    async def load_live_state(self, *, name: str) -> dict[str, Any]:
        return {
            "ok": True,
            "name": name,
            "owner": "alice",
            "head_sha": "base-sha",
            "latest_deployment": {
                "head_sha": "base-sha",
                "deploy_id": "deploy-base",
                "status": "live",
            },
        }

    async def patch_agent(self, **kwargs: Any) -> MutationRecord:
        self.patch_calls.append(dict(kwargs))
        return MutationRecord(
            status="pushed",
            summary="upgrade pushed",
            base_sha="base-sha",
            head_sha="next-sha",
            deployment_id="deploy-next",
            changed_files=["agent.py", "tests/test_agent.py"],
        )

    async def refresh_agent(self, *, name: str) -> dict[str, Any]:
        return {
            "ok": True,
            "name": name,
            "head_sha": "next-sha",
            "latest_deployment": {
                "head_sha": "next-sha",
                "deploy_id": "deploy-next",
                "status": "live",
            },
        }


@pytest.mark.asyncio
async def test_approved_upgrade_patches_exact_fresh_source_and_verifies() -> None:
    specialists = UpgradeSpecialists()
    ctx = FakeContext()
    report = await AgentStudioCoordinator(
        specialists,
        post_patch_refresh_attempts=1,
        post_patch_refresh_delay_seconds=0,
    ).upgrade(
        ctx,
        name="invoice-helper",
        idea="Add a bounded retry for transient invoice API failures.",
        proposal_id="aup_123",
        expected_head_sha="base-sha",
        evidence={"finding": {"file": "agent.py", "line": 20}},
    )

    assert report.ok is True
    assert report.status == "succeeded"
    assert report.base_sha == "base-sha"
    assert report.head_sha == "next-sha"
    assert report.deployment_id == "deploy-next"
    assert report.changed_files == ["agent.py", "tests/test_agent.py"]
    assert len(specialists.patch_calls) == 1
    assert "owner-approved" in specialists.patch_calls[0]["prompt"]
    assert specialists.patch_calls[0]["ref"] == "base-sha"


@pytest.mark.asyncio
async def test_approved_upgrade_refuses_stale_email_proposal() -> None:
    specialists = UpgradeSpecialists()
    report = await AgentStudioCoordinator(specialists).upgrade(
        FakeContext(),
        name="invoice-helper",
        idea="Add a bounded retry for transient invoice API failures.",
        expected_head_sha="older-sha",
    )

    assert report.ok is False
    assert report.status == "blocked"
    assert report.stop_reason == "source_changed_since_proposal"
    assert specialists.patch_calls == []
