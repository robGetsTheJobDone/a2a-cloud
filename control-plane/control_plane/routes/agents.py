from __future__ import annotations

import asyncio
import base64
import hashlib
from contextlib import ExitStack
import importlib.util
import io
import json
import logging
import math
import os
import re
import secrets
import sys
import tempfile
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import quote, urlparse, urlunparse

from a2a_pack import (
    AgentDsl,
    DagLimits,
    DagNode,
    ResolvedSubAgent,
    execute_dag_nodes,
    parse_dag,
    sanitize_meta_dag,
)
from a2a_pack.receipts import seal_receipt
import httpx
from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, desc, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..argo import (
    ArgoApplicationConflict,
    delete_application,
    delete_repo_secret,
    ensure_application,
    ensure_repo_secret,
)
from .. import agent_secrets as secret_store
from .. import devbox
from ..agent_access import (
    AgentBYOKRequired,
    account_access_policy,
    account_access_usage,
    resolve_account_llm_access,
)
from ..agent_review import enqueue_deploy_review
from ..agent_authorization import (
    active_organization_ids,
    require_agent_access,
    visible_agents_clause,
)
from ..agent_seo import enqueue_agent_seo_profile
from ..agent_search import (
    SemanticAgentMatch,
    SemanticAgentSearch,
    lexical_agent_score,
)
from ..auth import (
    AGENT_RUNTIME_TOKEN_MAX_TTL_SECONDS,
    agent_invoke_claims,
    _credential_token,
    current_user,
    current_user_or_agent_invoke,
    current_user_or_studio_job,
    issue_invocation_cp_credential,
    issue_runtime_cp_credential,
    optional_current_user,
    studio_job_claims,
    user_from_token,
)
from ..consumer_setup import (
    ConsumerSetupRequired,
    require_consumer_setup,
    resolve_consumer_setup,
    setup_required_payload,
)
from ..config import settings
from ..database_bindings import reconcile_agent_database_bindings
from ..database_resources import read_agent_database_declarations_from_tarball
from ..mailbox_resources import (
    agent_mailbox_address,
    read_agent_mailbox_declaration_from_tarball,
    reconcile_agent_mailbox,
)
from ..mailu_client import delete_agent_mailbox_best_effort
from ..db import get_session
from ..deployments import (
    ACTIVE_DEPLOY_STATUSES,
    TRANSIENT_AGENT_STATUSES,
    collect_deployment_logs,
    create_deployment,
    deployment_events,
    fail_deployment,
    latest_deployment_for_agent,
    record_deployment_event,
    sync_deployment_verification,
    utcnow,
)
from ..imported_agent_auth import (
    ExternalRequestAuth,
    ImportedAgentAuthError,
    detect_auth_requirements,
    resolve_request_auth,
    select_requirement_for_setup,
    upsert_connection,
)
from ..gitea import (
    GITEA_USER,
    RepoBaseMissingError,
    RepoDriftCheckError,
    delete_repo,
    ensure_repo,
    ensure_repo_push_webhook,
    ensure_runtime_repo_actions_secrets,
    ensure_runtime_repo_push_webhook,
    repo_web_url,
    repo_head_sha,
    repo_exists,
    runtime_repo_name,
    set_repo_visibility,
    source_changed_paths_since,
    source_tarball_from_repo,
)
from ..gitea_provisioning import gitea_owner_for_agent, resolve_agent_gitea_workspace
from ..job_wake import nudge_agent_api_worker
from ..k8s import (
    PLATFORM_RESERVED_AGENT_NAMES,
    custom_domain_certificate_status,
    deploy_agent,
    delete_agent as delete_k8s_agent,
    sync_custom_domain_ingress,
)
from ..agent_secrets import delete_agent_runtime_secret
from ..card_cache import invalidate_agent_card, warm_agent_card
from ..models import (
    Agent,
    AgentApiToken,
    AgentAuthConnection,
    AgentCodeEditorOptIn,
    AgentConsumerSetupValue,
    AgentCustomDomain,
    AgentDatabaseBinding,
    AgentDeployment,
    AgentDeploymentEvent,
    AgentDeploymentLog,
    AgentInstall,
    AgentMailbox,
    AgentMemoryEntry,
    AgentProofRun,
    AgentReceipt,
    AgentReviewRun,
    AgentSecret,
    AgentSession,
    Bounty,
    ChatThreadEmailLink,
    DatabaseProvisionEvent,
    GrantAudit,
    MailboxProvisionEvent,
    MetaAgentRun,
    TrialRun,
    User,
    UserLLMCreds,
    WorkEvent,
    WorkJob,
)
from ..grants import mint_grant_token
from ..minio_client import bucket_for_user, iter_file, stat_file, upload_file
from ..openapi_agent import (
    GeneratedOpenAPIAgent,
    OpenAPIGenerationError,
    build_openapi_agent_source,
    fetch_openapi_spec,
    source_tarball_bytes,
)
from ..repo_mounts import RepoMount, parse_repo_mounts
from ..meta_agent import (
    GeneratedMetaAgent,
    MetaAgentGenerationError,
    build_meta_agent_source,
)
from ..agent_proofs import card_hash
from ..analytics import track_event
from ..runtime_upgrade import (
    bump_agent_runtime_repo,
    runtime_upgrade_image_tag,
    runtime_upgrade_status,
)
from ..safe_http import (
    SafeHTTPError,
    SafeHTTPTimeoutError,
    UnsafeURLError,
    safe_fetch_url,
    safe_request_url,
)
from ..scaffold import (
    AVAILABILITY_ALWAYS_ON,
    AVAILABILITY_ON_DEMAND,
    commit_and_push_runtime,
    commit_and_push_source,
    read_runtime_availability_from_tarball,
)
from ..source_push_deployments import SourcePushDeployBlocked, deploy_source_push
from ..subagent_runs import SubagentRunRecorder
from ..template_updates import enqueue_template_update_job
from ..work_ledger import (
    append_event,
    complete_job,
    create_job,
    fail_job,
    get_job,
    serialize_event,
    serialize_job,
)
from .agent_auth import ensure_auth_placeholders, refresh_auth_card_status
from ..schemas import (
    AgentCodeEditorOut,
    AgentComposeIn,
    AgentComposePreviewOut,
    AgentCustomDomainIn,
    AgentCustomDomainOut,
    AgentDeploymentEventOut,
    AgentDeploymentLogOut,
    AgentDeploymentLogsOut,
    AgentDeploymentOut,
    AgentDetailOut,
    AgentFromSourceIn,
    AgentFromSourceOut,
    AgentFromTarballOut,
    AgentFromComposeOut,
    AgentFromOpenAPIOut,
    AgentImportAuthIn,
    AgentImportIn,
    AgentImportOut,
    AgentMCPOut,
    AgentMineOut,
    AgentMineSummaryOut,
    AgentOpenAPIGenerateIn,
    AgentOpenAPIPreviewOut,
    AgentSearchOut,
    AgentOut,
    AgentRegisterIn,
    AgentTemplateUpdateOut,
)


log = logging.getLogger(__name__)
AGENT_DESCRIPTION_MAX = 1024
#: Lifetime of the ``A2A_CP_JWT`` an agent's pod holds. Only a redeploy
#: re-mints it, so it cannot be call-length — but it is no longer a session:
#: see :func:`control_plane.auth.issue_agent_runtime_token`.
_RUNTIME_CP_JWT_TTL_SECONDS = AGENT_RUNTIME_TOKEN_MAX_TTL_SECONDS


class AgentApiInvokeFailed(HTTPException):
    """An invoke that failed at the agent, not inside the control plane.

    It is an ``HTTPException`` so the app's own error handler renders it: this
    used to be a bare ``RuntimeError`` that nothing caught, so every agent
    failure — a 422 with a usable validation message included — reached the
    caller as an opaque platform 500 with the agent's status and body gone.
    ``detail`` carries the agent, the skill, the agent's own status code, and a
    bounded redacted excerpt of what the agent said.
    """

    def __init__(
        self,
        detail: dict[str, Any],
        *,
        status_code: int,
        grant_id: str | None = None,
        agent_status: int | None = None,
    ) -> None:
        super().__init__(status_code, detail)
        self.grant_id = grant_id
        self.agent_status = agent_status


#: Characters of an agent's error body parsed as JSON, characters scanned when
#: it is not JSON, and characters of the redacted excerpt kept. Together they
#: bound what a hostile agent can push into a signed receipt, a log line, and
#: the platform's own error envelope.
_AGENT_ERROR_PARSE_LIMIT = 64 * 1024
_AGENT_ERROR_BODY_LIMIT = 4096
_AGENT_ERROR_EXCERPT_LIMIT = 512


def _redacted_agent_excerpt(
    text: str,
    *,
    limit: int = _AGENT_ERROR_EXCERPT_LIMIT,
) -> str:
    """A bounded, redacted excerpt of an untrusted agent response body.

    The agent-ingress gateway already owns the platform's secret redactor;
    reuse it rather than growing a second set of patterns that would drift.
    The excerpt is always returned as a flat string, so an agent cannot inject
    keys into the platform's own error envelope.

    Which branch runs is chosen by the *agent*, so neither may be weaker than
    the other: a padded body used to skip the JSON parse and reach the caller
    unredacted, and a key quoted inside a JSON string used to slip past a
    redactor that only tests whole values. Both now finish in
    :func:`~control_plane.agent_ingress._scrub_untrusted_text`, which applies
    the key-based and the shape-based rule to the rendered text and strips
    control characters, so no shape of input reaches an unscrubbed emit.
    """
    from ..agent_ingress import _preview as _redacted_preview
    from ..agent_ingress import _redact as _redact_untrusted
    from ..agent_ingress import _scrub_untrusted_text

    raw = str(text or "").strip()
    if not raw:
        return ""
    value: Any = None
    parsed = False
    if len(raw) <= _AGENT_ERROR_PARSE_LIMIT:
        # Parse whole or not at all: truncating first would break the JSON and
        # cost the redactor its structural walk. The limit is a CPU bound, not
        # a rule change - an unparsed body still gets every rule below.
        try:
            value = json.loads(raw)
            parsed = True
        except ValueError:
            parsed = False
    if parsed:
        # ``_preview`` walks the structure, redacts by key, and JSON-encodes.
        rendered = _redacted_preview(value, limit=limit)
    else:
        # Not JSON, or too big to parse. Scrub first so control characters are
        # gone before the split, then let ``_redact`` judge each whole token.
        rendered = " ".join(
            str(_redact_untrusted(token))
            for token in _scrub_untrusted_text(raw[:_AGENT_ERROR_BODY_LIMIT]).split()
        )
    rendered = _scrub_untrusted_text(rendered)
    return rendered if len(rendered) <= limit else rendered[: limit - 1] + "…"


def _agent_invoke_message(detail: dict[str, Any]) -> str:
    """One actionable sentence: which agent, which skill, what it said."""
    agent_name = str(detail.get("agent") or "")
    skill_name = str(detail.get("skill") or "")
    target = ".".join(part for part in (agent_name, skill_name) if part) or "the agent"
    reason = str(detail.get("error") or "agent_error")
    said = str(detail.get("agent_error") or "")
    if reason == "agent_timeout":
        timeout = detail.get("timeout_seconds")
        window = f" within {timeout:g}s" if isinstance(timeout, (int, float)) else ""
        return f"{target} did not respond{window}"
    if reason == "agent_unreachable":
        return f"{target} could not be reached ({said or 'connection failed'})"
    if reason == "agent_bad_response":
        return f"{target} returned a body the platform could not parse: {said}"
    agent_status = detail.get("agent_status")
    said = said or "<empty response body>"
    if agent_status is None:
        return f"{target} returned an error: {said}"
    return f"{target} returned HTTP {agent_status}: {said}"


def agent_invoke_error_text(exc: BaseException) -> str:
    """One line a human can read out of an invoke failure.

    ``str()`` on an ``HTTPException`` is ``"502: {'error': ...}"`` — a Python
    repr. Surfaces that render a failure as text want the sentence instead.
    """
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict) and isinstance(detail.get("message"), str):
        return detail["message"]
    if isinstance(detail, str) and detail:
        return detail
    return str(exc)


def _agent_error_status(agent_status: int) -> int:
    """The status the platform answers with when an agent answered ``agent_status``.

    A 4xx is the agent's statement about *this request*, so passing it through
    is both honest and the only way the caller can fix the call. A 5xx says the
    agent itself broke; re-emitting that as the control plane's own 500 is
    exactly the lie this path used to tell, and 502 is the accurate one. Either
    way the agent's own code survives verbatim in ``detail.agent_status``.
    """
    return agent_status if 400 <= agent_status < 500 else 502


def _agent_invoke_error_response(description: str) -> dict[str, Any]:
    return {
        "description": description,
        "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}
        },
    }


#: The failures the *control plane itself* picks on an invoke, declared on the
#: routes because nothing else can find them. ``openapi._function_facts`` reads
#: literal ``HTTPException(<int>, …)`` calls, so a status raised through
#: :func:`_agent_invoke_error` is invisible to it - and ``openapi.py`` states
#: that "not documented means the code cannot produce it". Leaving these off
#: the document would make that statement false for the two statuses this route
#: chooses. A relayed agent 4xx stays open-ended by nature and is tagged
#: instead: ``detail.error == "agent_error"`` with the agent's own
#: ``detail.agent_status``.
_AGENT_INVOKE_RESPONSES: dict[int | str, dict[str, Any]] = {
    502: _agent_invoke_error_response(
        "The agent could not be reached, or answered with a 5xx of its own. "
        "`detail.agent_status` carries the agent's code when it sent one; its "
        "absence means nothing was observed from the agent at all."
    ),
    504: _agent_invoke_error_response(
        "The agent did not answer within the invoke timeout. No response was "
        "observed, so nothing is claimed about the agent's own status."
    ),
}


def _agent_invoke_error(
    *,
    agent_name: str,
    skill_name: str,
    reason: str,
    status_code: int,
    agent_status: int | None = None,
    excerpt: str = "",
    timeout_seconds: float | None = None,
    grant_id: str | None = None,
) -> AgentApiInvokeFailed:
    detail: dict[str, Any] = {
        "error": reason,
        "agent": agent_name,
        "skill": skill_name,
    }
    if agent_status is not None:
        detail["agent_status"] = agent_status
    if excerpt:
        detail["agent_error"] = excerpt
    if timeout_seconds is not None:
        detail["timeout_seconds"] = timeout_seconds
    detail["message"] = _agent_invoke_message(detail)
    return AgentApiInvokeFailed(
        detail,
        status_code=status_code,
        grant_id=grant_id,
        agent_status=agent_status,
    )


def _canonical_url(name: str) -> str:
    """Public HTTPS URL for an agent, derived purely from settings.

    Single source of truth so stale rows (e.g. older nip.io hosts) get
    rewritten on the next read. Cert-manager mints per-host LE certs
    against the same template via the agent repo's ingress TLS block.
    """
    return f"https://{settings.ingress_host_template.format(name=name)}"


def _reject_unsafe_user_card(card: dict[str, Any]) -> None:
    runtime = card.get("runtime") if isinstance(card, dict) else None
    llm_mode = runtime.get("llm_provisioning") if isinstance(runtime, dict) else None
    if llm_mode == "platform" and not settings.allow_user_platform_llm:
        raise HTTPException(
            400,
            "user-deployed agents cannot use runtime.llm_provisioning=platform; "
            "enable the LiteLLM A2A grant auth path or use caller_provided",
        )
    if isinstance(runtime, dict) and runtime.get("grant_signing") is True:
        raise HTTPException(
            400,
            "user-deployed agents cannot request runtime.grant_signing; "
            "platform private signing keys are isolated from user workloads",
        )


def _provision_runtime_cp_jwt_if_requested(
    *,
    dsl: AgentDsl,
    agent: Agent,
    user: User,
) -> None:
    """Write the agent's pod-resident control-plane credential.

    This value is persisted in the runtime secret and lands in the agent's
    environment, so it survives every log line, crash dump, and shell an
    operator or the agent's own code opens in that pod. It is therefore minted
    as an agent-scoped runtime token, not as a platform session: a leaked copy
    addresses this one agent on the invoke callback surface and is rejected
    outright by ``current_user``.

    The class is chosen by :func:`issue_runtime_cp_credential` for every agent,
    including the platform's own build specialists — see there for why the
    ``PLATFORM_TOOLCHAIN_AGENTS`` carve-out that
    :func:`issue_invocation_cp_credential` applies to the per-call credential
    deliberately does not apply to this one.
    """
    runtime = getattr(dsl, "runtime", None)
    if not bool(getattr(runtime, "wants_cp_jwt", False)):
        return
    secret_store.upsert_agent_secret_value(
        agent_name=agent.name,
        key="A2A_CP_JWT",
        value=issue_runtime_cp_credential(
            user.id,
            agent=agent.name,
            ttl_seconds=_RUNTIME_CP_JWT_TTL_SECONDS,
        ),
        owner_id=user.id,
    )


def _runtime_availability_from_card(card: dict[str, Any] | None) -> str:
    runtime = card.get("runtime") if isinstance(card, dict) else None
    raw = runtime.get("availability") if isinstance(runtime, dict) else None
    availability = str(raw or AVAILABILITY_ON_DEMAND).strip().lower()
    if availability not in {AVAILABILITY_ON_DEMAND, AVAILABILITY_ALWAYS_ON}:
        raise HTTPException(
            400,
            "runtime.availability must be 'on_demand' or 'always_on'",
        )
    return availability


def _approved_always_on(agent: Agent | None) -> bool:
    return (
        agent is not None
        and _runtime_availability_from_card(agent.card) == AVAILABILITY_ALWAYS_ON
    )


def _set_card_runtime_availability(card: dict[str, Any], availability: str) -> None:
    runtime = card.get("runtime")
    if not isinstance(runtime, dict):
        runtime = {}
        card["runtime"] = runtime
    runtime["availability"] = availability


def _assert_can_request_availability(
    user: User,
    availability: str,
    *,
    existing: Agent | None = None,
) -> bool:
    """Return whether scaffold may honor ``always_on`` for this deploy."""
    if availability != AVAILABILITY_ALWAYS_ON:
        return _approved_always_on(existing) or bool(user.is_admin)
    if user.is_admin or _approved_always_on(existing):
        return True
    raise HTTPException(
        403,
        "runtime.availability=always_on requires site admin provisioning",
    )


def _public_repo_url(name: str, *, owner: str | None = None) -> str:
    """Human-facing Gitea repo URL without embedded credentials."""
    return repo_web_url(name, owner=owner)


def _ensure_runtime_repo(agent_name: str, description: str) -> tuple[str, str]:
    # Agents deploy as Knative Services. Migration off any Deployment-era core
    # resources is handled by Argo prune on the GitOps path and by
    # ``delete_legacy_deployment_agent`` on the direct-apply path; this stamping
    # step must never delete the target Knative Service.
    runtime_name = runtime_repo_name(agent_name)
    push_url, internal_url = ensure_repo(
        runtime_name,
        description or f"runtime for {agent_name}",
    )
    ensure_runtime_repo_actions_secrets(runtime_name)
    if settings.in_cluster or settings.kubeconfig:
        ensure_repo_secret(f"gitea-repo-{agent_name}", internal_url)
        try:
            ensure_application(agent_name, internal_url)
        except ArgoApplicationConflict as exc:
            raise HTTPException(409, str(exc)) from exc
    if settings.gitea_runtime_webhooks_enabled:
        ensure_runtime_repo_push_webhook(
            runtime_name,
            url=settings.gitea_runtime_webhook_url,
            secret=settings.gitea_runtime_webhook_secret,
        )
    return push_url, internal_url


def _ensure_source_repo_push_webhook(name: str, *, owner: str | None) -> None:
    if not settings.gitea_source_webhooks_enabled:
        return
    ensure_repo_push_webhook(
        name,
        owner=owner,
        url=settings.gitea_source_webhook_url,
        secret=settings.gitea_source_webhook_secret,
    )


async def _resolve_source_repo_scope(
    session: AsyncSession,
    user: User,
    *,
    existing: Agent | None = None,
    organization_slug: str | None = None,
    source: str,
) -> tuple[int | None, str | None]:
    if existing is not None and existing.organization_id is not None:
        owner = await gitea_owner_for_agent(
            session,
            user,
            existing.organization_id,
        )
        return existing.organization_id, owner
    if existing is not None and existing.gitea_owner:
        return existing.organization_id, existing.gitea_owner
    try:
        org, workspace = await resolve_agent_gitea_workspace(
            session,
            user,
            organization_slug=organization_slug,
            source=source,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return org.id, workspace.gitea_org_name


def _agent_mine_out(
    agent: Agent,
    latest_deployment: AgentDeploymentOut | None = None,
    openapi_source_urls: list[str] | None = None,
    code_editor: AgentCodeEditorOut | None = None,
) -> AgentMineOut:
    data = AgentOut.model_validate(agent).model_dump()
    data["status"] = _agent_status_for_latest_deployment(
        agent.status,
        latest_deployment,
    )
    data["repo_url"] = _public_repo_url(agent.name, owner=agent.gitea_owner)
    data["runtime_upgrade"] = _runtime_upgrade_status_for_agent(
        agent,
        latest_deployment=latest_deployment,
    )
    data["latest_deployment"] = latest_deployment
    source_urls = openapi_source_urls or _openapi_source_urls_from_card(agent)
    data["openapi_source"] = (
        {"url": source_urls[0], "urls": source_urls, "regenerable": True}
        if source_urls
        else None
    )
    data["code_editor"] = code_editor or _agent_code_editor_out(agent, None)
    from ..self_healing import public_policy_from_card

    data["self_healing"] = public_policy_from_card(
        agent.card if isinstance(agent.card, dict) else None
    )
    return AgentMineOut(**data)


def _agent_mine_summary_out(
    agent: Agent,
    latest_deployment: AgentDeploymentOut | None = None,
) -> AgentMineSummaryOut:
    skills = []
    if isinstance(agent.card, dict):
        raw_skills = agent.card.get("skills")
        if isinstance(raw_skills, list):
            skills = raw_skills
    return AgentMineSummaryOut(
        id=agent.id,
        name=agent.name,
        description=agent.description,
        version=agent.version,
        public=agent.public,
        status=_agent_status_for_latest_deployment(agent.status, latest_deployment),
        url=agent.url,
        created_at=agent.created_at,
        skill_count=len(skills),
        runtime_upgrade=_runtime_upgrade_status_for_agent(
            agent,
            latest_deployment=latest_deployment,
        ),
        latest_deployment=latest_deployment,
    )


def _agent_status_for_latest_deployment(
    status: str,
    latest_deployment: AgentDeploymentOut | None,
) -> str:
    if status not in TRANSIENT_AGENT_STATUSES or latest_deployment is None:
        return status
    if latest_deployment.status == "live":
        return "running"
    if latest_deployment.status == "failed":
        return "failed"
    return status


def _agent_code_editor_workspace_key(agent: Agent) -> str:
    owner = agent.gitea_owner or "default"
    return f"{owner}/{agent.name}"


def _agent_code_editor_runtime() -> dict[str, Any]:
    return {
        "mode": "shared",
        "codegraph": {
            "index_scope": "target_repo_workspace",
            "mcp": "codegraph serve --mcp --no-watch --path <workspace>",
        },
    }


def _first_party_repo_mount(name: str) -> RepoMount | None:
    target = name.strip()
    if not target:
        return None
    for mount in parse_repo_mounts(settings.repo_mounts, default_owner=GITEA_USER):
        if mount.repo == target:
            return mount
    return None


def _first_party_repo_code_editor_out(name: str, user: User) -> AgentCodeEditorOut | None:
    mount = _first_party_repo_mount(name)
    if mount is None:
        return None
    if not user.is_admin:
        raise HTTPException(403, "admin role required for first-party repo code editor")
    return AgentCodeEditorOut(
        target_agent_name=mount.repo,
        enabled=True,
        status="enabled",
        shared_agent_name=settings.code_editor_agent_name,
        target_repo_url=_public_repo_url(mount.repo, owner=mount.owner),
        workspace_key=f"{mount.owner}/{mount.repo}",
        runtime=_agent_code_editor_runtime(),
    )


def _agent_code_editor_out(
    agent: Agent,
    opt_in: AgentCodeEditorOptIn | None,
) -> AgentCodeEditorOut:
    enabled = opt_in is not None and opt_in.status == "enabled"
    return AgentCodeEditorOut(
        target_agent_name=agent.name,
        enabled=enabled,
        status=opt_in.status if opt_in is not None else "disabled",
        shared_agent_name=(
            opt_in.shared_agent_name
            if opt_in is not None
            else settings.code_editor_agent_name
        ),
        target_repo_url=_public_repo_url(agent.name, owner=agent.gitea_owner),
        workspace_key=opt_in.workspace_key if opt_in is not None else None,
        last_error=opt_in.last_error if opt_in is not None else None,
        enabled_at=opt_in.created_at if opt_in is not None else None,
        updated_at=opt_in.updated_at if opt_in is not None else None,
        runtime=_agent_code_editor_runtime(),
    )


def _openapi_source_urls_from_card(agent: Agent) -> list[str]:
    card = agent.card if isinstance(agent.card, dict) else {}
    capabilities = card.get("capabilities")
    if not isinstance(capabilities, dict):
        return []
    openapi_auto = capabilities.get("openapi_auto_agent")
    if not isinstance(openapi_auto, dict):
        return []
    values = openapi_auto.get("source_openapi_urls")
    if isinstance(values, list):
        urls = [str(value).strip() for value in values if str(value).strip()]
        if urls:
            return urls
    value = openapi_auto.get("source_openapi_url")
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    default_base_url = openapi_auto.get("default_base_url")
    if isinstance(default_base_url, str):
        parsed = urlparse(default_base_url.strip())
        if parsed.scheme in {"http", "https"} and parsed.netloc.endswith(settings.platform_host_suffix):
            path = parsed.path.rstrip("/") + "/openapi.json"
            return [urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))]
    return []


router = APIRouter(prefix="/v1/agents", tags=["agents"])
_MAX_TARBALL_BYTES = int(os.environ.get("A2A_CP_MAX_AGENT_TARBALL_BYTES", str(50 * 1024 * 1024)))
_EXTERNAL_IMAGE_PREFIX = "external-a2a:"
_EXTERNAL_AUTH_SECRET_KEY = "EXTERNAL_A2A_AUTH_VALUE"
_AGENT_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?$")
_CARD_PATHS = ("/.well-known/agent-card", "/.well-known/agent-card.json")
_MAX_AGENT_CARD_BYTES = 1024 * 1024
_MAX_EXTERNAL_AGENT_RESPONSE_BYTES = 5 * 1024 * 1024
_HTTP_AUTH_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9!#$%&'*+.^_`|~-]*$")
_CUSTOM_DOMAIN_VERIFICATION_PREFIX = "_a2a-agent"
_CUSTOM_DOMAIN_VERIFICATION_VALUE_PREFIX = "a2a-agent-verification="
_CUSTOM_DOMAIN_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_AGENT_SEARCH_LIMIT_MAX = 25
_AGENT_SEARCH_BOOTSTRAP_LIMIT = 10_000
semantic_agent_search = SemanticAgentSearch.from_settings()
_agent_search_index_bootstrapped = False
_agent_search_index_bootstrap_lock = asyncio.Lock()
_AGENT_API_TOKEN_PREFIX = "a2a_app_"
_AGENT_API_TOKEN_BYTES = 32
_AGENT_API_DEFAULT_TTL_SECONDS = 15 * 60
_AGENT_API_DEFAULT_INVOKE_TIMEOUT_SECONDS = int(
    os.environ.get("A2A_AGENT_API_INVOKE_TIMEOUT_SECONDS", str(_AGENT_API_DEFAULT_TTL_SECONDS))
)
_AGENT_API_INVOKE_TIMEOUT_GRACE_SECONDS = int(
    os.environ.get("A2A_AGENT_API_INVOKE_TIMEOUT_GRACE_SECONDS", "30")
)
_AGENT_API_RUN_KIND = "agent_api_invoke"
# Use an immutable image tag here. Sandbox runtimes can cache ``latest`` long
# enough to keep using an older base image after the registry tag moves.
_COMPOSE_VALIDATION_DEFAULT_IMAGE = (
    f"{settings.base_image_repo}:0.1.92"
)


def _positive_env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


# Integration links are long-lived shared credentials, so they always expire.
# The URL-token variant is deliberately shorter lived: it can leak through
# access logs, browser history and ``Referer`` headers.
_AGENT_INTEGRATION_LINK_TTL_DAYS = _positive_env_int(
    "A2A_AGENT_INTEGRATION_LINK_TTL_DAYS", 90
)
_AGENT_INTEGRATION_LINK_URL_TOKEN_TTL_DAYS = _positive_env_int(
    "A2A_AGENT_INTEGRATION_LINK_URL_TOKEN_TTL_DAYS", 30
)
_AGENT_INTEGRATION_LINK_MAX_TTL_DAYS = _positive_env_int(
    "A2A_AGENT_INTEGRATION_LINK_MAX_TTL_DAYS", 365
)
# Links minted before expiry was mandatory keep working, but the first time one
# is listed or used it is given a finite window instead of being revoked.
_AGENT_INTEGRATION_LINK_LEGACY_GRACE_DAYS = _positive_env_int(
    "A2A_AGENT_INTEGRATION_LINK_LEGACY_GRACE_DAYS", 90
)
_INTEGRATION_LINK_LABEL_SUFFIX = " integration link"
_INTEGRATION_LINK_URL_TOKEN_SUFFIX = " (URL token)"


def _integration_link_label(agent_name: str, *, url_token: bool = False) -> str:
    """The ``name`` this route stamps on every link it mints."""
    label = f"{agent_name}{_INTEGRATION_LINK_LABEL_SUFFIX}"
    return f"{label}{_INTEGRATION_LINK_URL_TOKEN_SUFFIX}" if url_token else label


_URL_TOKEN_SECURITY_NOTICE = (
    "Lower security: this token is embedded in the URL query string, so it can "
    "leak through proxy and web-server access logs, browser history, Referer "
    "headers and shared screenshots. Only use it for MCP or OpenAPI clients "
    "that cannot send an Authorization header, and rotate it often. Prefer the "
    "header form: Authorization: Bearer <token>."
)


class AgentApiTokenCreateIn(BaseModel):
    name: str = Field(default="Default API token", min_length=1, max_length=160)
    expires_at: datetime | None = None
    scopes: list[str] = Field(default_factory=lambda: ["invoke"], max_length=16)


class AgentApiTokenOut(BaseModel):
    id: int
    agent_name: str
    name: str
    token_last4: str
    scopes: list[str]
    enabled: bool
    expires_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AgentApiTokenCreatedOut(AgentApiTokenOut):
    token: str


class AgentIntegrationLinkCreateIn(BaseModel):
    url_token: bool = Field(
        default=False,
        description=(
            "Opt in to the lower-security form that embeds the token in the URL "
            "query string. Only needed for MCP/OpenAPI clients that cannot send "
            "an Authorization header."
        ),
    )
    expires_in_days: int | None = Field(
        default=None, ge=1, le=_AGENT_INTEGRATION_LINK_MAX_TTL_DAYS
    )


