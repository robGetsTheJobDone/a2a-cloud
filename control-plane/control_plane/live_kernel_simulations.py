"""Hybrid live-agent evidence adapter for user kernel simulations."""
from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from main_agent.config import load_settings as load_main_agent_settings

from .agent_proofs import sample_args_from_schema
from .auth import issue_invocation_cp_credential
from .config import settings
from .consumer_setup import require_consumer_setup
from .grants import mint_grant_token
from .minio_client import bucket_for_user
from .models import Agent, User
from .routes import agents as agent_routes
from .routes.llm_creds import get_creds_for_user

_COORDINATOR_IDS = {"kernel-orchestrator", "orchestrator", "coordinator", "market", "arena"}
_RUNNABLE_STATUSES = {"ready", "running"}
_MAX_JSON_CHARS = 12_000
_MAX_STRING_CHARS = 2_000


class LiveKernelSimulationError(ValueError):
    pass


@dataclass(frozen=True)
class LiveKernelInvocationPlan:
    node_id: str
    agent_name: str
    skill_name: str
    role: str | None


@dataclass(frozen=True)
class LiveKernelInvocationResult:
    passed: bool
    records: tuple[dict[str, Any], ...]
    digest: str

    @property
    def fail_count(self) -> int:
        return sum(1 for record in self.records if record.get("status") != "passed")

    def to_payload(self) -> dict[str, Any]:
        return {
            "mode": "hybrid",
            "passed": self.passed,
            "count": len(self.records),
            "fail_count": self.fail_count,
            "digest": self.digest,
            "records": list(self.records),
        }


