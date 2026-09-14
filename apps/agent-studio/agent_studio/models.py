from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

RunStatus = Literal["succeeded", "partial", "failed", "blocked"]
EvaluationStatus = Literal["pass", "warning", "fail"]
ReviewStatus = Literal["passed", "warnings", "critical", "skipped"]
MutationStatus = Literal["pushed", "dry_run", "skipped", "failed"]
StartupRecipe = Literal[
    "auto",
    "csv_tool",
    "document_generator",
    "email_assistant",
    "scheduled_monitor",
    "calculator",
    "approval_workflow",
    "dashboard",
    "custom",
]
LaunchCapabilityName = Literal[
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
]


class AcceptanceCall(BaseModel):
    """One deterministic production call used to prove an app workflow."""

    purpose: Literal["success", "reload", "failure", "mcp"]
    tool: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)
    expect_error: bool = False
    expected: dict[str, Any] = Field(default_factory=dict)


class OutputExpectation(BaseModel):
    """A user-visible result that production acceptance must prove."""

    kind: Literal["json", "text", "table", "preview", "download", "artifact"]
    name: str = Field(default="", max_length=160)
    media_type: str = Field(default="", max_length=160)
    filename: str = Field(default="", max_length=255)
    minimum_count: int = Field(default=1, ge=1, le=20)


class BrowserStep(BaseModel):
    """One bounded user action/assertion in the packed frontend."""

    action: Literal[
        "fill",
        "upload",
        "click",
        "assert_visible",
        "assert_text",
        "assert_download",
    ]
    selector: str = Field(min_length=1, max_length=300)
    value: str = Field(default="", max_length=4_000)
    filename: str = Field(default="", max_length=255)


class BrowserJourney(BaseModel):
    """Browser-level proof run against the deployed ``/app`` frontend."""

    name: str = Field(default="primary-workflow", min_length=1, max_length=120)
    path: str = Field(default="/app/", pattern=r"^/[^\s]*$")
    steps: list[BrowserStep] = Field(default_factory=list, max_length=30)
    screenshot: bool = True


class DistributionSpec(BaseModel):
    """Surfaces a successful public launch must expose together."""

    public_page: bool = True
    live_app: bool = True
    agent_card: bool = True
    mcp: bool = True
    cli: bool = True
    seo: bool = True
    source: bool = True
    shareable_demo: bool = True


class CapabilityDecision(BaseModel):
    capability: LaunchCapabilityName
    required: bool
    reason: str = Field(min_length=1, max_length=300)


class LaunchPlan(BaseModel):
    """Deterministic capability/recipe plan created before source generation."""

    recipe: StartupRecipe = "custom"
    capabilities: list[CapabilityDecision] = Field(default_factory=list)
    account_trial_calls: int = Field(default=0, ge=0, le=100)
    after_trial: Literal["byok"] = "byok"
    distribution: DistributionSpec = Field(default_factory=DistributionSpec)


class AppSpec(BaseModel):
    """Structured product requirements preserved from Studio to Builder."""

    profile: Literal["headless", "full_stack"] = "headless"
    product_ui: bool = False
    auth: Literal["none", "platform"] = "none"
    uploads: bool = False
    persistence: bool = False
    database_scope: Literal["user", "org"] = "user"
    integrations: list[str] = Field(default_factory=list)
    primary_workflow: str = ""
    success_fixture: str = ""
    failure_fixture: str = ""
    reload_check: str = ""
    requires_mcp: bool = False
    requires_receipt: bool = False
    acceptance_calls: list[AcceptanceCall] = Field(default_factory=list)
    recipe: StartupRecipe = "auto"
    capabilities: list[LaunchCapabilityName] = Field(default_factory=list)
    output_expectations: list[OutputExpectation] = Field(default_factory=list)
    browser_journeys: list[BrowserJourney] = Field(default_factory=list)
    account_trial_calls: int = Field(default=0, ge=0, le=100)
    distribution: DistributionSpec = Field(default_factory=DistributionSpec)

    def prompt_block(self) -> str:
        return json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True)