class AgentIntegrationLinkOut(AgentApiTokenOut):
    # Listing a link never has the secret to embed, so these URLs are always
    # the clean ones. Deliberately no ``token_placement`` here: the placement a
    # link was minted with is not persisted, so any value on a listed row would
    # be a guess about a credential the caller already holds elsewhere. It is
    # reported only where it is known - on the create response.
    openapi_url: str
    invoke_base_url: str
    sample_invoke_url: str | None = None
    mcp_url: str


class AgentIntegrationLinkCreatedOut(AgentIntegrationLinkOut):
    token: str
    curl_example: str
    # ``header`` means the URLs above carry no secret and the caller must send
    # ``Authorization: Bearer <token>``. ``url_query`` means the token is baked
    # into the URLs and they must be treated as secrets.
    token_placement: str = "header"
    auth_header: str = "Authorization: Bearer <token>"
    security_notice: str | None = None


class AgentAccountAccessOut(BaseModel):
    agent: str
    required: bool
    platform_skill_calls: int
    platform_skill_calls_used: int
    platform_skill_calls_remaining: int
    after_trial: str
    has_byok: bool
    setup_url: str


async def _copy_upload_to_temp(upload: UploadFile, *, max_bytes: int) -> tuple[str, int]:
    total = 0
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz")
    try:
        with tmp:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(413, f"upload exceeds {max_bytes} bytes")
                tmp.write(chunk)
    except Exception:
        try:
            os.unlink(tmp.name)
        except FileNotFoundError:
            pass
        raise
    return tmp.name, total


def _is_external_agent(agent: Agent) -> bool:
    return isinstance(agent.image, str) and agent.image.startswith(_EXTERNAL_IMAGE_PREFIX)


def _external_image(base_url: str) -> str:
    value = f"{_EXTERNAL_IMAGE_PREFIX}{base_url}"
    if len(value) > 512:
        raise HTTPException(400, "agent URL is too long")
    return value


def _external_base_url(agent: Agent) -> str:
    if _is_external_agent(agent):
        return agent.image.removeprefix(_EXTERNAL_IMAGE_PREFIX)
    return agent.url or ""


def _normalize_agent_base_url(raw_url: str) -> str:
    value = raw_url.strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(400, "url must start with http:// or https://")
    if not parsed.netloc:
        raise HTTPException(400, "url must include a host")
    if parsed.query or parsed.fragment:
        raise HTTPException(400, "url must not include query parameters or a fragment")
    path = parsed.path.rstrip("/")
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def _validate_agent_name(name: str) -> str:
    value = name.strip().lower()
    if not _AGENT_NAME_RE.fullmatch(value):
        raise HTTPException(
            400,
            "name must be a DNS-safe slug: lowercase letters, numbers, and hyphens",
        )
    if value in PLATFORM_RESERVED_AGENT_NAMES:
        raise HTTPException(400, "name is reserved for platform infrastructure")
    return value


def _canonical_host(name: str) -> str:
    return settings.ingress_host_template.format(name=name).strip().lower().rstrip(".")


def _platform_host_suffix() -> str:
    suffix = _canonical_host("{name}").replace("{name}", "").strip(".")
    return suffix or _canonical_host("")


def _normalize_custom_hostname(raw_hostname: str) -> str:
    value = raw_hostname.strip().lower().rstrip(".")
    if "://" in value or "/" in value or "?" in value or "#" in value:
        raise HTTPException(400, "hostname must be a bare DNS name, not a URL")
    if len(value) > 253 or "." not in value:
        raise HTTPException(400, "hostname must be a fully qualified DNS name")
    labels = value.split(".")
    if any(not _CUSTOM_DOMAIN_LABEL_RE.fullmatch(label) for label in labels):
        raise HTTPException(400, "hostname contains an invalid DNS label")
    platform_suffix = _platform_host_suffix()
    if value == platform_suffix or value.endswith("." + platform_suffix):
        raise HTTPException(400, f"use the default {settings.platform_domain} URL directly")
    return value


def _custom_domain_verification_record(hostname: str, token: str) -> tuple[str, str]:
    return (
        f"{_CUSTOM_DOMAIN_VERIFICATION_PREFIX}.{hostname}",
        f"{_CUSTOM_DOMAIN_VERIFICATION_VALUE_PREFIX}{token}",
    )


def _custom_domain_routing_record(agent_name: str, hostname: str) -> tuple[str, str, str]:
    return ("CNAME", hostname, _canonical_host(agent_name))


def _custom_domain_routing_fallback_record(
    agent_name: str,
    hostname: str,
) -> tuple[str, str, str, str] | None:
    if not _is_apex_hostname(hostname):
        return None
    addresses = _dns_host_addresses(_canonical_host(agent_name), ("A",))
    if not addresses:
        return None
    return (
        "A",
        hostname,
        sorted(addresses)[0],
        "Use this when your DNS provider cannot publish an apex CNAME/ALIAS record.",
    )


def _is_apex_hostname(hostname: str) -> bool:
    labels = hostname.split(".")
    return len(labels) == 2 or (
        len(labels) == 3 and len(labels[-2]) <= 3 and labels[-1] == "uk"
    )


def _www_pair_hostname(hostname: str) -> str | None:
    if hostname.startswith("www."):
        return hostname[4:]
    if _is_apex_hostname(hostname):
        return f"www.{hostname}"
    return None


def _certificate_status_for_domain(
    domain: AgentCustomDomain,
    certificate_status: str | None = None,
) -> str:
    if domain.status != "active":
        return "pending"
    return certificate_status or "provisioning"


def _agent_custom_domain_out(
    domain: AgentCustomDomain,
    certificate_status: str | None = None,
) -> AgentCustomDomainOut:
    verify_name, verify_value = _custom_domain_verification_record(
        domain.hostname,
        domain.verification_token,
    )
    record_type, record_name, record_value = _custom_domain_routing_record(
        domain.agent_name,
        domain.hostname,
    )
    fallback = _custom_domain_routing_fallback_record(
        domain.agent_name,
        domain.hostname,
    )
    is_apex = _is_apex_hostname(domain.hostname)
    paired_www = _www_pair_hostname(domain.hostname)
    canonical = domain.canonical_hostname or (
        domain.hostname[4:] if domain.hostname.startswith("www.") else domain.hostname
    )
    redirect_target = (
        f"https://{canonical}"
        if domain.status == "active" and domain.redirect_enabled and canonical != domain.hostname
        else None
    )
    return AgentCustomDomainOut(
        id=domain.id,
        agent_name=domain.agent_name,
        hostname=domain.hostname,
        status=domain.status,
        url=f"https://{domain.hostname}" if domain.status == "active" else None,
        verification_record_name=verify_name,
        verification_record_value=verify_value,
        routing_record_type=record_type,
        routing_record_name=record_name,
        routing_record_value=record_value,
        routing_fallback_record_type=fallback[0] if fallback else None,
        routing_fallback_record_name=fallback[1] if fallback else None,
        routing_fallback_record_value=fallback[2] if fallback else None,
        routing_fallback_reason=fallback[3] if fallback else None,
        dns_setup_kind="apex-flattened-cname" if is_apex else "cname",
        is_apex=is_apex,
        paired_www_hostname=paired_www,
        canonical_hostname=canonical,
        redirect_enabled=bool(domain.redirect_enabled),
        redirect_target_url=redirect_target,
        certificate_status=_certificate_status_for_domain(domain, certificate_status),
        verified_at=domain.verified_at,
        created_at=domain.created_at,
        updated_at=domain.updated_at,
        last_error=domain.last_error,
    )


def _dns_txt_record_matches(name: str, expected: str) -> bool:
    try:
        import dns.resolver
    except ImportError as exc:  # pragma: no cover - depends on runtime image
        raise HTTPException(500, "DNS resolver dependency is not installed") from exc
    try:
        answers = dns.resolver.resolve(name, "TXT")
    except Exception:
        return False
    for answer in answers:
        parts = []
        for raw in getattr(answer, "strings", []):
            parts.append(raw.decode() if isinstance(raw, bytes) else str(raw))
        if not parts:
            parts = [str(answer).strip('"')]
        if "".join(parts).strip().strip('"') == expected:
            return True
    return False


def _dns_cname_points_to(hostname: str, target: str) -> bool:
    try:
        import dns.resolver
    except ImportError as exc:  # pragma: no cover - depends on runtime image
        raise HTTPException(500, "DNS resolver dependency is not installed") from exc
    expected = target.strip().lower().rstrip(".")
    try:
        answers = dns.resolver.resolve(hostname, "CNAME")
    except Exception:
        return _dns_flattened_host_points_to(hostname, expected)
    for answer in answers:
        raw_target = getattr(answer, "target", answer)
        if str(raw_target).strip().lower().rstrip(".") == expected:
            return True
    return False


def _dns_host_addresses(hostname: str, record_types: tuple[str, ...] = ("A", "AAAA")) -> set[str]:
    try:
        import dns.resolver
    except ImportError as exc:  # pragma: no cover - depends on runtime image
        raise HTTPException(500, "DNS resolver dependency is not installed") from exc
    addresses: set[str] = set()
    for record_type in record_types:
        try:
            for answer in dns.resolver.resolve(hostname, record_type):
                addresses.add(str(answer).strip().lower())
        except Exception:
            pass
    return addresses


def _dns_flattened_host_points_to(hostname: str, target: str) -> bool:
    host_addresses = _dns_host_addresses(hostname)
    target_addresses = _dns_host_addresses(target)
    return bool(host_addresses and target_addresses and host_addresses <= target_addresses)


async def _custom_domain_hostnames(
    agent: Agent,
    session: AsyncSession,
) -> list[dict[str, str | bool]]:
    if not agent.public or _is_external_agent(agent):
        return []
    rows = (
        await session.execute(
            select(AgentCustomDomain)
            .where(AgentCustomDomain.agent_id == agent.id)
            .where(AgentCustomDomain.verified_at.is_not(None))
            .order_by(AgentCustomDomain.hostname)
        )
    ).scalars().all()
    return [
        {
            "hostname": row.hostname,
            "canonical_hostname": row.canonical_hostname or row.hostname,
            "redirect_enabled": bool(row.redirect_enabled),
        }
        for row in rows
    ]


async def _activate_paired_custom_domains(
    agent: Agent,
    session: AsyncSession,
    domain: AgentCustomDomain,
) -> None:
    pair = _www_pair_hostname(domain.hostname)
    if pair is None:
        return
    canonical = domain.canonical_hostname or domain.hostname
    rows = (
        await session.execute(
            select(AgentCustomDomain)
            .where(AgentCustomDomain.agent_id == agent.id)
            .where(AgentCustomDomain.hostname == pair)
        )
    ).scalars().all()
    for row in rows:
        if (row.canonical_hostname or row.hostname) != canonical:
            continue
        _record_type, cname_name, cname_value = _custom_domain_routing_record(
            agent.name,
            row.hostname,
        )
        if not _dns_cname_points_to(cname_name, cname_value):
            row.status = "pending"
            row.last_error = f"DNS must point {cname_name} to {cname_value} with CNAME/ALIAS"
            continue
        row.status = "verified"
        row.verified_at = row.verified_at or utcnow()
        row.last_error = None


async def _sync_agent_custom_domain_ingress(
    agent: Agent,
    session: AsyncSession,
) -> None:
    sync_custom_domain_ingress(
        agent.name,
        await _custom_domain_hostnames(agent, session),
    )


def _slugify_agent_name(value: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", value.strip().lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    if not slug:
        slug = "external-agent"
    return slug[:128].strip("-") or "external-agent"


def _card_declares_auth(raw_card: dict[str, Any]) -> bool:
    if any(
        _auth_declaration_is_present(raw_card.get(key))
        for key in (
            "security",
            "securityRequirements",
            "securitySchemes",
            "authentication",
        )
    ):
        return True
    for skill in raw_card.get("skills") or []:
        if not isinstance(skill, dict):
            continue
        if any(
            _auth_declaration_is_present(skill.get(key))
            for key in ("security", "securityRequirements", "authentication")
        ):
            return True
    return False


def _auth_declaration_is_present(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(value)
    if isinstance(value, list):
        return bool(value)
    if isinstance(value, str):
        return bool(value.strip())
    return False


def _normalize_import_auth(
    auth: AgentImportAuthIn | None,
    *,
    card_declares_auth: bool,
) -> tuple[dict[str, Any], str | None]:
    if auth is None or auth.type.strip().lower().replace("-", "_") in {"", "none"}:
        return {"type": "none"}, None

    auth_type = auth.type.strip().lower().replace("-", "_")
    secret_value = (auth.value or "").strip()
    if not secret_value:
        raise HTTPException(400, "auth.value is required when auth.type is set")

    if auth_type in {"bearer", "http"}:
        scheme = (auth.scheme or "Bearer").strip()
        if scheme.lower() == "bearer":
            scheme = "Bearer"
        if not _HTTP_AUTH_SCHEME_RE.fullmatch(scheme):
            raise HTTPException(400, "auth.scheme is not a valid HTTP auth scheme")
        return {"type": "http", "scheme": scheme, "stored": True}, secret_value

    if auth_type in {"api_key", "apikey"}:
        location = (auth.location or "header").strip().lower()
        if location not in {"header", "query"}:
            raise HTTPException(400, "auth.location must be 'header' or 'query'")
        name = (auth.name or "").strip()
        if not name or any(ch in name for ch in "\r\n"):
            raise HTTPException(400, "auth.name is required for API-key auth")
        return {
            "type": "api_key",
            "location": location,
            "name": name,
            "stored": True,
        }, secret_value

    raise HTTPException(400, "auth.type must be one of: none, bearer, http, api_key")


def _external_request_auth(
    auth: dict[str, Any] | None,
    secret_value: str | None,
) -> tuple[dict[str, str], dict[str, str]]:
    if not isinstance(auth, dict) or auth.get("type") in {None, "none"}:
        return {}, {}
    if not secret_value:
        raise RuntimeError("imported agent auth credential is missing")

    auth_type = str(auth.get("type") or "").lower()
    if auth_type == "http":
        scheme = str(auth.get("scheme") or "Bearer").strip()
        if not scheme:
            raise RuntimeError("imported agent HTTP auth scheme is missing")
        return {"authorization": f"{scheme} {secret_value}"}, {}

    if auth_type == "api_key":
        name = str(auth.get("name") or "").strip()
        if not name:
            raise RuntimeError("imported agent API-key name is missing")
        location = str(auth.get("location") or "header").lower()
        if location == "header":
            return {name: secret_value}, {}
        if location == "query":
            return {}, {name: secret_value}
        raise RuntimeError("imported agent API-key location is unsupported")

    raise RuntimeError("imported agent auth type is unsupported")


def _external_auth_tuple(auth: ExternalRequestAuth) -> tuple[dict[str, str], dict[str, str]]:
    return auth.headers, auth.params


def _external_import_info(card: dict[str, Any]) -> dict[str, Any]:
    capabilities = card.get("capabilities")
    if not isinstance(capabilities, dict):
        return {}
    imported = capabilities.get("a2a_cloud_import")
    return imported if isinstance(imported, dict) else {}


def _external_auth_metadata_from_card(card: dict[str, Any]) -> dict[str, Any] | None:
    auth = _external_import_info(card).get("auth")
    return auth if isinstance(auth, dict) else None


def _legacy_external_request_auth_for_agent(agent: Agent) -> tuple[dict[str, str], dict[str, str]]:
    if not isinstance(agent.card, dict):
        return {}, {}
    auth = _external_auth_metadata_from_card(agent.card)
    if not auth or auth.get("type") in {None, "none"}:
        return {}, {}
    secret_value = secret_store.read_agent_secret_value(
        agent_name=agent.name,
        key=_EXTERNAL_AUTH_SECRET_KEY,
    )
    return _external_request_auth(auth, secret_value)


async def _external_request_auth_for_agent(
    agent: Agent,
    session: AsyncSession,
    *,
    user: User | None = None,
) -> ExternalRequestAuth:
    if user is None or agent.owner_id == user.id:
        try:
            legacy_headers, legacy_params = _legacy_external_request_auth_for_agent(agent)
            if legacy_headers or legacy_params:
                return ExternalRequestAuth(headers=legacy_headers, params=legacy_params)
        except Exception:  # noqa: BLE001
            pass
    return await resolve_request_auth(session, agent, user=user)


async def _fetch_card_from_base_url(
    base_url: str,
    *,
    request_auth: tuple[dict[str, str], dict[str, str]] | None = None,
) -> tuple[dict[str, Any], str]:
    last_error = "agent card not found"
    last_status: int | None = None
    auth_headers, auth_params = request_auth or ({}, {})
    for path in _CARD_PATHS:
        url = f"{base_url.rstrip('/')}{path}"
        try:
            r = await safe_fetch_url(
                url,
                headers={"accept": "application/json"},
                sensitive_headers=auth_headers,
                params=auth_params or None,
                max_response_bytes=_MAX_AGENT_CARD_BYTES,
                timeout_seconds=5.0,
            )
        except UnsafeURLError as exc:
            raise HTTPException(400, f"could not fetch Agent Card: {exc}") from exc
        except SafeHTTPError as exc:
            last_error = str(exc)
            continue
        last_status = r.status_code
        if r.status_code == 404:
            last_error = f"{path} returned 404"
            continue
        if r.status_code >= 400:
            last_error = f"{path} returned HTTP {r.status_code}"
            continue
        try:
            body = json.loads(r.text)
        except ValueError:
            last_error = f"{path} did not return JSON"
            continue
        if isinstance(body, dict):
            return body, r.url
        last_error = f"{path} did not return a JSON object"
    if last_status in {401, 403}:
        last_error = f"{last_error}; provide import auth if the Agent Card is protected"
    raise HTTPException(400, f"could not fetch Agent Card: {last_error}")


def _normalize_schema(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {"type": "object", "properties": {}}


def _normalize_external_skill(skill: dict[str, Any], *, index: int) -> dict[str, Any] | None:
    name = str(skill.get("name") or skill.get("id") or f"skill_{index + 1}").strip()
    if not name:
        return None
    input_schema = (
        skill.get("input_schema")
        or skill.get("inputSchema")
        or skill.get("parameters")
        or {"type": "object", "properties": {}}
    )
    output_schema = (
        skill.get("output_schema")
        or skill.get("outputSchema")
        or {"type": "object", "properties": {}}
    )
    normalized = {
        "id": str(skill.get("id") or name),
        "name": name,
        "description": str(skill.get("description") or name),
        "tags": [str(tag) for tag in (skill.get("tags") or [])],
        "scopes": [str(scope) for scope in (skill.get("scopes") or [])],
        "stream": bool(skill.get("stream", False)),
        "policy": skill.get("policy") if isinstance(skill.get("policy"), dict) else {},
        "input_schema": _normalize_schema(input_schema),
        "output_schema": _normalize_schema(output_schema),
    }
    for key in ("securityRequirements", "security", "authentication"):
        value = skill.get(key)
        if _auth_declaration_is_present(value):
            normalized[key] = value
    return normalized


def _external_mcp_info(
    *,
    registry_name: str,
    base_url: str,
    card: dict[str, Any],
    auth: dict[str, Any] | None = None,
) -> dict[str, str]:
    endpoint = card.get("mcp_endpoint")
    connector_endpoint = _external_connector_mcp_endpoint(card)
    uses_server_side_auth = isinstance(auth, dict) and auth.get("type") not in {None, "none"}
    if not uses_server_side_auth and isinstance(endpoint, str) and endpoint.strip():
        path = endpoint.strip()
        info = {
            "mode": "native",
            "base_url": base_url,
            "path": path if path.startswith("/") else f"/{path}",
        }
        if connector_endpoint:
            info["connector_path"] = connector_endpoint
        return info
    return {
        "mode": "generated",
        "base_url": f"{settings.public_cp_url.rstrip('/')}/v1/agents/{registry_name}",
        "path": "/mcp",
    }


def _external_connector_mcp_endpoint(card: dict[str, Any]) -> str | None:
    endpoints = card.get("mcp_endpoints")
    if not isinstance(endpoints, dict):
        capabilities = card.get("capabilities")
        if isinstance(capabilities, dict) and isinstance(capabilities.get("mcp"), dict):
            endpoints = capabilities["mcp"]
    connector = endpoints.get("connector") if isinstance(endpoints, dict) else None
    if isinstance(connector, dict):
        path = connector.get("path")
        if isinstance(path, str) and path.strip():
            clean = path.strip()
            return clean if clean.startswith("/") else f"/{clean}"
    endpoint = card.get("connector_mcp_endpoint")
    if isinstance(endpoint, str) and endpoint.strip():
        clean = endpoint.strip()
        return clean if clean.startswith("/") else f"/{clean}"
    return None


def _mcp_endpoints_for_card(card: dict[str, Any], mcp: dict[str, Any]) -> dict[str, Any]:
    endpoints = card.get("mcp_endpoints")
    if not isinstance(endpoints, dict):
        capabilities = card.get("capabilities")
        if isinstance(capabilities, dict) and isinstance(capabilities.get("mcp"), dict):
            endpoints = capabilities["mcp"]
    if isinstance(endpoints, dict) and endpoints:
        return endpoints
    result: dict[str, Any] = {
        "standard": {
            "path": mcp["path"],
            "intended_for": "code_sdk_clients",
        },
    }
    connector_path = mcp.get("connector_path")
    if isinstance(connector_path, str) and connector_path:
        result["connector"] = {
            "path": connector_path,
            "intended_for": ["chatgpt", "claude", "hosted_connectors"],
            "supports_async_jobs": True,
            "supports_structured_interrupts": True,
            "poll_tool": "job_result",
            "resume_tool": "submit_interaction",
            "instructions": (
                "Use this endpoint for hosted connector UIs. Use /mcp for "
                "normal code or SDK MCP clients."
            ),
        }
    return result


def _normalize_external_card(
    raw_card: dict[str, Any],
    *,
    registry_name: str,
    base_url: str,
    card_url: str,
    auth: dict[str, Any] | None = None,
) -> dict[str, Any]:
    skills = [
        normalized
        for i, skill in enumerate(raw_card.get("skills") or [])
        if isinstance(skill, dict)
        for normalized in [_normalize_external_skill(skill, index=i)]
        if normalized is not None
    ]
    if not skills:
        raise HTTPException(400, "Agent Card must expose at least one skill")

    capabilities = raw_card.get("capabilities")
    if not isinstance(capabilities, dict):
        capabilities = {}
    mcp = _external_mcp_info(
        registry_name=registry_name,
        base_url=base_url,
        card=raw_card,
        auth=auth,
    )
    import_info: dict[str, Any] = {
        "source": "external_a2a",
        "url": base_url,
        "card_url": card_url,
        "mcp": mcp,
    }
    if isinstance(auth, dict) and auth.get("type") not in {None, "none"}:
        import_info["auth"] = auth
    capabilities = {
        **capabilities,
        "a2a_cloud_import": import_info,
    }

    input_modes = raw_card.get("input_modes") or raw_card.get("defaultInputModes")
    output_modes = raw_card.get("output_modes") or raw_card.get("defaultOutputModes")
    normalized_card = {
        "name": registry_name,
        "description": str(raw_card.get("description") or ""),
        "version": str(raw_card.get("version") or "external"),
        "skills": skills,
        "capabilities": capabilities,
        "input_modes": input_modes if isinstance(input_modes, list) else ["application/json"],
        "output_modes": output_modes if isinstance(output_modes, list) else ["application/json"],
        "required_secrets": raw_card.get("required_secrets")
        if isinstance(raw_card.get("required_secrets"), list)
        else [],
        "required_env": raw_card.get("required_env")
        if isinstance(raw_card.get("required_env"), list)
        else [],
        "consumer_setup": raw_card.get("consumer_setup")
        if isinstance(raw_card.get("consumer_setup"), dict)
        else {"fields": []},
        "runtime": raw_card.get("runtime") if isinstance(raw_card.get("runtime"), dict) else {},
        "state_schema": raw_card.get("state_schema")
        if isinstance(raw_card.get("state_schema"), dict)
        else None,
        "workspace_access": raw_card.get("workspace_access")
        if isinstance(raw_card.get("workspace_access"), dict)
        else {},
        "mcp_endpoint": mcp["path"],
        "mcp_endpoints": _mcp_endpoints_for_card(raw_card, mcp),
    }
    connector_path = mcp.get("connector_path")
    if isinstance(connector_path, str) and connector_path:
        normalized_card["connector_mcp_endpoint"] = connector_path
    for key in ("securitySchemes", "securityRequirements", "security", "authentication"):
        value = raw_card.get(key)
        if _auth_declaration_is_present(value):
            normalized_card[key] = value
    return normalized_card


def _mcp_out_for_agent(agent: Agent) -> AgentMCPOut:
    info: dict[str, Any] = {}
    if isinstance(agent.card, dict):
        capabilities = agent.card.get("capabilities")
        if isinstance(capabilities, dict):
            imported = capabilities.get("a2a_cloud_import")
            if isinstance(imported, dict):
                mcp = imported.get("mcp")
                if isinstance(mcp, dict):
                    info = mcp
    base = str(info.get("base_url") or _external_base_url(agent)).rstrip("/")
    path = str(info.get("path") or "/mcp")
    if not path.startswith("/"):
        path = f"/{path}"
    connector_url = None
    connector_path = info.get("connector_path")
    if isinstance(connector_path, str) and connector_path.strip():
        clean = connector_path.strip()
        if not clean.startswith("/"):
            clean = f"/{clean}"
        connector_url = f"{base}{clean}"
    endpoints = {
        "standard": {"url": f"{base}{path}", "path": path},
    }
    if connector_url is not None:
        endpoints["connector"] = {
            "url": connector_url,
            "path": connector_path,
            "poll_tool": "job_result",
            "resume_tool": "submit_interaction",
        }
    return AgentMCPOut(
        mode=str(info.get("mode") or "native"),
        url=f"{base}{path}",
        connector_url=connector_url,
        endpoints=endpoints,
    )


def _agent_import_out(agent: Agent) -> AgentImportOut:
    data = AgentOut.model_validate(agent).model_dump()
    return AgentImportOut(
        **data,
        source="external_a2a",
        card_hash=card_hash(agent.card if isinstance(agent.card, dict) else {}),
        mcp=_mcp_out_for_agent(agent),
    )


async def _apply_import_auth_connections(
    *,
    session: AsyncSession,
    agent: Agent,
    user: User,
    auth_metadata: dict[str, Any],
    auth_secret: str | None,
    auth_requirements: list[dict[str, Any]],
) -> list[Any]:
    if auth_secret is None:
        if not auth_requirements:
            return []
        return await ensure_auth_placeholders(
            session,
            agent=agent,
            user=user,
            requirements=auth_requirements,
        )

    scheme_type = str(auth_metadata.get("type") or "")
    api_key_location = (
        str(auth_metadata.get("location"))
        if auth_metadata.get("location") is not None
        else None
    )
    api_key_name = (
        str(auth_metadata.get("name"))
        if auth_metadata.get("name") is not None
        else None
    )
    requirement = select_requirement_for_setup(
        auth_requirements,
        scheme_type=scheme_type,
        scheme_name=None,
        api_key_location=api_key_location,
        api_key_name=api_key_name,
    )
    if auth_requirements and requirement is None:
        raise HTTPException(400, "import auth does not match the Agent Card security schemes")
    scheme_name = str((requirement or {}).get("scheme_name") or "import")
    metadata = {**(requirement or {}), **auth_metadata}
    if scheme_type == "api_key":
        secret_payload = {
            "value": auth_secret,
            "location": metadata.get("location") or "header",
            "name": metadata.get("name"),
        }
    else:
        secret_payload = {
            "token": auth_secret,
            "scheme": metadata.get("scheme") or "Bearer",
        }
    await upsert_connection(
        session,
        agent=agent,
        user=user,
        scheme_name=scheme_name,
        scheme_type=scheme_type,
        credential_scope="agent",
        secret_payload=secret_payload,
        metadata=metadata,
        status="connected",
    )
    await ensure_auth_placeholders(
        session,
        agent=agent,
        user=user,
        requirements=[
            item for item in auth_requirements if item.get("scheme_name") != scheme_name
        ],
    )
    return await refresh_auth_card_status(session, agent)


def _jsonrpc_result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _ensure_mcp_object_schema(schema: dict[str, Any]) -> dict[str, Any]:
    if schema.get("type") == "object":
        return schema
    if not schema:
        return {"type": "object", "properties": {}}
    return {"type": "object", "properties": {"value": schema}}


def _external_mcp_tools(card: dict[str, Any]) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for skill in card.get("skills") or []:
        if not isinstance(skill, dict):
            continue
        name = str(skill.get("name") or skill.get("id") or "").strip()
        if not name:
            continue
        tool: dict[str, Any] = {
            "name": name,
            "description": str(skill.get("description") or name),
            "inputSchema": _ensure_mcp_object_schema(
                skill.get("input_schema") if isinstance(skill.get("input_schema"), dict) else {}
            ),
        }
        output_schema = skill.get("output_schema")
        if isinstance(output_schema, dict) and output_schema:
            tool["outputSchema"] = _ensure_mcp_object_schema(output_schema)
        tools.append(tool)
    return tools


def _mcp_tool_result(value: Any, *, is_error: bool = False) -> dict[str, Any]:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": value if isinstance(value, dict) else {"result": value},
        "isError": is_error,
    }


def _external_httpx_options(
    auth: ExternalRequestAuth,
    stack: ExitStack,
) -> dict[str, Any]:
    options: dict[str, Any] = {}
    if auth.cert_pem and auth.key_pem:
        cert_path = _write_temp_auth_file(auth.cert_pem, suffix=".crt", stack=stack)
        key_path = _write_temp_auth_file(auth.key_pem, suffix=".key", stack=stack)
        options["cert"] = (cert_path, key_path)
    if auth.ca_pem:
        options["verify"] = _write_temp_auth_file(auth.ca_pem, suffix=".ca.pem", stack=stack)
    return options


def _write_temp_auth_file(value: str, *, suffix: str, stack: ExitStack) -> str:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, mode="w", encoding="utf-8")
    try:
        with tmp:
            tmp.write(value)
    except Exception:
        try:
            os.unlink(tmp.name)
        except FileNotFoundError:
            pass
        raise
    stack.callback(lambda path=tmp.name: _unlink_temp_auth_file(path))
    return tmp.name


def _unlink_temp_auth_file(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


async def _call_external_invoke(
    *,
    base_url: str,
    skill_name: str,
    arguments: dict[str, Any],
    request_auth: ExternalRequestAuth | None = None,
    timeout_seconds: float = 60.0,
) -> Any | None:
    url = f"{base_url.rstrip('/')}/invoke/{quote(skill_name, safe='')}"
    auth = request_auth or ExternalRequestAuth(headers={}, params={})
    with ExitStack() as stack:
        options = _external_httpx_options(auth, stack)
        r = await safe_request_url(
            "POST",
            url,
            sensitive_headers=auth.headers or None,
            params=auth.params or None,
            json_body={"arguments": arguments},
            max_response_bytes=_MAX_EXTERNAL_AGENT_RESPONSE_BYTES,
            timeout_seconds=timeout_seconds,
            max_redirects=0,
            client_options=options,
            require_https=True,
        )
    if r.status_code in {404, 405}:
        return None
    if r.status_code >= 400:
        # ``agent`` is filled in by ``_call_external_agent_skill``, which is the
        # nearest caller that knows the registration this URL belongs to.
        raise _agent_invoke_error(
            agent_name="",
            skill_name=skill_name,
            reason="agent_error",
            status_code=_agent_error_status(r.status_code),
            agent_status=r.status_code,
            excerpt=_redacted_agent_excerpt(r.text),
        )
    try:
        data = json.loads(r.text)
    except ValueError as exc:
        raise _agent_invoke_error(
            agent_name="",
            skill_name=skill_name,
            reason="agent_bad_response",
            status_code=502,
            agent_status=r.status_code,
            excerpt=_redacted_agent_excerpt(r.text),
        ) from exc
    return data.get("result") if isinstance(data, dict) and "result" in data else data


async def _call_external_a2a_message(
    *,
    base_url: str,
    skill_name: str,
    arguments: dict[str, Any],
    request_auth: ExternalRequestAuth | None = None,
    timeout_seconds: float = 60.0,
) -> Any:
    payload = {
        "jsonrpc": "2.0",
        "id": "mcp-adapter-call",
        "method": "message/send",
        "params": {
            "message": {
                "messageId": "mcp-adapter-message",
                "role": "ROLE_USER",
                "parts": [
                    {
                        "data": {
                            "skill": skill_name,
                            "arguments": arguments,
                        }
                    }
                ],
            }
        },
    }
    auth = request_auth or ExternalRequestAuth(headers={}, params={})
    with ExitStack() as stack:
        options = _external_httpx_options(auth, stack)
        r = await safe_request_url(
            "POST",
            f"{base_url.rstrip('/')}/",
            sensitive_headers=auth.headers or None,
            params=auth.params or None,
            json_body=payload,
            max_response_bytes=_MAX_EXTERNAL_AGENT_RESPONSE_BYTES,
            timeout_seconds=timeout_seconds,
            max_redirects=0,
            client_options=options,
            require_https=True,
        )
    if r.status_code >= 400:
        raise _agent_invoke_error(
            agent_name="",
            skill_name=skill_name,
            reason="agent_error",
            status_code=_agent_error_status(r.status_code),
            agent_status=r.status_code,
            excerpt=_redacted_agent_excerpt(r.text),
        )
    try:
        data = json.loads(r.text)
    except ValueError as exc:
        raise _agent_invoke_error(
            agent_name="",
            skill_name=skill_name,
            reason="agent_bad_response",
            status_code=502,
            agent_status=r.status_code,
            excerpt=_redacted_agent_excerpt(r.text),
        ) from exc
    if isinstance(data, dict) and data.get("error"):
        # A JSON-RPC error: the transport succeeded, so there is no HTTP status
        # of the agent's to preserve — only what it said.
        raise _agent_invoke_error(
            agent_name="",
            skill_name=skill_name,
            reason="agent_error",
            status_code=502,
            excerpt=_redacted_agent_excerpt(json.dumps(data["error"], default=str)),
        )
    return data.get("result") if isinstance(data, dict) and "result" in data else data


async def _call_external_agent_skill(
    *,
    agent: Agent,
    session: AsyncSession,
    user: User,
    skill_name: str,
    arguments: dict[str, Any],
) -> Any:
    base_url = _external_base_url(agent)
    request_auth = await _external_request_auth_for_agent(agent, session, user=user)
    timeout_seconds = float(_agent_api_grant_ttl_seconds(agent))
    try:
        result = await _call_external_invoke(
            base_url=base_url,
            skill_name=skill_name,
            arguments=arguments,
            request_auth=request_auth,
            timeout_seconds=timeout_seconds,
        )
        if result is not None:
            return result
        return await _call_external_a2a_message(
            base_url=base_url,
            skill_name=skill_name,
            arguments=arguments,
            request_auth=request_auth,
            timeout_seconds=timeout_seconds,
        )
    except AgentApiInvokeFailed as exc:
        # The transport helpers see a URL, not a registration. Name the agent
        # here so the caller learns which one failed.
        if isinstance(exc.detail, dict) and not exc.detail.get("agent"):
            exc.detail["agent"] = agent.name
            exc.detail["message"] = _agent_invoke_message(exc.detail)
        raise
    except SafeHTTPTimeoutError as exc:
        raise _agent_invoke_error(
            agent_name=agent.name,
            skill_name=skill_name,
            reason="agent_timeout",
            status_code=504,
            timeout_seconds=timeout_seconds,
        ) from exc
    except httpx.RequestError as exc:
        raise _agent_invoke_error(
            agent_name=agent.name,
            skill_name=skill_name,
            reason="agent_unreachable",
            status_code=502,
            excerpt=type(exc).__name__,
        ) from exc


async def _fetch_card_with_resolved_auth(
    agent: Agent,
    *,
    request_auth: ExternalRequestAuth | None = None,
) -> dict[str, Any] | None:
    """Fetch a live agent card without touching the database session."""
    if _is_external_agent(agent):
        try:
            raw, card_url = await _fetch_card_from_base_url(
                _external_base_url(agent),
                request_auth=_external_auth_tuple(
                    request_auth or ExternalRequestAuth(headers={}, params={})
                ),
            )
            return _normalize_external_card(
                raw,
                registry_name=agent.name,
                base_url=_external_base_url(agent),
                card_url=card_url,
                auth=_external_auth_metadata_from_card(agent.card)
                if isinstance(agent.card, dict)
                else None,
            )
        except Exception:  # noqa: BLE001
            return None
    internal = f"http://{agent.name}.agents.svc.cluster.local"
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{internal}/.well-known/agent-card")
        if r.status_code != 200:
            return None
        body = r.json()
        return body if isinstance(body, dict) else None
    except Exception:  # noqa: BLE001
        return None


async def _refresh_cards_inplace(
    agents: list[Agent], session: AsyncSession, *, wake_hosted: bool = False
) -> None:
    """Re-fetch agents' live cards, persisting any changes.

    Passive registry/marketplace reads must not wake scaled-to-zero hosted
    agents, so by default hosted (cluster-internal) agents are skipped and
    their stored card is served as-is. External/imported agents are still
    refreshed because their card lives off-cluster and that fetch never wakes
    one of our pods. Set ``wake_hosted=True`` only for explicit owner/operator
    refreshes that intentionally cold-start a hosted agent.

    Resolve any DB-backed external-agent auth sequentially, then run the
    network fetches concurrently. A single AsyncSession cannot be shared by
    overlapping coroutines; asyncpg reports that as "another operation is in
    progress". If fetch fails we keep whatever was in the DB; if it succeeds
    and the live card differs, persist the new one.
    """
    if not agents:
        return

    request_auth_by_name: dict[str, ExternalRequestAuth] = {}
    skipped_external_names: set[str] = set()
    for agent in agents:
        if not _is_external_agent(agent):
            continue
        try:
            request_auth_by_name[agent.name] = await _external_request_auth_for_agent(
                agent, session,
            )
        except Exception:  # noqa: BLE001
            skipped_external_names.add(agent.name)

    async def fetch(agent: Agent) -> dict[str, Any] | None:
        if agent.name in skipped_external_names:
            return None
        # Hosted agents serve from a scale-to-zero Knative Service; a live
        # card GET would cold-start them. Only do it on an explicit refresh.
        if not _is_external_agent(agent) and not wake_hosted:
            return None
        return await _fetch_card_with_resolved_auth(
            agent,
            request_auth=request_auth_by_name.get(agent.name),
        )

    cards = await asyncio.gather(
        *(fetch(a) for a in agents), return_exceptions=True,
    )
    changed = False
    for agent, card in zip(agents, cards):
        if isinstance(card, dict) and card.get("skills"):
            if agent.card != card:
                agent.card = card
                changed = True
            # A live fetch just succeeded (external or explicit refresh); keep
            # the Redis cache warm so subsequent reads stay off the pod.
            await warm_agent_card(agent.name, card)
            # Card fetch succeeded = pod is up and serving. Flip
            # transient deploy states ("building", "deploying",
            # "provisioning") to the steady-state value.
            if agent.status != "running":
                agent.status = "running"
                changed = True
        if agent.public and not _is_external_agent(agent):
            want = _canonical_url(agent.name)
            if agent.url != want:
                agent.url = want
                changed = True
    if changed:
        try:
            await session.commit()
            await _index_agents_for_search(list(agents))
        except Exception:  # noqa: BLE001
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001
                pass


async def _ensure_agent_search_index(session: AsyncSession) -> None:
    global _agent_search_index_bootstrapped
    if not semantic_agent_search.configured or _agent_search_index_bootstrapped:
        return
    async with _agent_search_index_bootstrap_lock:
        if _agent_search_index_bootstrapped:
            return
        rows = (
            await session.execute(
                select(Agent).order_by(Agent.updated_at.desc()).limit(
                    _AGENT_SEARCH_BOOTSTRAP_LIMIT
                )
            )
        ).scalars().all()
        await semantic_agent_search.index_agents(list(rows))
        _agent_search_index_bootstrapped = True


async def _index_agents_for_search(agents: list[Agent]) -> None:
    if not semantic_agent_search.configured or not agents:
        return
    try:
        await semantic_agent_search.index_agents(agents)
    except Exception:  # noqa: BLE001
        log.warning(
            "agent semantic index update failed for %s",
            ", ".join(agent.name for agent in agents),
            exc_info=True,
        )


async def _delete_agent_from_search(agent_id: int, agent_name: str) -> None:
    if not semantic_agent_search.configured:
        return
    try:
        await semantic_agent_search.delete_agent(agent_id)
    except Exception:  # noqa: BLE001
        log.warning(
            "agent semantic index delete failed for %s",
            agent_name,
            exc_info=True,
        )


async def _search_visible_agents(
    *,
    session: AsyncSession,
    user: User,
    q: str,
    tags: list[str],
    skill: str | None,
    limit: int,
) -> tuple[list[Agent], dict[int, float], str]:
    clean_q = q.strip()
    limit = min(max(1, limit), _AGENT_SEARCH_LIMIT_MAX)
    if clean_q and semantic_agent_search.configured:
        try:
            await _ensure_agent_search_index(session)
            organization_ids = await active_organization_ids(session, user.id)
            try:
                matches = await semantic_agent_search.query_agents(
                    clean_q,
                    user_id=user.id,
                    organization_ids=organization_ids,
                    limit=min(limit * 5, 100),
                    score_threshold=settings.agent_search_score_threshold,
                )
            except TypeError as exc:
                # Keep test/extension search adapters written before org-aware
                # visibility compatible while the built-in index uses it.
                if "organization_ids" not in str(exc):
                    raise
                matches = await semantic_agent_search.query_agents(
                    clean_q,
                    user_id=user.id,
                    limit=min(limit * 5, 100),
                    score_threshold=settings.agent_search_score_threshold,
                )
            rows = await _agents_by_semantic_matches(
                session=session,
                user=user,
                matches=matches,
                tags=tags,
                skill=skill,
                limit=limit,
            )
            if rows:
                return rows, {match.agent_id: match.score for match in matches}, "semantic"
        except Exception:  # noqa: BLE001
            log.warning("semantic agent search failed; falling back to lexical", exc_info=True)

    rows = (
        await session.execute(
            select(Agent).where(visible_agents_clause(user.id))
        )
    ).scalars().all()
    ranked = [
        (agent, lexical_agent_score(agent, clean_q, tags=tags, skill=skill))
        for agent in rows
        if _agent_matches_filters(agent, tags=tags, skill=skill)
    ]
    if clean_q or tags or skill:
        ranked = [(agent, score) for agent, score in ranked if score > 0]
    ranked.sort(
        key=lambda item: (
            item[1],
            item[0].public,
            item[0].updated_at or item[0].created_at,
        ),
        reverse=True,
    )
    selected = ranked[:limit]
    return (
        [agent for agent, _score in selected],
        {agent.id: score for agent, score in selected if agent.id is not None},
        "lexical",
    )


async def _agents_by_semantic_matches(
    *,
    session: AsyncSession,
    user: User,
    matches: list[SemanticAgentMatch],
    tags: list[str],
    skill: str | None,
    limit: int,
) -> list[Agent]:
    ids = [match.agent_id for match in matches]
    if not ids:
        return []
    rows = (
        await session.execute(
            select(Agent).where(
                Agent.id.in_(ids),
                visible_agents_clause(user.id),
            )
        )
    ).scalars().all()
    by_id = {agent.id: agent for agent in rows}
    ordered: list[Agent] = []
    for agent_id in ids:
        agent = by_id.get(agent_id)
        if agent is None or not _agent_matches_filters(agent, tags=tags, skill=skill):
            continue
        ordered.append(agent)
        if len(ordered) >= limit:
            break
    return ordered


def _agent_search_out(
    agent: Agent,
    *,
    score: float | None,
    match_source: str,
) -> AgentSearchOut:
    card = agent.card if isinstance(agent.card, dict) else {}
    runtime = card.get("runtime") if isinstance(card.get("runtime"), dict) else {}
    return AgentSearchOut(
        name=agent.name,
        description=_truncate(str(agent.description or card.get("description") or ""), 280),
        status=agent.status,
        public=agent.public,
        url=agent.url,
        score=score,
        match_source=match_source,
        llm_provisioning=(
            str(runtime["llm_provisioning"])
            if runtime.get("llm_provisioning") is not None
            else None
        ),
        account_access=(
            runtime.get("account_access")
            if isinstance(runtime.get("account_access"), dict)
            else None
        ),
        setup_required=_card_requires_setup(card),
        skills=[_compact_skill(skill) for skill in _card_skills(card)],
    )


def _compact_skill(skill: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": str(skill.get("name") or ""),
        "description": _truncate(str(skill.get("description") or ""), 220),
        "tags": [str(tag) for tag in skill.get("tags") or []][:8],
        "input_fields": _schema_input_fields(skill.get("input_schema")),
    }


def _schema_input_fields(schema: Any) -> list[dict[str, Any]]:
    if not isinstance(schema, dict):
        return []
    props = schema.get("properties")
    if not isinstance(props, dict):
        return []
    required = {
        str(name)
        for name in schema.get("required", [])
        if isinstance(name, str)
    }
    fields: list[dict[str, Any]] = []
    for name, spec in list(props.items())[:12]:
        fields.append(
            {
                "name": str(name),
                "type": _schema_type(spec),
                "required": str(name) in required,
            }
        )
    return fields


def _schema_type(spec: Any) -> str | None:
    if not isinstance(spec, dict):
        return None
    raw = spec.get("type")
    if isinstance(raw, list):
        return "|".join(str(item) for item in raw)
    if raw is not None:
        return str(raw)
    if spec.get("enum"):
        return "enum"
    return None


def _agent_matches_filters(agent: Agent, *, tags: list[str], skill: str | None) -> bool:
    card = agent.card if isinstance(agent.card, dict) else {}
    skills = _card_skills(card)
    if skill and skill not in {str(item.get("name") or "") for item in skills}:
        return False
    if tags:
        card_tags = {
            str(tag).lower()
            for item in skills
            for tag in (item.get("tags") or [])
        }
        if not ({tag.lower() for tag in tags} & card_tags):
            return False
    return True


def _card_skills(card: dict[str, Any]) -> list[dict[str, Any]]:
    skills = card.get("skills") if isinstance(card.get("skills"), list) else []
    return [skill for skill in skills if isinstance(skill, dict) and skill.get("name")]


def _card_requires_setup(card: dict[str, Any]) -> bool:
    setup = card.get("consumer_setup")
    if not isinstance(setup, dict):
        return False
    fields = setup.get("fields")
    if not isinstance(fields, list):
        return False
    return any(
        isinstance(field, dict) and bool(field.get("required", True))
        for field in fields
    )


def _truncate(value: str, limit: int) -> str:
    clean = " ".join(value.split())
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 3)].rstrip() + "..."


