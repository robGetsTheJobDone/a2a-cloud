"""Drive the deployed ``agent-studio`` coordinator from the control plane.

``POST /v1/agents/studio/runs`` creates an :class:`AgentStudioRun` and a durable
work-ledger job. :mod:`control_plane.agent_studio_worker` leases that job and
invokes the agent-studio ``create_agent`` skill over its ``/invoke`` SSE channel
(the same channel handoffs use), mirrors the coordinator's progress into
:class:`AgentStudioRunEvent` rows, and records the final report so the dashboard
can stream a live run view.

Mirrors the async-review pattern in :mod:`control_plane.agent_review`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import SessionLocal
from .models import Agent, AgentDeployment, AgentStudioRun, AgentStudioRunEvent, WorkJob
from .work_ledger import create_job
from main_agent.config import load_settings as load_runtime_settings
from main_agent.grants import GrantClaims, mint_grant_token

logger = logging.getLogger(__name__)

STUDIO_AGENT = "agent-studio"
STUDIO_SKILL = "create_agent"
STUDIO_UPGRADE_SKILL = "upgrade_agent"
# create_agent's tool timeout is 3600s; give the stream headroom over that.
STUDIO_TIMEOUT_S = float(os.environ.get("A2A_CP_STUDIO_TIMEOUT_S", "3600"))
STUDIO_GRANT_TTL_S = int(os.environ.get("A2A_CP_STUDIO_GRANT_TTL_S", "3600"))

REVIEW_TO_ITERATIONS = {"light": 1, "standard": 2, "strict": 3}
MAX_STUDIO_SPEND_CENTS = 3_000
STUDIO_JOB_KIND = "agent.studio.build"
STUDIO_JOB_QUEUE = "agent_studio"
STUDIO_JOB_WORKER = "agent_studio_worker"

_STUDIO_RECIPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("csv_tool", re.compile(r"\b(csv|spreadsheet|rows?|columns?)\b", re.I)),
    (
        "document_generator",
        re.compile(r"\b(document|report|proposal|contract|pdf|docx)\b", re.I),
    ),
    (
        "email_assistant",
        re.compile(r"\b(emails?|inbox|mailbox|reply|support ticket)\b", re.I),
    ),
    (
        "scheduled_monitor",
        re.compile(r"\b(monitor|watch|alert|schedule|recurring|daily|weekly)\b", re.I),
    ),
    (
        "calculator",
        re.compile(r"\b(calculate|calculator|estimate|quote|score|convert)\b", re.I),
    ),
    (
        "approval_workflow",
        re.compile(r"\b(approve|approval|sign[ -]?off|refund|payout|charge)\b", re.I),
    ),
    ("dashboard", re.compile(r"\b(dashboard|tracker|portal|analytics|chart)\b", re.I)),
)


def _studio_url(skill_name: str = STUDIO_SKILL) -> str:
    host = os.environ.get(
        "A2A_CP_STUDIO_HOST", f"{STUDIO_AGENT}.agents.svc.cluster.local"
    )
    return f"http://{host}/invoke/{skill_name}"


def brief_to_skill_args(brief: dict[str, Any]) -> dict[str, Any]:
    """Map a dashboard build brief to the ``create_agent`` skill arguments.

    Preserve product requirements as a structured application contract instead
    of flattening them into a generic chat-agent request. Review depth drives
    both iteration count and the quality bar.
    """
    review = str(brief.get("review") or "standard")
    goal_parts = [str(brief.get("goal") or "").strip()]
    inputs = [str(i) for i in brief.get("inputs") or [] if str(i).strip()]
    integrations = [str(i) for i in brief.get("integrations") or [] if str(i).strip()]
    if inputs:
        goal_parts.append(f"Inputs each run receives: {', '.join(inputs)}.")
    if integrations:
        goal_parts.append(f"Required integrations: {', '.join(integrations)}.")
    product_ui = bool(brief.get("frontend"))
    combined = " ".join([str(brief.get("goal") or ""), *inputs]).lower()
    uploads = bool(
        re.search(
            r"\b(upload|file|csv|pdf|document|contract|quote|invoice)\b", combined
        )
    )
    # Legacy dashboard/public briefs do not carry deterministic create/read/
    # failure fixtures yet. Only enable the strict reload gate when an explicit
    # structured caller supplies it; otherwise the full-stack scaffold still
    # installs managed storage, but Studio does not invent a passing reload.
    persistence = bool(brief.get("persistence"))
    requested_recipe = str(brief.get("recipe") or "auto")
    recipe = (
        requested_recipe
        if requested_recipe != "auto"
        else next(
            (name for name, pattern in _STUDIO_RECIPES if pattern.search(combined)),
            "custom",
        )
    )
    output_expectations = _recipe_output_expectations(
        recipe,
        agent_name=str(brief.get("name") or "agent"),
    )
    browser_journeys = []
    if product_ui:
        steps = [
            {"action": "assert_visible", "selector": "[data-testid='agent-app']"},
            {"action": "assert_visible", "selector": "[data-testid='agent-submit']"},
            {"action": "click", "selector": "[data-testid='agent-submit']"},
            {
                "action": "assert_visible",
                "selector": "[data-testid='agent-result'][data-state='success']",
            },
        ]
        download = next(
            (item for item in output_expectations if item.get("kind") == "download"),
            None,
        )
        if download:
            steps.append(
                {
                    "action": "assert_download",
                    "selector": "[data-testid='agent-download']",
                    "filename": download.get("filename") or "",
                }
            )
        browser_journeys.append(
            {
                "name": "primary-workflow",
                "path": "/app/",
                "steps": steps,
                "screenshot": True,
            }
        )
    public = bool(brief.get("public"))
    account_trial_calls = max(
        0,
        min(int(brief.get("account_trial_calls") or (3 if public else 0)), 100),
    )
    app_spec = {
        "profile": "full_stack" if product_ui else "headless",
        "product_ui": product_ui,
        "auth": "platform" if product_ui else "none",
        "uploads": uploads,
        "persistence": persistence,
        "database_scope": "user",
        "integrations": integrations,
        "primary_workflow": str(brief.get("goal") or "").strip(),
        "requires_mcp": True,
        "requires_receipt": True,
        "recipe": recipe,
        "output_expectations": output_expectations,
        "browser_journeys": browser_journeys,
        "account_trial_calls": account_trial_calls,
    }
    return {
        "name": str(brief.get("name") or "").strip(),
        "organization_slug": str(brief.get("organization_slug") or "").strip(),
        "goal": " ".join(p for p in goal_parts if p),
        "public": bool(brief.get("public")),
        "max_iterations": REVIEW_TO_ITERATIONS.get(review, 2),
        "quality_bar": "high" if review == "strict" else "standard",
        "exercise_code_editor": False,
        "max_spend_cents": max(
            100,
            min(int(brief.get("budget_cents") or 500), MAX_STUDIO_SPEND_CENTS),
        ),
        "app_spec_json": json.dumps(app_spec, separators=(",", ":"), sort_keys=True),
    }


def _recipe_output_expectations(
    recipe: str, *, agent_name: str
) -> list[dict[str, Any]]:
    safe_name = re.sub(r"[^a-z0-9-]+", "-", agent_name.lower()).strip("-") or "agent"
    if recipe == "csv_tool":
        return [
            {"kind": "table", "name": "preview"},
            {
                "kind": "download",
                "name": "corrected-csv",
                "filename": f"{safe_name}-output.csv",
                "media_type": "text/csv",
            },
            {
                "kind": "artifact",
                "name": "corrected-csv",
                "filename": f"{safe_name}-output.csv",
                "media_type": "text/csv",
            },
        ]
    if recipe == "document_generator":
        return [
            {
                "kind": "preview",
                "name": "document-preview",
                "media_type": "text/markdown",
            },
            {
                "kind": "download",
                "name": "document",
                "filename": f"{safe_name}-output.md",
                "media_type": "text/markdown",
            },
            {
                "kind": "artifact",
                "name": "document",
                "filename": f"{safe_name}-output.md",
                "media_type": "text/markdown",
            },
        ]
    if recipe == "dashboard":
        return [{"kind": "table", "name": "dashboard-data"}]
    return [{"kind": "json", "name": "structured-result"}]


def _studio_grant(
    *,
    user_id: int,
    run_id: str,
    agent_name: str,
    budget_cents: int,
    skill_name: str = STUDIO_SKILL,
) -> tuple[str, dict[str, Any]]:
    """Mint one workspace + LLM grant capped at the user's selected budget."""
    runtime = load_runtime_settings()
    models = tuple(runtime.platform_llm_models) or (
        (runtime.litellm_model,) if runtime.litellm_model else ()
    )
    if not models:
        raise RuntimeError("Agent Studio has no platform LLM model configured")
    prefix = f"agents/{agent_name}/"
    token, payload = mint_grant_token(
        GrantClaims(
            issuer=f"agent-studio:user-{user_id}",
            audience=STUDIO_AGENT,
            bucket=f"user-{user_id}-files",
            mode="read_write_overlay",
            allow_patterns=(f"{prefix}**",),
            # The coordinator delegates the same bounded agent root to the
            # first-party builder. Keep that delegation within the original
            # grant so an anonymous build never pauses for human scope approval.
            outputs_prefix=prefix,
            write_prefixes=(prefix, f"{prefix}.agent-studio/"),
            llm_models=models,
            llm_max_budget_usd=budget_cents / 100,
            llm_rpm_limit=runtime.platform_llm_rpm_limit,
            llm_tpm_limit=runtime.platform_llm_tpm_limit,
            # Agent Studio delegates this same repository scope to the
            # first-party builder. Source grants are independently bounded by
            # the SDK, so the parent must explicitly authorize that child
            # source grant as well as its workspace paths.
            source_grants=({"agent": agent_name, "scope": "write"},),
            ttl_seconds=STUDIO_GRANT_TTL_S,
        )
    )
    creds = {
        "base_url": str(runtime.litellm_url).rstrip("/") + "/v1",
        "api_key": token,
        "model": models[0],
        "temperature_mode": "omit",
        "extra_body": {},
        "metadata": {
            "a2a_user_id": user_id,
            "a2a_grant_id": payload.get("grant_id"),
            "a2a_agent_name": STUDIO_AGENT,
            "a2a_skill_name": skill_name,
            "a2a_llm_source": "agent-studio",
            "agent_studio_run_id": run_id,
        },
    }
    return token, creds