class BuildBrief(BaseModel):
    agent_name: str
    organization_slug: str = ""
    goal: str
    target_user: str
    quality_bar: Literal["standard", "high"] = "standard"
    max_spend_cents: int | None = Field(default=None, ge=100, le=3000)
    desired_skills: list[dict[str, Any]] = Field(default_factory=list)
    acceptance_checks: list[str] = Field(default_factory=list)
    runtime_notes: list[str] = Field(default_factory=list)
    app_spec: AppSpec = Field(default_factory=AppSpec)
    launch_plan: LaunchPlan = Field(default_factory=LaunchPlan)

    def to_prompt(self) -> str:
        checks = "\n".join(f"- {item}" for item in self.acceptance_checks)
        notes = "\n".join(f"- {item}" for item in self.runtime_notes)
        return (
            f"Build an A2A agent named `{self.agent_name}`.\n\n"
            f"Goal:\n{self.goal}\n\n"
            f"Target user:\n{self.target_user}\n\n"
            f"Target organization:\n{self.organization_slug or 'personal workspace'}\n\n"
            f"Quality bar: {self.quality_bar}\n\n"
            f"Maximum build spend: {self.max_spend_cents or 'platform default'} cents\n\n"
            f"Acceptance checks:\n{checks}\n\n"
            f"Application contract (authoritative JSON):\n"
            f"```json\n{self.app_spec.prompt_block()}\n```\n\n"
            f"Capability and launch plan (authoritative JSON):\n"
            f"```json\n{json.dumps(self.launch_plan.model_dump(mode='json'), indent=2, sort_keys=True)}\n```\n\n"
            f"Runtime notes:\n{notes}\n"
        )


class HandoffRecord(BaseModel):
    agent: str
    skill: str
    args_summary: dict[str, Any] = Field(default_factory=dict)
    status: str
    started_at: str
    completed_at: str | None = None
    result_summary: dict[str, Any] = Field(default_factory=dict)
    correlation: dict[str, Any] = Field(default_factory=dict)


class EvaluationResult(BaseModel):
    name: str
    status: EvaluationStatus
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)


class ReviewSummary(BaseModel):
    status: ReviewStatus
    summary: str = ""
    review_id: str | None = None
    critical_count: int = 0
    warning_count: int = 0
    findings: list[dict[str, Any]] = Field(default_factory=list)
    finding_hashes: list[str] = Field(default_factory=list)


class MutationRecord(BaseModel):
    status: MutationStatus
    summary: str
    base_sha: str | None = None
    head_sha: str | None = None
    target_finding_hash: str | None = None
    target_review_id: str | None = None
    deployment_id: str | None = None
    review_id: str | None = None
    proof_id: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    smoke_test_ids: list[str] = Field(default_factory=list)
    exit_code: int | None = None
    timed_out: bool = False


class IterationRecord(BaseModel):
    index: int
    status: str
    evaluations: list[EvaluationResult] = Field(default_factory=list)
    review: ReviewSummary | None = None
    mutation: MutationRecord | None = None
    notes: list[str] = Field(default_factory=list)


class AgentStudioReport(BaseModel):
    ok: bool
    status: RunStatus
    run_id: str
    agent_name: str
    public: bool = False
    started_at: str
    completed_at: str | None = None
    stop_reason: str | None = None
    version: str | None = None
    agent_url: str | None = None
    workspace_dir: str | None = None
    head_sha: str | None = None
    deployment_id: str | None = None
    live_card: dict[str, Any] | None = None
    build_brief: BuildBrief | None = None
    handoffs: list[HandoffRecord] = Field(default_factory=list)
    iterations: list[IterationRecord] = Field(default_factory=list)
    tests: list[EvaluationResult] = Field(default_factory=list)
    review: ReviewSummary = Field(
        default_factory=lambda: ReviewSummary(status="skipped")
    )
    residual_risks: list[str] = Field(default_factory=list)
    budgets: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: dict[str, Any] = Field(default_factory=dict)
    distribution: dict[str, Any] = Field(default_factory=dict)
    ledger_path: str | None = None
    publish_next_step: str = "already-private"


class AgentUpgradeReport(BaseModel):
    """Result of one owner-approved, bounded upgrade to an existing agent."""

    ok: bool
    status: Literal["succeeded", "failed", "blocked", "partial"]
    run_id: str
    proposal_id: str | None = None
    agent_name: str
    idea: str
    started_at: str
    completed_at: str | None = None
    stop_reason: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    deployment_id: str | None = None
    review_before: ReviewSummary = Field(
        default_factory=lambda: ReviewSummary(status="skipped")
    )
    review_after: ReviewSummary = Field(
        default_factory=lambda: ReviewSummary(status="skipped")
    )
    mutation: MutationRecord | None = None
    changed_files: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