async def _deployment_out(
    deploy: AgentDeployment,
    session: AsyncSession,
    *,
    agent: Agent | None = None,
    events: list[AgentDeploymentEvent] | None = None,
) -> AgentDeploymentOut:
    deployment_synced = False
    if agent is not None and (
        deploy.status in ACTIVE_DEPLOY_STATUSES or deploy.status == "failed"
    ):
        async def reindex_changed_agent(changed_agent: Agent) -> None:
            await _index_agents_for_search([changed_agent])

        deploy = await sync_deployment_verification(
            session,
            agent,
            deploy,
            on_agent_changed=reindex_changed_agent,
        )
        deployment_synced = True
    if events is None or deployment_synced:
        events = await deployment_events(session, deploy)
    return AgentDeploymentOut(
        id=deploy.id,
        deploy_id=deploy.deploy_id,
        agent_name=deploy.agent_name,
        trigger=deploy.trigger,
        status=deploy.status,
        source_repo_url=deploy.source_repo_url,
        head_sha=deploy.head_sha,
        image=deploy.image,
        agent_url=deploy.agent_url,
        error=deploy.error,
        verification=deploy.verification or {},
        created_at=deploy.created_at,
        updated_at=deploy.updated_at,
        started_at=deploy.started_at,
        completed_at=deploy.completed_at,
        events=[AgentDeploymentEventOut.model_validate(event) for event in events],
    )


async def _deployment_events_by_deployment(
    session: AsyncSession,
    deployment_ids: list[int],
) -> dict[int, list[AgentDeploymentEvent]]:
    if not deployment_ids:
        return {}
    rows = (
        await session.execute(
            select(AgentDeploymentEvent)
            .where(AgentDeploymentEvent.deployment_id.in_(deployment_ids))
            .order_by(
                AgentDeploymentEvent.deployment_id.asc(),
                AgentDeploymentEvent.id.asc(),
            )
        )
    ).scalars().all()
    out: dict[int, list[AgentDeploymentEvent]] = {}
    for row in rows:
        out.setdefault(row.deployment_id, []).append(row)
    return out


async def _latest_deployment_outs(
    agents: list[Agent],
    session: AsyncSession,
) -> dict[str, AgentDeploymentOut]:
    if not agents:
        return {}
    agents_by_name = {agent.name: agent for agent in agents}
    agent_names = list(agents_by_name)
    owner_ids = {agent.owner_id for agent in agents}
    latest_ids = (
        select(func.max(AgentDeployment.id).label("id"))
        .where(AgentDeployment.agent_name.in_(agent_names))
        .where(AgentDeployment.user_id.in_(owner_ids))
        .group_by(AgentDeployment.agent_name)
        .subquery()
    )
    rows = (
        await session.execute(
            select(AgentDeployment)
            .join(latest_ids, AgentDeployment.id == latest_ids.c.id)
            .order_by(AgentDeployment.agent_name.asc())
        )
    ).scalars().all()
    events_by_deployment = await _deployment_events_by_deployment(
        session,
        [row.id for row in rows],
    )
    out: dict[str, AgentDeploymentOut] = {}
    for deploy in rows:
        agent = agents_by_name.get(deploy.agent_name)
        if agent is not None and deploy.user_id == agent.owner_id:
            out[agent.name] = await _deployment_out(
                deploy,
                session,
                agent=agent,
                events=events_by_deployment.get(deploy.id, []),
            )
    return out


async def _openapi_source_urls(
    agents: list[Agent],
    session: AsyncSession,
) -> dict[str, list[str]]:
    names = [agent.name for agent in agents]
    if not names:
        return {}
    rows = (
        await session.execute(
            select(AgentDeploymentEvent)
            .where(AgentDeploymentEvent.agent_name.in_(names))
            .order_by(desc(AgentDeploymentEvent.id))
        )
    ).scalars().all()
    out: dict[str, list[str]] = {}
    for row in rows:
        if row.agent_name in out:
            continue
        data = row.data if isinstance(row.data, dict) else {}
        values = data.get("openapi_urls")
        if isinstance(values, list):
            urls = [str(value).strip() for value in values if str(value).strip()]
            if urls:
                out[row.agent_name] = urls
                continue
        value = data.get("openapi_url")
        if isinstance(value, str) and value.strip():
            out[row.agent_name] = [value.strip()]
    return out


async def _code_editor_opt_ins(
    agents: list[Agent],
    session: AsyncSession,
) -> dict[int, AgentCodeEditorOptIn]:
    agent_ids = [agent.id for agent in agents if agent.id is not None]
    if not agent_ids:
        return {}
    rows = (
        await session.execute(
            select(AgentCodeEditorOptIn).where(
                AgentCodeEditorOptIn.agent_id.in_(agent_ids)
            )
        )
    ).scalars().all()
    return {row.agent_id: row for row in rows}


async def _code_editor_opt_in_for_agent(
    agent: Agent,
    session: AsyncSession,
) -> AgentCodeEditorOptIn | None:
    return (
        await session.execute(
            select(AgentCodeEditorOptIn).where(
                AgentCodeEditorOptIn.agent_id == agent.id
            )
        )
    ).scalar_one_or_none()


async def _agent_mine_out_for_agent(
    agent: Agent,
    session: AsyncSession,
    latest_deployment: AgentDeploymentOut | None = None,
) -> AgentMineOut:
    openapi_source_urls = await _openapi_source_urls([agent], session)
    opt_in = await _code_editor_opt_in_for_agent(agent, session)
    return _agent_mine_out(
        agent,
        latest_deployment,
        openapi_source_urls=openapi_source_urls.get(agent.name),
        code_editor=_agent_code_editor_out(agent, opt_in),
    )