async def enqueue_studio_build(
    session: AsyncSession,
    *,
    run_id: str,
    agent_name: str,
    user_id: int,
    brief: dict[str, Any],
    cp_url: str | None = None,
    skill_name: str = STUDIO_SKILL,
    skill_args: dict[str, Any] | None = None,
) -> WorkJob:
    """Persist a Studio build for a separately deployed, leased worker."""
    resolved_cp_url = cp_url or os.environ.get("A2A_CP_URL", settings.public_cp_url)
    payload: dict[str, Any] = {
        "run_id": run_id,
        "agent_name": agent_name,
        "builder_user_id": user_id,
        "cp_url": resolved_cp_url,
        "skill_name": skill_name,
        "skill_args": (
            skill_args if skill_args is not None else brief_to_skill_args(brief)
        ),
        "budget_cents": int(brief.get("budget_cents") or 500),
    }
    return await create_job(
        session,
        user_id=user_id,
        kind=STUDIO_JOB_KIND,
        payload=payload,
        title=f"Build Studio agent {agent_name}",
        queue=STUDIO_JOB_QUEUE,
        source_type="agent_studio_run",
        source_id=run_id,
        subject_type="agent_studio_run",
        subject_id=run_id,
        worker_type="agent_studio",
        worker_name=STUDIO_JOB_WORKER,
        idempotency_key=f"agent-studio-build:{run_id}",
        idempotency_scope="agent_studio_builds",
        max_attempts=2,
        commit=False,
    )


