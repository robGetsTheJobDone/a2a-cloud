"""Daily read-only agent reviews and owner-approved Agent Studio upgrades."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import smtplib
import uuid
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from html import escape
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from sqlalchemy import select, update

from .agent_review import (
    REVIEWER_AGENT_NAME,
    REVIEWER_CALL_TTL_SECONDS,
    _call_reviewer,
    _reviewer_llm_creds,
)
from .agent_studio_runs import _studio_grant
from .auth import issue_invocation_cp_credential
from .config import settings
from .db import SessionLocal
from .models import (
    Agent,
    AgentDeployment,
    AgentStudioAutopilotPolicy,
    AgentStudioUpgradeProposal,
    User,
)

log = logging.getLogger(__name__)

UPGRADE_SKILL = "upgrade_agent"
UPGRADE_TIMEOUT_SECONDS = 3660.0
#: The agent ``_call_studio_upgrade`` invokes. Named so the credential minted
#: for that call is checked against ``PLATFORM_TOOLCHAIN_AGENTS`` by the same
#: helper every other outbound invoke uses.
STUDIO_AGENT_NAME = "agent-studio"


def utcnow() -> datetime:
    return datetime.now(UTC)


def next_daily_time(
    timezone_name: str,
    daily_hour: int,
    *,
    after: datetime | None = None,
) -> datetime:
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("unknown timezone") from exc
    hour = max(0, min(int(daily_hour), 23))
    current = (after or utcnow()).astimezone(zone)
    candidate = current.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= current:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def start_autopilot_loop(app: Any, *, interval_seconds: float) -> asyncio.Task[None]:
    task = asyncio.create_task(
        _autopilot_loop(app, interval_seconds=max(1.0, interval_seconds)),
        name="agent-studio-autopilot",
    )
    app.state.agent_studio_autopilot_loop = task
    if not hasattr(app.state, "agent_studio_autopilot_tasks"):
        app.state.agent_studio_autopilot_tasks = set()
    return task


async def stop_autopilot_loop(app: Any) -> None:
    loop = getattr(app.state, "agent_studio_autopilot_loop", None)
    if loop is not None and not loop.done():
        loop.cancel()
        try:
            await loop
        except asyncio.CancelledError:
            pass
    tasks = list(getattr(app.state, "agent_studio_autopilot_tasks", set()) or [])
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _autopilot_loop(app: Any, *, interval_seconds: float) -> None:
    while True:
        try:
            for policy_id in await claim_due_policies():
                enqueue_policy_scan(app, policy_id=policy_id)
            for proposal_id in await accepted_proposal_ids():
                enqueue_proposal_upgrade(app, proposal_id=proposal_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Agent Studio autopilot tick failed")
        await asyncio.sleep(interval_seconds)


def _track(app: Any, task: asyncio.Task[None]) -> None:
    tasks = getattr(app.state, "agent_studio_autopilot_tasks", None)
    if tasks is None:
        tasks = set()
        app.state.agent_studio_autopilot_tasks = tasks
    tasks.add(task)
    task.add_done_callback(tasks.discard)


def enqueue_policy_scan(app: Any, *, policy_id: int) -> None:
    task = asyncio.create_task(
        run_policy_scan(policy_id),
        name=f"studio-autopilot-review-{policy_id}",
    )
    _track(app, task)


def enqueue_proposal_upgrade(app: Any, *, proposal_id: str) -> None:
    task = asyncio.create_task(
        run_proposal_upgrade(proposal_id),
        name=f"studio-autopilot-upgrade-{proposal_id}",
    )
    _track(app, task)


def _is_openapi_agent(agent: Agent) -> bool:
    """Return whether an agent was generated from an OpenAPI description."""
    card = agent.card if isinstance(agent.card, dict) else {}
    capabilities = card.get("capabilities")
    if isinstance(capabilities, dict) and "openapi_auto_agent" in capabilities:
        return True
    runtime = card.get("runtime")
    tools = runtime.get("tools_used") if isinstance(runtime, dict) else None
    return isinstance(tools, list) and "openapi" in tools


async def claim_due_policies(
    *, now: datetime | None = None, limit: int = 10
) -> list[int]:
    due_at = now or utcnow()
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(AgentStudioAutopilotPolicy)
                .where(
                    AgentStudioAutopilotPolicy.enabled == True,  # noqa: E712
                    AgentStudioAutopilotPolicy.next_run_at.is_not(None),
                    AgentStudioAutopilotPolicy.next_run_at <= due_at,
                )
                .order_by(AgentStudioAutopilotPolicy.next_run_at.asc())
                .limit(limit)
            )
        ).scalars().all()
        claimed: list[int] = []
        for row in rows:
            next_run = next_daily_time(row.timezone, row.daily_hour, after=due_at)
            result = await session.execute(
                update(AgentStudioAutopilotPolicy)
                .where(
                    AgentStudioAutopilotPolicy.id == row.id,
                    AgentStudioAutopilotPolicy.enabled == True,  # noqa: E712
                    AgentStudioAutopilotPolicy.next_run_at <= due_at,
                )
                .values(next_run_at=next_run, last_run_status="queued", updated_at=due_at)
            )
            if result.rowcount:
                claimed.append(row.id)
        await session.commit()
        return claimed


async def accepted_proposal_ids(*, limit: int = 10) -> list[str]:
    async with SessionLocal() as session:
        return list(
            (
                await session.execute(
                    select(AgentStudioUpgradeProposal.proposal_id)
                    .where(AgentStudioUpgradeProposal.status == "accepted")
                    .order_by(AgentStudioUpgradeProposal.decided_at.asc())
                    .limit(limit)
                )
            ).scalars().all()
        )


async def run_policy_scan(policy_id: int) -> None:
    async with SessionLocal() as session:
        policy = await session.get(AgentStudioAutopilotPolicy, policy_id)
        if policy is None or not policy.enabled:
            return
        user = await session.get(User, policy.user_id)
        if user is None:
            return
        policy.last_run_at = utcnow()
        policy.last_run_status = "running"
        policy.last_error = None
        await session.commit()

        candidate_agents = list(
            (
                await session.execute(
                    select(Agent)
                    .where(
                        Agent.owner_id == user.id,
                        Agent.gitea_owner.is_not(None),
                    )
                    .order_by(Agent.updated_at.desc())
                )
            ).scalars().all()
        )
        agents = [
            agent for agent in candidate_agents if not _is_openapi_agent(agent)
        ][: settings.agent_studio_autopilot_max_agents_per_run]
        errors: list[str] = []
        created: list[AgentStudioUpgradeProposal] = []
        cp_url = os.environ.get("A2A_CP_URL", settings.public_cp_url)
        for agent in agents:
            try:
                # One identity per reviewer call, capped at that call's own
                # timeout. A scan walks up to
                # ``agent_studio_autopilot_max_agents_per_run`` agents, so a
                # single credential covering the whole sweep would have to
                # outlive it by hours.
                proposals = await _review_agent_for_ideas(
                    session,
                    user=user,
                    agent=agent,
                    user_jwt=issue_invocation_cp_credential(
                        user.id,
                        agent=REVIEWER_AGENT_NAME,
                        ttl_seconds=REVIEWER_CALL_TTL_SECONDS,
                    ),
                    cp_url=cp_url,
                )
                created.extend(proposals)
            except Exception as exc:  # noqa: BLE001
                log.exception("daily Studio review failed agent=%s", agent.name)
                errors.append(f"{agent.name}: {type(exc).__name__}: {exc}")

        await session.execute(
            update(AgentStudioUpgradeProposal)
            .where(
                AgentStudioUpgradeProposal.user_id == user.id,
                AgentStudioUpgradeProposal.status == "pending",
                AgentStudioUpgradeProposal.expires_at <= utcnow(),
            )
            .values(status="expired", completed_at=utcnow())
        )
        await session.commit()

        unemailed = list(
            (
                await session.execute(
                    select(AgentStudioUpgradeProposal)
                    .where(
                        AgentStudioUpgradeProposal.user_id == user.id,
                        AgentStudioUpgradeProposal.status == "pending",
                        AgentStudioUpgradeProposal.emailed_at.is_(None),
                    )
                    .order_by(AgentStudioUpgradeProposal.created_at.desc())
                    .limit(20)
                )
            ).scalars().all()
        )
        sent = False
        if unemailed:
            sent = await asyncio.to_thread(_send_digest_email, user, unemailed)
            if sent:
                sent_at = utcnow()
                for proposal in unemailed:
                    proposal.emailed_at = sent_at
                policy.last_email_at = sent_at
            else:
                errors.append("could not send the improvement email")
        policy.last_run_status = "partial" if errors else "complete"
        policy.last_error = "\n".join(errors)[:4000] or None
        await session.commit()


async def _review_agent_for_ideas(
    session: Any,
    *,
    user: User,
    agent: Agent,
    user_jwt: str,
    cp_url: str,
) -> list[AgentStudioUpgradeProposal]:
    latest = (
        await session.execute(
            select(AgentDeployment)
            .where(
                AgentDeployment.agent_id == agent.id,
                AgentDeployment.head_sha.is_not(None),
            )
            .order_by(AgentDeployment.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is None or not latest.head_sha:
        raise RuntimeError("managed source has no immutable revision to review")
    ref = str(latest.head_sha)
    review_id = f"daily-{uuid.uuid4().hex[:16]}"
    report = await _call_reviewer(
        agent_name=agent.name,
        ref=ref,
        owner=agent.gitea_owner,
        cp_jwt=user_jwt,
        cp_url=cp_url,
        llm_creds=_reviewer_llm_creds(
            user_id=user.id,
            review_id=review_id,
            max_budget_usd=settings.agent_studio_autopilot_review_budget_usd,
        ),
        mode="improvements",
    )
    if report.get("error"):
        raise RuntimeError(str(report["error"]))
    findings = [item for item in report.get("findings") or [] if isinstance(item, dict)]
    rank = {"critical": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda item: rank.get(str(item.get("severity")), 3))
    existing = list(
        (
            await session.execute(
                select(AgentStudioUpgradeProposal).where(
                    AgentStudioUpgradeProposal.agent_id == agent.id,
                    AgentStudioUpgradeProposal.source_head_sha == ref,
                )
            )
        ).scalars().all()
    )
    fingerprints = {
        str((row.evidence or {}).get("fingerprint"))
        for row in existing
        if (row.evidence or {}).get("fingerprint")
    }
    created: list[AgentStudioUpgradeProposal] = []
    for finding in findings[: settings.agent_studio_autopilot_max_proposals_per_agent]:
        message = " ".join(str(finding.get("message") or "").split())
        suggestion = " ".join(str(finding.get("suggestion") or "").split())
        if not message:
            continue
        fingerprint = hashlib.sha256(
            json.dumps(finding, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if fingerprint in fingerprints:
            continue
        idea = suggestion or f"Resolve this reviewer finding: {message}"
        location = str(finding.get("file") or "")
        if finding.get("line"):
            location += f":{finding['line']}"
        row = AgentStudioUpgradeProposal(
            proposal_id=f"aup_{uuid.uuid4().hex[:20]}",
            user_id=user.id,
            agent_id=agent.id,
            agent_name=agent.name,
            source_head_sha=ref,
            status="pending",
            severity=str(finding.get("severity") or "info")[:32],
            category=str(finding.get("category") or "ergonomics")[:64],
            title=message[:237] + ("…" if len(message) > 237 else ""),
            idea=idea,
            rationale=(
                "Found during the daily read-only source review"
                + (f" at {location}" if location else "")
                + "."
            ),
            evidence={
                "fingerprint": fingerprint,
                "review_id": review_id,
                "review_summary": str(report.get("summary") or ""),
                "finding": finding,
            },
            expires_at=utcnow()
            + timedelta(days=settings.agent_studio_autopilot_proposal_ttl_days),
        )
        session.add(row)
        created.append(row)
        fingerprints.add(fingerprint)
    if created:
        await session.flush()
    return created


def _send_digest_email(
    user: User, proposals: list[AgentStudioUpgradeProposal]
) -> bool:
    host = os.environ.get("A2A_CP_STUDIO_SMTP_HOST", "").strip()
    port = int(os.environ.get("A2A_CP_STUDIO_SMTP_PORT", "587"))
    smtp_user = os.environ.get("A2A_CP_STUDIO_SMTP_USER", "").strip()
    password = os.environ.get("A2A_CP_STUDIO_SMTP_PASSWORD", "")
    sender = os.environ.get(
        "A2A_CP_STUDIO_SMTP_FROM", f"a2a cloud <hello@{settings.platform_domain}>"
    ).strip()
    if not host or not sender or bool(smtp_user) != bool(password):
        log.error("Agent Studio autopilot email is not configured")
        return False
    dashboard = str(settings.dashboard_url).rstrip("/")
    text_blocks: list[str] = []
    html_blocks: list[str] = []
    for proposal in proposals:
        accept_url = f"{dashboard}/studio?{urlencode({'proposal': proposal.proposal_id, 'decision': 'accept'})}"
        reject_url = f"{dashboard}/studio?{urlencode({'proposal': proposal.proposal_id, 'decision': 'reject'})}"
        text_blocks.append(
            f"{proposal.agent_name} — {proposal.title}\n"
            f"Why: {proposal.rationale}\n"
            f"Accept: {accept_url}\nReject: {reject_url}"
        )
        html_blocks.append(
            '<div style="padding:18px;margin:14px 0;border:1px solid #26352d;'
            'border-radius:12px;background:#0c1310">'
            f'<div style="color:#9da9a3;font-size:12px">{escape(proposal.agent_name)} · '
            f'{escape(proposal.severity)}</div>'
            f'<h2 style="font-size:17px;margin:8px 0;color:#f4f7f5">{escape(proposal.title)}</h2>'
            f'<p style="color:#b9c3be;line-height:1.55">{escape(proposal.rationale)}</p>'
            f'<a href="{escape(accept_url, quote=True)}" style="display:inline-block;padding:10px 14px;'
            'background:#b8ffe0;color:#071b16;border-radius:8px;text-decoration:none;font-weight:700">'
            'Review &amp; accept</a> '
            f'<a href="{escape(reject_url, quote=True)}" style="display:inline-block;padding:10px 14px;'
            'color:#d2dad6;border:1px solid #3a4841;border-radius:8px;text-decoration:none">'
            'Review &amp; reject</a></div>'
        )
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = user.email
    msg["Subject"] = f"Agent Studio found {len(proposals)} upgrade idea{'s' if len(proposals) != 1 else ''}"
    msg.set_content(
        "Agent Studio reviewed your agents and their source code. No code changes "
        "unless you approve an idea.\n\n" + "\n\n".join(text_blocks)
    )
    msg.add_alternative(
        '<!doctype html><html><body style="margin:0;background:#080c0a;color:#f4f7f5;'
        'font-family:Arial,sans-serif"><div style="max-width:680px;margin:30px auto;padding:30px;'
        'background:#101713;border:1px solid #26352d;border-radius:18px">'
        '<div style="color:#b8ffe0;font-size:12px;font-weight:700;letter-spacing:.14em;'
        'text-transform:uppercase">Agent Studio · daily review</div>'
        f'<h1 style="font-size:27px;margin:14px 0">{len(proposals)} upgrade idea'
        f'{"s" if len(proposals) != 1 else ""}</h1>'
        '<p style="color:#b9c3be;line-height:1.6">We reviewed your agents and their source. '
        'Nothing changes until you approve an idea.</p>'
        + "".join(html_blocks)
        + '<p style="color:#87938d;font-size:12px;line-height:1.5;margin-top:24px">'
        'Each link opens a confirmation screen. Proposals expire automatically.</p>'
        '</div></body></html>',
        subtype="html",
    )
    try:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.starttls()
            if smtp_user and password:
                smtp.login(smtp_user, password)
            smtp.send_message(msg)
        return True
    except Exception:  # noqa: BLE001
        log.exception("could not send Agent Studio autopilot digest user_id=%s", user.id)
        return False


async def run_proposal_upgrade(proposal_id: str) -> None:
    now = utcnow()
    async with SessionLocal() as session:
        claimed = await session.execute(
            update(AgentStudioUpgradeProposal)
            .where(
                AgentStudioUpgradeProposal.proposal_id == proposal_id,
                AgentStudioUpgradeProposal.status == "accepted",
            )
            .values(status="applying", started_at=now, updated_at=now)
        )
        if not claimed.rowcount:
            await session.rollback()
            return
        await session.commit()
        proposal = (
            await session.execute(
                select(AgentStudioUpgradeProposal).where(
                    AgentStudioUpgradeProposal.proposal_id == proposal_id
                )
            )
        ).scalar_one()
        try:
            # Through the same helper as every other outbound invoke, not
            # around it: ``agent-studio`` keeps an ordinary platform credential
            # only because ``PLATFORM_TOOLCHAIN_AGENTS`` says so, and that set
            # has to be the single place the platform expresses which agents
            # may receive one. Minting it directly here would leave a full
            # account session flowing into a deployed agent process on the day
            # someone removes agent-studio from that set, with no test failing.
            #
            # The helper's 1830s clamp is not a shortening in practice: the
            # 3660s below is an httpx client timeout, while the upgrade runs
            # inside agent-studio's own Knative revision, whose timeout
            # ``k8s._declared_runtime_timeout`` caps at
            # ``KNATIVE_MAX_TIMEOUT_SECONDS`` (1800s) no matter what its card
            # asks for — a2a.yaml asks for 3600.
            user_jwt = issue_invocation_cp_credential(
                proposal.user_id,
                agent=STUDIO_AGENT_NAME,
                ttl_seconds=int(UPGRADE_TIMEOUT_SECONDS),
            )
            grant, creds = _studio_grant(
                user_id=proposal.user_id,
                run_id=proposal.proposal_id,
                agent_name=proposal.agent_name,
                budget_cents=500,
                skill_name=UPGRADE_SKILL,
            )
            body = {
                "arguments": {
                    "name": proposal.agent_name,
                    "idea": proposal.idea,
                    "proposal_id": proposal.proposal_id,
                    "expected_head_sha": proposal.source_head_sha or "",
                    "evidence_json": json.dumps(proposal.evidence or {}, sort_keys=True),
                },
                "cp_jwt": user_jwt,
                "cp_url": os.environ.get("A2A_CP_URL", settings.public_cp_url),
                "grant": grant,
                "llm_creds": creds,
            }
            report = await _call_studio_upgrade(body)
            proposal.upgrade_run_id = str(report.get("run_id") or "") or None
            proposal.upgrade_report = report
            proposal.completed_at = utcnow()
            if report.get("ok") and report.get("status") == "succeeded":
                proposal.status = "applied"
                proposal.error = None
            else:
                proposal.status = "failed"
                proposal.error = str(report.get("stop_reason") or "upgrade failed")[:4000]
        except Exception as exc:  # noqa: BLE001
            log.exception("Agent Studio autopilot upgrade failed proposal=%s", proposal_id)
            proposal.status = "failed"
            proposal.error = f"{type(exc).__name__}: {exc}"[:4000]
            proposal.completed_at = utcnow()
        await session.commit()


async def _call_studio_upgrade(body: dict[str, Any]) -> dict[str, Any]:
    host = os.environ.get("A2A_CP_STUDIO_HOST", "agent-studio.agents.svc.cluster.local")
    url = f"http://{host}/invoke/{UPGRADE_SKILL}"
    result: dict[str, Any] | None = None
    async with httpx.AsyncClient(timeout=UPGRADE_TIMEOUT_SECONDS) as client:
        async with client.stream(
            "POST", url, json=body, headers={"Accept": "text/event-stream"}
        ) as response:
            if response.status_code >= 400:
                detail = (await response.aread()).decode("utf-8", "replace")[:500]
                raise RuntimeError(f"agent-studio {response.status_code}: {detail}")
            buffer = ""
            async for chunk in response.aiter_text():
                buffer += chunk
                while "\n\n" in buffer:
                    frame, buffer = buffer.split("\n\n", 1)
                    data = "\n".join(
                        line[5:].lstrip()
                        for line in frame.splitlines()
                        if line.startswith("data:")
                    )
                    if not data or data == "[DONE]":
                        continue
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "result":
                        payload = event.get("result", event)
                        if isinstance(payload, dict):
                            result = payload
    if result is None:
        raise RuntimeError("agent-studio upgrade ended without a result")
    return result