def live_invocation_digest(records: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> str:
    stable = [
        {
            "node_id": str(record.get("node_id") or ""),
            "agent_name": str(record.get("agent_name") or ""),
            "skill_name": str(record.get("skill_name") or ""),
            "status": str(record.get("status") or ""),
            "result": record.get("result"),
            "error": record.get("error"),
        }
        for record in records
    ]
    encoded = json.dumps(stable, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def live_invocation_summary(payload: dict[str, Any]) -> dict[str, Any]:
    live = payload.get("live_invocations") if isinstance(payload.get("live_invocations"), dict) else {}
    records = live.get("records") if isinstance(live.get("records"), list) else []
    return {
        "live_invocation_count": len(records),
        "live_invocation_fail_count": sum(
            1 for record in records if isinstance(record, dict) and record.get("status") != "passed"
        ),
        "live_invocation_digest": live_invocation_digest([record for record in records if isinstance(record, dict)]),
    }


async def run_hybrid_live_invocations(
    *,
    spec: dict[str, Any],
    user: User,
    session: AsyncSession,
    max_live_calls: int,
) -> LiveKernelInvocationResult:
    plans = _live_invocation_plans(spec)[:max_live_calls]
    if not plans:
        raise LiveKernelSimulationError("hybrid execution requires at least one agent actor")

    records: list[dict[str, Any]] = []
    for plan in plans:
        agent = await _resolve_accessible_agent(session, user, plan.agent_name)
        if str(agent.status or "").lower() not in _RUNNABLE_STATUSES:
            raise LiveKernelSimulationError(
                f"agent {plan.agent_name!r} is not runnable for hybrid simulations: {agent.status}"
            )
        skill = _select_live_skill_spec(agent, plan.skill_name)
        skill_name = str(skill.get("name") or "").strip()
        record = await _invoke_live_agent(
            agent=agent,
            plan=LiveKernelInvocationPlan(
                node_id=plan.node_id,
                agent_name=plan.agent_name,
                skill_name=skill_name,
                role=plan.role,
            ),
            spec=spec,
            skill=skill,
            user=user,
            session=session,
        )
        records.append(record)
    digest = live_invocation_digest(records)
    return LiveKernelInvocationResult(
        passed=all(record.get("status") == "passed" for record in records),
        records=tuple(records),
        digest=digest,
    )


def _live_invocation_plans(spec: dict[str, Any]) -> list[LiveKernelInvocationPlan]:
    metadata = spec.get("metadata") if isinstance(spec.get("metadata"), dict) else {}
    source_names = [str(item) for item in metadata.get("source_agent_names") or [] if str(item).strip()]
    if not source_names:
        raise LiveKernelSimulationError(
            "hybrid execution requires selected live agents; build the spec from your agents or run this template in proof-only mode"
        )
    source_skills = metadata.get("source_agent_skills") if isinstance(metadata.get("source_agent_skills"), dict) else {}
    actors = [item for item in spec.get("actors") or [] if isinstance(item, dict)]
    invoke_ports = _invoke_ports_by_node(spec)
    plans: list[LiveKernelInvocationPlan] = []
    source_index = 0
    for actor in actors:
        node_id = str(actor.get("id") or "").strip()
        if not node_id or node_id in _COORDINATOR_IDS:
            continue
        label = str(actor.get("label") or "").strip()
        agent_name = label or node_id
        if source_index < len(source_names):
            agent_name = source_names[source_index]
        source_index += 1
        if not agent_name:
            continue
        raw_skill = source_skills.get(agent_name) or invoke_ports.get(node_id) or "pursue"
        skill_name = str(raw_skill).split(":", 1)[-1].strip() or "pursue"
        plans.append(
            LiveKernelInvocationPlan(
                node_id=node_id,
                agent_name=agent_name,
                skill_name=skill_name,
                role=str(actor.get("role") or "") or None,
            )
        )
    return plans


def _invoke_ports_by_node(spec: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for port in spec.get("ports") or []:
        if not isinstance(port, dict):
            continue
        port_id = str(port.get("id") or port.get("port_id") or "")
        if not port_id.startswith("invoke:"):
            continue
        node_id = str(port.get("node_id") or "")
        if node_id and node_id not in out:
            out[node_id] = port_id.split(":", 1)[1]
    return out


async def _resolve_accessible_agent(session: AsyncSession, user: User, name: str) -> Agent:
    agent = (
        await session.execute(
            select(Agent).where(
                Agent.name == name,
                or_(Agent.owner_id == user.id, Agent.public == True),  # noqa: E712
            )
        )
    ).scalar_one_or_none()
    if agent is None:
        raise LiveKernelSimulationError(f"agent {name!r} was not found or is not accessible")
    return agent


def _select_live_skill_spec(agent: Agent, requested: str) -> dict[str, Any]:
    card = agent.card if isinstance(agent.card, dict) else {}
    skills = card.get("skills")
    if not isinstance(skills, list) or not skills:
        if requested:
            return {"name": requested}
        raise LiveKernelSimulationError(f"agent {agent.name!r} has no callable skills")
    for skill in skills:
        if not isinstance(skill, dict):
            continue
        name = str(skill.get("name") or "").strip()
        if name and name == requested:
            return skill
    if requested and requested != "pursue":
        raise LiveKernelSimulationError(f"skill {requested!r} was not found on agent {agent.name!r}")
    for skill in skills:
        if isinstance(skill, dict) and str(skill.get("name") or "").strip():
            return skill
    raise LiveKernelSimulationError(f"agent {agent.name!r} has no callable skills")


async def _invoke_live_agent(
    *,
    agent: Agent,
    plan: LiveKernelInvocationPlan,
    spec: dict[str, Any],
    skill: dict[str, Any],
    user: User,
    session: AsyncSession,
) -> dict[str, Any]:
    started = time.monotonic()
    args = _live_arguments(spec, plan, skill)
    try:
        consumer_setup = await require_consumer_setup(
            agent=agent,
            user=user,
            session=session,
        )
        if agent_routes._is_external_agent(agent):
            raw = await agent_routes._call_external_agent_skill(
                agent=agent,
                session=session,
                user=user,
                skill_name=plan.skill_name,
                arguments=args,
            )
        else:
            raw = await _call_internal_agent(
                agent=agent,
                skill_name=plan.skill_name,
                arguments=args,
                user=user,
                session=session,
                consumer_setup=consumer_setup.invocation_payload(),
            )
        result = _result_payload(raw)
        status = "failed" if _result_is_error(result) else "passed"
        return {
            "node_id": plan.node_id,
            "agent_name": agent.name,
            "skill_name": plan.skill_name,
            "role": plan.role,
            "status": status,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "arguments": _json_boundary(args),
            "result": _json_boundary(result),
            "events": _events_payload(raw),
            "error": str(result.get("error"))[:_MAX_STRING_CHARS] if status == "failed" else None,
            "live_apply_enabled": False,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "node_id": plan.node_id,
            "agent_name": agent.name,
            "skill_name": plan.skill_name,
            "role": plan.role,
            "status": "failed",
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "arguments": _json_boundary(args),
            "result": {},
            "events": [],
            "error": (
                f"{type(exc).__name__}: "
                f"{agent_routes.agent_invoke_error_text(exc)}"
            )[:_MAX_STRING_CHARS],
            "live_apply_enabled": False,
        }


async def _call_internal_agent(
    *,
    agent: Agent,
    skill_name: str,
    arguments: dict[str, Any],
    user: User,
    session: AsyncSession,
    consumer_setup: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    creds = await get_creds_for_user(user.id, session)
    platform_llm = None if creds else _platform_llm_grant_plan()
    token, grant_payload = mint_grant_token(
        issuer=f"kernel-simulation:user-{user.id}",
        audience=agent.name,
        bucket=bucket_for_user(user.id),
        mode="read_only",
        allow_patterns=("**",),
        llm_models=platform_llm["models"] if platform_llm else (),
        llm_max_budget_usd=platform_llm["max_budget_usd"] if platform_llm else None,
        llm_rpm_limit=platform_llm["rpm_limit"] if platform_llm else None,
        llm_tpm_limit=platform_llm["tpm_limit"] if platform_llm else None,
        ttl_seconds=180,
    )
    body: dict[str, Any] = {
        "arguments": arguments,
        "grant": token,
        # Not the caller's session: this body lands in the agent's process.
        "cp_jwt": issue_invocation_cp_credential(user.id, agent=agent.name),
        "cp_url": settings.public_cp_url,
    }
    if consumer_setup:
        body.update(consumer_setup)
    if creds:
        body["llm_creds"] = creds
    elif platform_llm:
        body["llm_creds"] = _platform_llm_creds(
            platform_llm,
            token,
            metadata=_live_kernel_llm_metadata(
                user=user,
                grant_payload=grant_payload,
                agent=agent,
                skill_name=skill_name,
            ),
        )
    async with httpx.AsyncClient(timeout=130.0) as client:
        resp = await client.post(
            f"http://{agent.name}.agents.svc.cluster.local/invoke/{skill_name}",
            json=body,
        )
    if resp.status_code >= 400:
        detail = resp.text[:1200]
        try:
            parsed = resp.json()
            if isinstance(parsed, dict):
                detail = str(parsed.get("detail") or parsed)
        except ValueError:
            pass
        raise RuntimeError(f"agent {resp.status_code}: {detail}")
    parsed = resp.json()
    if not isinstance(parsed, dict):
        raise RuntimeError("agent returned non-object JSON")
    return parsed


def _platform_llm_grant_plan() -> dict[str, Any] | None:
    runtime_settings = load_main_agent_settings()
    models = tuple(
        str(item).strip()
        for item in runtime_settings.platform_llm_models
        if str(item).strip()
    )
    if not models:
        return None
    return {
        "base_url": str(runtime_settings.litellm_url).rstrip("/") + "/v1",
        "models": models,
        "max_budget_usd": runtime_settings.platform_llm_max_budget_usd,
        "rpm_limit": runtime_settings.platform_llm_rpm_limit,
        "tpm_limit": runtime_settings.platform_llm_tpm_limit,
    }


def _platform_llm_creds(
    platform_llm: dict[str, Any],
    token: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    models = (
        platform_llm.get("models")
        if isinstance(platform_llm.get("models"), tuple)
        else ()
    )
    model = str(models[0]) if models else str(settings.litellm_model)
    return {
        "base_url": str(platform_llm.get("base_url") or "").rstrip("/"),
        "api_key": token,
        "model": model,
        "temperature_mode": "omit",
        "extra_body": {},
        "metadata": dict(metadata or {}),
    }


def _live_kernel_llm_metadata(
    *,
    user: User,
    grant_payload: dict[str, Any],
    agent: Agent,
    skill_name: str,
) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "a2a_user_id": user.id,
            "a2a_user_email": user.email,
            "a2a_grant_id": grant_payload.get("grant_id"),
            "a2a_agent_name": agent.name,
            "a2a_skill_name": skill_name,
            "a2a_llm_source": "live_kernel",
        }.items()
        if value is not None
    }


def _live_arguments(
    spec: dict[str, Any],
    plan: LiveKernelInvocationPlan,
    skill: dict[str, Any] | None = None,
) -> dict[str, Any]:
    schema = skill.get("input_schema") if isinstance(skill, dict) else None
    if not isinstance(schema, dict):
        return {}
    args = sample_args_from_schema(schema)
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    if not properties:
        return args

    context = _live_argument_context(spec, plan)
    for key, field_schema in properties.items():
        if not isinstance(key, str) or not isinstance(field_schema, dict):
            continue
        value = _live_argument_value_for_key(key, field_schema, context)
        if value is not None:
            args[key] = value
    return args


def _live_argument_context(spec: dict[str, Any], plan: LiveKernelInvocationPlan) -> dict[str, str]:
    simulation_type = str(spec.get("simulation_type") or "").strip()
    scenario_id = str(spec.get("scenario_id") or "").strip()
    title = str(spec.get("title") or "").strip()
    goal = str(spec.get("goal") or "").strip()
    role = str(plan.role or "").strip()
    prompt = _live_simulation_brief(
        spec=spec,
        plan=plan,
        simulation_type=simulation_type,
        scenario_id=scenario_id,
        title=title,
        goal=goal,
        role=role,
    )
    return {
        "simulation_type": simulation_type,
        "scenario_id": scenario_id,
        "title": title,
        "goal": goal,
        "agent_name": plan.agent_name,
        "node_id": plan.node_id,
        "role": role,
        "prompt": prompt,
        "instruction": prompt,
    }


def _live_simulation_brief(
    *,
    spec: dict[str, Any],
    plan: LiveKernelInvocationPlan,
    simulation_type: str,
    scenario_id: str,
    title: str,
    goal: str,
    role: str,
) -> str:
    step_types = _step_type_summary(spec)
    roster = _live_roster_lines(spec, plan)
    risk_class = str(spec.get("risk_class") or "operator_guarded").strip()
    template_ref = str(spec.get("template_ref") or spec.get("template_kind") or "").strip()
    actors = [item for item in spec.get("actors") or [] if isinstance(item, dict)]
    policies = [item for item in spec.get("policies") or [] if isinstance(item, dict)]
    capabilities = [item for item in spec.get("capabilities") or [] if isinstance(item, dict)]
    invariants = spec.get("invariants") if isinstance(spec.get("invariants"), list) else []
    lines = [
        "Produce real evidence for this bounded kernel simulation.",
        "Use this brief as complete. Do not ask for more information.",
        "This is simulation-only: do not perform external writes, active apply, purchases, deletes, or runtime mutation.",
        "",
        "Scenario:",
        f"- title: {title or 'Untitled simulation'}",
        f"- simulation_type: {simulation_type or 'unspecified'}",
        f"- scenario_id: {scenario_id or 'unspecified'}",
        f"- goal: {goal or 'Produce concise JSON-compatible observations and risks'}",
        f"- risk_class: {risk_class or 'operator_guarded'}",
    ]
    if template_ref:
        lines.append(f"- template_ref: {template_ref}")
    lines.extend(
        [
            "",
            "Selected agent roster:",
            *roster,
            "",
            "Current agent assignment:",
            f"- agent: {plan.agent_name}",
            f"- node_id: {plan.node_id}",
            f"- skill: {plan.skill_name}",
            f"- role: {role or 'participant'}",
            "",
            "Simulation bounds:",
            f"- actors: {len(actors)}",
            f"- policies: {len(policies)}",
            f"- capabilities: {len(capabilities)}",
            f"- steps: {len(spec.get('steps') or [])}{step_types}",
            f"- invariants: {len(invariants)}",
            "",
            "Assessment rubric:",
            "- capability fit: 40",
            "- evidence quality: 25",
            "- risk awareness: 20",
            "- cost and simplicity: 15",
            "",
            "Return concise JSON-compatible output with: agent, role, summary, strengths, risks, score, recommended_next_step.",
        ]
    )
    return "\n".join(lines)


def _live_roster_lines(spec: dict[str, Any], current: LiveKernelInvocationPlan) -> list[str]:
    metadata = spec.get("metadata") if isinstance(spec.get("metadata"), dict) else {}
    source_names = [str(item).strip() for item in metadata.get("source_agent_names") or [] if str(item).strip()]
    source_skills = metadata.get("source_agent_skills") if isinstance(metadata.get("source_agent_skills"), dict) else {}
    actors = [item for item in spec.get("actors") or [] if isinstance(item, dict)]
    invoke_ports = _invoke_ports_by_node(spec)
    lines: list[str] = []
    source_index = 0
    for actor in actors:
        node_id = str(actor.get("id") or "").strip()
        if not node_id or node_id in _COORDINATOR_IDS:
            continue
        label = str(actor.get("label") or "").strip()
        agent_name = label or node_id
        if source_index < len(source_names):
            agent_name = source_names[source_index]
        source_index += 1
        if not agent_name:
            continue
        role = str(actor.get("role") or "participant").strip() or "participant"
        skill = str(source_skills.get(agent_name) or invoke_ports.get(node_id) or "pursue").split(":", 1)[-1]
        marker = " current" if agent_name == current.agent_name or node_id == current.node_id else ""
        lines.append(f"- {agent_name}: node={node_id}, role={role}, skill={skill or 'pursue'}{marker}")
    if not lines:
        lines.append(
            f"- {current.agent_name}: node={current.node_id}, role={current.role or 'participant'}, skill={current.skill_name} current"
        )
    return lines


def _step_type_summary(spec: dict[str, Any]) -> str:
    steps = [item for item in spec.get("steps") or [] if isinstance(item, dict)]
    if not steps:
        return ""
    counts = Counter(str(step.get("type") or "unknown") for step in steps)
    summary = ", ".join(
        f"{kind} x{count}" if count > 1 else kind
        for kind, count in counts.most_common(8)
    )
    return f" ({summary})" if summary else ""


def _live_argument_value_for_key(
    key: str,
    field_schema: dict[str, Any],
    context: dict[str, str],
) -> Any:
    normalized = key.lower().replace("-", "_")

    typ = field_schema.get("type")
    if isinstance(typ, list):
        typ = next((item for item in typ if item != "null"), typ[0] if typ else None)
    if typ != "string" and field_schema.get("format") not in {"email", "uri"}:
        return None

    if normalized in {
        "prompt",
        "goal",
        "instruction",
        "query",
        "question",
        "task",
        "request",
        "message",
        "text",
        "topic",
        "brief",
    }:
        return context["prompt"]
    if normalized in context:
        return context[normalized]
    if normalized.endswith("_url") or normalized in {"url", "uri", "website", "company_url"}:
        return "https://example.com"
    if normalized in {"html", "body_html", "preview_html"}:
        return (
            f"<h1>{context['title'] or 'Kernel simulation'}</h1>"
            f"<p>{context['prompt']}</p>"
        )
    if normalized in {"body", "content", "description", "summary", "report"} or normalized.endswith("_brief"):
        return context["prompt"]
    if normalized in {"subject", "title"}:
        return context["title"] or "Kernel simulation"
    if normalized in {"company", "account", "target", "market", "segment"}:
        return context["title"] or context["simulation_type"] or "kernel simulation"
    return None


def _result_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict) and "result" in raw:
        result = raw.get("result")
    else:
        result = raw
    if isinstance(result, dict):
        return result
    return {"value": result}


def _events_payload(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict) and isinstance(raw.get("events"), list):
        return [_json_boundary(event) for event in raw["events"] if isinstance(event, dict)]
    return []


def _result_is_error(result: dict[str, Any]) -> bool:
    if result.get("error"):
        return True
    status = str(result.get("status") or "").lower()
    return status in {"error", "failed", "failure"}


def _json_boundary(value: Any) -> Any:
    if isinstance(value, dict):
        return _truncate_json({str(key): _json_boundary(item) for key, item in value.items()})
    if isinstance(value, list):
        return _truncate_json([_json_boundary(item) for item in value])
    if isinstance(value, tuple):
        return _truncate_json([_json_boundary(item) for item in value])
    if isinstance(value, str):
        return value[:_MAX_STRING_CHARS]
    if isinstance(value, int | float | bool) or value is None:
        return value
    return str(value)[:_MAX_STRING_CHARS]


def _truncate_json(value: Any) -> Any:
    encoded = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
    if len(encoded) <= _MAX_JSON_CHARS:
        return value
    return {
        "truncated": True,
        "sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "preview": encoded[:_MAX_STRING_CHARS],
    }