def enqueue_studio_deployment_monitor(
    app: Any,
    *,
    run_id: str,
    agent_name: str,
    deploy_id: str,
) -> None:
    """Mirror a compose/fork deployment into the Studio run lifecycle."""
    task = asyncio.create_task(
        _monitor_studio_deployment(
            run_id=run_id,
            agent_name=agent_name,
            deploy_id=deploy_id,
        ),
        name=f"agent-studio-deploy-{agent_name}-{run_id[:8]}",
    )
    tasks: set[asyncio.Task[Any]] | None = getattr(
        app.state, "agent_studio_tasks", None
    )
    if tasks is None:
        tasks = set()
        app.state.agent_studio_tasks = tasks
    tasks.add(task)
    task.add_done_callback(tasks.discard)


async def _monitor_studio_deployment(
    *,
    run_id: str,
    agent_name: str,
    deploy_id: str,
) -> None:
    deadline = asyncio.get_running_loop().time() + STUDIO_TIMEOUT_S
    last_status: str | None = None
    while asyncio.get_running_loop().time() < deadline:
        async with SessionLocal() as session:
            run = (
                await session.execute(
                    select(AgentStudioRun).where(AgentStudioRun.run_id == run_id)
                )
            ).scalar_one_or_none()
            deployment = (
                await session.execute(
                    select(AgentDeployment).where(
                        AgentDeployment.deploy_id == deploy_id
                    )
                )
            ).scalar_one_or_none()
            if run is None or deployment is None:
                return
            status = str(deployment.status or "")
            if status != last_status:
                run.status = "deploying"
                await _record_event(
                    session,
                    run,
                    phase="deploy",
                    actor="deployer",
                    status="running",
                    message=f"Deployment is {status or 'pending'}.",
                    data={"deploy_id": deploy_id, "deployment_status": status},
                )
                await session.commit()
                last_status = status
            if status == "live":
                agent = (
                    await session.execute(select(Agent).where(Agent.name == agent_name))
                ).scalar_one_or_none()
                run.status = "live"
                report = dict(run.report or {})
                report.update(
                    {
                        "status": "succeeded",
                        "agent_name": agent_name,
                        "agent_url": agent.url if agent is not None else None,
                        "deploy_id": deploy_id,
                        "reuse_action": run.action,
                        "next_actions": [],
                    }
                )
                run.report = report
                await _record_event(
                    session,
                    run,
                    phase="deploy",
                    actor="deployer",
                    status="passed",
                    message="Agent is live.",
                    data={"deploy_id": deploy_id},
                )
                await session.commit()
                return
            if status == "failed":
                run.status = "failed"
                run.stop_reason = "deployment failed"
                await _record_event(
                    session,
                    run,
                    phase="deploy",
                    actor="deployer",
                    status="failed",
                    message="Deployment failed.",
                    data={"deploy_id": deploy_id},
                )
                await session.commit()
                return
        await asyncio.sleep(5.0)

    async with SessionLocal() as session:
        run = (
            await session.execute(
                select(AgentStudioRun).where(AgentStudioRun.run_id == run_id)
            )
        ).scalar_one_or_none()
        if run is not None and run.status == "deploying":
            run.status = "failed"
            run.stop_reason = "deployment verification timed out"
            await session.commit()