async def _get_owned_agent(
    name: str,
    user: User,
    session: AsyncSession,
) -> Agent:
    agent = (
        await session.execute(select(Agent).where(Agent.name == name))
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(404, "agent not found")
    if agent.owner_id != user.id:
        raise HTTPException(403, "not allowed")
    return agent


async def _get_writable_agent(
    name: str,
    user: User,
    session: AsyncSession,
) -> Agent:
    agent = (
        await session.execute(select(Agent).where(Agent.name == name))
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(404, "agent not found")
    await require_agent_access(
        session,
        user=user,
        agent=agent,
        action="edit_existing",
    )
    return agent


async def _get_visible_agent(
    name: str,
    user: User | None,
    session: AsyncSession,
) -> Agent:
    agent = (
        await session.execute(select(Agent).where(Agent.name == name))
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(404, "agent not found")
    if not agent.public and (user is None or agent.owner_id != user.id):
        raise HTTPException(403, "not allowed")
    return agent


class AgentSshIn(BaseModel):
    public_key: str = Field(..., description="Caller's ephemeral SSH public key")
    credentials_json: str | None = Field(
        None,
        max_length=16384,
        description="Caller's ~/.a2a/credentials.json contents, injected into "
        "the dev box so its a2a CLI is already logged in",
    )


class AgentSshOut(BaseModel):
    agent: str
    wss_url: str
    access_token: str
    user: str
    # Public host of the box, e.g. ``<agent>-devbox.<domain>``. `a2a dev` serves
    # the agent at https://<host>/ (the bridge fronts the agent on this origin).
    host: str


_SSH_KEY_PREFIXES = ("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-", "sk-ssh-", "sk-ecdsa-")


@router.post("/{name}/ssh", response_model=AgentSshOut)
async def open_agent_ssh(
    name: str,
    body: AgentSshIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> AgentSshOut:
    """Provision (or wake) the agent's throwaway dev box and return the WSS
    connection info. The caller's ephemeral public key is the only key the box
    accepts; the returned access token gates the transport tunnel."""
    agent = await _get_owned_agent(name, user, session)
    public_key = body.public_key.strip()
    if not public_key.startswith(_SSH_KEY_PREFIXES):
        raise HTTPException(400, "public_key must be an OpenSSH public key")
    credentials_json = (body.credentials_json or "").strip() or None
    if credentials_json is not None:
        try:
            if not isinstance(json.loads(credentials_json), dict):
                raise ValueError
        except ValueError:
            raise HTTPException(400, "credentials_json must be a JSON object")
    info = devbox.ensure_devbox(
        agent_name=agent.name,
        owner_id=agent.owner_id,
        gitea_owner=agent.gitea_owner,
        public_key=public_key,
        credentials_json=credentials_json,
    )
    return AgentSshOut(agent=agent.name, **info)


def _hash_agent_api_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_agent_api_token() -> str:
    return f"{_AGENT_API_TOKEN_PREFIX}{secrets.token_urlsafe(_AGENT_API_TOKEN_BYTES)}"


def _extract_bearer(authorization: str | None) -> str | None:
    if not isinstance(authorization, str) or not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _query_token_value(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    token = value.strip()
    return token or None


def _normalize_agent_api_scopes(scopes: list[str]) -> list[str]:
    values: list[str] = []
    for scope in scopes or ["invoke"]:
        clean = str(scope).strip().lower()
        if not clean:
            continue
        if clean not in {"invoke", "mcp"}:
            raise HTTPException(400, f"unsupported API token scope: {clean}")
        if clean not in values:
            values.append(clean)
    return values or ["invoke"]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _is_integration_link(row: AgentApiToken) -> bool:
    """Rows the integration-link surface lists and revokes.

    Deliberately unchanged: this is the predicate the list and delete routes
    have always used, so no row disappears from a dashboard that used to show
    it. It is *not* precise enough to decide whether we minted the row.
    """
    return "mcp" in set(row.scopes or [])


def _is_minted_integration_link(row: AgentApiToken) -> bool:
    """Rows :func:`create_agent_integration_link` minted itself.

    Strictly narrower than :func:`_is_integration_link`. A plain agent API
    token created through ``POST /{name}/api-tokens`` with ``scopes:
    ["invoke", "mcp"]`` and an explicitly null ``expires_at`` also has "mcp"
    in its scopes, and must not be handed a lifetime its owner never asked
    for. Integration links are the only rows whose ``name`` this route
    controls, so the minted label is the marker.
    """
    if "mcp" not in set(row.scopes or []):
        return False
    label = (row.name or "").strip()
    agent_name = row.agent_name or ""
    return label in {
        _integration_link_label(agent_name),
        _integration_link_label(agent_name, url_token=True),
    }


def _ensure_integration_link_expiry(row: AgentApiToken) -> bool:
    """Give a legacy never-expiring integration link a finite lifetime.

    Integration links minted before expiry was mandatory have
    ``expires_at IS NULL``. Rather than mass-revoking them, the first list or
    use after this change starts a grace window; the owner sees the new
    ``expires_at`` in the dashboard and can regenerate before it lapses.

    Only links this route minted are touched (see
    :func:`_is_minted_integration_link`); an mcp-scoped API token the caller
    created with a deliberately null ``expires_at`` keeps its null.
    Returns ``True`` when the row was changed and needs committing.
    """
    if row.expires_at is not None or not _is_minted_integration_link(row):
        return False
    row.expires_at = datetime.now(timezone.utc) + timedelta(
        days=_AGENT_INTEGRATION_LINK_LEGACY_GRACE_DAYS
    )
    return True


async def _agent_api_token_agent(
    *,
    name: str,
    authorization: str | None,
    integration_token: str | None = None,
    required_scope: str = "invoke",
    session: AsyncSession,
) -> tuple[AgentApiToken, Agent, User]:
    token = _query_token_value(integration_token) or _extract_bearer(authorization)
    if not token:
        raise HTTPException(
            401,
            "missing app API bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    row = (
        await session.execute(
            select(AgentApiToken, Agent, User)
            .join(Agent, Agent.id == AgentApiToken.agent_id)
            .join(User, User.id == AgentApiToken.user_id)
            .where(AgentApiToken.token_hash == _hash_agent_api_token(token))
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(
            401,
            "invalid app API token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    api_token, agent, user = row
    if agent.name != name:
        raise HTTPException(403, "API token is not scoped to this agent")
    if not api_token.enabled:
        raise HTTPException(401, "API token is disabled")
    _ensure_integration_link_expiry(api_token)
    if (
        api_token.expires_at is not None
        and _as_utc(api_token.expires_at) <= datetime.now(timezone.utc)
    ):
        raise HTTPException(401, "API token is expired")
    if required_scope not in set(api_token.scopes or []):
        raise HTTPException(403, f"API token cannot use {required_scope} for this agent")
    api_token.last_used_at = datetime.now(timezone.utc)
    return api_token, agent, user


async def _agent_api_file_user(
    *,
    name: str,
    authorization: str | None,
    session_cookie: str | None,
    session: AsyncSession,
) -> tuple[Agent, User, AgentApiToken | None]:
    bearer = _extract_bearer(authorization)
    if bearer and bearer.startswith(_AGENT_API_TOKEN_PREFIX):
        api_token, agent, user = await _agent_api_token_agent(
            name=name,
            authorization=authorization,
            session=session,
        )
        return agent, user, api_token

    token = _credential_token(authorization, session_cookie)
    if not token:
        raise HTTPException(
            401,
            "missing bearer token or session cookie",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = await user_from_token(session, token)
    agent = await _get_visible_agent(name, user, session)
    return agent, user, None


def _sanitize_agent_api_file_path(path: str) -> str:
    key = path.strip().lstrip("/").rstrip()
    if not key:
        raise HTTPException(400, "empty path")
    parts = [part for part in key.split("/") if part and part != "."]
    if any(part == ".." for part in parts):
        raise HTTPException(400, "'..' not allowed in path")
    return "/".join(parts)


def _request_public_base_url(request: Request | None) -> str | None:
    if request is None:
        return None
    headers = getattr(request, "headers", {})
    forwarded_host = str(headers.get("x-forwarded-host") or "").split(",", 1)[0].strip()
    host = forwarded_host or str(headers.get("host") or "").strip()
    if not host:
        return None
    forwarded_proto = (
        str(headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip()
    )
    proto = forwarded_proto or str(getattr(getattr(request, "url", None), "scheme", "") or "")
    if not proto:
        proto = "https" if not host.startswith(("localhost", "127.0.0.1")) else "http"
    return f"{proto}://{host}".rstrip("/")


def _agent_api_base_url(agent: Agent, request: Request | None = None) -> str:
    request_base = _request_public_base_url(request)
    if request_base:
        return f"{request_base}/v1/agents/{agent.name}/api"
    public_base = settings.public_cp_url.rstrip("/") or "http://localhost:8000"
    return f"{public_base}/v1/agents/{agent.name}/api"


def _agent_api_run_poll_url(
    agent: Agent,
    run_id: str,
    request: Request | None = None,
    *,
    integration_token: str | None = None,
) -> str:
    url = f"{_agent_api_base_url(agent, request=request)}/runs/{quote(run_id, safe='')}"
    if integration_token:
        return _append_query_param(url, "integration_token", integration_token)
    return url


def _agent_api_file_download_url(
    agent_name: str,
    path: str,
    *,
    public_base_url: str | None = None,
    request: Request | None = None,
    integration_token: str | None = None,
) -> str:
    if public_base_url is None:
        public_base_url = _agent_api_public_base_url(request)
    url = (
        f"{public_base_url.rstrip('/')}/v1/agents/{agent_name}/api/files/"
        f"{quote(path.strip().lstrip('/'), safe='/')}"
    )
    if integration_token:
        return _append_query_param(url, "integration_token", integration_token)
    return url


def _agent_api_public_base_url(request: Request | None = None) -> str:
    return (
        _request_public_base_url(request)
        or settings.public_cp_url.rstrip("/")
        or "http://localhost:8000"
    )


def _append_query_param(url: str, name: str, value: str) -> str:
    parsed = urlparse(url)
    query = f"{name}={quote(value, safe='')}"
    if parsed.query:
        query = f"{parsed.query}&{query}"
    return urlunparse(parsed._replace(query=query))


def _first_agent_skill_name(agent: Agent) -> str | None:
    for skill in _card_skills(agent.card if isinstance(agent.card, dict) else {}):
        name = str(skill.get("name") or "").strip()
        if name:
            return name
    return None


def _agent_integration_urls(
    agent: Agent,
    *,
    token: str | None = None,
    request: Request | None = None,
    embed_token: bool = False,
) -> dict[str, str | None]:
    """Build the integration URLs for ``agent``.

    The token is only ever written into the query string when the caller
    explicitly opted in (``embed_token``). By default these URLs carry no
    secret and the credential travels in an ``Authorization`` header.
    """
    base_url = _agent_api_base_url(agent, request=request).rstrip("/")
    openapi_url = f"{base_url}/openapi.json"
    invoke_base_url = f"{base_url}/invoke"
    first_skill = _first_agent_skill_name(agent)
    sample_invoke_url = (
        f"{invoke_base_url}/{quote(first_skill, safe='')}"
        if first_skill
        else None
    )
    mcp_url = (
        f"{_agent_api_public_base_url(request).rstrip('/')}"
        f"/v1/agents/{quote(agent.name, safe='')}/mcp"
    )
    if token and embed_token:
        openapi_url = _append_query_param(openapi_url, "integration_token", token)
        invoke_base_url = _append_query_param(invoke_base_url, "integration_token", token)
        sample_invoke_url = (
            _append_query_param(sample_invoke_url, "integration_token", token)
            if sample_invoke_url
            else None
        )
        mcp_url = _append_query_param(mcp_url, "integration_token", token)
    return {
        "openapi_url": openapi_url,
        "invoke_base_url": invoke_base_url,
        "sample_invoke_url": sample_invoke_url,
        "mcp_url": mcp_url,
    }


def _safe_operation_id(agent_name: str, skill_name: str, action: str = "invoke") -> str:
    value = re.sub(r"[^A-Za-z0-9_]+", "_", f"{agent_name}_{skill_name}_{action}")
    return value.strip("_") or "invoke_agent_skill"


def _positive_seconds(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        seconds = math.ceil(float(value))
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def _agent_max_runtime_seconds(agent: Agent) -> int | None:
    card = agent.card if isinstance(agent.card, dict) else {}
    candidates: list[int] = []
    runtime = card.get("runtime") if isinstance(card.get("runtime"), dict) else {}
    resources = runtime.get("resources") if isinstance(runtime.get("resources"), dict) else None
    if resources is None:
        resources = card.get("resources")
    if isinstance(resources, dict):
        for key in ("max_runtime_seconds", "maxRuntimeSeconds"):
            seconds = _positive_seconds(resources.get(key))
            if seconds is not None:
                candidates.append(seconds)
    skills = card.get("skills")
    if isinstance(skills, list):
        for skill in skills:
            if not isinstance(skill, dict):
                continue
            policy = skill.get("policy")
            if not isinstance(policy, dict):
                continue
            seconds = _positive_seconds(policy.get("timeout_seconds"))
            if seconds is not None:
                candidates.append(seconds)
    return max(candidates) if candidates else None


def _hosted_agent_invoke_timeout(agent: Agent) -> float:
    return float(_agent_api_grant_ttl_seconds(agent))


def _agent_api_grant_ttl_seconds(agent: Agent) -> int:
    default_ttl = max(
        _AGENT_API_DEFAULT_TTL_SECONDS,
        _AGENT_API_DEFAULT_INVOKE_TIMEOUT_SECONDS,
    )
    max_runtime = _agent_max_runtime_seconds(agent)
    if max_runtime is None:
        return default_ttl
    return max(
        default_ttl,
        max_runtime + _AGENT_API_INVOKE_TIMEOUT_GRACE_SECONDS,
    )


def _validate_agent_api_skill(agent: Agent, skill_name: str) -> None:
    skills = _card_skills(agent.card if isinstance(agent.card, dict) else {})
    if skills and skill_name not in {str(item.get("name") or "") for item in skills}:
        raise HTTPException(404, f"unknown skill: {skill_name}")


def _skill_input_schema(skill: dict[str, Any]) -> dict[str, Any]:
    schema = skill.get("input_schema") or skill.get("inputSchema")
    if isinstance(schema, dict) and schema:
        return schema
    return {"type": "object", "properties": {}, "additionalProperties": True}


def _agent_api_skill(agent: Agent, skill_name: str) -> dict[str, Any] | None:
    skills = _card_skills(agent.card if isinstance(agent.card, dict) else {})
    for skill in skills:
        if str(skill.get("name") or "") == skill_name:
            return skill
    return None


def _file_upload_metadata(schema: Any) -> dict[str, Any] | None:
    if not isinstance(schema, dict):
        return None
    metadata = schema.get("x-a2a-file-upload")
    return metadata if isinstance(metadata, dict) else None


def _skill_file_upload_fields(skill: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not skill:
        return {}
    schema = _skill_input_schema(skill)
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    fields: dict[str, dict[str, Any]] = {}
    for name, property_schema in properties.items():
        metadata = _file_upload_metadata(property_schema)
        if isinstance(name, str) and metadata is not None:
            fields[name] = metadata
    return fields


def _agent_api_request_content(skill: dict[str, Any]) -> dict[str, Any]:
    schema = _skill_input_schema(skill)
    upload_fields = _skill_file_upload_fields(skill)
    if not upload_fields:
        return {"application/json": {"schema": schema}}

    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    multipart_properties: dict[str, Any] = {}
    encoding: dict[str, Any] = {}
    for name, property_schema in properties.items():
        if name in upload_fields:
            metadata = upload_fields[name]
            if metadata.get("multiple") is True:
                multipart_properties[name] = {
                    "type": "array",
                    "items": {"type": "string", "format": "binary"},
                }
            else:
                multipart_properties[name] = {"type": "string", "format": "binary"}
            accept = metadata.get("accept")
            if isinstance(accept, list) and accept:
                encoding[name] = {"contentType": ", ".join(str(item) for item in accept)}
        elif isinstance(property_schema, dict):
            multipart_properties[name] = dict(property_schema)
        else:
            multipart_properties[name] = {}

    multipart_schema: dict[str, Any] = {
        "type": "object",
        "properties": multipart_properties,
    }
    required = schema.get("required")
    if isinstance(required, list):
        multipart_schema["required"] = [str(item) for item in required if isinstance(item, str)]
    if "additionalProperties" in schema:
        multipart_schema["additionalProperties"] = schema["additionalProperties"]
    content: dict[str, Any] = {
        "multipart/form-data": {
            "schema": multipart_schema,
        }
    }
    if encoding:
        content["multipart/form-data"]["encoding"] = encoding
    return content


def _skill_output_schema(skill: dict[str, Any]) -> dict[str, Any]:
    schema = skill.get("output_schema") or skill.get("outputSchema")
    if isinstance(schema, dict) and schema:
        return schema
    return {}


def _file_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "path": {"type": "string"},
            "download_url": {"type": "string", "format": "uri"},
            "mime_type": {"type": "string"},
            "size_bytes": {"type": "integer", "minimum": 0},
            "source": {"type": "string"},
            "metadata": {"type": "object", "additionalProperties": True},
        },
        "required": ["name", "path", "download_url"],
        "additionalProperties": True,
    }


def _agent_api_response_schema(skill: dict[str, Any]) -> dict[str, Any]:
    result_schema = _skill_output_schema(skill) or {}
    return {
        "type": "object",
        "properties": {
            "result": result_schema or {},
            "structured": result_schema or {},
            "file_outputs": {
                "type": "array",
                "items": _file_output_schema(),
            },
            "artifacts": {
                "type": "array",
                "items": {"type": "object", "additionalProperties": True},
            },
            "events": {
                "type": "array",
                "items": {"type": "object", "additionalProperties": True},
            },
            "grant_id": {"type": "string", "nullable": True},
        },
        "required": ["result", "file_outputs"],
        "additionalProperties": True,
    }


def _agent_api_run_schema(skill: dict[str, Any] | None = None) -> dict[str, Any]:
    result_schema = _agent_api_response_schema(skill) if skill is not None else {
        "type": "object",
        "additionalProperties": True,
    }
    return {
        "type": "object",
        "properties": {
            "run_id": {"type": "string"},
            "job_id": {"type": "string"},
            "status": {
                "type": "string",
                "enum": ["queued", "running", "complete", "error", "failed"],
            },
            "agent": {"type": "string"},
            "skill": {"type": "string"},
            "poll_url": {"type": "string", "format": "uri"},
            "result": {"anyOf": [result_schema, {"type": "null"}]},
            "error": {"type": ["string", "null"]},
            "created_at": {"type": ["string", "null"], "format": "date-time"},
            "updated_at": {"type": ["string", "null"], "format": "date-time"},
            "completed_at": {"type": ["string", "null"], "format": "date-time"},
        },
        "required": ["run_id", "job_id", "status", "agent", "skill", "poll_url"],
        "additionalProperties": True,
    }


def _agent_openapi_document(
    agent: Agent,
    request: Request | None = None,
    *,
    integration_token: str | None = None,
) -> dict[str, Any]:
    card = agent.card if isinstance(agent.card, dict) else {}
    access_policy = account_access_policy(card)
    skills = _card_skills(card)
    paths: dict[str, Any] = {}
    integration_token_param: dict[str, Any] | None = None
    if integration_token:
        integration_token_param = {
            "name": "integration_token",
            "in": "query",
            "required": True,
            "schema": {"type": "string", "default": integration_token},
            "description": "Pre-authorized integration link token.",
        }
    operation_security: list[dict[str, list[Any]]] = (
        [] if integration_token_param is not None else [{"AgentApiToken": []}]
    )
    file_security: list[dict[str, list[Any]]] = (
        []
        if integration_token_param is not None
        else [
            {"AgentApiToken": []},
            {"ControlPlaneBearer": []},
            {"ControlPlaneSession": []},
        ]
    )
    for skill in skills:
        skill_name = str(skill.get("name") or "")
        description = str(skill.get("description") or f"Invoke {skill_name}.")
        paths[f"/invoke/{quote(skill_name, safe='')}"] = {
            "post": {
                "operationId": _safe_operation_id(agent.name, skill_name),
                "summary": description[:120],
                "description": description,
                "security": operation_security,
                "parameters": [integration_token_param] if integration_token_param else [],
                "requestBody": {
                    "required": True,
                    "content": _agent_api_request_content(skill),
                },
                "responses": {
                    "200": {
                        "description": "Agent skill result with structured file outputs.",
                        "content": {
                            "application/json": {
                                "schema": _agent_api_response_schema(skill),
                            }
                        },
                    },
                    **(
                        {
                            "402": {
                                "description": (
                                    "Platform-funded calls are exhausted; add a BYOK model key."
                                )
                            }
                        }
                        if access_policy.enabled
                        else {}
                    ),
                },
            }
        }
        paths[f"/runs/{quote(skill_name, safe='')}/start"] = {
            "post": {
                "operationId": _safe_operation_id(agent.name, skill_name, "start_run"),
                "summary": f"Start async: {description[:100]}",
                "description": (
                    f"{description}\n\nCreates a durable async run and returns immediately. "
                    "Poll the returned poll_url, or GET /runs/{run_id}, until status is complete or error."
                ),
                "security": operation_security,
                "parameters": [integration_token_param] if integration_token_param else [],
                "requestBody": {
                    "required": True,
                    "content": _agent_api_request_content(skill),
                },
                "responses": {
                    "202": {
                        "description": "Async agent run accepted.",
                        "content": {
                            "application/json": {
                                "schema": _agent_api_run_schema(skill),
                            }
                        },
                    }
                },
            }
        }
    paths["/runs/{run_id}"] = {
        "get": {
            "operationId": _safe_operation_id(agent.name, "run", "get"),
            "summary": "Get async run status",
            "description": "Returns status and, when complete, the normalized agent result.",
            "security": operation_security,
            "parameters": [
                {
                    "name": "run_id",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string"},
                }
            ] + ([integration_token_param] if integration_token_param else []),
            "responses": {
                "200": {
                    "description": "Async agent run status.",
                    "content": {
                        "application/json": {
                            "schema": _agent_api_run_schema(),
                        }
                    },
                }
            },
        }
    }
    paths["/files/{path}"] = {
        "get": {
            "operationId": _safe_operation_id(agent.name, "file", "download"),
            "summary": "Download an agent output file",
            "description": (
                "Streams the file bytes for a file path returned by this agent."
            ),
            "security": file_security,
            "parameters": [
                {
                    "name": "path",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string"},
                    "description": "Workspace file path, including nested folders.",
                }
            ] + ([integration_token_param] if integration_token_param else []),
            "responses": {
                "200": {
                    "description": "Agent output file bytes.",
                    "content": {
                        "application/octet-stream": {
                            "schema": {"type": "string", "format": "binary"}
                        }
                    },
                }
            },
        }
    }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": f"{agent.name} API",
            "description": agent.description or card.get("description") or "",
            "version": str(card.get("version") or agent.version or "0.1.0"),
            **(
                {
                    "x-a2a-account-access": {
                        "required": True,
                        "platform_skill_calls": access_policy.platform_skill_calls,
                        "after_trial": access_policy.after_trial,
                    }
                }
                if access_policy.enabled
                else {}
            ),
        },
        "servers": [{"url": _agent_api_base_url(agent, request=request)}],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "AgentApiToken": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "A2A Platform App API Token",
                },
                "ControlPlaneBearer": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "Control Plane JWT or Keycloak access token",
                },
                "ControlPlaneSession": {
                    "type": "apiKey",
                    "in": "cookie",
                    "name": settings.session_cookie_name,
                }
            },
            "schemas": {
                "FileOutput": _file_output_schema(),
                "AgentApiRun": _agent_api_run_schema(),
            },
        },
    }


def _invoke_arguments_from_body(body: Any) -> dict[str, Any]:
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")
    if "arguments" in body:
        arguments = body.get("arguments")
        if not isinstance(arguments, dict):
            raise HTTPException(400, "arguments must be a JSON object")
        return arguments
    return body


def _agent_runtime_endpoint(agent: Agent, endpoint_name: str) -> dict[str, Any]:
    card = agent.card if isinstance(agent.card, dict) else {}
    runtime = card.get("runtime") if isinstance(card.get("runtime"), dict) else {}
    endpoints = runtime.get("endpoints") if isinstance(runtime.get("endpoints"), list) else []
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            continue
        if str(endpoint.get("name") or "").strip() == endpoint_name:
            return endpoint
    raise HTTPException(404, "agent endpoint not found")


def _agent_endpoint_methods(endpoint: dict[str, Any]) -> set[str]:
    raw_methods = endpoint.get("methods")
    if isinstance(raw_methods, list) and raw_methods:
        return {str(item).strip().upper() for item in raw_methods if str(item).strip()}
    method = str(endpoint.get("method") or "").strip().upper()
    return {method} if method else {"POST"}


def _is_json_media_type(content_type: str) -> bool:
    media_type = content_type.split(";", 1)[0].strip().lower()
    return media_type == "application/json" or media_type.endswith("+json")


async def _agent_endpoint_body_from_request(request: Request) -> Any:
    raw = await request.body()
    if not raw:
        return None
    content_type = request.headers.get("content-type", "")
    if _is_json_media_type(content_type):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "endpoint body must be valid JSON") from exc
    return raw.decode("utf-8", errors="replace")


def _forwardable_endpoint_headers(request: Request) -> dict[str, str]:
    blocked = {"authorization", "cookie", "x-api-key"}
    return {
        str(key).lower(): value
        for key, value in request.headers.items()
        if str(key).lower() not in blocked
    }


def _forwardable_endpoint_query(request: Request) -> dict[str, str]:
    blocked = {"integration_token", "access_token", "token", "api_key", "x-api-key"}
    return {
        key: value
        for key, value in request.query_params.items()
        if str(key).lower() not in blocked
    }


async def _agent_endpoint_arguments_from_request(
    request: Request,
    endpoint: dict[str, Any],
) -> dict[str, Any]:
    body_arg = str(endpoint.get("body_arg") or "body").strip() or "body"
    arguments: dict[str, Any] = {
        body_arg: await _agent_endpoint_body_from_request(request),
    }
    headers_arg = str(endpoint.get("headers_arg") or "").strip()
    if headers_arg:
        arguments[headers_arg] = _forwardable_endpoint_headers(request)
    query_arg = str(endpoint.get("query_arg") or "").strip()
    if query_arg:
        arguments[query_arg] = _forwardable_endpoint_query(request)
    return arguments


def _safe_upload_filename(value: str | None) -> str:
    name = (value or "upload").rsplit("/", 1)[-1].rsplit("\\", 1)[-1].strip()
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return name or "upload"


def _media_type_allowed(content_type: str, accept: Any) -> bool:
    if not isinstance(accept, list) or not accept:
        return True
    normalized = content_type.split(";", 1)[0].strip().lower()
    for item in accept:
        pattern = str(item or "").split(";", 1)[0].strip().lower()
        if not pattern:
            continue
        if pattern == normalized:
            return True
        if pattern.endswith("/*") and normalized.startswith(pattern[:-1]):
            return True
    return False


def _parse_multipart_scalar(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    cleaned = value.strip()
    if not cleaned:
        return value
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return value


async def _invoke_arguments_from_multipart(
    request: Request,
    *,
    agent: Agent,
    skill_name: str,
    user: User,
) -> dict[str, Any]:
    skill = _agent_api_skill(agent, skill_name)
    upload_fields = _skill_file_upload_fields(skill)
    form = await request.form()
    arguments: dict[str, Any] = {}
    call_id = secrets.token_hex(8)
    bucket = bucket_for_user(user.id)

    for name, value in form.multi_items():
        field_name = str(name)
        is_upload = hasattr(value, "filename") and hasattr(value, "read")
        if not is_upload:
            parsed = _parse_multipart_scalar(value)
            if field_name == "arguments":
                if not isinstance(parsed, dict):
                    raise HTTPException(400, "multipart arguments field must be a JSON object")
                arguments.update(parsed)
            else:
                arguments[field_name] = parsed
            continue

        metadata = upload_fields.get(field_name)
        filename = _safe_upload_filename(getattr(value, "filename", None))
        content_type = str(getattr(value, "content_type", None) or "application/octet-stream")
        if metadata is not None and not _media_type_allowed(content_type, metadata.get("accept")):
            raise HTTPException(415, f"file field {field_name!r} does not match accepted media types")
        data = await value.read()
        max_bytes = metadata.get("max_bytes") if metadata is not None else None
        if isinstance(max_bytes, int) and len(data) > max_bytes:
            raise HTTPException(413, f"file field {field_name!r} exceeds max_bytes={max_bytes}")
        path = f"inputs/api/{agent.name}/{call_id}/{field_name}/{filename}"
        info = upload_file(bucket, path, data, content_type)
        uploaded = {
            "path": path,
            "filename": filename,
            "media_type": content_type,
            "size_bytes": int(info.get("size") or len(data)),
        }
        if metadata is not None and metadata.get("multiple") is True:
            existing = arguments.setdefault(field_name, [])
            if not isinstance(existing, list):
                raise HTTPException(400, f"file field {field_name!r} received mixed single and multiple values")
            existing.append(uploaded)
        else:
            arguments[field_name] = uploaded
    return arguments


async def _invoke_arguments_from_request(
    request: Request,
    *,
    agent: Agent,
    skill_name: str,
    user: User,
) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "").lower()
    if content_type.startswith("multipart/form-data"):
        return await _invoke_arguments_from_multipart(
            request,
            agent=agent,
            skill_name=skill_name,
            user=user,
        )
    try:
        raw_body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, "request body must be valid JSON") from exc
    return _invoke_arguments_from_body(raw_body)


def _hosted_agent_invoke_url(agent: Agent, skill_name: str) -> str:
    base_url = f"http://{agent.name}.agents.svc.cluster.local"
    return f"{base_url}/invoke/{quote(skill_name, safe='')}"


def _card_llm_provisioning(agent: Agent) -> str:
    card = agent.card if isinstance(agent.card, dict) else {}
    runtime = card.get("runtime") if isinstance(card.get("runtime"), dict) else {}
    return str(runtime.get("llm_provisioning") or "").strip().lower()


def _load_platform_llm_settings() -> Any | None:
    try:
        from main_agent.config import load_settings as load_runtime_settings

        return load_runtime_settings()
    except Exception:  # noqa: BLE001
        log.exception("could not load platform LLM runtime settings")
        return None


def _platform_llm_grant_kwargs(agent: Agent) -> dict[str, Any]:
    if _card_llm_provisioning(agent) not in {"platform", "platform_or_caller_provided"}:
        return {}
    runtime_settings = _load_platform_llm_settings()
    if runtime_settings is None:
        return {}
    models = tuple(item for item in runtime_settings.platform_llm_models if item)
    if not models:
        return {}
    return {
        "llm_models": models,
        "llm_max_budget_usd": runtime_settings.platform_llm_max_budget_usd,
        "llm_rpm_limit": runtime_settings.platform_llm_rpm_limit,
        "llm_tpm_limit": runtime_settings.platform_llm_tpm_limit,
    }


def _platform_llm_creds_for_grant(
    agent: Agent,
    grant_token: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    kwargs = _platform_llm_grant_kwargs(agent)
    models = tuple(kwargs.get("llm_models") or ())
    if not models:
        return None
    runtime_settings = _load_platform_llm_settings()
    if runtime_settings is None:
        return None
    return {
        "base_url": runtime_settings.litellm_url.rstrip("/") + "/v1",
        "api_key": grant_token,
        "model": models[0],
        "temperature_mode": "omit",
        "extra_body": {},
        "metadata": dict(metadata or {}),
    }


def _agent_api_llm_metadata(
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
            "a2a_llm_source": "agent_api",
        }.items()
        if value is not None
    }


async def _audit_agent_api_grant(
    *,
    session: AsyncSession,
    user: User,
    payload: dict[str, Any],
) -> None:
    try:
        row = GrantAudit(
            grant_id=payload["grant_id"],
            parent_grant_id=None,
            issuer=payload["issuer"],
            audience=payload["audience"],
            bucket=payload["bucket"],
            mode=payload["mode"],
            allow_patterns=list(payload.get("allow_patterns") or []),
            deny_patterns=list(payload.get("deny_patterns") or []),
            outputs_prefix=payload.get("outputs_prefix"),
            ttl_seconds=int(payload.get("expires_at", 0) - payload.get("issued_at", 0)),
            user_id=user.id,
            decision="auto_approve",
            decided_by="api_token",
            reason="agent API token invoke",
        )
        session.add(row)
        await session.commit()
    except IntegrityError:
        await session.rollback()
    except Exception:  # noqa: BLE001
        await session.rollback()


async def _post_hosted_agent_invoke(
    *,
    agent: Agent,
    skill_name: str,
    body: dict[str, Any],
    authorization: str,
    grant_id: str | None = None,
) -> dict[str, Any]:
    timeout_seconds = _hosted_agent_invoke_timeout(agent)
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(
                _hosted_agent_invoke_url(agent, skill_name),
                json=body,
                headers={"Authorization": authorization},
            )
    except httpx.TimeoutException as exc:
        # The platform's own failure, not the agent's: nothing was observed.
        raise _agent_invoke_error(
            agent_name=agent.name,
            skill_name=skill_name,
            reason="agent_timeout",
            status_code=504,
            timeout_seconds=timeout_seconds,
            grant_id=grant_id,
        ) from exc
    except httpx.RequestError as exc:
        raise _agent_invoke_error(
            agent_name=agent.name,
            skill_name=skill_name,
            reason="agent_unreachable",
            status_code=502,
            excerpt=type(exc).__name__,
            grant_id=grant_id,
        ) from exc
    if response.status_code >= 400:
        raise _agent_invoke_error(
            agent_name=agent.name,
            skill_name=skill_name,
            reason="agent_error",
            status_code=_agent_error_status(response.status_code),
            agent_status=response.status_code,
            excerpt=_redacted_agent_excerpt(response.text),
            grant_id=grant_id,
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise _agent_invoke_error(
            agent_name=agent.name,
            skill_name=skill_name,
            reason="agent_bad_response",
            status_code=502,
            agent_status=response.status_code,
            excerpt=_redacted_agent_excerpt(response.text),
            grant_id=grant_id,
        ) from exc
    return payload if isinstance(payload, dict) else {"result": payload}


async def _call_hosted_agent_api_skill(
    *,
    agent: Agent,
    user: User,
    session: AsyncSession,
    skill_name: str,
    arguments: dict[str, Any],
    consumer_setup: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], str]:
    byok_creds: dict[str, Any] | None = None
    access_decision = None
    policy = account_access_policy(agent.card)
    if policy.enabled:
        from .llm_creds import get_creds_for_user

        byok_creds = await get_creds_for_user(user.id, session, name="default")
        if byok_creds is None and not _platform_llm_grant_kwargs(agent):
            raise HTTPException(503, "platform-funded LLM trial is unavailable")
        access_decision = await resolve_account_llm_access(
            session,
            agent=agent,
            user_id=user.id,
            skill_name=skill_name,
            has_byok=byok_creds is not None,
        )

    call_id = secrets.token_hex(8)
    outputs_prefix = f"outputs/api/{agent.name}/{call_id}/"
    extra_write_prefixes, source_grants = _agent_api_extra_workspace_grants(
        agent=agent,
        skill_name=skill_name,
        arguments=arguments,
    )
    grant_kwargs = _platform_llm_grant_kwargs(agent)
    if access_decision is not None and access_decision.source == "byok":
        from .chat import _litellm_model_alias

        alias = _litellm_model_alias(user.id, "default")
        runtime_settings = _load_platform_llm_settings()
        grant_kwargs = {
            "llm_models": (alias,),
            "llm_max_budget_usd": getattr(
                runtime_settings, "platform_llm_max_budget_usd", 1.0
            ),
            "llm_rpm_limit": getattr(
                runtime_settings, "platform_llm_rpm_limit", 60
            ),
            "llm_tpm_limit": getattr(
                runtime_settings, "platform_llm_tpm_limit", 200000
            ),
        }
    grant_token, payload = mint_grant_token(
        issuer=f"app-api:user-{user.id}",
        audience=agent.name,
        bucket=bucket_for_user(user.id),
        mode="read_write_overlay",
        allow_patterns=("**",),
        outputs_prefix=outputs_prefix,
        write_prefixes=(outputs_prefix, *extra_write_prefixes),
        source_grants=source_grants,
        ttl_seconds=_agent_api_grant_ttl_seconds(agent),
        **grant_kwargs,
    )
    await _audit_agent_api_grant(session=session, user=user, payload=payload)
    cp_token = _hosted_invoke_cp_token(agent=agent, user=user)
    invoke_body: dict[str, Any] = {
        "arguments": arguments,
        "grant": grant_token,
        "cp_url": settings.public_cp_url.rstrip("/") or None,
        # Same credential as the Authorization header below, and the SDK's
        # subagent-run tracking reads only this field (no header fallback), so
        # withholding it would buy nothing — the token is in the header on the
        # same request either way — while silently blanking the work ledger and
        # LLM cost attribution for every agent that did not set
        # ``runtime.wants_cp_jwt``. The boundary is the token's shape.
        "cp_jwt": cp_token,
    }
    if consumer_setup:
        invoke_body.update(
            {
                key: value
                for key, value in consumer_setup.items()
                if key in {"consumer_config", "consumer_secrets"}
                and isinstance(value, dict)
                and value
            }
        )
    llm_metadata = _agent_api_llm_metadata(
        user=user,
        grant_payload=payload,
        agent=agent,
        skill_name=skill_name,
    )
    if access_decision is not None:
        llm_metadata["a2a_account_access_source"] = access_decision.source
        llm_metadata["a2a_platform_skill_calls_remaining"] = (
            access_decision.remaining
        )
    if access_decision is not None and access_decision.source == "byok":
        from .chat import _main_llm_runtime_creds

        llm_creds = await _main_llm_runtime_creds(
            byok_creds,
            user_id=user.id,
            llm_creds_name="default",
            runtime_litellm_key=grant_token,
            litellm_metadata=llm_metadata,
        )
    else:
        llm_creds = _platform_llm_creds_for_grant(
            agent,
            grant_token,
            metadata=llm_metadata,
        )
    if llm_creds is not None:
        invoke_body["llm_creds"] = llm_creds
    return (
        await _post_hosted_agent_invoke(
            agent=agent,
            skill_name=skill_name,
            body=invoke_body,
            authorization=f"Bearer {cp_token}",
            grant_id=str(payload.get("grant_id") or ""),
        ),
        outputs_prefix,
    )


def _hosted_invoke_cp_token(*, agent: Agent, user: User) -> str:
    """The caller credential handed to a hosted agent for one invocation.

    Marketplace agents get a scoped invoke token: it is not a session and it
    names the invoked agent as its audience. The platform's own build
    specialists still need the full source/deploy surface on the caller's
    behalf, so they keep an ordinary credential.

    Either way the lifetime is the workspace grant's *clamped* to
    ``AGENT_INVOKE_TOKEN_MAX_TTL_SECONDS``. The grant TTL is derived from the
    agent card, which the seller controls and nothing validates, so an
    unclamped credential would be exactly as long-lived as the card asked for.
    """
    return issue_invocation_cp_credential(
        user.id,
        agent=agent.name,
        ttl_seconds=_agent_api_grant_ttl_seconds(agent),
    )


def _agent_api_extra_workspace_grants(
    *,
    agent: Agent,
    skill_name: str,
    arguments: dict[str, Any],
) -> tuple[tuple[str, ...], tuple[dict[str, Any], ...]]:
    prefixes: list[str] = []
    skill = _agent_api_skill(agent, skill_name)
    policy = skill.get("policy") if isinstance(skill, dict) else None
    if isinstance(policy, dict):
        outputs_prefix = _render_agent_api_grant_template(
            policy.get("grant_outputs_prefix"),
            agent=agent,
            skill_name=skill_name,
            arguments=arguments,
        )
        if outputs_prefix:
            prefixes.append(_normalize_agent_api_write_prefix(outputs_prefix))
        for prefix in _render_agent_api_grant_templates(
            policy.get("grant_write_prefixes"),
            agent=agent,
            skill_name=skill_name,
            arguments=arguments,
        ):
            prefixes.append(_normalize_agent_api_write_prefix(prefix))

    # Compatibility fallback for older live agent-builder cards that do not
    # expose skill policy metadata yet.
    if agent.name == "agent-builder" and skill_name == "build":
        target = arguments.get("name")
        if isinstance(target, str) and target.strip():
            agent_name = _validate_agent_name(target)
            prefixes.append(f"agents/{agent_name}/")

    write_prefixes = _dedupe_strings(prefixes)
    return write_prefixes, _agent_api_source_grants_for_write_prefixes(write_prefixes)


def _render_agent_api_grant_templates(
    value: Any,
    *,
    agent: Agent,
    skill_name: str,
    arguments: dict[str, Any],
) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        rendered = _render_agent_api_grant_template(
            value,
            agent=agent,
            skill_name=skill_name,
            arguments=arguments,
        )
        return (rendered,) if rendered else ()
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            rendered = _render_agent_api_grant_template(
                item,
                agent=agent,
                skill_name=skill_name,
                arguments=arguments,
            )
            if rendered:
                out.append(rendered)
        return tuple(out)
    return ()


def _render_agent_api_grant_template(
    value: Any,
    *,
    agent: Agent,
    skill_name: str,
    arguments: dict[str, Any],
) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    mapping = {
        "agent": agent.name,
        "agent_name": agent.name,
        "skill": skill_name,
        "skill_name": skill_name,
    }
    for key, arg_value in arguments.items():
        if isinstance(key, str) and isinstance(arg_value, (str, int, float, bool)):
            mapping[key] = str(arg_value)
    try:
        return raw.format_map(_AgentApiGrantTemplateMapping(mapping))
    except (KeyError, ValueError):
        return raw


class _AgentApiGrantTemplateMapping(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _normalize_agent_api_write_prefix(prefix: str) -> str:
    clean = prefix.replace("\\", "/").strip()
    if not clean:
        return clean
    return clean if clean.endswith("/") else clean + "/"


def _agent_api_source_grants_for_write_prefixes(
    write_prefixes: tuple[str, ...],
) -> tuple[dict[str, Any], ...]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for prefix in write_prefixes:
        parts = prefix.strip("/").split("/")
        if len(parts) < 2 or parts[0] != "agents":
            continue
        try:
            agent_name = _validate_agent_name(parts[1])
        except HTTPException:
            continue
        if agent_name in seen:
            continue
        seen.add(agent_name)
        out.append({"agent": agent_name, "scope": "write"})
    return tuple(out)


def _dedupe_strings(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    out: list[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return tuple(out)


async def _invoke_agent_api_skill(
    *,
    agent: Agent,
    user: User,
    skill_name: str,
    arguments: dict[str, Any],
    session: AsyncSession,
) -> tuple[dict[str, Any], str | None]:
    _validate_agent_api_skill(agent, skill_name)
    if _is_external_agent(agent):
        raw_result = await _call_external_agent_skill(
            agent=agent,
            session=session,
            user=user,
            skill_name=skill_name,
            arguments=arguments,
        )
        return {"result": raw_result, "events": [], "artifacts": []}, None

    try:
        consumer_setup = await require_consumer_setup(
            agent=agent,
            user=user,
            session=session,
        )
    except ConsumerSetupRequired as exc:
        raise HTTPException(
            409,
            setup_required_payload(agent=agent, resolution=exc.resolution),
        ) from exc
    try:
        return await _call_hosted_agent_api_skill(
            agent=agent,
            user=user,
            session=session,
            skill_name=skill_name,
            arguments=arguments,
            consumer_setup=consumer_setup.invocation_payload(),
        )
    except AgentBYOKRequired as exc:
        raise HTTPException(402, exc.payload) from exc


def _agent_api_failure_message(result: dict[str, Any]) -> str | None:
    def _check(value: Any) -> str | None:
        if not isinstance(value, dict):
            return None
        status = str(value.get("status") or "").strip().lower()
        if value.get("ok") is not False and status not in {"failed", "error"}:
            return None
        message = value.get("error") or value.get("stop_reason")
        return str(message or "agent API run returned a failure payload")

    for key in ("structured", "result", "structuredContent"):
        message = _check(result.get(key))
        if message is not None:
            return message
    nested = result.get("result")
    if isinstance(nested, dict):
        for key in ("structured", "structuredContent", "result"):
            message = _check(nested.get(key))
            if message is not None:
                return message
    return None


def _agent_api_receipt_status(result: dict[str, Any]) -> str:
    return "error" if _agent_api_failure_message(result) is not None else "ok"


async def _persist_agent_api_receipt(
    *,
    session: AsyncSession,
    agent: Agent,
    user: User,
    skill_name: str,
    arguments: dict[str, Any],
    result: Any,
    started_at: datetime,
    status: str = "ok",
    error_type: str = "",
    task_id: str = "",
    self_heal_actionable: bool = False,
) -> str | None:
    """Seal and stage the run's receipt; returns its id, or ``None`` if unsigned."""
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    ended_at = datetime.now(timezone.utc)
    agent_name = str(agent.name)
    agent_version = str((agent.card or {}).get("version") or "")
    caller = f"user:{user.id}"
    try:
        receipt, signed_token = seal_receipt(
            agent_name=agent_name,
            agent_version=agent_version,
            caller=caller,
            task_id=task_id,
            skill_name=skill_name,
            inputs=arguments,
            result=result,
            status=status,
            error_type=error_type,
            started_at=int(started_at.timestamp()),
            ended_at=int(ended_at.timestamp()),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("agent API receipt signing failed for %s.%s: %s", agent_name, skill_name, exc)
        return None

    row = AgentReceipt(
        receipt_id=receipt.receipt_id,
        agent_id=agent.id,
        agent_name=agent_name,
        agent_version=receipt.agent_version,
        caller=receipt.caller,
        task_id=receipt.task_id,
        skill_name=receipt.skill_name,
        status=receipt.status,
        eval_score=receipt.eval_score,
        started_at=started_at,
        ended_at=ended_at,
        elapsed_ms=max(0, int((ended_at - started_at).total_seconds() * 1000)),
        signed_token=signed_token,
        payload=receipt.model_dump(mode="json"),
    )
    session.add(row)

    if receipt.status == "error" and self_heal_actionable:
        try:
            from ..self_healing import maybe_enqueue_runtime_failure

            await session.flush()
            await maybe_enqueue_runtime_failure(
                session,
                agent_id=agent.id,
                receipt_id=receipt.receipt_id,
                skill_name=skill_name,
                error_type=error_type,
                error_preview=str(result),
                force_actionable=True,
            )
        except Exception:  # noqa: BLE001
            await session.rollback()
            log.exception(
                "failed to evaluate self-healing policy for Agent API receipt %s",
                receipt.receipt_id,
            )
            # The rollback discarded the row this id names. Returning it would
            # hand the caller a receipt reference that resolves to nothing -
            # a new false claim, on the one path whose job is not to make any.
            return None
    return receipt.receipt_id


def _agent_api_error_result(exc: BaseException) -> dict[str, Any]:
    """What a failed run's signed receipt records about the failure.

    An agent-reported failure arrives already structured and already redacted;
    anything else is rendered through the same bounded redactor. The raw agent
    body must not land here — it used to, and a receipt is signed and durable.
    """
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        return {"error": type(exc).__name__, **detail}
    raw = detail if isinstance(detail, str) and detail else str(exc)
    return {
        "error": type(exc).__name__,
        "message": _redacted_agent_excerpt(raw) or type(exc).__name__,
    }


def _attach_failure_receipt_id(exc: BaseException, receipt_id: str | None) -> None:
    """Point the caller at the signed receipt for the run that just failed.

    The receipt is committed by the handler's ``finally`` before the exception
    leaves, so the id is live by the time the client reads it. An id only
    arrives here when the row survived, so it never names a receipt that was
    rolled back. Both halves ``a2a receipt show <id> --agent <name>`` needs are
    in this detail: a bare id resolves only out of the CLI's local cache, and a
    receipt sealed for an HTTP Agent API call was never written there.
    """
    if not receipt_id or not isinstance(exc, AgentApiInvokeFailed):
        return
    if isinstance(exc.detail, dict):
        exc.detail["receipt_id"] = receipt_id


async def _record_agent_api_subagent_failure(
    *,
    session: AsyncSession,
    user: User,
    grant_id: str | None,
    agent: Agent,
    skill_name: str,
    arguments: dict[str, Any],
    message: str,
    error_payload: dict[str, Any],
) -> None:
    if not grant_id:
        return
    recorder = SubagentRunRecorder(
        session=session,
        user_id=user.id,
        thread_id=None,
    )
    await recorder.record(
        {
            "type": "agent_invoke_error",
            "grant_id": grant_id,
            "to": agent.name,
            "agent": agent.name,
            "skill": skill_name,
            "args_json": json.dumps(arguments, separators=(",", ":")),
            "scopes": {},
            "ok": False,
            "summary": message,
            "error": message,
            "result": error_payload,
        }
    )


def _agent_api_run_out(
    job: Any,
    agent: Agent | None = None,
    request: Request | None = None,
    *,
    integration_token: str | None = None,
) -> dict[str, Any]:
    data = serialize_job(job)
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    agent_name = str(
        metadata.get("agent")
        or payload.get("agent")
        or (agent.name if agent is not None else "")
    )
    skill_name = str(metadata.get("skill") or payload.get("skill") or "")
    run_id = str(data.get("job_id") or "")
    status = str(data.get("status") or "queued")
    result = data.get("result") if status in {"complete", "completed"} else None
    error = data.get("error")
    poll_url = (
        _agent_api_run_poll_url(
            agent,
            run_id,
            request=request,
            integration_token=integration_token,
        )
        if agent is not None
        else str(metadata.get("poll_url") or "")
    )
    return {
        "run_id": run_id,
        "job_id": run_id,
        "status": status,
        "agent": agent_name,
        "skill": skill_name,
        "poll_url": poll_url,
        "result": result,
        "error": error,
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
        "completed_at": data.get("completed_at"),
    }


async def _get_agent_api_run(
    *,
    session: AsyncSession,
    agent: Agent,
    user: User,
    run_id: str,
) -> Any:
    job = await get_job(session, run_id, user_id=user.id)
    if job is None:
        raise HTTPException(404, "run not found")
    data = serialize_job(job)
    if data.get("kind") != _AGENT_API_RUN_KIND:
        raise HTTPException(404, "run not found")
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    if str(metadata.get("agent") or "") != agent.name:
        raise HTTPException(404, "run not found")
    return job


async def _start_agent_api_run_job(
    *,
    session: AsyncSession,
    agent: Agent,
    user: User,
    skill_name: str,
    arguments: dict[str, Any],
    request: Request | None = None,
) -> Any:
    _validate_agent_api_skill(agent, skill_name)
    run_id = f"api-{secrets.token_hex(8)}"
    public_base_url = _agent_api_public_base_url(request)
    job = await create_job(
        session,
        user_id=user.id,
        kind=_AGENT_API_RUN_KIND,
        payload={
            "agent": agent.name,
            "skill": skill_name,
            "arguments": arguments,
        },
        title=f"{agent.name}.{skill_name}",
        metadata={
            "agent": agent.name,
            "agent_id": agent.id,
            "skill": skill_name,
            "poll_url": _agent_api_run_poll_url(agent, run_id, request=request),
            "public_base_url": public_base_url,
        },
        job_id=run_id,
        status="queued",
        queue="agent-api",
        subject_type="agent",
        subject_id=agent.name,
        worker_type="agent",
        worker_name=agent.name,
    )
    await nudge_agent_api_worker()
    return job


async def _execute_agent_api_run_job(
    *,
    session: AsyncSession,
    job_id: str,
    user_id: int | None = None,
    agent_id: int | None = None,
    skill_name: str | None = None,
    arguments: dict[str, Any] | None = None,
) -> None:
    job = await get_job(session, job_id, user_id=user_id)
    if job is None:
        return
    if str(getattr(job, "status", "queued")) not in {"queued", "running"}:
        return
    job_data = serialize_job(job)
    payload = job_data.get("payload") if isinstance(job_data.get("payload"), dict) else {}
    metadata = (
        job_data.get("metadata") if isinstance(job_data.get("metadata"), dict) else {}
    )
    if user_id is None:
        raw_user_id = job_data.get("user_id")
        user_id = int(raw_user_id) if raw_user_id is not None else None
    if agent_id is None:
        raw_agent_id = metadata.get("agent_id")
        agent_id = int(raw_agent_id) if raw_agent_id is not None else None
    if skill_name is None:
        skill_name = str(payload.get("skill") or metadata.get("skill") or "")
    if arguments is None:
        raw_arguments = payload.get("arguments")
        arguments = raw_arguments if isinstance(raw_arguments, dict) else {}
    if user_id is None or agent_id is None:
        await fail_job(
            session,
            job,
            error="agent api run is missing user_id or agent_id",
            event_type="agent_api_run_failed",
        )
        return
    agent = await session.get(Agent, agent_id)
    user = await session.get(User, user_id)
    public_base_url = str(metadata.get("public_base_url") or "").strip() or None
    if agent is None or user is None:
        await fail_job(
            session,
            job,
            error="agent or user not found",
            user_id=user_id,
            event_type="agent_api_run_failed",
        )
        return

    now = datetime.now(timezone.utc)
    job.status = "running"
    job.started_at = job.started_at or now
    job.updated_at = now
    await append_event(
        session,
        job,
        event_type="agent_api_run_started",
        status="running",
        user_id=user_id,
        payload={"agent": agent.name, "skill": skill_name},
        commit=False,
    )
    await session.commit()
    await session.refresh(job)

    receipt_started_at = job.started_at or now
    try:
        raw_payload, outputs_prefix = await _invoke_agent_api_skill(
            agent=agent,
            user=user,
            skill_name=skill_name,
            arguments=arguments,
            session=session,
        )
        result = _normalize_agent_api_result(
            payload=raw_payload,
            agent_name=agent.name,
            skill_name=skill_name,
            outputs_prefix=outputs_prefix,
            public_base_url=public_base_url,
        )
        failure_message = _agent_api_failure_message(result)
        await _persist_agent_api_receipt(
            session=session,
            agent=agent,
            user=user,
            skill_name=skill_name,
            arguments=arguments,
            result=result,
            started_at=receipt_started_at,
            status="error" if failure_message is not None else "ok",
            error_type="agent_api_failure_payload" if failure_message is not None else "",
            task_id=job_id,
        )
        if failure_message is not None:
            await fail_job(
                session,
                job,
                error=failure_message,
                result=result,
                user_id=user_id,
                event_type="agent_api_run_failed",
            )
            return
        await complete_job(
            session,
            job,
            result=result,
            summary="Agent API run complete",
            user_id=user_id,
            event_type="agent_api_run_completed",
        )
    except Exception as exc:  # noqa: BLE001
        await session.rollback()
        refreshed_agent = await session.get(Agent, agent_id)
        refreshed_user = await session.get(User, user_id)
        if refreshed_agent is not None:
            agent = refreshed_agent
        if refreshed_user is not None:
            user = refreshed_user
        detail = getattr(exc, "detail", None)
        status_code = getattr(exc, "status_code", None)
        # Prefer a human message the client can render; only fall back to the
        # raw detail/exception when there isn't one.
        if isinstance(detail, dict):
            message = str(detail.get("message") or detail.get("reason") or detail).strip()
        else:
            message = str(detail if detail is not None else exc).strip()
        if not message:
            message = type(exc).__name__
        error_payload = {
            "error": type(exc).__name__,
            "message": message,
        }
        if status_code is not None:
            error_payload["status_code"] = status_code
        if isinstance(detail, dict) and detail.get("reason"):
            # Keep the machine-readable reason so the UI can branch on it.
            error_payload["reason"] = detail["reason"]
        if isinstance(detail, dict):
            # An agent-reported failure: keep its own status and its redacted
            # excerpt on the run so polling clients see what the agent said.
            for k in ("agent", "skill", "agent_status", "agent_error"):
                if detail.get(k) is not None:
                    error_payload[k] = detail[k]
        try:
            await _record_agent_api_subagent_failure(
                session=session,
                user=user,
                grant_id=getattr(exc, "grant_id", None),
                agent=agent,
                skill_name=skill_name,
                arguments=arguments,
                message=message,
                error_payload=error_payload,
            )
        except Exception:  # noqa: BLE001
            await session.rollback()
            log.exception("Failed to record terminal subagent event for agent API failure")
        receipt_id = await _persist_agent_api_receipt(
            session=session,
            agent=agent,
            user=user,
            skill_name=skill_name,
            arguments=arguments,
            result=error_payload,
            started_at=receipt_started_at,
            status="error",
            error_type=type(exc).__name__,
            task_id=job_id,
            self_heal_actionable=True,
        )
        if receipt_id:
            error_payload["receipt_id"] = receipt_id
        await fail_job(
            session,
            job_id,
            error=error_payload["message"],
            result=error_payload,
            user_id=user_id,
            event_type="agent_api_run_failed",
        )


def _artifact_output_path(
    *,
    skill_name: str,
    outputs_prefix: str | None,
    item: dict[str, Any],
) -> str | None:
    uri = item.get("uri")
    if isinstance(uri, str) and uri.startswith("s3://"):
        rest = uri.removeprefix("s3://")
        _, _, key = rest.partition("/")
        return key.strip("/") or None
    for key in ("path", "key"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lstrip("/")
    name = item.get("name")
    if not isinstance(name, str) or not name.strip() or outputs_prefix is None:
        return None
    clean_name = name.strip("/").replace("..", "_") or "artifact"
    return f"{outputs_prefix.strip('/')}/http-{skill_name}/{clean_name}"


def _file_output_from_item(
    *,
    agent_name: str | None = None,
    skill_name: str,
    outputs_prefix: str | None,
    item: dict[str, Any],
    source: str,
    public_base_url: str | None = None,
    integration_token: str | None = None,
) -> dict[str, Any] | None:
    path = _artifact_output_path(
        skill_name=skill_name,
        outputs_prefix=outputs_prefix,
        item=item,
    )
    existing_url = item.get("download_url") or item.get("url")
    if not path and isinstance(existing_url, str) and existing_url.strip():
        path = str(item.get("path") or item.get("name") or existing_url).strip()
    if not path:
        return None
    if outputs_prefix is None and not (
        isinstance(existing_url, str) and existing_url.strip()
    ):
        return None
    name = str(item.get("name") or path.rsplit("/", 1)[-1])
    public_base = (
        public_base_url
        or settings.public_cp_url.rstrip("/")
        or "http://localhost:8000"
    )
    existing_url_is_internal = (
        isinstance(existing_url, str)
        and "control-plane.control-plane.svc.cluster.local" in existing_url
    )
    out: dict[str, Any] = {
        "name": name,
        "path": path,
        "download_url": (
            existing_url.strip()
            if isinstance(existing_url, str)
            and existing_url.strip()
            and not existing_url_is_internal
            else (
                _agent_api_file_download_url(
                    agent_name,
                    path,
                    public_base_url=public_base,
                    integration_token=integration_token,
                )
                if agent_name
                else f"{public_base}/v1/me/files/{quote(path, safe='/')}"
            )
        ),
        "source": source,
    }
    for src, dst in (
        ("mime_type", "mime_type"),
        ("content_type", "mime_type"),
        ("size_bytes", "size_bytes"),
        ("size", "size_bytes"),
    ):
        value = item.get(src)
        if value is not None and dst not in out:
            out[dst] = value
    metadata = item.get("metadata")
    if isinstance(metadata, dict):
        out["metadata"] = metadata
    return out


def _result_declared_file_outputs(
    *,
    result: Any,
    agent_name: str | None = None,
    skill_name: str,
    outputs_prefix: str | None,
    public_base_url: str | None = None,
    integration_token: str | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    outputs: list[dict[str, Any]] = []
    for key in ("file_outputs", "fileOutputs"):
        value = result.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, dict):
                output = _file_output_from_item(
                    agent_name=agent_name,
                    skill_name=skill_name,
                    outputs_prefix=outputs_prefix,
                    item=item,
                    source="result",
                    public_base_url=public_base_url,
                    integration_token=integration_token,
                )
                if output is not None:
                    outputs.append(output)
    return outputs


def _normalize_agent_api_result(
    *,
    payload: Any,
    agent_name: str | None = None,
    skill_name: str,
    outputs_prefix: str | None,
    public_base_url: str | None = None,
    integration_token: str | None = None,
) -> dict[str, Any]:
    if isinstance(payload, dict) and {"result", "events", "artifacts"} & set(payload):
        result = payload.get("result")
        events = payload.get("events") if isinstance(payload.get("events"), list) else []
        artifacts = (
            payload.get("artifacts") if isinstance(payload.get("artifacts"), list) else []
        )
        grant_id = payload.get("grant_id")
    else:
        result = payload
        events = []
        artifacts = []
        grant_id = None
    file_outputs = _result_declared_file_outputs(
        result=result,
        agent_name=agent_name,
        skill_name=skill_name,
        outputs_prefix=outputs_prefix,
        public_base_url=public_base_url,
        integration_token=integration_token,
    )
    for item in artifacts:
        if not isinstance(item, dict):
            continue
        output = _file_output_from_item(
            agent_name=agent_name,
            skill_name=skill_name,
            outputs_prefix=outputs_prefix,
            item=item,
            source="artifact",
            public_base_url=public_base_url,
            integration_token=integration_token,
        )
        if output is not None:
            file_outputs.append(output)
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in file_outputs:
        key = str(item.get("path") or item.get("download_url") or item.get("name") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        deduped.append(item)
    return {
        "result": result,
        "structured": result if isinstance(result, dict) else {"value": result},
        "file_outputs": deduped,
        "artifacts": artifacts,
        "events": events,
        "grant_id": grant_id,
    }


def _template_lineage_for_agent(agent: Agent) -> dict[str, Any]:
    card = agent.card if isinstance(agent.card, dict) else {}
    lineage = card.get("template_lineage")
    if not isinstance(lineage, dict):
        raise HTTPException(409, "agent card does not declare template_lineage")
    clean = {str(key): value for key, value in lineage.items() if value is not None}
    template_ref = str(clean.get("template_ref") or "").strip()
    source_agent = str(clean.get("source_agent") or "").strip()
    if not template_ref and not source_agent:
        raise HTTPException(
            409,
            "agent template_lineage must include template_ref or source_agent",
        )
    return clean


async def _ensure_code_editor_target_source(
    agent: Agent,
    user: User,
    session: AsyncSession,
) -> None:
    if not settings.code_editor_runtime_enabled:
        raise HTTPException(503, "code editor runtime is disabled")
    if agent.image.startswith(_EXTERNAL_IMAGE_PREFIX):
        raise HTTPException(
            409,
            "Code editor requires a managed source repo; imported external agents are not supported.",
        )
    if agent.gitea_owner is None and agent.organization_id is not None:
        agent.gitea_owner = await gitea_owner_for_agent(
            session,
            user,
            agent.organization_id,
        )
    try:
        exists = repo_exists(agent.name, owner=agent.gitea_owner)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"could not reach agent source repo: {exc}") from exc
    if not exists:
        raise HTTPException(
            409,
            "Code editor requires a managed source repo. Redeploy this agent from source or tarball first.",
        )


def _looks_like_git_sha(value: str) -> bool:
    return 7 <= len(value) <= 64 and all(c in "0123456789abcdefABCDEF" for c in value)


def _runtime_upgrade_status_for_agent(
    agent: Agent,
    *,
    latest_deployment: AgentDeploymentOut | None = None,
) -> dict[str, Any]:
    status = runtime_upgrade_status(
        name=agent.name,
        image=agent.image,
        card=agent.card if isinstance(agent.card, dict) else {},
    )
    block = _active_deployment_message(latest_deployment)
    if block and status["update_available"]:
        return {
            **status,
            "can_redeploy": False,
            "message": f"{status['message']} {block}",
        }
    return status


def _active_deployment_message(
    deployment: AgentDeployment | AgentDeploymentOut | None,
) -> str | None:
    if deployment is None or deployment.status not in ACTIVE_DEPLOY_STATUSES:
        return None
    trigger = deployment.trigger.replace("_", " ")
    return (
        f"Deployment {deployment.deploy_id} ({trigger}) is already "
        f"{deployment.status}; wait for it to finish before redeploying."
    )


def _cleanup_agent_resources(agent_name: str, *, repo_owner: str | None = None) -> list[str]:
    failures: list[str] = []
    steps: list[tuple[str, Callable[[], None]]] = []
    if settings.in_cluster or settings.kubeconfig:
        steps.extend(
            [
                ("argocd application", lambda: delete_application(agent_name)),
                ("kubernetes runtime", lambda: delete_k8s_agent(agent_name)),
                ("runtime secret", lambda: delete_agent_runtime_secret(agent_name)),
                ("argocd repo secret", lambda: delete_repo_secret(f"gitea-repo-{agent_name}")),
            ]
        )
    steps.extend(
        [
            ("gitea runtime repo", lambda: delete_repo(runtime_repo_name(agent_name))),
            ("gitea source repo", lambda: delete_repo(agent_name, owner=repo_owner)),
            (
                "mailu mailbox",
                lambda: delete_agent_mailbox_best_effort(agent_mailbox_address(agent_name)),
            ),
        ]
    )
    for label, fn in steps:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{label}: {_short_cleanup_error(exc)}")
    return failures


async def _lookup_source_agent(
    session: AsyncSession, server_url: str | None
) -> Agent | None:
    if not server_url:
        return None
    needle = server_url.rstrip("/").lower()
    if not needle:
        return None
    return (
        await session.execute(select(Agent).where(func.lower(Agent.url) == needle))
    ).scalar_one_or_none()


def _cleanup_external_agent_resources(agent_name: str) -> list[str]:
    if not (settings.in_cluster or settings.kubeconfig):
        return []
    try:
        delete_agent_runtime_secret(agent_name)
    except Exception as exc:  # noqa: BLE001
        return [f"runtime secret: {_short_cleanup_error(exc)}"]
    return []


def _short_cleanup_error(exc: Exception) -> str:
    text = str(exc).strip() or type(exc).__name__
    return " ".join(text.split())[:260]


async def _delete_agent_database_dependents(
    session: AsyncSession,
    agent_id: int,
) -> None:
    """Remove/detach rows that may block deleting an agent row.

    The model declares ON DELETE rules, but production tables are created
    incrementally and older constraints can drift. Keep the route deterministic
    by doing the same cleanup in application code before deleting ``agents``.
    """
    await session.execute(
        update(AgentProofRun)
        .where(AgentProofRun.agent_id == agent_id)
        .values(agent_id=None)
    )
    await session.execute(
        update(AgentReceipt)
        .where(AgentReceipt.agent_id == agent_id)
        .values(agent_id=None)
    )
    await session.execute(
        update(AgentSession)
        .where(AgentSession.agent_id == agent_id)
        .values(agent_id=None)
    )
    await session.execute(
        update(Bounty)
        .where(Bounty.claimed_agent_id == agent_id)
        .values(claimed_agent_id=None)
    )
    await session.execute(
        update(TrialRun)
        .where(TrialRun.agent_id == agent_id)
        .values(agent_id=None)
    )
    await session.execute(
        update(Agent)
        .where(Agent.source_agent_id == agent_id)
        .values(source_agent_id=None)
    )

    deployment_ids = select(AgentDeployment.id).where(
        AgentDeployment.agent_id == agent_id
    )
    await session.execute(
        delete(AgentDeploymentEvent).where(
            AgentDeploymentEvent.deployment_id.in_(deployment_ids)
        )
    )
    mailbox_ids = select(AgentMailbox.id).where(AgentMailbox.agent_id == agent_id)
    await session.execute(
        delete(ChatThreadEmailLink).where(ChatThreadEmailLink.mailbox_id.in_(mailbox_ids))
    )
    # Deleted by ``agent_id`` with no owner predicate, so rows other users
    # created go too — an ``AgentInstall``, an ``AgentConsumerSetupValue``, an
    # ``AgentReviewRun`` or an ``AgentCodeEditorOptIn`` belonging to a consumer
    # of this agent. That is not a scoping bug: they are child rows of the
    # agent being removed and cannot outlive it. Callers that need to warn
    # those users must do it before calling here.
    for model in (
        AgentApiToken,
        AgentAuthConnection,
        AgentCodeEditorOptIn,
        AgentConsumerSetupValue,
        AgentCustomDomain,
        AgentDatabaseBinding,
        AgentDeployment,
        AgentInstall,
        AgentMailbox,
        AgentMemoryEntry,
        AgentReviewRun,
        AgentSecret,
        DatabaseProvisionEvent,
        MailboxProvisionEvent,
        MetaAgentRun,
    ):
        await session.execute(delete(model).where(model.agent_id == agent_id))


async def _guard_fresh_source_upload(
    session: AsyncSession,
    agent: Agent,
    base_head_sha: str | None,
) -> None:
    base = (base_head_sha or "").strip()
    if not base:
        return
    if not _looks_like_git_sha(base):
        raise HTTPException(400, "base_head_sha must be a git commit SHA")

    latest = await latest_deployment_for_agent(session, agent)
    try:
        changed_paths = source_changed_paths_since(
            agent.name,
            base,
            owner=agent.gitea_owner,
        )
    except RepoBaseMissingError as exc:
        raise HTTPException(
            409,
            {
                "error": "stale_source",
                "reason": "base_head_missing",
                "message": (
                    "This builder workspace was based on a source commit that "
                    "is no longer present in the managed repo. Refresh the "
                    "builder workspace before deploying."
                ),
                "agent": agent.name,
                "base_head_sha": base,
                "current_repo_head_sha": repo_head_sha(agent.name, owner=agent.gitea_owner),
                "current_deployment_head_sha": latest.head_sha if latest else None,
                "current_deployment_id": latest.deploy_id if latest else None,
                "detail": str(exc),
            },
        ) from exc
    except RepoDriftCheckError as exc:
        raise HTTPException(
            502,
            f"could not verify current source repo head: {exc}",
        ) from exc
    if changed_paths:
        raise HTTPException(
            409,
            {
                "error": "stale_source",
                "message": (
                    "The managed repo has user source changes newer than "
                    "this workspace. Refresh the builder workspace before "
                    "deploying, or force only if you intend to replace them."
                ),
                "agent": agent.name,
                "base_head_sha": base,
                "current_deployment_head_sha": latest.head_sha if latest else None,
                "current_deployment_id": latest.deploy_id if latest else None,
                "changed_paths": changed_paths[:25],
            },
        )


@router.post("", response_model=AgentDetailOut, status_code=201)
async def register(
    body: AgentRegisterIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> AgentDetailOut:
    _reject_unsafe_user_card(body.card)
    existing = (
        await session.execute(select(Agent).where(Agent.name == body.name))
    ).scalar_one_or_none()
    if existing is not None and existing.owner_id != user.id:
        raise HTTPException(409, f"agent {body.name!r} already exists (owned by another user)")
    availability = _runtime_availability_from_card(body.card)
    _assert_can_request_availability(user, availability, existing=existing)

    agent = existing or Agent(owner_id=user.id, name=body.name)
    agent.description = body.description
    agent.version = body.version
    agent.image = body.image
    agent.public = body.public
    agent.card = body.card
    agent.status = "deploying"
    if existing is None:
        session.add(agent)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, str(exc.orig))
    await session.refresh(agent)

    deployment = await create_deployment(
        session,
        agent,
        trigger="direct_register",
        status="deploying",
        source_repo_url=None,
        image=agent.image,
        agent_url=_canonical_url(agent.name) if agent.public else None,
    )
    await record_deployment_event(
        session,
        deployment,
        stage="source",
        status="passed",
        message="External image registered with the control plane.",
        data={"image": agent.image},
    )
    await record_deployment_event(
        session,
        deployment,
        stage="deploy",
        status="running",
        message="Applying runtime resources.",
        data={"public": agent.public, "availability": availability},
    )

    try:
        url = deploy_agent(
            agent.name, agent.image, agent.public, agent.card,
            owner_id=agent.owner_id,
        )
    except Exception as exc:  # noqa: BLE001
        agent.status = "failed"
        await fail_deployment(
            session,
            deployment,
            stage="deploy",
            error=f"deploy failed: {exc}",
        )
        await session.commit()
        raise HTTPException(500, f"deploy failed: {exc}")

    agent.status = "ready"
    agent.url = url
    deployment.agent_url = url
    deployment.status = "verifying"
    await record_deployment_event(
        session,
        deployment,
        stage="deploy",
        status="passed",
        message="Kubernetes resources were accepted. Verifying the live agent now.",
        data={"url": url},
    )
    await session.commit()
    await session.refresh(agent)
    track_event(
        "agent_registered",
        profile_id=f"user:{user.id}",
        properties={"agentName": agent.name, "public": agent.public},
    )
    await warm_agent_card(agent.name, agent.card)
    await _index_agents_for_search([agent])
    return AgentDetailOut.model_validate(agent)


@router.post("/import", response_model=AgentImportOut, status_code=201)
async def import_external_agent(
    body: AgentImportIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> AgentImportOut:
    base_url = _normalize_agent_base_url(body.url)
    fetch_auth, fetch_secret = _normalize_import_auth(
        body.auth,
        card_declares_auth=False,
    )
    raw_card, card_url = await _fetch_card_from_base_url(
        base_url,
        request_auth=_external_request_auth(fetch_auth, fetch_secret),
    )
    auth_metadata, auth_secret = _normalize_import_auth(
        body.auth,
        card_declares_auth=_card_declares_auth(raw_card),
    )
    auth_requirements = detect_auth_requirements(raw_card)
    registry_name = _validate_agent_name(
        body.name or _slugify_agent_name(str(raw_card.get("name") or "external-agent"))
    )
    card = _normalize_external_card(
        raw_card,
        registry_name=registry_name,
        base_url=base_url,
        card_url=card_url,
        auth=auth_metadata,
    )

    existing = (
        await session.execute(select(Agent).where(Agent.name == registry_name))
    ).scalar_one_or_none()
    if existing is not None and existing.owner_id != user.id:
        raise HTTPException(409, f"agent {registry_name!r} already exists (owned by another user)")

    agent_org_id, _repo_owner = await _resolve_source_repo_scope(
        session,
        user,
        existing=existing,
        organization_slug=body.organization_slug,
        source="agent_import",
    )
    agent = existing or Agent(owner_id=user.id, name=registry_name)
    agent.organization_id = agent.organization_id or agent_org_id
    agent.description = card.get("description") or raw_card.get("description") or ""
    agent.version = str(card.get("version") or "external")
    agent.image = _external_image(base_url)
    agent.public = body.public
    agent.card = card
    agent.status = "running"
    agent.url = base_url
    if existing is None:
        session.add(agent)
    try:
        await session.flush()
        auth_connections = await _apply_import_auth_connections(
            session=session,
            agent=agent,
            user=user,
            auth_metadata=auth_metadata,
            auth_secret=auth_secret,
            auth_requirements=auth_requirements,
        )
        if auth_requirements and not any(row.status == "connected" for row in auth_connections):
            agent.status = "needs_auth"
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, str(exc.orig))
    await session.refresh(agent)
    await warm_agent_card(agent.name, agent.card)

    deployment = await create_deployment(
        session,
        agent,
        trigger="external_import",
        status="live",
        source_repo_url=base_url,
        image=agent.image,
        agent_url=base_url,
    )
    deployment.completed_at = utcnow()
    await record_deployment_event(
        session,
        deployment,
        stage="agent_card",
        status="passed",
        message="External A2A Agent Card fetched and stored.",
        data={
            "card_url": card_url,
            "card_hash": card_hash(card),
            "skills": [s["name"] for s in card.get("skills", [])],
            "auth": auth_metadata
            if auth_metadata.get("type") != "none"
            else {"type": "none"},
        },
        commit=False,
    )
    await record_deployment_event(
        session,
        deployment,
        stage="mcp",
        status="passed",
        message="MCP access configured for the imported agent.",
        data=_mcp_out_for_agent(agent).model_dump(),
        commit=False,
    )
    await session.commit()
    await session.refresh(agent)
    await _index_agents_for_search([agent])
    return _agent_import_out(agent)


@router.post("/from-source", response_model=AgentFromSourceOut, status_code=201)
async def from_source(
    body: AgentFromSourceIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> AgentFromSourceOut:
    """Provision an editable source repo for ``body.name``.

    Build/deploy internals are created later in the hidden runtime repo by
    the tarball deploy path.
    """
    existing = (
        await session.execute(select(Agent).where(Agent.name == body.name))
    ).scalar_one_or_none()
    if existing is not None and existing.owner_id != user.id:
        raise HTTPException(409, f"agent {body.name!r} already owned by another user")

    org_id, repo_owner = await _resolve_source_repo_scope(
        session,
        user,
        existing=existing,
        organization_slug=body.organization_slug,
        source="agent_from_source",
    )
    ensure_repo(
        body.name,
        body.description,
        owner=repo_owner,
        public=body.public,
    )
    _ensure_source_repo_push_webhook(body.name, owner=repo_owner)
    public_repo_url = _public_repo_url(body.name, owner=repo_owner)

    expected = _canonical_url(body.name) if body.public else None

    agent = existing or Agent(owner_id=user.id, name=body.name)
    agent.organization_id = org_id
    agent.gitea_owner = repo_owner
    agent.description = body.description
    agent.version = body.version
    agent.image = settings.agent_image(body.name, "latest")
    agent.public = body.public
    agent.card = {}  # filled in once first build registers card
    agent.status = "provisioning"
    agent.url = expected
    if existing is None:
        session.add(agent)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, str(exc.orig))
    await invalidate_agent_card(agent.name)
    await _index_agents_for_search([agent])

    deployment = await create_deployment(
        session,
        agent,
        trigger="from_source",
        status="pending",
        source_repo_url=_public_repo_url(agent.name, owner=agent.gitea_owner),
        image=agent.image,
        agent_url=expected,
    )
    await record_deployment_event(
        session,
        deployment,
        stage="source",
        status="passed",
        message="Managed repo is ready. Upload source through the tarball deploy path to start the image build.",
        data={"repo_url": _public_repo_url(agent.name, owner=agent.gitea_owner)},
    )
    await record_deployment_event(
        session,
        deployment,
        stage="argo",
        status="pending",
        message="ArgoCD runtime wiring will be created after source upload.",
        data={},
    )
    return AgentFromSourceOut(
        name=body.name,
        push_url=public_repo_url,
        repo_url=public_repo_url,
        expected_url=expected,
        deployment_id=deployment.deploy_id,
    )


async def _generate_openapi_agent_source(
    body: AgentOpenAPIGenerateIn,
) -> GeneratedOpenAPIAgent:
    try:
        source_urls = _openapi_request_urls(body)
        specs = await asyncio.gather(*(fetch_openapi_spec(url) for url in source_urls))
        generated = build_openapi_agent_source(
            specs[0] if len(specs) == 1 else list(specs),
            name=body.name,
            description=body.description,
            spec_url=source_urls[0] if len(source_urls) == 1 else None,
            spec_urls=source_urls if len(source_urls) > 1 else None,
            base_url=body.base_url,
            base_urls=body.base_urls or None,
        )
    except OpenAPIGenerationError as exc:
        raise HTTPException(400, str(exc)) from exc
    _validate_agent_name(generated.name)
    return generated


def _openapi_request_urls(body: AgentOpenAPIGenerateIn) -> list[str]:
    raw_urls = list(body.urls or [])
    if not raw_urls and body.url:
        raw_urls = re.split(r"[\r\n,]+", body.url)
    urls: list[str] = []
    seen: set[str] = set()
    for raw_url in raw_urls:
        value = str(raw_url).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        urls.append(value)
    if not urls:
        raise OpenAPIGenerationError("at least one OpenAPI URL is required")
    if len(urls) > 12:
        raise OpenAPIGenerationError("at most 12 OpenAPI URLs can be combined")
    return urls


@router.post("/openapi/preview", response_model=AgentOpenAPIPreviewOut)
async def preview_openapi_agent(
    body: AgentOpenAPIGenerateIn,
    _user: User = Depends(current_user),
) -> AgentOpenAPIPreviewOut:
    generated = await _generate_openapi_agent_source(body)
    return AgentOpenAPIPreviewOut.model_validate(generated.preview)


def _openapi_refresh_required_detail(
    existing: Agent,
    generated: GeneratedOpenAPIAgent,
) -> dict[str, Any]:
    return {
        "error": "openapi_refresh_required",
        "message": (
            f"Agent {generated.name!r} already exists. Confirm refresh to "
            "regenerate and redeploy it from the current OpenAPI spec."
        ),
        "agent": generated.name,
        "existing_version": existing.version,
        "generated_version": generated.version,
        "operation_count": generated.preview.get("operation_count"),
        "source_files": generated.preview.get("source_files", []),
    }


def _compose_manifest_from_body(body: AgentComposeIn) -> dict[str, Any]:
    raw = dict(body.manifest or {})
    if body.composition is not None:
        raw["composition"] = body.composition
    if body.goal is not None:
        raw["goal"] = body.goal
    if body.memory is not None:
        raw["memory"] = body.memory
    return raw


async def _generate_meta_agent_source(body: AgentComposeIn) -> GeneratedMetaAgent:
    try:
        generated = build_meta_agent_source(
            _compose_manifest_from_body(body),
            name=body.name,
            description=body.description,
            version=body.version,
        )
    except MetaAgentGenerationError as exc:
        raise HTTPException(400, str(exc)) from exc
    _validate_agent_name(generated.name)
    return generated


def _compose_refresh_required_detail(
    existing: Agent,
    generated: GeneratedMetaAgent,
) -> dict[str, Any]:
    return {
        "error": "compose_refresh_required",
        "message": (
            f"Agent {generated.name!r} already exists. Confirm refresh to "
            "regenerate and redeploy it from the current composition manifest."
        ),
        "agent": generated.name,
        "existing_version": existing.version,
        "generated_version": generated.version,
        "source_files": generated.preview.get("source_files", []),
        "sub_agents": generated.preview.get("sub_agents", []),
    }


async def _validate_compose_subagent_refs(
    session: AsyncSession,
    user: User,
    generated: GeneratedMetaAgent,
) -> list[ResolvedSubAgent]:
    resolved: list[ResolvedSubAgent] = []
    for ref in generated.preview.get("sub_agents") or []:
        if not isinstance(ref, dict):
            continue
        required = bool(ref.get("required", True))
        skills = [str(item).strip() for item in ref.get("skills") or [] if str(item).strip()]
        name = str(ref.get("name") or "").strip()
        tag = str(ref.get("tag") or "").strip()
        if name:
            agent = (
                await session.execute(
                    select(Agent).where(
                        Agent.name == name,
                        visible_agents_clause(user.id),
                    )
                )
            ).scalar_one_or_none()
            if agent is None:
                if required:
                    raise HTTPException(400, f"required sub-agent {name!r} is not accessible")
                continue
            missing = _missing_compose_skills(agent, skills)
            if missing and required:
                raise HTTPException(
                    400,
                    f"required sub-agent {name!r} is missing skill(s): {', '.join(missing)}",
                )
            if missing:
                continue
            skill_rows = _compose_skill_rows(agent, skills)
            if required and not skill_rows:
                raise HTTPException(
                    400,
                    f"required sub-agent {name!r} has no callable skills in its agent card",
                )
            if required:
                await _ensure_compose_child_setup_ready(session, user, agent)
            if skill_rows:
                resolved.append(_compose_resolved_subagent(agent, ref, skill_rows))
            continue
        if tag:
            rows = (
                await session.execute(
                    select(Agent).where(
                        visible_agents_clause(user.id)
                    )
                )
            ).scalars().all()
            matches = [
                agent
                for agent in rows
                if _agent_card_has_tagged_skill(agent, tag=tag, skills=skills)
            ]
            if required and not matches:
                skill_hint = f" with skill(s) {', '.join(skills)}" if skills else ""
                raise HTTPException(
                    400,
                    f"required sub-agent tag {tag!r}{skill_hint} has no accessible matches",
                )
            for agent in matches:
                skill_rows = _compose_skill_rows(agent, skills)
                if skill_rows:
                    if required:
                        await _ensure_compose_child_setup_ready(session, user, agent)
                    resolved.append(_compose_resolved_subagent(agent, ref, skill_rows))
    if not resolved:
        raise HTTPException(
            400,
            "composition manifest resolved no callable sub-agent skills",
        )
    return resolved


def _card_skill_rows(agent: Agent) -> list[dict[str, Any]]:
    card = agent.card if isinstance(agent.card, dict) else {}
    rows = card.get("skills") if isinstance(card.get("skills"), list) else []
    return [row for row in rows if isinstance(row, dict)]


def _missing_compose_skills(agent: Agent, skills: list[str]) -> list[str]:
    if not skills:
        return []
    available = {
        str(row.get("name") or row.get("id") or "").strip()
        for row in _card_skill_rows(agent)
    }
    return [skill for skill in skills if skill not in available]


def _compose_skill_rows(agent: Agent, skills: list[str]) -> tuple[dict[str, Any], ...]:
    wanted = set(skills)
    rows: list[dict[str, Any]] = []
    for row in _card_skill_rows(agent):
        name = str(row.get("name") or row.get("id") or "").strip()
        if not name or (wanted and name not in wanted):
            continue
        rows.append(
            {
                "name": name,
                "description": str(row.get("description") or ""),
                "tags": [
                    str(item).strip()
                    for item in row.get("tags") or []
                    if str(item).strip()
                ],
                "input_schema": (
                    dict(row.get("input_schema") or {})
                    if isinstance(row.get("input_schema") or {}, dict)
                    else {}
                ),
            }
        )
    return tuple(rows)


def _compose_resolved_subagent(
    agent: Agent,
    ref: dict[str, Any],
    skill_rows: tuple[dict[str, Any], ...],
) -> ResolvedSubAgent:
    default_args = ref.get("default_args")
    return ResolvedSubAgent(
        name=agent.name,
        url=agent.url,
        description=agent.description,
        version=agent.version,
        skills=skill_rows,
        default_args=dict(default_args) if isinstance(default_args, dict) else {},
        source="control-plane-compose",
        required=bool(ref.get("required", True)),
    )


async def _ensure_compose_child_setup_ready(
    session: AsyncSession,
    user: User,
    agent: Agent,
) -> None:
    issues = await _compose_child_setup_issues(session, user, agent)
    if not issues:
        return
    raise HTTPException(
        409,
        {
            "error": "compose_child_setup_required",
            "message": (
                f"Sub-agent {agent.name!r} requires user setup before it can "
                "be composed into a meta-agent."
            ),
            "agent": agent.name,
            "setup": issues,
        },
    )


async def _compose_child_setup_issues(
    session: AsyncSession,
    user: User,
    agent: Agent,
) -> list[dict[str, Any]]:
    card = agent.card if isinstance(agent.card, dict) else {}
    issues: list[dict[str, Any]] = []
    requirements = detect_auth_requirements(card)
    if requirements:
        try:
            await resolve_request_auth(session, agent, user=user)
        except ImportedAgentAuthError as exc:
            issues.append(
                {
                    "kind": "imported_agent_auth",
                    "status": "needs_setup",
                    "message": str(exc),
                    "requirements": requirements,
                    "setup_url": f"/v1/agents/{agent.name}/auth",
                }
            )
    setup = await resolve_consumer_setup(agent=agent, user=user, session=session)
    if not setup.complete:
        issues.append(
            {
                "kind": "consumer_setup",
                "status": "needs_setup",
                "message": "consumer setup required",
                "missing_required": list(setup.missing_required),
                "declaration": setup.declaration,
                "setup_url": f"/v1/agents/{agent.name}/consumer-setup",
            }
        )
    return issues


def _agent_card_has_tagged_skill(agent: Agent, *, tag: str, skills: list[str]) -> bool:
    wanted = set(skills)
    for row in _card_skill_rows(agent):
        name = str(row.get("name") or row.get("id") or "").strip()
        if wanted and name not in wanted:
            continue
        tags = {str(item).strip() for item in row.get("tags") or [] if str(item).strip()}
        if tag in tags:
            return True
    return False


async def _validate_generated_meta_agent_predeploy(
    generated: GeneratedMetaAgent,
    resolved_subagents: list[ResolvedSubAgent],
) -> dict[str, Any]:
    card = _import_generated_meta_agent_card(generated)
    dry_run = await _dry_run_generated_meta_agent_graph(generated, resolved_subagents)
    sandbox = await _sandbox_validate_generated_meta_agent_source(generated)
    return {"card": card, "dry_run": dry_run, "sandbox": sandbox}


def _import_generated_meta_agent_card(generated: GeneratedMetaAgent) -> dict[str, Any]:
    module_name = f"_a2a_compose_validation_{generated.name}_{secrets.token_hex(6)}"
    with tempfile.TemporaryDirectory(prefix="a2a-compose-validate-") as tmpdir:
        for rel, content in generated.files.items():
            path = os.path.join(tmpdir, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
        agent_py = os.path.join(tmpdir, "agent.py")
        spec = importlib.util.spec_from_file_location(module_name, agent_py)
        if spec is None or spec.loader is None:
            raise HTTPException(400, "compose validation failed: generated agent.py is not importable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
            cls = getattr(module, generated.class_name, None)
            if cls is None:
                raise RuntimeError(f"missing generated class {generated.class_name}")
            card = cls().card().model_dump(mode="json")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                400,
                f"compose validation failed: generated source failed import/card check: {exc}",
            ) from exc
        finally:
            sys.modules.pop(module_name, None)
    skills = card.get("skills") if isinstance(card.get("skills"), list) else []
    if not any(isinstance(skill, dict) and skill.get("name") == "pursue" for skill in skills):
        raise HTTPException(
            400,
            "compose validation failed: generated card does not expose pursue skill",
        )
    return card


async def _dry_run_generated_meta_agent_graph(
    generated: GeneratedMetaAgent,
    resolved_subagents: list[ResolvedSubAgent],
) -> dict[str, Any]:
    composition = generated.preview.get("composition")
    composition = composition if isinstance(composition, dict) else {}
    try:
        limits = DagLimits(
            max_nodes=int(composition.get("max_nodes") or 8),
            max_parallel=int(composition.get("max_parallel") or 3),
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            400,
            f"compose validation failed: invalid DAG limits: {exc}",
        ) from exc
    nodes: list[dict[str, Any]] = []
    for index, subagent in enumerate(resolved_subagents[: limits.max_nodes], start=1):
        if not subagent.skill_names:
            continue
        nodes.append(
            {
                "id": f"dry{index}",
                "agent": subagent.name,
                "skill": subagent.skill_names[0],
                "args": {"_compose_validation": True},
                "expected_outputs": ["echo"],
            }
        )
    if not nodes:
        raise HTTPException(
            400,
            "compose validation failed: dry-run resolved no executable nodes",
        )
    goal = str((generated.preview.get("goal") or {}).get("objective") or "compose validation")
    try:
        sanitized = sanitize_meta_dag(
            {"goal": goal, "nodes": nodes},
            goal=goal,
            subagents=resolved_subagents,
            limits=limits,
        )
        _, dag_nodes = parse_dag(json.dumps(sanitized), max_nodes=limits.max_nodes)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            400,
            f"compose validation failed: generated manifest cannot form a safe DAG: {exc}",
        ) from exc

    async def echo_node(node: DagNode, rendered_args: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "result": {
                "echo": {
                    "agent": node.agent,
                    "skill": node.skill,
                    "args": rendered_args,
                },
                "summary": "compose dry-run ok",
            },
        }

    dry_run = await execute_dag_nodes(
        goal,
        dag_nodes,
        echo_node,
        limits=limits,
        run_id=f"compose-validate-{generated.name}",
    )
    if not dry_run.get("ok"):
        raise HTTPException(
            400,
            f"compose validation failed: dry-run failed: {dry_run.get('error')}",
        )
    return dry_run


async def _sandbox_validate_generated_meta_agent_source(
    generated: GeneratedMetaAgent,
) -> dict[str, Any]:
    sandbox_url = (
        os.environ.get("A2A_CP_COMPOSE_VALIDATION_SANDBOX_URL")
        or os.environ.get("A2A_SANDBOX_URL")
        or ""
    ).rstrip("/")
    if not sandbox_url:
        return {"ok": True, "skipped": True, "reason": "sandbox url not configured"}
    bundle = source_tarball_bytes(generated.files)
    encoded = base64.b64encode(bundle).decode("ascii")
    script = "\n".join(
        [
            "set -euo pipefail",
            "python - <<'PY'",
            "import base64, io, json, os, sys, tarfile, importlib.util",
            "root = '/tmp/a2a-compose-validation'",
            "os.makedirs(root, exist_ok=True)",
            f"bundle = base64.b64decode({json.dumps(encoded)})",
            "with tarfile.open(fileobj=io.BytesIO(bundle), mode='r:gz') as tf:",
            "    tf.extractall(root)",
            "sys.path.insert(0, root)",
            "spec = importlib.util.spec_from_file_location('generated_agent', os.path.join(root, 'agent.py'))",
            "if spec is None or spec.loader is None:",
            "    raise RuntimeError('generated agent.py is not importable')",
            "module = importlib.util.module_from_spec(spec)",
            "spec.loader.exec_module(module)",
            f"cls = getattr(module, {json.dumps(generated.class_name)}, None)",
            "if cls is None:",
            f"    raise RuntimeError('missing generated class {generated.class_name}')",
            "card = cls().card().model_dump(mode='json')",
            "skills = [skill.get('name') for skill in card.get('skills', []) if isinstance(skill, dict)]",
            "if 'pursue' not in skills:",
            "    raise RuntimeError('generated card does not expose pursue skill')",
            "print(json.dumps({'name': card.get('name'), 'skills': skills}))",
            "PY",
        ]
    )
    headers: dict[str, str] = {"content-type": "application/json"}
    token = (
        os.environ.get("A2A_CP_COMPOSE_VALIDATION_SANDBOX_TOKEN")
        or os.environ.get("A2A_SANDBOX_TOKEN")
        or ""
    ).strip()
    if token:
        headers["authorization"] = f"Bearer {token}"
    image = (
        os.environ.get("A2A_CP_COMPOSE_VALIDATION_IMAGE")
        or _COMPOSE_VALIDATION_DEFAULT_IMAGE
    )
    try:
        timeout = float(
            os.environ.get("A2A_CP_COMPOSE_VALIDATION_TIMEOUT_S")
            or os.environ.get("A2A_SANDBOX_TIMEOUT_S")
            or "180"
        )
    except ValueError:
        timeout = 180.0
    try:
        async with httpx.AsyncClient(timeout=timeout + 15.0) as client:
            response = await client.post(
                f"{sandbox_url}/v1/run_shell",
                headers=headers,
                json={
                    "bucket": f"agent-{generated.name}",
                    "script": script,
                    "image": image,
                    "memory_mib": 512,
                    "timeout_seconds": timeout,
                    "network_disabled": True,
                },
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            400,
            f"compose validation failed: sandbox unreachable: {exc}",
        ) from exc
    if response.status_code >= 400:
        raise HTTPException(
            400,
            f"compose validation failed: sandbox {response.status_code}: {response.text[:1000]}",
        )
    payload = response.json() or {}
    if int(payload.get("exit_code") or 0) != 0:
        stderr = str(payload.get("stderr") or "")[:1000]
        stdout = str(payload.get("stdout") or "")[:1000]
        raise HTTPException(
            400,
            f"compose validation failed: sandbox import/card check failed: {stderr or stdout}",
        )
    return {
        "ok": True,
        "skipped": False,
        "image": image,
        "stdout": str(payload.get("stdout") or "")[:1000],
    }


@router.post("/from-openapi", response_model=AgentFromOpenAPIOut, status_code=201)
async def from_openapi(
    body: AgentOpenAPIGenerateIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> AgentFromOpenAPIOut:
    """Generate editable A2APack source from OpenAPI and deploy it.

    The generated repo is a normal agent project. Developers can edit the
    produced ``agent.py`` / ``a2a.yaml`` and redeploy through the usual
    tarball or builder flows.
    """
    generated = await _generate_openapi_agent_source(body)
    existing = (
        await session.execute(select(Agent).where(Agent.name == generated.name))
    ).scalar_one_or_none()
    if existing is not None:
        await require_agent_access(
            session,
            user=user,
            agent=existing,
            action="edit_existing",
        )
    if existing is not None and not body.refresh_existing:
        raise HTTPException(409, _openapi_refresh_required_detail(existing, generated))

    org_id, repo_owner = await _resolve_source_repo_scope(
        session,
        user,
        existing=existing,
        organization_slug=body.organization_slug,
        source="agent_from_openapi",
    )
    push_url, _ = ensure_repo(
        generated.name,
        generated.description,
        owner=repo_owner,
        public=body.public,
    )
    _ensure_source_repo_push_webhook(generated.name, owner=repo_owner)

    source_bundle = source_tarball_bytes(generated.files)
    try:
        sha = commit_and_push_source(
            tarball_bytes=source_bundle,
            name=generated.name,
            entrypoint=f"agent:{generated.class_name}",
            push_url=push_url,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"push generated OpenAPI source failed: {exc}") from exc

    try:
        runtime_push_url, runtime_internal_url = _ensure_runtime_repo(
            generated.name,
            generated.description,
        )
        runtime_sha = commit_and_push_runtime(
            tarball_bytes=source_bundle,
            name=generated.name,
            entrypoint=f"agent:{generated.class_name}",
            source_repo_url=push_url,
            source_sha=sha,
            push_url=runtime_push_url,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"push generated runtime failed: {exc}") from exc

    expected_url = _canonical_url(generated.name) if body.public else None
    source_agent = await _lookup_source_agent(session, generated.server_url)
    agent = existing or Agent(owner_id=user.id, name=generated.name)
    agent.organization_id = org_id
    agent.gitea_owner = repo_owner
    agent.description = _truncate(generated.description, AGENT_DESCRIPTION_MAX)
    agent.version = generated.version
    agent.image = settings.agent_image(generated.name, "latest")
    agent.public = body.public
    agent.card = {}
    agent.status = "building"
    agent.url = expected_url
    agent.source_agent_id = source_agent.id if source_agent else None
    if existing is None:
        session.add(agent)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, str(exc.orig))
    await invalidate_agent_card(agent.name)
    await _index_agents_for_search([agent])

    deployment = await create_deployment(
        session,
        agent,
        trigger="from_openapi",
        status="building",
        source_repo_url=_public_repo_url(agent.name, owner=agent.gitea_owner),
        head_sha=sha,
        image=agent.image,
        agent_url=expected_url,
    )
    await record_deployment_event(
        session,
        deployment,
        stage="source",
        status="passed",
        message="OpenAPI spec converted to editable A2APack source and committed.",
        data={
            "head_sha": sha,
            "runtime_head_sha": runtime_sha,
            "openapi_url": (generated.source_openapi_urls or [body.url])[0],
            "openapi_urls": generated.source_openapi_urls or ([body.url] if body.url else []),
            "repo_url": _public_repo_url(agent.name, owner=agent.gitea_owner),
            "operation_count": generated.preview["operation_count"],
            "source_files": generated.preview["source_files"],
            "warnings": generated.preview["warnings"],
        },
    )
    await record_deployment_event(
        session,
        deployment,
        stage="build",
        status="running",
        message="Gitea Actions is building and publishing the generated agent image.",
        data={"image": agent.image},
    )
    await record_deployment_event(
        session,
        deployment,
        stage="argo",
        status="running",
        message="ArgoCD is watching hidden runtime deploy manifests.",
        data={
            "repo_url": runtime_internal_url,
            "expected_revision": runtime_sha,
            "expected_image": settings.agent_image(agent.name, sha),
        },
    )
    return AgentFromOpenAPIOut(
        name=agent.name,
        version=agent.version,
        status=agent.status,
        repo_url=_public_repo_url(agent.name, owner=agent.gitea_owner),
        expected_url=expected_url,
        deployment_id=deployment.deploy_id,
        head_sha=sha,
        preview=AgentOpenAPIPreviewOut.model_validate(generated.preview),
        refreshed_existing=existing is not None,
    )


@router.post("/compose", response_model=AgentFromComposeOut, status_code=201)
async def compose_agent(
    body: AgentComposeIn,
    user: User = Depends(current_user_or_studio_job),
    session: AsyncSession = Depends(get_session),
) -> AgentFromComposeOut:
    """Generate editable A2APack source from a composition manifest and deploy it."""

    generated = await _generate_meta_agent_source(body)
    resolved_subagents = await _validate_compose_subagent_refs(session, user, generated)
    existing = (
        await session.execute(select(Agent).where(Agent.name == generated.name))
    ).scalar_one_or_none()
    if existing is not None and existing.owner_id != user.id:
        raise HTTPException(409, f"agent {generated.name!r} already owned by another user")
    if existing is not None and not body.refresh_existing:
        raise HTTPException(409, _compose_refresh_required_detail(existing, generated))
    validation = await _validate_generated_meta_agent_predeploy(
        generated,
        resolved_subagents,
    )

    org_id, repo_owner = await _resolve_source_repo_scope(
        session,
        user,
        existing=existing,
        organization_slug=body.organization_slug,
        source="agent_compose",
    )
    push_url, _ = ensure_repo(
        generated.name,
        generated.description,
        owner=repo_owner,
        public=body.public,
    )
    _ensure_source_repo_push_webhook(generated.name, owner=repo_owner)

    source_bundle = source_tarball_bytes(generated.files)
    try:
        sha = commit_and_push_source(
            tarball_bytes=source_bundle,
            name=generated.name,
            entrypoint=f"agent:{generated.class_name}",
            push_url=push_url,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"push generated meta-agent source failed: {exc}") from exc

    try:
        runtime_push_url, runtime_internal_url = _ensure_runtime_repo(
            generated.name,
            generated.description,
        )
        runtime_sha = commit_and_push_runtime(
            tarball_bytes=source_bundle,
            name=generated.name,
            entrypoint=f"agent:{generated.class_name}",
            source_repo_url=push_url,
            source_sha=sha,
            push_url=runtime_push_url,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"push generated meta-agent runtime failed: {exc}") from exc

    expected_url = _canonical_url(generated.name) if body.public else None
    agent = existing or Agent(owner_id=user.id, name=generated.name)
    agent.organization_id = org_id
    agent.gitea_owner = repo_owner
    agent.description = _truncate(generated.description, AGENT_DESCRIPTION_MAX)
    agent.version = generated.version
    agent.image = settings.agent_image(generated.name, "latest")
    agent.public = body.public
    agent.card = validation["card"] if isinstance(validation.get("card"), dict) else {}
    agent.status = "building"
    agent.url = expected_url
    if existing is None:
        session.add(agent)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, str(exc.orig))
    await warm_agent_card(agent.name, agent.card)
    await _index_agents_for_search([agent])

    deployment = await create_deployment(
        session,
        agent,
        trigger="from_compose",
        status="building",
        source_repo_url=_public_repo_url(agent.name, owner=agent.gitea_owner),
        head_sha=sha,
        image=agent.image,
        agent_url=expected_url,
    )
    await record_deployment_event(
        session,
        deployment,
        stage="source",
        status="passed",
        message="Composition manifest converted to editable A2APack source and committed.",
        data={
            "head_sha": sha,
            "runtime_head_sha": runtime_sha,
            "repo_url": _public_repo_url(agent.name, owner=agent.gitea_owner),
            "source_files": generated.preview["source_files"],
            "sub_agents": generated.preview["sub_agents"],
            "warnings": generated.preview["warnings"],
            "validation": {
                "card_name": validation["card"].get("name"),
                "card_skills": [
                    skill.get("name")
                    for skill in validation["card"].get("skills", [])
                    if isinstance(skill, dict)
                ],
                "dry_run_nodes": len(validation["dry_run"].get("nodes", [])),
                "sandbox": {
                    "ok": validation["sandbox"].get("ok"),
                    "skipped": validation["sandbox"].get("skipped"),
                    "image": validation["sandbox"].get("image"),
                },
            },
        },
    )
    await record_deployment_event(
        session,
        deployment,
        stage="build",
        status="running",
        message="Gitea Actions is building and publishing the generated meta-agent image.",
        data={"image": agent.image},
    )
    await record_deployment_event(
        session,
        deployment,
        stage="argo",
        status="running",
        message="ArgoCD is watching hidden runtime deploy manifests.",
        data={
            "repo_url": runtime_internal_url,
            "expected_revision": runtime_sha,
            "expected_image": settings.agent_image(agent.name, sha),
        },
    )
    return AgentFromComposeOut(
        name=agent.name,
        version=agent.version,
        status=agent.status,
        repo_url=_public_repo_url(agent.name, owner=agent.gitea_owner),
        expected_url=expected_url,
        deployment_id=deployment.deploy_id,
        head_sha=sha,
        preview=AgentComposePreviewOut.model_validate(generated.preview),
        refreshed_existing=existing is not None,
    )


@router.post("/from-tarball", response_model=AgentFromTarballOut, status_code=201)
async def from_tarball(
    request: Request,
    name: str = Form(...),
    version: str = Form(...),
    entrypoint: str = Form(...),
    agent_dsl: str = Form(...),
    description: str = Form(""),
    public: bool = Form(True),
    base_head_sha: str | None = Form(None),
    organization_slug: str | None = Form(None),
    source: UploadFile = File(...),
    user: User = Depends(current_user_or_studio_job),
    session: AsyncSession = Depends(get_session),
) -> AgentFromTarballOut:
    """Receive user source, then wire a hidden runtime repo for build/deploy."""
    claims = studio_job_claims(request)
    if claims is not None and str(claims.get("target_agent") or "") != name:
        raise HTTPException(403, "Studio job token is bound to a different agent")
    try:
        dsl = AgentDsl.model_validate_json(agent_dsl)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"invalid agent_dsl: {exc}") from exc
    if dsl.name != name:
        raise HTTPException(400, "agent_dsl.name must match name")
    if dsl.version != version:
        raise HTTPException(400, "agent_dsl.version must match version")
    dsl_entrypoint = ":".join(
        part
        for part in (dsl.entrypoint.module, dsl.entrypoint.class_name)
        if part
    )
    if dsl_entrypoint and dsl_entrypoint != entrypoint:
        raise HTTPException(400, "agent_dsl.entrypoint must match entrypoint")

    existing = (
        await session.execute(select(Agent).where(Agent.name == name))
    ).scalar_one_or_none()
    if existing is not None:
        await require_agent_access(
            session,
            user=user,
            agent=existing,
            action="edit_existing",
        )
    card = dsl.to_agent_card().model_dump(mode="json")
    card_availability = _runtime_availability_from_card(card)

    tarball_path, tarball_size = await _copy_upload_to_temp(
        source,
        max_bytes=_MAX_TARBALL_BYTES,
    )
    if not tarball_size:
        try:
            os.unlink(tarball_path)
        except FileNotFoundError:
            pass
        raise HTTPException(400, "empty tarball")
    try:
        database_declarations = read_agent_database_declarations_from_tarball(tarball_path)
        mailbox_declaration = read_agent_mailbox_declaration_from_tarball(tarball_path)
        tarball_availability = read_runtime_availability_from_tarball(tarball_path)
    except ValueError as exc:
        try:
            os.unlink(tarball_path)
        except FileNotFoundError:
            pass
        raise HTTPException(400, str(exc)) from exc
    availability = (
        AVAILABILITY_ALWAYS_ON
        if AVAILABILITY_ALWAYS_ON in {card_availability, tarball_availability}
        else AVAILABILITY_ON_DEMAND
    )
    _set_card_runtime_availability(card, availability)
    allow_always_on = _assert_can_request_availability(
        user,
        availability,
        existing=existing,
    )
    org_id, repo_owner = await _resolve_source_repo_scope(
        session,
        user,
        existing=existing,
        organization_slug=organization_slug,
        source="agent_from_tarball",
    )
    if org_id is None and any(item.scope == "org" for item in database_declarations):
        try:
            os.unlink(tarball_path)
        except FileNotFoundError:
            pass
        raise HTTPException(400, "org database declarations require an organization-scoped agent")
    if existing is not None:
        existing.organization_id = org_id
        existing.gitea_owner = repo_owner
        await _guard_fresh_source_upload(session, existing, base_head_sha)

    push_url, _ = ensure_repo(
        name,
        description,
        owner=repo_owner,
        public=public,
    )
    _ensure_source_repo_push_webhook(name, owner=repo_owner)

    try:
        sha = commit_and_push_source(
            tarball_path=tarball_path,
            name=name,
            entrypoint=entrypoint,
            push_url=push_url,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"push to gitea failed: {exc}")
    try:
        runtime_push_url, runtime_internal_url = _ensure_runtime_repo(name, description)
        runtime_sha = commit_and_push_runtime(
            tarball_path=tarball_path,
            name=name,
            entrypoint=entrypoint,
            source_repo_url=push_url,
            source_sha=sha,
            push_url=runtime_push_url,
            allow_always_on=allow_always_on,
        )
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"push runtime repo failed: {exc}")
    finally:
        try:
            os.unlink(tarball_path)
        except FileNotFoundError:
            pass
    runtime_gitops_wired = settings.in_cluster or settings.kubeconfig

    expected_url = _canonical_url(name) if public else None

    agent = existing or Agent(owner_id=user.id, name=name)
    agent.organization_id = org_id
    agent.gitea_owner = repo_owner
    agent.description = description
    agent.version = version
    agent.image = settings.agent_image(name, "latest")
    agent.public = public
    agent.card = card
    agent.status = "building"
    agent.url = expected_url
    if existing is None:
        session.add(agent)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, str(exc.orig))
    _provision_runtime_cp_jwt_if_requested(dsl=dsl, agent=agent, user=user)
    await warm_agent_card(agent.name, agent.card)
    await _index_agents_for_search([agent])

    deployment = await create_deployment(
        session,
        agent,
        trigger="from_tarball",
        status="building",
        source_repo_url=_public_repo_url(agent.name, owner=agent.gitea_owner),
        head_sha=sha,
        image=agent.image,
        agent_url=expected_url,
    )
    try:
        database_bindings = await reconcile_agent_database_bindings(
            session,
            agent=agent,
            user=user,
            declarations=database_declarations,
        )
    except ValueError as exc:
        await fail_deployment(
            session,
            deployment,
            stage="database",
            error=str(exc),
        )
        raise HTTPException(400, str(exc)) from exc
    if database_declarations or database_bindings.removed:
        await record_deployment_event(
            session,
            deployment,
            stage="database",
            status="queued",
            message="Agent database declarations were reconciled.",
            data={
                "requested": database_bindings.requested,
                "removed": database_bindings.removed,
            },
        )
    mailbox = await reconcile_agent_mailbox(
        session,
        agent=agent,
        user=user,
        declaration=mailbox_declaration,
    )
    await session.commit()
    if mailbox_declaration is not None and mailbox is not None:
        await record_deployment_event(
            session,
            deployment,
            stage="mailbox",
            status="queued",
            message=f"Agent mailbox {mailbox.address} was requested.",
            data={"address": mailbox.address, "status": mailbox.status},
        )
    await record_deployment_event(
        session,
        deployment,
        stage="source",
        status="passed",
        message="Source uploaded, scaffolded, and committed to the managed repo.",
        data={
            "head_sha": sha,
            "runtime_head_sha": runtime_sha,
            "base_head_sha": base_head_sha,
            "repo_url": _public_repo_url(agent.name, owner=agent.gitea_owner),
            "agent_dsl_schema_version": dsl.schema_version,
            "agent_dsl_language": dsl.language,
            "availability": availability,
        },
    )
    await record_deployment_event(
        session,
        deployment,
        stage="build",
        status="running",
        message="Gitea Actions is building and publishing the agent image.",
        data={"image": agent.image},
    )
    await record_deployment_event(
        session,
        deployment,
        stage="argo",
        status="running" if runtime_gitops_wired else "skipped",
        message=(
            "ArgoCD will reconcile hidden runtime deploy manifests."
            if runtime_gitops_wired
            else "Local control plane has no Kubernetes config; runtime repo was staged without ArgoCD wiring."
        ),
        data={
            "url": expected_url,
            "repo_url": runtime_internal_url,
            "expected_revision": runtime_sha,
            "expected_image": settings.agent_image(agent.name, sha),
            "gitops_wired": bool(runtime_gitops_wired),
            "availability": availability,
        },
    )
    # Advisory pre-deploy review — fire-and-forget. The deploy proceeds
    # regardless; findings land on AgentReviewRun + a stage="review" event.
    enqueue_deploy_review(
        request.app,
        agent_id=agent.id,
        agent_name=agent.name,
        ref=sha,
        owner=agent.gitea_owner,
        user_id=user.id,
        deploy_id=deployment.deploy_id,
    )
    return AgentFromTarballOut(
        name=name,
        version=version,
        status=agent.status,
        url=expected_url,
        head_sha=sha,
        deployment_id=deployment.deploy_id,
    )


@router.get("/mine", response_model=list[AgentMineOut])
async def list_my_agents(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[AgentMineOut]:
    """Agents owned by the caller (built by them or deployed via CLI).

    The orchestrator surfaces this as ``list_my_agents`` so the LLM
    can answer "iterate on X" without scanning the full marketplace.
    """
    rows = (
        await session.execute(
            select(Agent).where(Agent.owner_id == user.id)
        )
    ).scalars().all()
    await _refresh_cards_inplace(list(rows), session)
    latest_deployments = await _latest_deployment_outs(list(rows), session)
    openapi_source_urls = await _openapi_source_urls(list(rows), session)
    code_editor_opt_ins = await _code_editor_opt_ins(list(rows), session)
    out: list[AgentMineOut] = []
    for row in rows:
        out.append(
            _agent_mine_out(
                row,
                latest_deployments.get(row.name),
                openapi_source_urls=openapi_source_urls.get(row.name),
                code_editor=_agent_code_editor_out(
                    row,
                    code_editor_opt_ins.get(row.id),
                ),
            )
        )
    return out


@router.get("/mine/summary", response_model=list[AgentMineSummaryOut])
async def list_my_agent_summaries(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[AgentMineSummaryOut]:
    rows = (
        await session.execute(
            select(Agent).where(Agent.owner_id == user.id)
        )
    ).scalars().all()
    latest_deployments = await _latest_deployment_outs(list(rows), session)
    return [
        _agent_mine_summary_out(row, latest_deployments.get(row.name))
        for row in rows
    ]


@router.get("/mine/{name}", response_model=AgentMineOut)
async def get_my_agent(
    name: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> AgentMineOut:
    agent = await _get_owned_agent(name, user, session)
    await _refresh_cards_inplace([agent], session)
    latest_deployment = await latest_deployment_for_agent(session, agent)
    return await _agent_mine_out_for_agent(agent, session, latest_deployment)


class AgentVisibilityIn(BaseModel):
    public: bool


class AgentReusePolicyIn(BaseModel):
    fork_policy: Literal["private", "organization", "public"]


@router.patch("/mine/{name}/visibility", response_model=AgentMineOut)
async def update_my_agent_visibility(
    name: str,
    body: AgentVisibilityIn,
    request: Request,
    user: User = Depends(current_user_or_studio_job),
    session: AsyncSession = Depends(get_session),
) -> AgentMineOut:
    """Publish or unlist an owned agent without rebuilding its runtime."""
    agent = await _get_owned_agent(name, user, session)
    if body.public and (
        not agent.url or agent.status not in {"ready", "running", "needs_auth"}
    ):
        raise HTTPException(409, "agent must be deployed before it can be published")
    if not _is_external_agent(agent):
        set_repo_visibility(
            agent.name,
            owner=agent.gitea_owner,
            public=body.public,
        )
    agent.public = body.public
    await session.commit()
    await session.refresh(agent)
    await _index_agents_for_search([agent])
    if agent.public:
        enqueue_agent_seo_profile(request.app, agent.id)
    latest_deployment = await latest_deployment_for_agent(session, agent)
    return await _agent_mine_out_for_agent(agent, session, latest_deployment)


@router.patch("/mine/{name}/reuse-policy", response_model=AgentMineOut)
async def update_my_agent_reuse_policy(
    name: str,
    body: AgentReusePolicyIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> AgentMineOut:
    """Set source-fork permission independently from public visibility."""
    agent = await _get_owned_agent(name, user, session)
    agent.fork_policy = body.fork_policy
    await session.commit()
    await session.refresh(agent)
    latest_deployment = await latest_deployment_for_agent(session, agent)
    return await _agent_mine_out_for_agent(agent, session, latest_deployment)


@router.get("/{name}/code-editor", response_model=AgentCodeEditorOut)
async def get_agent_code_editor(
    name: str,
    user: User = Depends(current_user_or_studio_job),
    session: AsyncSession = Depends(get_session),
) -> AgentCodeEditorOut:
    first_party = _first_party_repo_code_editor_out(name, user)
    if first_party is not None:
        return first_party
    agent = await _get_writable_agent(name, user, session)
    opt_in = await _code_editor_opt_in_for_agent(agent, session)
    return _agent_code_editor_out(agent, opt_in)


@router.get("/{name}/self-healing")
async def list_agent_self_healing(
    name: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    """Owner-visible, sanitized repair history for a manifest-opted-in agent."""
    from ..self_healing import (
        SELF_HEALING_JOB_KIND,
        public_policy_from_card,
        sanitize_public_payload,
    )

    agent = await _get_owned_agent(name, user, session)
    jobs = (
        await session.execute(
            select(WorkJob)
            .where(WorkJob.user_id == user.id)
            .where(WorkJob.kind == SELF_HEALING_JOB_KIND)
            .where(WorkJob.subject_type == "agent")
            .where(WorkJob.subject_id == str(agent.id))
            .order_by(desc(WorkJob.created_at), desc(WorkJob.id))
            .limit(limit)
        )
    ).scalars().all()
    job_ids = [job.job_id for job in jobs]
    events = (
        (
            await session.execute(
                select(WorkEvent)
                .where(WorkEvent.job_id.in_(job_ids))
                .order_by(WorkEvent.job_id.asc(), WorkEvent.event_seq.asc(), WorkEvent.id.asc())
            )
        ).scalars().all()
        if job_ids
        else []
    )
    events_by_job: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        events_by_job.setdefault(event.job_id, []).append(
            sanitize_public_payload(dict(serialize_event(event)))
        )
    items: list[dict[str, Any]] = []
    for job in jobs:
        item = sanitize_public_payload(dict(serialize_job(job)))
        item["events"] = events_by_job.get(job.job_id, [])
        items.append(item)
    return {
        "agent_name": agent.name,
        "policy": public_policy_from_card(
            agent.card if isinstance(agent.card, dict) else None
        ),
        "runs": items,
    }


@router.post("/{name}/code-editor", response_model=AgentMineOut)
async def enable_agent_code_editor(
    name: str,
    user: User = Depends(current_user_or_studio_job),
    session: AsyncSession = Depends(get_session),
) -> AgentMineOut:
    agent = await _get_writable_agent(name, user, session)
    await _ensure_code_editor_target_source(agent, user, session)
    opt_in = await _code_editor_opt_in_for_agent(agent, session)
    if opt_in is None:
        opt_in = AgentCodeEditorOptIn(
            agent_id=agent.id,
            user_id=user.id,
            status="enabled",
            shared_agent_name=settings.code_editor_agent_name,
        )
        session.add(opt_in)
    opt_in.status = "enabled"
    opt_in.shared_agent_name = settings.code_editor_agent_name
    opt_in.workspace_key = _agent_code_editor_workspace_key(agent)
    opt_in.last_error = None
    await session.commit()
    await session.refresh(agent)
    await session.refresh(opt_in)

    latest_deployment = await latest_deployment_for_agent(session, agent)
    latest_out = (
        await _deployment_out(latest_deployment, session, agent=agent)
        if latest_deployment is not None
        else None
    )
    return await _agent_mine_out_for_agent(agent, session, latest_out)


@router.delete("/{name}/code-editor", response_model=AgentMineOut)
async def disable_agent_code_editor(
    name: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> AgentMineOut:
    agent = await _get_owned_agent(name, user, session)
    opt_in = await _code_editor_opt_in_for_agent(agent, session)
    if opt_in is not None:
        await session.delete(opt_in)
        await session.commit()

    latest_deployment = await latest_deployment_for_agent(session, agent)
    latest_out = (
        await _deployment_out(latest_deployment, session, agent=agent)
        if latest_deployment is not None
        else None
    )
    return await _agent_mine_out_for_agent(agent, session, latest_out)


@router.get("", response_model=list[AgentOut])
async def list_agents(
    request: Request,
    user: User = Depends(current_user_or_agent_invoke),
    session: AsyncSession = Depends(get_session),
) -> list[AgentOut]:
    # A hosted invocation runs seller-controlled code as the buyer, so it gets
    # the public marketplace and nothing else. ``visible_agents_clause`` also
    # matches the buyer's private and org-only agents — capability discovery
    # from inside a paid call is not a reason to hand a seller an inventory of
    # what else the buyer owns, with each card's URL. A named card read
    # (``GET /v1/agents/{name}``) stays unfiltered: A2A hand-off has to resolve
    # its target, which may legitimately be a private agent of the same buyer.
    visibility = (
        Agent.public.is_(True)
        if agent_invoke_claims(request) is not None
        else visible_agents_clause(user.id)
    )
    rows = (
        await session.execute(
            select(Agent).where(visibility)
        )
    ).scalars().all()
    # Lazy-refresh any agent whose card is empty (placeholder from initial
    # registration). Without this, discover_agent in the orchestrator sees
    # ``skills=[]`` and drops every agent through its tag filter.
    await _refresh_cards_inplace(list(rows), session)
    return [AgentOut.model_validate(row) for row in rows]


@router.get("/search", response_model=list[AgentSearchOut])
async def search_agents(
    q: str = Query(default="", max_length=500),
    tag: list[str] | None = Query(default=None),
    skill: str | None = Query(default=None, max_length=128),
    limit: int = Query(default=8, ge=1, le=_AGENT_SEARCH_LIMIT_MAX),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[AgentSearchOut]:
    rows, scores, source = await _search_visible_agents(
        session=session,
        user=user,
        q=q,
        tags=[item.strip() for item in tag or [] if item.strip()],
        skill=skill.strip() if skill and skill.strip() else None,
        limit=limit,
    )
    return [
        _agent_search_out(
            agent,
            score=scores.get(agent.id),
            match_source=source,
        )
        for agent in rows
    ]


@router.get("/{name}/source")
async def export_agent_source(
    name: str,
    user: User = Depends(current_user_or_studio_job),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    agent = await _get_writable_agent(name, user, session)
    try:
        bundle, head_sha = source_tarball_from_repo(agent.name, owner=agent.gitea_owner)
    except RepoDriftCheckError as exc:
        raise HTTPException(
            502,
            f"could not export current source repo: {exc}",
        ) from exc
    return StreamingResponse(
        io.BytesIO(bundle),
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{agent.name}.tar.gz"',
            "X-A2A-Repo-Head-Sha": head_sha,
        },
    )


@router.post("/{name}/source/deploy")
async def deploy_agent_source(
    name: str,
    request: Request,
    user: User = Depends(current_user_or_studio_job),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    agent = await _get_writable_agent(name, user, session)
    if agent.gitea_owner is None and agent.organization_id is not None:
        agent.gitea_owner = await gitea_owner_for_agent(
            session,
            user,
            agent.organization_id,
        )
        await session.commit()

    owner = agent.gitea_owner or GITEA_USER
    try:
        source_sha = repo_head_sha(agent.name, owner=owner)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"could not reach agent source repo: {exc}") from exc
    if not source_sha:
        raise HTTPException(409, "No managed source repo exists for this agent.")

    try:
        result = await deploy_source_push(
            session,
            request.app,
            owner=owner,
            repo=agent.name,
            source_sha=source_sha,
            changed_paths=["<explicit-source-deploy>"],
            delivery_id=None,
            ref="refs/heads/main",
            trigger="source_manual",
        )
    except SourcePushDeployBlocked as exc:
        raise HTTPException(409, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc

    deploy_id = result.get("deploy_id")
    track_event(
        "agent_deployed",
        profile_id=f"user:{user.id}",
        properties={
            "agentName": agent.name,
            "trigger": "source_manual",
            **({"deployId": deploy_id} if isinstance(deploy_id, str) else {}),
        },
        request=request,
    )
    if isinstance(deploy_id, str) and deploy_id:
        deploy = (
            await session.execute(
                select(AgentDeployment).where(
                    AgentDeployment.agent_name == agent.name,
                    AgentDeployment.deploy_id == deploy_id,
                    AgentDeployment.user_id == user.id,
                )
            )
        ).scalar_one_or_none()
        if deploy is not None:
            result["deployment"] = (
                await _deployment_out(deploy, session, agent=agent)
            ).model_dump(mode="json")
    return result


@router.get("/{name}/deployments", response_model=list[AgentDeploymentOut])
async def list_agent_deployments(
    name: str,
    user: User = Depends(current_user_or_studio_job),
    session: AsyncSession = Depends(get_session),
) -> list[AgentDeploymentOut]:
    agent = await _get_writable_agent(name, user, session)
    rows = (
        await session.execute(
            select(AgentDeployment)
            .where(
                AgentDeployment.agent_name == agent.name,
            )
            .order_by(desc(AgentDeployment.id))
            .limit(25)
        )
    ).scalars().all()
    return [
        await _deployment_out(row, session, agent=agent if i == 0 else None)
        for i, row in enumerate(rows)
    ]


@router.get("/{name}/deployments/{deploy_id}", response_model=AgentDeploymentOut)
async def get_agent_deployment(
    name: str,
    deploy_id: str,
    user: User = Depends(current_user_or_studio_job),
    session: AsyncSession = Depends(get_session),
) -> AgentDeploymentOut:
    agent = await _get_writable_agent(name, user, session)
    deploy = (
        await session.execute(
            select(AgentDeployment).where(
                AgentDeployment.agent_name == agent.name,
                AgentDeployment.deploy_id == deploy_id,
            )
        )
    ).scalar_one_or_none()
    if deploy is None:
        raise HTTPException(404, "deployment not found")
    return await _deployment_out(deploy, session, agent=agent)


@router.get("/{name}/deployments/{deploy_id}/stream")
async def stream_agent_deployment(
    name: str,
    deploy_id: str,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    agent = await _get_owned_agent(name, user, session)
    deploy = await _get_owned_deployment(agent, deploy_id, user.id, session)
    if deploy is None:
        raise HTTPException(404, "deployment not found")

    async def gen() -> AsyncIterator[bytes]:
        last_event_id = 0
        while True:
            if await request.is_disconnected():
                break
            current = await _get_owned_deployment(agent, deploy_id, user.id, session)
            if current is None:
                yield _deployment_sse({"type": "error", "message": "deployment not found"})
                yield _deployment_sse({"type": "done"})
                break
            snapshot = await _deployment_out(current, session, agent=agent)
            snapshot_payload = snapshot.model_dump(mode="json")
            yield _deployment_sse({"type": "snapshot", "deployment": snapshot_payload})
            for event in snapshot.events:
                if event.id <= last_event_id:
                    continue
                yield _deployment_sse(
                    {
                        "type": "event",
                        "deploy_id": snapshot.deploy_id,
                        "event": event.model_dump(mode="json"),
                    }
                )
                last_event_id = max(last_event_id, event.id)
            if snapshot.status not in ACTIVE_DEPLOY_STATUSES:
                yield _deployment_sse({"type": "done", "deployment": snapshot_payload})
                yield b"data: [DONE]\n\n"
                break
            await asyncio.sleep(2.0)
            yield _deployment_sse_comment()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/{name}/deployments/{deploy_id}/logs")
async def get_agent_deployment_logs(
    name: str,
    deploy_id: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> AgentDeploymentLogsOut:
    agent = await _get_owned_agent(name, user, session)
    deploy = await _get_owned_deployment(agent, deploy_id, user.id, session)
    if deploy is None:
        raise HTTPException(404, "deployment not found")
    # Sync first so status/verification are current, then lazily proxy the raw
    # build/runtime logs for this deploy (only done here, on drawer open).
    await _deployment_out(deploy, session, agent=agent)
    await collect_deployment_logs(session, deploy, deploy.verification or {})
    await session.commit()
    rows = (
        await session.execute(
            select(AgentDeploymentLog)
            .where(AgentDeploymentLog.deploy_id == deploy_id)
            .order_by(AgentDeploymentLog.stage.asc(), AgentDeploymentLog.source.asc())
        )
    ).scalars().all()
    return AgentDeploymentLogsOut(
        deploy_id=deploy_id,
        agent_name=agent.name,
        logs=[AgentDeploymentLogOut.model_validate(row) for row in rows],
    )


async def _get_owned_deployment(
    agent: Agent,
    deploy_id: str,
    user_id: int,
    session: AsyncSession,
) -> AgentDeployment | None:
    return (
        await session.execute(
            select(AgentDeployment).where(
                AgentDeployment.agent_name == agent.name,
                AgentDeployment.deploy_id == deploy_id,
                AgentDeployment.user_id == user_id,
            )
        )
    ).scalar_one_or_none()


def _deployment_sse(payload: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(payload, separators=(",", ":")).encode() + b"\n\n"


def _deployment_sse_comment(comment: str = "ping") -> bytes:
    return f": {comment}\n\n".encode()


@router.get("/{name}/domains", response_model=list[AgentCustomDomainOut])
async def list_agent_custom_domains(
    name: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[AgentCustomDomainOut]:
    agent = await _get_owned_agent(name, user, session)
    rows = (
        await session.execute(
            select(AgentCustomDomain)
            .where(AgentCustomDomain.agent_id == agent.id)
            .order_by(AgentCustomDomain.hostname)
        )
    ).scalars().all()
    cert_status: str | None = None
    if any(row.status == "active" for row in rows):
        try:
            cert_status = custom_domain_certificate_status(agent.name)
        except Exception as exc:  # noqa: BLE001
            log.warning("custom domain cert status lookup failed for %s: %s", agent.name, exc)
            cert_status = "unknown"
    return [_agent_custom_domain_out(row, cert_status) for row in rows]


@router.post("/{name}/domains", response_model=AgentCustomDomainOut, status_code=201)
async def add_agent_custom_domain(
    name: str,
    body: AgentCustomDomainIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> AgentCustomDomainOut:
    agent = await _get_owned_agent(name, user, session)
    if not agent.public:
        raise HTTPException(400, "custom domains require a public agent")
    if _is_external_agent(agent):
        raise HTTPException(400, "custom domains are only available for hosted agents")
    hostname = _normalize_custom_hostname(body.hostname)
    requested_hostnames = [hostname]
    if body.include_www:
        www_pair = _www_pair_hostname(hostname)
        if www_pair and www_pair != hostname:
            requested_hostnames.append(www_pair)
    if body.canonical_hostname:
        canonical = _normalize_custom_hostname(body.canonical_hostname)
        if canonical not in requested_hostnames:
            raise HTTPException(400, "canonical hostname must be one of the requested hostnames")
    else:
        canonical = hostname
    if hostname == _canonical_host(agent.name):
        raise HTTPException(400, "hostname is already the agent's default host")

    created_or_existing: list[AgentCustomDomain] = []
    for requested_hostname in requested_hostnames:
        if requested_hostname == _canonical_host(agent.name):
            raise HTTPException(400, "hostname is already the agent's default host")
        existing = (
            await session.execute(
                select(AgentCustomDomain).where(AgentCustomDomain.hostname == requested_hostname)
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.agent_id == agent.id:
                existing.canonical_hostname = canonical
                existing.redirect_enabled = requested_hostname != canonical
                created_or_existing.append(existing)
                continue
            raise HTTPException(409, "hostname is already connected to another agent")
        row = AgentCustomDomain(
            agent_id=agent.id,
            user_id=user.id,
            agent_name=agent.name,
            hostname=requested_hostname,
            verification_token=secrets.token_urlsafe(24),
            status="pending",
            canonical_hostname=canonical,
            redirect_enabled=requested_hostname != canonical,
        )
        session.add(row)
        created_or_existing.append(row)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, str(exc.orig))
    for row in created_or_existing:
        await session.refresh(row)
    return _agent_custom_domain_out(created_or_existing[0])


@router.post("/{name}/domains/{hostname}/verify", response_model=AgentCustomDomainOut)
async def verify_agent_custom_domain(
    name: str,
    hostname: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> AgentCustomDomainOut:
    agent = await _get_owned_agent(name, user, session)
    normalized = _normalize_custom_hostname(hostname)
    row = (
        await session.execute(
            select(AgentCustomDomain)
            .where(AgentCustomDomain.agent_id == agent.id)
            .where(AgentCustomDomain.hostname == normalized)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "custom domain not found")
    if not agent.public or _is_external_agent(agent):
        row.status = "pending"
        row.last_error = "custom domains require a public hosted agent"
        await session.commit()
        raise HTTPException(400, row.last_error)

    verify_name, verify_value = _custom_domain_verification_record(
        row.hostname,
        row.verification_token,
    )
    if not _dns_txt_record_matches(verify_name, verify_value):
        row.status = "pending"
        row.last_error = "DNS TXT verification record was not found"
        await session.commit()
        raise HTTPException(400, row.last_error)

    _record_type, cname_name, cname_value = _custom_domain_routing_record(
        agent.name,
        row.hostname,
    )
    if not _dns_cname_points_to(cname_name, cname_value):
        row.status = "pending"
        fallback = _custom_domain_routing_fallback_record(agent.name, row.hostname)
        fallback_text = (
            f", or set {fallback[0]} {fallback[1]} to {fallback[2]}"
            if fallback
            else ""
        )
        row.last_error = (
            f"DNS must point {cname_name} to {cname_value} with CNAME/ALIAS"
            f"{fallback_text}"
        )
        await session.commit()
        raise HTTPException(400, row.last_error)

    row.status = "verified"
    row.verified_at = row.verified_at or utcnow()
    row.last_error = None
    await _activate_paired_custom_domains(agent, session, row)
    await session.flush()
    try:
        await _sync_agent_custom_domain_ingress(agent, session)
    except Exception as exc:  # noqa: BLE001
        row.status = "verified"
        row.last_error = f"ingress sync failed: {_short_cleanup_error(exc)}"
        await session.commit()
        raise HTTPException(502, row.last_error) from exc
    pair = _www_pair_hostname(row.hostname)
    if pair is not None:
        canonical = row.canonical_hostname or row.hostname
        paired = (
            await session.execute(
                select(AgentCustomDomain)
                .where(AgentCustomDomain.agent_id == agent.id)
                .where(AgentCustomDomain.hostname == pair)
            )
        ).scalars().all()
        for paired_row in paired:
            if paired_row.verified_at is not None and (
                paired_row.canonical_hostname or paired_row.hostname
            ) == canonical:
                paired_row.status = "active"
                paired_row.last_error = None
    row.status = "active"
    row.last_error = None
    await session.commit()
    await session.refresh(row)
    return _agent_custom_domain_out(row)


@router.delete("/{name}/domains/{hostname}", status_code=204)
async def delete_agent_custom_domain(
    name: str,
    hostname: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    agent = await _get_owned_agent(name, user, session)
    normalized = _normalize_custom_hostname(hostname)
    row = (
        await session.execute(
            select(AgentCustomDomain)
            .where(AgentCustomDomain.agent_id == agent.id)
            .where(AgentCustomDomain.hostname == normalized)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "custom domain not found")
    await session.delete(row)
    await session.flush()
    try:
        await _sync_agent_custom_domain_ingress(agent, session)
    except Exception as exc:  # noqa: BLE001
        await session.rollback()
        raise HTTPException(
            502,
            f"ingress sync failed: {_short_cleanup_error(exc)}",
        ) from exc
    await session.commit()


def _is_agent_api_bearer(authorization: str | None) -> bool:
    token = _extract_bearer(authorization)
    return bool(token and token.startswith(_AGENT_API_TOKEN_PREFIX))


async def _agent_openapi_target(
    *,
    name: str,
    authorization: str | None,
    integration_token: str | None,
    user: User | None,
    session: AsyncSession,
) -> tuple[Agent, str | None]:
    """Resolve the agent whose OpenAPI document to render.

    An app API token is accepted from the ``Authorization`` header as well as
    the query string, so the header form works for private agents too. Only the
    query-string form is echoed back into the document.

    A header token that does not resolve must never make this endpoint stricter
    than it was: a client carrying another agent's integration token (or a
    stale one) still gets the anonymous read of a *public* agent it got before
    the header form was accepted here. The token error is only surfaced when the
    anonymous path would have been denied too.
    """
    query_token = _query_token_value(integration_token)
    if query_token:
        api_token, agent, _token_user = await _agent_api_token_agent(
            name=name,
            authorization=authorization,
            integration_token=query_token,
            session=session,
        )
        api_token.last_used_at = datetime.now(timezone.utc)
        await session.commit()
        return agent, query_token
    if _is_agent_api_bearer(authorization):
        try:
            api_token, agent, _token_user = await _agent_api_token_agent(
                name=name,
                authorization=authorization,
                session=session,
            )
        except HTTPException as token_error:
            await session.rollback()
            try:
                return await _get_visible_agent(name, user, session), None
            except HTTPException:
                raise token_error from None
        api_token.last_used_at = datetime.now(timezone.utc)
        await session.commit()
        return agent, None
    return await _get_visible_agent(name, user, session), None


@router.get("/{name}/openapi.json")
async def agent_api_openapi(
    name: str,
    request: Request,
    authorization: str | None = Header(default=None),
    integration_token: str | None = Query(default=None),
    user: User | None = Depends(optional_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    agent, query_token = await _agent_openapi_target(
        name=name,
        authorization=authorization,
        integration_token=integration_token,
        user=user,
        session=session,
    )
    return _agent_openapi_document(agent, request=request, integration_token=query_token)


@router.get("/{name}/api/openapi.json")
async def agent_api_openapi_alias(
    name: str,
    request: Request,
    authorization: str | None = Header(default=None),
    integration_token: str | None = Query(default=None),
    user: User | None = Depends(optional_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    agent, query_token = await _agent_openapi_target(
        name=name,
        authorization=authorization,
        integration_token=integration_token,
        user=user,
        session=session,
    )
    return _agent_openapi_document(agent, request=request, integration_token=query_token)


@router.get("/{name}/api-tokens", response_model=list[AgentApiTokenOut])
async def list_agent_api_tokens(
    name: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[AgentApiTokenOut]:
    agent = await _get_owned_agent(name, user, session)
    rows = (
        await session.execute(
            select(AgentApiToken)
            .where(AgentApiToken.agent_id == agent.id)
            .order_by(AgentApiToken.id.desc())
        )
    ).scalars().all()
    return [AgentApiTokenOut.model_validate(row) for row in rows]


@router.get("/{name}/account-access", response_model=AgentAccountAccessOut)
async def get_agent_account_access(
    name: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> AgentAccountAccessOut:
    agent = await _get_visible_agent(name, user, session)
    policy = account_access_policy(agent.card)
    used = await account_access_usage(
        session,
        agent_id=agent.id,
        user_id=user.id,
    )
    has_byok = (
        await session.execute(
            select(UserLLMCreds.id).where(
                UserLLMCreds.user_id == user.id,
                UserLLMCreds.name == "default",
            )
        )
    ).scalar_one_or_none() is not None
    return AgentAccountAccessOut(
        agent=agent.name,
        required=policy.required,
        platform_skill_calls=policy.platform_skill_calls,
        platform_skill_calls_used=used,
        platform_skill_calls_remaining=max(0, policy.platform_skill_calls - used),
        after_trial=policy.after_trial,
        has_byok=has_byok,
        setup_url=f"{str(settings.dashboard_url).rstrip('/')}/llm-keys",
    )


@router.post(
    "/{name}/api-tokens",
    response_model=AgentApiTokenCreatedOut,
    status_code=201,
)
async def create_agent_api_token(
    name: str,
    body: AgentApiTokenCreateIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> AgentApiTokenCreatedOut:
    agent = await _get_owned_agent(name, user, session)
    raw_token = _new_agent_api_token()
    scopes = _normalize_agent_api_scopes(body.scopes)
    row = AgentApiToken(
        agent_id=agent.id,
        user_id=user.id,
        agent_name=agent.name,
        name=body.name.strip(),
        token_hash=_hash_agent_api_token(raw_token),
        token_last4=raw_token[-4:],
        scopes=scopes,
        enabled=True,
        expires_at=body.expires_at,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    data = AgentApiTokenOut.model_validate(row).model_dump()
    return AgentApiTokenCreatedOut(**data, token=raw_token)


@router.delete("/{name}/api-tokens/{token_id}", status_code=204)
async def revoke_agent_api_token(
    name: str,
    token_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    agent = await _get_owned_agent(name, user, session)
    row = (
        await session.execute(
            select(AgentApiToken)
            .where(AgentApiToken.agent_id == agent.id)
            .where(AgentApiToken.id == token_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "API token not found")
    row.enabled = False
    await session.commit()


def _agent_integration_curl_example(
    urls: dict[str, str | None],
    *,
    token: str,
) -> str:
    url = urls.get("sample_invoke_url") or urls.get("invoke_base_url") or ""
    return (
        f"curl -X POST {json.dumps(url)} \\\n"
        f"  -H {json.dumps(f'Authorization: Bearer {token}')} \\\n"
        '  -H "Content-Type: application/json" \\\n'
        '  -d \'{"arguments":{}}\''
    )


def _agent_integration_link_out(
    row: AgentApiToken,
    agent: Agent,
    *,
    token: str | None = None,
    request: Request | None = None,
    url_token: bool = False,
) -> AgentIntegrationLinkOut | AgentIntegrationLinkCreatedOut:
    data = AgentApiTokenOut.model_validate(row).model_dump()
    urls = _agent_integration_urls(
        agent, token=token, request=request, embed_token=url_token
    )
    if token is not None:
        # The curl example always demonstrates the header form, even for the
        # opt-in URL-token variant, so the safe path stays the documented one.
        header_urls = _agent_integration_urls(agent, request=request)
        return AgentIntegrationLinkCreatedOut(
            **data,
            token=token,
            curl_example=_agent_integration_curl_example(header_urls, token=token),
            token_placement="url_query" if url_token else "header",
            security_notice=_URL_TOKEN_SECURITY_NOTICE if url_token else None,
            **urls,
        )
    return AgentIntegrationLinkOut(**data, **urls)


@router.get("/{name}/integration-links", response_model=list[AgentIntegrationLinkOut])
async def list_agent_integration_links(
    name: str,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[AgentIntegrationLinkOut]:
    agent = await _get_visible_agent(name, user, session)
    rows = (
        await session.execute(
            select(AgentApiToken)
            .where(AgentApiToken.agent_id == agent.id)
            .where(AgentApiToken.user_id == user.id)
            .order_by(AgentApiToken.id.desc())
        )
    ).scalars().all()
    links = [row for row in rows if _is_integration_link(row)]
    changed = False
    for row in links:
        changed = _ensure_integration_link_expiry(row) or changed
    if changed:
        await session.commit()
    return [
        _agent_integration_link_out(row, agent, request=request)
        for row in links
    ]


@router.post(
    "/{name}/integration-links",
    response_model=AgentIntegrationLinkCreatedOut,
    status_code=201,
)
async def create_agent_integration_link(
    name: str,
    request: Request,
    body: AgentIntegrationLinkCreateIn | None = None,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> AgentIntegrationLinkCreatedOut:
    agent = await _get_visible_agent(name, user, session)
    options = body or AgentIntegrationLinkCreateIn()
    url_token = bool(options.url_token)
    default_ttl_days = (
        _AGENT_INTEGRATION_LINK_URL_TOKEN_TTL_DAYS
        if url_token
        else _AGENT_INTEGRATION_LINK_TTL_DAYS
    )
    ttl_days = min(
        options.expires_in_days or default_ttl_days,
        _AGENT_INTEGRATION_LINK_MAX_TTL_DAYS,
    )
    raw_token = _new_agent_api_token()
    # The label is also the marker `_is_minted_integration_link` keys off, so
    # it has to come from the shared helper.
    label = _integration_link_label(agent.name, url_token=url_token)
    row = AgentApiToken(
        agent_id=agent.id,
        user_id=user.id,
        agent_name=agent.name,
        name=label,
        token_hash=_hash_agent_api_token(raw_token),
        token_last4=raw_token[-4:],
        scopes=["invoke", "mcp"],
        enabled=True,
        expires_at=datetime.now(timezone.utc) + timedelta(days=ttl_days),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _agent_integration_link_out(
        row, agent, token=raw_token, request=request, url_token=url_token
    )


@router.delete("/{name}/integration-links/{link_id}", status_code=204)
async def revoke_agent_integration_link(
    name: str,
    link_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    agent = await _get_visible_agent(name, user, session)
    row = (
        await session.execute(
            select(AgentApiToken)
            .where(AgentApiToken.agent_id == agent.id)
            .where(AgentApiToken.user_id == user.id)
            .where(AgentApiToken.id == link_id)
        )
    ).scalar_one_or_none()
    if row is None or "mcp" not in set(row.scopes or []):
        raise HTTPException(404, "integration link not found")
    row.enabled = False
    await session.commit()


@router.api_route(
    "/{name}/api/endpoints/{endpoint_name}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    responses=_AGENT_INVOKE_RESPONSES,
)
async def invoke_agent_api_endpoint(
    name: str,
    endpoint_name: str,
    request: Request,
    authorization: str | None = Header(default=None),
    integration_token: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    query_token = _query_token_value(integration_token)
    api_token, agent, token_user = await _agent_api_token_agent(
        name=name,
        authorization=authorization,
        integration_token=query_token,
        session=session,
    )
    endpoint = _agent_runtime_endpoint(agent, endpoint_name)
    allowed_methods = _agent_endpoint_methods(endpoint)
    if request.method.upper() not in allowed_methods:
        raise HTTPException(405, "method not allowed for agent endpoint")
    skill_name = str(endpoint.get("skill") or "").strip()
    if not skill_name:
        raise HTTPException(500, "agent endpoint is missing skill mapping")
    arguments = await _agent_endpoint_arguments_from_request(request, endpoint)

    outputs_prefix: str | None = None
    started_at = datetime.now(timezone.utc)
    try:
        raw_payload, outputs_prefix = await _invoke_agent_api_skill(
            agent=agent,
            user=token_user,
            skill_name=skill_name,
            arguments=arguments,
            session=session,
        )
        normalized = _normalize_agent_api_result(
            payload=raw_payload,
            agent_name=agent.name,
            skill_name=skill_name,
            outputs_prefix=outputs_prefix,
            public_base_url=_agent_api_public_base_url(request),
            integration_token=query_token,
        )
        await _persist_agent_api_receipt(
            session=session,
            agent=agent,
            user=token_user,
            skill_name=skill_name,
            arguments=arguments,
            result=normalized,
            started_at=started_at,
            status=_agent_api_receipt_status(normalized),
        )
    except Exception as exc:
        receipt_id = await _persist_agent_api_receipt(
            session=session,
            agent=agent,
            user=token_user,
            skill_name=skill_name,
            arguments=arguments,
            result=_agent_api_error_result(exc),
            started_at=started_at,
            status="error",
            error_type=type(exc).__name__,
            self_heal_actionable=True,
        )
        _attach_failure_receipt_id(exc, receipt_id)
        raise
    finally:
        api_token.last_used_at = datetime.now(timezone.utc)
        await session.commit()
    result = raw_payload.get("result") if isinstance(raw_payload, dict) else None
    return result if isinstance(result, dict) else normalized


@router.post("/{name}/api/invoke/{skill_name}", responses=_AGENT_INVOKE_RESPONSES)
async def invoke_agent_api(
    name: str,
    skill_name: str,
    request: Request,
    authorization: str | None = Header(default=None),
    integration_token: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    query_token = _query_token_value(integration_token)
    api_token, agent, token_user = await _agent_api_token_agent(
        name=name,
        authorization=authorization,
        integration_token=query_token,
        session=session,
    )
    arguments = await _invoke_arguments_from_request(
        request,
        agent=agent,
        skill_name=skill_name,
        user=token_user,
    )

    outputs_prefix: str | None = None
    started_at = datetime.now(timezone.utc)
    try:
        raw_payload, outputs_prefix = await _invoke_agent_api_skill(
            agent=agent,
            user=token_user,
            skill_name=skill_name,
            arguments=arguments,
            session=session,
        )
        normalized = _normalize_agent_api_result(
            payload=raw_payload,
            agent_name=agent.name,
            skill_name=skill_name,
            outputs_prefix=outputs_prefix,
            public_base_url=_agent_api_public_base_url(request),
            integration_token=query_token,
        )
        await _persist_agent_api_receipt(
            session=session,
            agent=agent,
            user=token_user,
            skill_name=skill_name,
            arguments=arguments,
            result=normalized,
            started_at=started_at,
            status=_agent_api_receipt_status(normalized),
        )
    except Exception as exc:
        receipt_id = await _persist_agent_api_receipt(
            session=session,
            agent=agent,
            user=token_user,
            skill_name=skill_name,
            arguments=arguments,
            result=_agent_api_error_result(exc),
            started_at=started_at,
            status="error",
            error_type=type(exc).__name__,
            self_heal_actionable=True,
        )
        _attach_failure_receipt_id(exc, receipt_id)
        raise
    finally:
        api_token.last_used_at = datetime.now(timezone.utc)
        await session.commit()
    return normalized


@router.post("/{name}/api/runs/{skill_name}/start", status_code=202)
async def start_agent_api_run(
    name: str,
    skill_name: str,
    request: Request,
    authorization: str | None = Header(default=None),
    integration_token: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    query_token = _query_token_value(integration_token)
    api_token, agent, token_user = await _agent_api_token_agent(
        name=name,
        authorization=authorization,
        integration_token=query_token,
        session=session,
    )
    arguments = await _invoke_arguments_from_request(
        request,
        agent=agent,
        skill_name=skill_name,
        user=token_user,
    )
    job = await _start_agent_api_run_job(
        session=session,
        agent=agent,
        user=token_user,
        skill_name=skill_name,
        arguments=arguments,
        request=request,
    )
    api_token.last_used_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(job)
    return _agent_api_run_out(
        job,
        agent,
        request=request,
        integration_token=query_token,
    )


@router.get("/{name}/api/runs/{run_id}")
async def get_agent_api_run(
    name: str,
    run_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    integration_token: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    query_token = _query_token_value(integration_token)
    api_token, agent, token_user = await _agent_api_token_agent(
        name=name,
        authorization=authorization,
        integration_token=query_token,
        session=session,
    )
    job = await _get_agent_api_run(
        session=session,
        agent=agent,
        user=token_user,
        run_id=run_id,
    )
    api_token.last_used_at = datetime.now(timezone.utc)
    await session.commit()
    return _agent_api_run_out(
        job,
        agent,
        request=request,
        integration_token=query_token,
    )


@router.get("/{name}/api/files/{path:path}")
async def download_agent_api_file(
    name: str,
    path: str,
    authorization: str | None = Header(default=None),
    integration_token: str | None = Query(default=None),
    session_cookie: str | None = Cookie(default=None, alias=settings.session_cookie_name),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    query_token = _query_token_value(integration_token)
    if query_token:
        api_token, _agent, token_user = await _agent_api_token_agent(
            name=name,
            authorization=authorization,
            integration_token=query_token,
            session=session,
        )
    else:
        _agent, token_user, api_token = await _agent_api_file_user(
            name=name,
            authorization=authorization,
            session_cookie=session_cookie,
            session=session,
        )
    key = _sanitize_agent_api_file_path(path)
    bucket = bucket_for_user(token_user.id)
    try:
        meta = stat_file(bucket, key)
        chunks, content_type = iter_file(bucket, key)
    except FileNotFoundError as exc:
        raise HTTPException(404, f"not found: {key}") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"file download failed: {exc}") from exc
    if api_token is not None:
        api_token.last_used_at = datetime.now(timezone.utc)
        await session.commit()
    filename = key.rsplit("/", 1)[-1] or "download"
    return StreamingResponse(
        chunks,
        media_type=content_type or str(meta.get("content_type") or "application/octet-stream"),
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}",
            "Content-Length": str(int(meta.get("size") or 0)),
            "Cache-Control": "private, no-store",
        },
    )


@router.post("/{name}/mcp")
async def external_agent_mcp(
    name: str,
    request: Request,
    authorization: str | None = Header(default=None),
    integration_token: str | None = Query(default=None),
    user: User | None = Depends(optional_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    api_token: AgentApiToken | None = None
    query_token = _query_token_value(integration_token)
    if query_token or _is_agent_api_bearer(authorization):
        # Header form is the primary path; the query param stays supported for
        # MCP clients that can only be handed a URL.
        api_token, agent, user = await _agent_api_token_agent(
            name=name,
            authorization=authorization,
            integration_token=query_token,
            required_scope="mcp",
            session=session,
        )
    else:
        if user is None:
            raise HTTPException(
                401,
                "missing user session or integration token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        agent = await _get_visible_agent(name, user, session)
    if not _is_external_agent(agent):
        raise HTTPException(404, "MCP adapter is only available for imported agents")

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _jsonrpc_error(None, -32700, "Parse error")
    if not isinstance(body, dict):
        return _jsonrpc_error(None, -32600, "Invalid Request")
    request_id = body.get("id")
    method = body.get("method")
    if body.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return _jsonrpc_error(request_id, -32600, "Invalid Request")
    params = body.get("params") or {}
    if not isinstance(params, dict):
        return _jsonrpc_error(request_id, -32602, "Invalid params")

    if method == "tools/list":
        if api_token is not None:
            api_token.last_used_at = datetime.now(timezone.utc)
            await session.commit()
        return _jsonrpc_result(
            request_id,
            {"tools": _external_mcp_tools(agent.card if isinstance(agent.card, dict) else {})},
        )
    if method != "tools/call":
        return _jsonrpc_error(request_id, -32601, "Method not found")

    skill_name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(skill_name, str) or not skill_name:
        return _jsonrpc_error(request_id, -32602, "Invalid params: missing tool name")
    if not isinstance(arguments, dict):
        return _jsonrpc_error(request_id, -32602, "Invalid params: arguments must be an object")
    try:
        result = await _call_external_agent_skill(
            agent=agent,
            session=session,
            user=user,
            skill_name=skill_name,
            arguments=arguments,
        )
    except ImportedAgentAuthError as exc:
        return _jsonrpc_result(
            request_id,
            _mcp_tool_result(str(exc), is_error=True),
        )
    except Exception as exc:  # noqa: BLE001
        # The message already names the agent, the skill, and the agent's own
        # status code, and its excerpt of the agent's body is redacted.
        return _jsonrpc_result(
            request_id,
            _mcp_tool_result(agent_invoke_error_text(exc), is_error=True),
        )
    if api_token is not None:
        api_token.last_used_at = datetime.now(timezone.utc)
        await session.commit()
    return _jsonrpc_result(request_id, _mcp_tool_result(result))


@router.get("/{name}", response_model=AgentDetailOut)
async def get_agent(
    name: str,
    request: Request,
    refresh: bool = False,
    user: User = Depends(current_user_or_agent_invoke),
    session: AsyncSession = Depends(get_session),
) -> AgentDetailOut:
    agent = (
        await session.execute(select(Agent).where(Agent.name == name))
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(404, "agent not found")
    await require_agent_access(
        session,
        user=user,
        agent=agent,
        action="discover",
    )
    await _sync_latest_visible_deployment(session, agent)
    # By default serve the stored card so viewing a hosted agent never wakes a
    # scaled-to-zero pod. Owners may pass ``?refresh=true`` to force a live
    # re-fetch — e.g. to wait out a redeploy — which cold-starts the agent.
    wake_hosted = refresh and (
        agent.owner_id == user.id or studio_job_claims(request) is not None
    )
    await _refresh_cards_inplace([agent], session, wake_hosted=wake_hosted)
    return AgentDetailOut.model_validate(agent)


async def _sync_latest_visible_deployment(
    session: AsyncSession,
    agent: Agent,
) -> None:
    latest = await latest_deployment_for_agent(session, agent)
    if latest is None:
        return
    if latest.status not in (ACTIVE_DEPLOY_STATUSES | {"failed"}):
        return

    async def reindex_changed_agent(changed_agent: Agent) -> None:
        await _index_agents_for_search([changed_agent])

    await sync_deployment_verification(
        session,
        agent,
        latest,
        on_agent_changed=reindex_changed_agent,
    )


@router.delete("/{name}", status_code=204)
async def remove_agent(
    name: str,
    cascade: bool = False,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete AGENT.

    ``?cascade=true`` additionally deletes the agents forked *directly* from
    it. Without it the request is refused with 409 while any such fork the
    caller owns still points at this agent.

    The cascade is one level deep, so the 409 names exactly the set that a
    cascade would destroy. Everything else that pointed at a deleted agent is
    detached rather than deleted — forks owned by other users, and forks of a
    fork, however deep. Detaching only clears ``source_agent_id``; the agent,
    its repo and its runtime all survive. Every agent this route detaches is
    named in the server log, since a 204 carries no body to name them in.
    """
    agent = (
        await session.execute(select(Agent).where(Agent.name == name))
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(404, "agent not found")
    if agent.owner_id != user.id:
        raise HTTPException(403, "not allowed")
    dependents = (
        (
            await session.execute(
                select(Agent).where(Agent.source_agent_id == agent.id)
            )
        )
        .scalars()
        .all()
    )
    # Deleting an agent must not silently destroy the forks made from it.
    # Forks the caller owns are only removed when they opt in explicitly;
    # forks owned by anyone else are never removed, just detached (the FK on
    # ``agents.source_agent_id`` is ON DELETE SET NULL).
    owned_dependents = [dep for dep in dependents if dep.owner_id == user.id]
    if owned_dependents and not cascade:
        # A plain string, not a structured body: every client of this route
        # renders ``detail`` straight into a message, so the names have to be
        # in the string itself to reach the person doing the deleting.
        blocking = ", ".join(sorted(dep.name for dep in owned_dependents))
        raise HTTPException(
            409,
            (
                f"{len(owned_dependents)} agent(s) were forked from "
                f"{agent.name!r} and would be deleted with it: {blocking}. "
                "Delete them first, or re-send with ?cascade=true to delete "
                "them along with this agent."
            ),
        )
    # Everything that pointed at a row this request destroys, and that this
    # request will not itself destroy: another user's fork, and any fork of a
    # fork. They are only detached, and a 204 has no body to say so, so the
    # audit line at the end of this route is their sole signal. Collect the
    # names now — ``_delete_agent_database_dependents`` NULLs
    # ``source_agent_id``, after which the rows cannot be found by pointer.
    destroyed_ids = {
        candidate_id
        for candidate_id in (agent.id, *(dep.id for dep in owned_dependents))
        if candidate_id is not None
    }
    detached_names = sorted(
        name
        for dependent_id, name in (
            await session.execute(
                select(Agent.id, Agent.name).where(
                    Agent.source_agent_id.in_(sorted(destroyed_ids))
                )
            )
        ).all()
        if dependent_id not in destroyed_ids
    )
    cleanup_failures: list[str] = []
    deleted_search_rows: list[tuple[int, str]] = []
    cascaded_names: list[str] = []
    for dep in owned_dependents:
        dep_failures = (
            _cleanup_external_agent_resources(dep.name)
            if _is_external_agent(dep)
            else _cleanup_agent_resources(dep.name, repo_owner=dep.gitea_owner)
        )
        if dep_failures:
            cleanup_failures.extend(f"{dep.name}: {msg}" for msg in dep_failures)
            continue
        dep_name = dep.name
        if dep.id is not None:
            deleted_search_rows.append((dep.id, dep.name))
            await _delete_agent_database_dependents(session, dep.id)
        await session.delete(dep)
        cascaded_names.append(dep_name)
    if cleanup_failures:
        await session.rollback()
        raise HTTPException(
            502,
            (
                "dependent agent cleanup failed; control-plane records kept for retry: "
                + "; ".join(cleanup_failures)
            ),
        )
    cleanup_failures = (
        _cleanup_external_agent_resources(agent.name)
        if _is_external_agent(agent)
        else _cleanup_agent_resources(agent.name, repo_owner=agent.gitea_owner)
    )
    if cleanup_failures:
        await session.rollback()
        raise HTTPException(
            502,
            (
                "agent cleanup failed; control-plane record was kept for retry: "
                + "; ".join(cleanup_failures)
            ),
        )
    agent_name = agent.name
    if agent.id is not None:
        deleted_search_rows.append((agent.id, agent.name))
        await _delete_agent_database_dependents(session, agent.id)
    await session.delete(agent)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        log.exception("agent database delete failed for %s", agent_name)
        raise HTTPException(
            409,
            "agent still has dependent database records; deletion was not completed",
        ) from exc
    if cascaded_names or detached_names:
        # Only when a fork was actually involved, so an ordinary delete stays
        # quiet and this line means something when it appears. Logged after the
        # commit: a rolled-back attempt detached nothing.
        log.info(
            "agent %r deleted by user %s (cascade=%s); forks deleted: %s; "
            "forks detached (source_agent_id cleared, agents kept): %s",
            agent_name,
            user.id,
            cascade,
            ", ".join(cascaded_names) or "none",
            ", ".join(detached_names) or "none",
        )
    track_event(
        "agent_deleted",
        profile_id=f"user:{user.id}",
        properties={"agentName": agent_name},
    )
    for agent_id, deleted_name in deleted_search_rows:
        await _delete_agent_from_search(agent_id, deleted_name)


@router.post("/{name}/runtime-upgrade", response_model=AgentMineOut)
async def upgrade_agent_runtime(
    name: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> AgentMineOut:
    agent = (
        await session.execute(select(Agent).where(Agent.name == name))
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(404, "agent not found")
    if agent.owner_id != user.id:
        raise HTTPException(403, "not allowed")

    latest_deployment = await latest_deployment_for_agent(session, agent)
    latest_out = (
        await _deployment_out(latest_deployment, session, agent=agent)
        if latest_deployment is not None
        else None
    )
    active_message = _active_deployment_message(latest_deployment)
    if active_message:
        raise HTTPException(409, active_message)

    status = runtime_upgrade_status(
        name=agent.name,
        image=agent.image,
        card=agent.card if isinstance(agent.card, dict) else {},
    )
    if not status["update_available"]:
        return await _agent_mine_out_for_agent(agent, session, latest_out)
    if not status["can_redeploy"]:
        raise HTTPException(409, status["message"])

    if agent.gitea_owner is None and agent.organization_id is not None:
        agent.gitea_owner = await gitea_owner_for_agent(
            session,
            user,
            agent.organization_id,
        )
    try:
        exists = repo_exists(agent.name, owner=agent.gitea_owner)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"could not reach agent source repo: {exc}") from exc
    if not exists:
        raise HTTPException(
            409,
            "No managed source repo exists for this agent. Redeploy it from the CLI.",
        )

    deployment = await create_deployment(
        session,
        agent,
        trigger="runtime_upgrade",
        status="queued",
        source_repo_url=_public_repo_url(agent.name, owner=agent.gitea_owner),
        image=agent.image,
        agent_url=agent.url or _canonical_url(agent.name),
    )
    await invalidate_agent_card(agent.name)

    try:
        push_url, _ = ensure_repo(
            agent.name,
            agent.description,
            owner=agent.gitea_owner,
            public=agent.public,
        )
        source_sha = repo_head_sha(agent.name, owner=agent.gitea_owner)
        if not source_sha:
            raise RuntimeError("source repo is missing main branch")
        image_tag = runtime_upgrade_image_tag(source_sha, status["latest_version"])
        expected_image = settings.agent_image(agent.name, image_tag)
        deployment.head_sha = source_sha
        deployment.image = expected_image
        await record_deployment_event(
            session,
            deployment,
            stage="source",
            status="running",
            message="Committing an a2a-pack runtime bump to the hidden runtime repo.",
            data={
                "from": status["current_version"],
                "to": status["latest_version"],
                "head_sha": source_sha,
                "image_tag": image_tag,
                "expected_image": expected_image,
            },
        )
        runtime_push_url, runtime_internal_url = _ensure_runtime_repo(
            agent.name,
            agent.description,
        )
        runtime_sha = bump_agent_runtime_repo(
            name=agent.name,
            source_repo_url=push_url,
            source_sha=source_sha,
            runtime_repo_url=runtime_push_url,
            latest_version=status["latest_version"],
            image_tag=image_tag,
        )
    except Exception as exc:  # noqa: BLE001
        await fail_deployment(
            session,
            deployment,
            stage="source",
            error=f"runtime upgrade failed: {exc}",
        )
        raise HTTPException(500, f"runtime upgrade failed: {exc}") from exc

    agent.status = "building"
    deployment.status = "building"
    deployment.head_sha = source_sha
    deployment.image = expected_image
    await record_deployment_event(
        session,
        deployment,
        stage="source",
        status="passed",
        message="Runtime bump committed. Build is queued.",
        data={
            "head_sha": source_sha,
            "runtime_head_sha": runtime_sha,
            "target_version": status["latest_version"],
            "image_tag": image_tag,
            "expected_image": expected_image,
        },
    )
    await record_deployment_event(
        session,
        deployment,
        stage="build",
        status="running",
        message="Gitea Actions is rebuilding the agent image from the latest runtime.",
        data={"image": expected_image, "latest_image": agent.image},
    )
    await record_deployment_event(
        session,
        deployment,
        stage="argo",
        status="running",
        message="ArgoCD will roll the deployment after the image and manifests update.",
        data={
            "repo_url": runtime_internal_url,
            "expected_revision": runtime_sha,
            "expected_image": expected_image,
        },
    )
    await session.commit()
    await session.refresh(agent)
    latest = await _deployment_out(deployment, session, agent=agent)
    return await _agent_mine_out_for_agent(agent, session, latest)


@router.post("/{name}/template-update", response_model=AgentTemplateUpdateOut)
async def request_agent_template_update(
    name: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> AgentTemplateUpdateOut:
    agent = await _get_owned_agent(name, user, session)
    lineage = _template_lineage_for_agent(agent)
    policy = str(lineage.get("update_policy") or "none").strip().lower()
    if policy == "none":
        raise HTTPException(409, "agent template_lineage update_policy is none")

    job = await enqueue_template_update_job(session, agent, lineage=lineage)
    return AgentTemplateUpdateOut(
        agent_name=agent.name,
        job_id=str(job.job_id),
        status=str(job.status),
        update_policy=policy,
        template_lineage=lineage,
        message=(
            "Template update request queued. "
            "The update worker will invoke the declared migration path according to policy."
        ),
    )