async def run_studio_build(
    *,
    run_id: str,
    agent_name: str,
    user_id: int,
    user_jwt: str,
    cp_url: str,
    skill_args: dict[str, Any],
    skill_name: str = STUDIO_SKILL,
    budget_cents: int = 500,
) -> None:
    """Invoke the studio coordinator and persist its streamed progress + report."""
    async with SessionLocal() as session:
        run = (
            await session.execute(
                select(AgentStudioRun).where(AgentStudioRun.run_id == run_id)
            )
        ).scalar_one_or_none()
        if run is None:
            logger.warning("studio run %s vanished before start", run_id)
            return
        run.status = "building"
        await session.commit()

        try:
            grant, creds = _studio_grant(
                user_id=user_id,
                run_id=run_id,
                agent_name=agent_name,
                budget_cents=budget_cents,
                skill_name=skill_name,
            )
            body: dict[str, Any] = {
                "arguments": skill_args,
                "cp_jwt": user_jwt,
                "cp_url": cp_url,
                "grant": grant,
                "llm_creds": creds,
            }
            report = await _stream_studio_invoke(
                session,
                run,
                body,
                skill_name=skill_name,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("studio run %s failed", run_id)
            run.status = "failed"
            run.stop_reason = _short(str(exc))
            await _record_event(
                session,
                run,
                phase="build",
                actor="coordinator",
                status="failed",
                message=f"Build failed: {_short(str(exc))}",
            )
            await session.commit()
            return

        _apply_report(run, report)
        await _record_event(
            session,
            run,
            phase="deploy" if run.status == "live" else "build",
            actor="deployer" if run.status == "live" else "coordinator",
            status="passed" if run.status == "live" else "failed",
            message=(
                "Agent is live."
                if run.status == "live"
                else run.stop_reason or "Run did not reach live."
            ),
        )
        await session.commit()


async def _stream_studio_invoke(
    session: Any,
    run: AgentStudioRun,
    body: dict[str, Any],
    *,
    skill_name: str = STUDIO_SKILL,
) -> dict[str, Any]:
    """POST to the studio invoke endpoint and record SSE progress; return report."""
    last_result: dict[str, Any] | None = None
    async with httpx.AsyncClient(timeout=STUDIO_TIMEOUT_S + 60.0) as client:
        async with client.stream(
            "POST",
            _studio_url(skill_name),
            json=body,
            headers={"Accept": "text/event-stream"},
        ) as resp:
            if resp.status_code >= 400:
                detail = (await resp.aread()).decode("utf-8", "replace")[:500]
                raise RuntimeError(f"agent-studio {resp.status_code}: {detail}")
            buf = ""
            async for chunk in resp.aiter_text():
                buf += chunk
                while "\n\n" in buf:
                    raw, buf = buf.split("\n\n", 1)
                    ev = _parse_sse_event(raw)
                    if ev is None:
                        continue
                    etype = ev.get("type")
                    if etype == "result":
                        result_payload = ev.get("result", ev)
                        if isinstance(result_payload, dict):
                            last_result = result_payload
                    else:
                        message = _event_message(ev)
                        if message:
                            await _record_progress(session, run, message)
    if last_result is None:
        recovered = await _recover_live_report(session, run)
        if recovered is not None:
            await _record_event(
                session,
                run,
                phase="deploy",
                actor="deployer",
                status="passed",
                message="The coordinator stream ended, but the live deployment and Agent Card were verified.",
            )
            await session.commit()
            return recovered
        raise RuntimeError("agent-studio stream ended without a result event")
    return last_result


async def _recover_live_report(
    session: Any, run: AgentStudioRun
) -> dict[str, Any] | None:
    """Recover a successful deploy when a long coordinator stream loses its result.

    Complex builds can finish deployment just as the upstream streaming request
    reaches its timeout. Never infer success from progress text alone: require a
    registered running agent and a public card whose name matches this run.
    """
    agent = (
        await session.execute(select(Agent).where(Agent.name == run.agent_name))
    ).scalar_one_or_none()
    if agent is None or agent.status not in {"ready", "running"} or not agent.url:
        return None
    card_url = f"{agent.url.rstrip('/')}/.well-known/agent-card"
    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
            response = await client.get(
                card_url, headers={"Accept": "application/json"}
            )
        if response.status_code != 200:
            return None
        card = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(card, dict) or card.get("name") != run.agent_name:
        return None
    return {
        # The coordinator result was lost, but the deployed registry row and
        # matching live Agent Card provide the same terminal evidence needed
        # to call the build successful. Keep the recovery marker so the UI and
        # operators can still distinguish this path from a normal result.
        "status": "succeeded",
        "agent_name": run.agent_name,
        "agent_url": agent.url.rstrip("/"),
        "deployment_id": None,
        "tests": [
            {
                "name": "agent-card",
                "status": "pass",
                "summary": "matching live Agent Card verified after coordinator stream ended",
            }
        ],
        "review": {},
        "iterations": [],
        "budgets": {"spend_cents": run.budget_spent_cents},
        "publish_next_step": "already-private",
        "recovered_live_deployment": True,
    }


def _parse_sse_event(raw: str) -> dict[str, Any] | None:
    data_lines = [ln[5:].lstrip() for ln in raw.split("\n") if ln.startswith("data:")]
    if not data_lines:
        return None
    payload = "\n".join(data_lines)
    if payload == "[DONE]":
        return None
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _event_message(ev: dict[str, Any]) -> str:
    for key in ("message", "text", "detail", "summary", "status"):
        val = ev.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    payload = ev.get("payload")
    if isinstance(payload, dict):
        for key in ("message", "text", "detail", "summary", "status"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


# Coordinator emit_progress() strings are free text; classify by substring so the
# dashboard timeline can attribute each line to a phase and crew member.
_PHASE_RULES: tuple[tuple[str, str, str], ...] = (
    ("code-editor", "improve", "editor"),
    ("agent-reviewer", "review", "reviewer"),
    ("review", "review", "reviewer"),
    ("agent-builder", "build", "builder"),
    ("live head", "deploy", "deployer"),
    ("deploy", "deploy", "deployer"),
    ("plan ready", "build", "coordinator"),
)


def _classify(message: str) -> tuple[str, str]:
    low = message.lower()
    for needle, phase, actor in _PHASE_RULES:
        if needle in low:
            return phase, actor
    return "build", "coordinator"


async def _record_progress(session: Any, run: AgentStudioRun, message: str) -> None:
    phase, actor = _classify(message)
    # Advance the run's coarse status so the timeline moves with the crew.
    status_for_phase = {
        "build": "building",
        "review": "reviewing",
        "improve": "improving",
        "deploy": "deploying",
    }.get(phase)
    if status_for_phase and run.status not in ("live", "failed"):
        run.status = status_for_phase
    await _record_event(
        session,
        run,
        phase=phase,
        actor=actor,
        status="running",
        message=message,
    )
    await session.commit()


async def _record_event(
    session: Any,
    run: AgentStudioRun,
    *,
    phase: str,
    actor: str,
    status: str,
    message: str,
    data: dict[str, Any] | None = None,
) -> AgentStudioRunEvent:
    event = AgentStudioRunEvent(
        studio_run_id=run.id,
        run_id=run.run_id,
        phase=phase,
        actor=actor,
        status=status,
        message=_short(message, 2000),
        data=data or {},
    )
    session.add(event)
    return event


def _apply_report(run: AgentStudioRun, report: dict[str, Any]) -> None:
    """Fold the coordinator's AgentStudioReport into the run row + frontend report."""
    run.report = _report_payload(report)
    run.deploy_id = _str_or_none(report.get("deployment_id"))
    run.stop_reason = _str_or_none(report.get("stop_reason"))
    budgets = report.get("budgets") if isinstance(report.get("budgets"), dict) else {}
    run.iteration = _int_or(budgets.get("iterations_recorded"), run.iteration)
    spend = budgets.get("spend_cents") or budgets.get("spent_cents")
    run.budget_spent_cents = _int_or(spend, run.budget_spent_cents)
    status = str(report.get("status") or "")
    run.status = (
        "live"
        if status == "succeeded" and _report_acceptance_satisfied(report)
        else "failed"
    )


def _report_acceptance_satisfied(report: dict[str, Any]) -> bool:
    tests = [item for item in report.get("tests") or [] if isinstance(item, dict)]
    latest = {str(item.get("name")): item for item in tests if item.get("name")}
    if any(item.get("status") == "fail" for item in latest.values()):
        return False
    brief = (
        report.get("build_brief") if isinstance(report.get("build_brief"), dict) else {}
    )
    app_spec = brief.get("app_spec") if isinstance(brief.get("app_spec"), dict) else {}
    required: set[str] = set()
    if app_spec.get("product_ui") or app_spec.get("profile") == "full_stack":
        required.add("packed-frontend")
    if app_spec.get("persistence"):
        required.add("managed-database")
        calls = (
            app_spec.get("acceptance_calls")
            if isinstance(app_spec.get("acceptance_calls"), list)
            else []
        )
        purposes = {
            str(call.get("purpose"))
            for call in calls
            if isinstance(call, dict) and call.get("purpose")
        }
        if not {"success", "reload", "failure"}.issubset(purposes):
            return False
        required.update(
            f"acceptance:{purpose}:{str(call.get('tool') or '')}"
            for purpose in ("success", "reload", "failure")
            for call in calls
            if isinstance(call, dict)
            and call.get("purpose") == purpose
            and call.get("tool")
        )
    if app_spec.get("requires_mcp"):
        required.update({"mcp:tools-list", "mcp:tools-call"})
    if app_spec.get("requires_receipt"):
        required.add("execution-receipt")
    for journey in app_spec.get("browser_journeys") or []:
        if isinstance(journey, dict) and journey.get("name"):
            required.add(f"browser:{journey['name']}")
    if app_spec.get("account_trial_calls"):
        required.add("account-funded-trial")
    acceptance_calls = (
        app_spec.get("acceptance_calls")
        if isinstance(app_spec.get("acceptance_calls"), list)
        else []
    )
    if any(
        isinstance(call, dict) and call.get("purpose") == "success"
        for call in acceptance_calls
    ):
        for output in app_spec.get("output_expectations") or []:
            if not isinstance(output, dict):
                continue
            label = output.get("name") or output.get("filename") or output.get("kind")
            if output.get("kind") and label:
                required.add(f"output:{output['kind']}:{label}")
    passed = {
        name
        for name, item in latest.items()
        if item.get("status") == "pass" and item.get("name")
    }
    return required.issubset(passed)


def _report_payload(report: dict[str, Any]) -> dict[str, Any]:
    tests = report.get("tests") if isinstance(report.get("tests"), list) else []
    passed = sum(1 for t in tests if isinstance(t, dict) and t.get("status") == "pass")
    review = report.get("review") if isinstance(report.get("review"), dict) else {}
    findings = _int_or(review.get("critical_count"), 0) + _int_or(
        review.get("warning_count"), 0
    )
    agent_url = _str_or_none(report.get("agent_url"))
    distribution = (
        report.get("distribution")
        if isinstance(report.get("distribution"), dict)
        else {}
    )
    next_step = str(report.get("publish_next_step") or "")
    residual_risks = [
        _short(str(item), 1000)
        for item in report.get("residual_risks") or []
        if str(item).strip()
    ]
    handoffs = (
        report.get("handoffs") if isinstance(report.get("handoffs"), list) else []
    )
    builder_error = None
    for handoff in handoffs:
        if not isinstance(handoff, dict) or handoff.get("agent") != "agent-builder":
            continue
        summary = (
            handoff.get("result_summary")
            if isinstance(handoff.get("result_summary"), dict)
            else {}
        )
        builder_error = _str_or_none(summary.get("error")) or _str_or_none(
            summary.get("warning")
        )
        if builder_error:
            builder_error = _short(builder_error, 1000)
            break
    next_actions = ["Add to a team", "Give it a schedule"]
    if not report.get("public"):
        next_actions.insert(0, "Publish to marketplace")
    if next_step and next_step != "already-private":
        next_actions.insert(0, next_step)
    return {
        "agent_name": report.get("agent_name"),
        "agent_url": agent_url,
        "mcp_url": f"{agent_url}/mcp" if agent_url else None,
        "frontend_url": f"{agent_url}/app" if agent_url else None,
        "public": bool(report.get("public")),
        "public_url": _str_or_none(distribution.get("public_url")),
        "source_url": _str_or_none(distribution.get("source_url")),
        "cli": _str_or_none(distribution.get("cli")),
        "distribution": distribution,
        "status": report.get("status"),
        "stop_reason": _str_or_none(report.get("stop_reason")),
        "failure_detail": builder_error
        or (residual_risks[0] if residual_risks else None),
        "residual_risks": residual_risks,
        "iterations": len(report.get("iterations") or []),
        "tests_passed": passed,
        "tests_total": len(tests),
        "findings": findings,
        "receipt_id": _str_or_none(review.get("review_id")),
        "deploy_id": _str_or_none(report.get("deployment_id")),
        "recovered_live_deployment": bool(report.get("recovered_live_deployment")),
        "acceptance_satisfied": _report_acceptance_satisfied(report),
        "acceptance": {
            str(test.get("name")): str(test.get("status"))
            for test in tests
            if isinstance(test, dict) and test.get("name")
        },
        "next_actions": next_actions,
    }


def _short(text: str, limit: int = 400) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _int_or(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
