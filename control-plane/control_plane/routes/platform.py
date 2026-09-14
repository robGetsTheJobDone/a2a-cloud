"""Platform-internal endpoints used by meta-agents.

These routes mint and revoke scoped Gitea credentials so meta-agents
(reviewer, patcher, migrator, composer) can read or modify other agents'
source. Authentication is the standard caller ``cp_jwt``.

Authorization (see :func:`_assert_caller_can_access_repo` below):

* The caller must already own the agent (direct ``Agent.owner_id`` match)
  OR be a member of the organization that owns it. Write scope additionally
  requires an owner-level org role.
* Configured first-party repo mounts are allowlisted by ``A2A_CP_REPO_MOUNTS``.
* Otherwise, the repo lookup uses ``Agent.name`` and either ``Agent.gitea_owner``
  or the platform default Gitea owner — so the caller cannot mint tokens for
  arbitrary repository names.

Auth model and credential handling: see :mod:`control_plane.gitea_meta`.
"""
from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import (
    current_credential_token,
    current_user_or_studio_job,
    studio_job_claims,
    user_for_agent_audience,
)
from ..agent_access import AgentBYOKRequired, account_access_policy, resolve_account_llm_access
from ..db import get_session
from ..gitea import GITEA_USER
from ..gitea_meta import (
    get_token_record,
    mint_scoped_token,
    revoke_token,
)
from ..grants import mint_grant_token
from ..minio_client import bucket_for_user
from ..models import Agent, OrganizationMember, User
from ..repo_mounts import parse_repo_mounts
from ..config import settings

router = APIRouter(prefix="/v1/platform", tags=["platform"])

logger = logging.getLogger(__name__)


def _assert_studio_job_repo(request: Request, repo: str) -> None:
    """Keep scoped Studio source credentials bound to their generated agent."""
    claims = studio_job_claims(request)
    if claims is None:
        return
    target = str(claims.get("target_agent") or "").strip()
    if not target or repo != target:
        raise HTTPException(403, "Studio job token is not scoped for this repository")


class GiteaTokenRequest(BaseModel):
    repo: str = Field(..., description="Repository name the caller intends to read/write.")
    owner: str | None = Field(
        default=None,
        description="Repository owner. Defaults to the platform's Gitea admin user.",
    )
    scope: Literal["read", "write"] = Field(default="read")
    ttl_seconds: int = Field(default=900, ge=60, le=86400)
    purpose: str | None = Field(
        default=None,
        description="Free-text reason (meta-agent name, run id, etc.) for the audit log.",
    )


class GiteaTokenResponse(BaseModel):
    token: str
    token_name: str
    username: str
    scopes: list[str]
    expires_at: int
    repo: str
    owner: str


class LLMGrantRequest(BaseModel):
    audience: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Agent name the grant will be presented to.",
    )
    ttl_seconds: int = Field(default=900, ge=60, le=86400)
    thread_id: str | None = Field(default=None, max_length=128)
    grant_id: str | None = Field(default=None, max_length=128)
    skill_name: str | None = Field(default=None, max_length=128)


class LLMCredsOut(BaseModel):
    base_url: str
    api_key: str
    model: str
    source: str = "user"
    temperature_mode: Literal["default", "omit", "custom"] = "default"
    temperature: float | None = None
    extra_body: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class LLMGrantResponse(BaseModel):
    grant: str
    grant_id: str
    expires_at: int
    llm_creds: LLMCredsOut
    account_access: dict[str, Any] | None = None


async def _assert_caller_can_access_repo(
    session: AsyncSession,
    user: User,
    *,
    owner: str,
    repo: str,
    scope: Literal["read", "write"],
) -> Agent | None:
    """Verify ``user`` is permitted to ``scope`` the agent at ``{owner}/{repo}``.

    Configured first-party repo mounts are accepted by explicit allowlist.
    Otherwise, lookup is by ``Agent.name == repo`` and
    (``Agent.gitea_owner == owner`` OR
    (``Agent.gitea_owner IS NULL`` AND ``owner == GITEA_USER``)) so a caller
    cannot mint tokens for arbitrary repo names that happen to exist in Gitea.

    Authorization:

    * Direct owner of the ``Agent`` → ok for any scope.
    * Member of the agent's organization → ok for read.
    * Owner-role org member → ok for write.
    * Otherwise → 403.
    """
    for mount in parse_repo_mounts(settings.repo_mounts, default_owner=GITEA_USER):
        if mount.repo == repo and mount.owner == owner:
            if not user.is_admin:
                raise HTTPException(403, "admin role required for first-party repo mounts")
            return None

    owner_match = (
        (Agent.gitea_owner == owner)
        if owner != GITEA_USER
        else (Agent.gitea_owner.is_(None) | (Agent.gitea_owner == owner))
    )
    agent = (
        await session.execute(
            select(Agent).where(Agent.name == repo, owner_match)
        )
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(404, f"agent not found for {owner}/{repo}")

    if agent.owner_id == user.id:
        return agent

    if agent.organization_id is not None:
        membership = (
            await session.execute(
                select(OrganizationMember).where(
                    OrganizationMember.organization_id == agent.organization_id,
                    OrganizationMember.user_id == user.id,
                    OrganizationMember.active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if membership is not None:
            if scope == "read":
                return agent
            if membership.role in {"owner", "admin", "maintainer"}:
                return agent

    raise HTTPException(403, "caller is not permitted to access this repository")


@router.post("/gitea-token", response_model=GiteaTokenResponse)
async def mint_gitea_token(
    body: GiteaTokenRequest,
    request: Request,
    user: User = Depends(current_user_or_studio_job),
    session: AsyncSession = Depends(get_session),
) -> GiteaTokenResponse:
    """Mint a short-lived Gitea token scoped to a single ``(owner, repo)``.

    The token is created as a dedicated service user (read or write
    variant). The same user is added as a collaborator on the target repo
    with a permission matching ``scope``. The returned token never has
    Gitea admin power.
    """
    owner = (body.owner or GITEA_USER).strip()
    repo = body.repo.strip()
    if not repo:
        raise HTTPException(400, "repo must be non-empty")
    _assert_studio_job_repo(request, repo)

    await _assert_caller_can_access_repo(
        session, user, owner=owner, repo=repo, scope=body.scope
    )

    try:
        row, secret = await mint_scoped_token(
            session,
            scope=body.scope,
            owner=owner,
            repo=repo,
            ttl_seconds=body.ttl_seconds,
            issued_by_user_id=user.id,
            purpose=body.purpose,
        )
    except RuntimeError as exc:
        logger.error("gitea token mint failed: %s", exc)
        raise HTTPException(502, str(exc)) from exc

    return GiteaTokenResponse(
        token=secret,
        token_name=row.token_name,
        username=row.username,
        scopes=list(row.scopes),
        expires_at=int(row.expires_at.timestamp()),
        repo=row.repo,
        owner=row.owner,
    )


@router.delete("/gitea-token/{token_name}", status_code=204)
async def release_gitea_token(
    token_name: str,
    request: Request,
    user: User = Depends(current_user_or_studio_job),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Revoke a previously minted Gitea token. Idempotent.

    Only the user who originally minted a given token may release it. Any
    other caller (or an unknown / already-revoked name) hits the 204 path
    quietly.
    """
    record = await get_token_record(session, token_name)
    if record is not None:
        _assert_studio_job_repo(request, record.repo)
    if record is not None and record.issued_by_user_id != user.id:
        raise HTTPException(403, "token was not issued for this caller")
    await revoke_token(session, token_name)


class AgentSessionVerifyRequest(BaseModel):
    token: str = Field(..., min_length=1, max_length=8192)
    audience: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Agent name asking whose visitor this is.",
    )


class AgentSessionVerifyResponse(BaseModel):
    id: int
    email: str


@router.post("/agent-session/verify", response_model=AgentSessionVerifyResponse)
async def verify_agent_session(
    body: AgentSessionVerifyRequest,
    session: AsyncSession = Depends(get_session),
) -> AgentSessionVerifyResponse:
    """Resolve the caller behind an agent-origin session cookie.

    Hosted agents cannot use ``/v1/me`` for this: agent-scoped tokens are
    deliberately rejected there. They ask here instead, naming themselves, and
    a token minted for another agent will not resolve.
    """
    user = await user_for_agent_audience(session, body.token, body.audience.strip())
    return AgentSessionVerifyResponse(id=user.id, email=user.email)


async def _llm_grant_caller(
    body: LLMGrantRequest,
    credential: str = Depends(current_credential_token),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Resolve the funding user for the audience named in the body.

    An agent-origin session token only funds the agent it was minted for, so a
    hosted agent cannot spend a visitor's LLM budget on another audience the
    way a forwarded platform cookie once allowed.
    """
    return await user_for_agent_audience(session, credential, body.audience.strip())


@router.post("/llm-grant", response_model=LLMGrantResponse)
async def mint_llm_grant(
    body: LLMGrantRequest,
    user: User = Depends(_llm_grant_caller),
    session: AsyncSession = Depends(get_session),
) -> LLMGrantResponse:
    """Return caller-funded LLM credentials for platform-declared agents.

    MCP tool calls do not pass through ``main_agent.tools.handoff``, but they
    still need the platform layer to attach ``ctx.llm`` for agents that declare
    ``LLMProvisioning.PLATFORM``. The old platform default key has been removed:
    platform-declared agents now use the caller's saved LLM credential routed
    through LiteLLM.
    """
    from .chat import _litellm_model_alias, _main_llm_runtime_creds
    from .llm_creds import get_creds_for_user

    audience = body.audience.strip()

    agent = (
        await session.execute(select(Agent).where(Agent.name == audience))
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(404, "agent not found")

    creds = await get_creds_for_user(user.id, session, name="default")
    policy = account_access_policy(agent.card)
    access_decision = None
    runtime_settings = None
    if policy.enabled and creds is None:
        from main_agent.config import load_settings as load_runtime_settings

        runtime_settings = load_runtime_settings()
        if not tuple(runtime_settings.platform_llm_models):
            raise HTTPException(503, "platform-funded LLM trial is unavailable")
    if policy.enabled:
        try:
            access_decision = await resolve_account_llm_access(
                session,
                agent=agent,
                user_id=user.id,
                skill_name=body.skill_name or "llm-grant",
                has_byok=creds is not None,
            )
        except AgentBYOKRequired as exc:
            raise HTTPException(402, exc.payload) from exc
    elif creds is None:
        raise HTTPException(
            400,
            "LLM key required. Add an LLM credential in Settings > LLM credentials.",
        )

    llm_models: tuple[str, ...]
    if access_decision is not None and access_decision.source == "platform_trial":
        llm_models = tuple(runtime_settings.platform_llm_models)
    else:
        llm_models = (_litellm_model_alias(user.id, "default"),)

    token, payload = mint_grant_token(
        issuer=f"self:user-{user.id}",
        audience=audience,
        bucket=bucket_for_user(user.id),
        mode="read_write_overlay",
        allow_patterns=("**",),
        outputs_prefix="outputs/",
        write_prefixes=("outputs/",),
        llm_models=llm_models,
        llm_max_budget_usd=(
            getattr(runtime_settings, "platform_llm_max_budget_usd", 1.0)
            if runtime_settings is not None
            else 1.0
        ),
        llm_rpm_limit=(
            getattr(runtime_settings, "platform_llm_rpm_limit", 60)
            if runtime_settings is not None
            else 60
        ),
        llm_tpm_limit=(
            getattr(runtime_settings, "platform_llm_tpm_limit", 200000)
            if runtime_settings is not None
            else 200000
        ),
        ttl_seconds=body.ttl_seconds,
    )
    metadata = _llm_grant_metadata(body=body, user=user, payload=payload)
    if access_decision is not None:
        metadata["a2a_account_access_source"] = access_decision.source
        metadata["a2a_platform_skill_calls_remaining"] = access_decision.remaining
    if access_decision is not None and access_decision.source == "platform_trial":
        runtime_creds = {
            "base_url": runtime_settings.litellm_url.rstrip("/") + "/v1",
            "api_key": token,
            "model": llm_models[0],
            "temperature_mode": "omit",
            "extra_body": {},
            "metadata": metadata,
        }
        credential_source = "platform_trial"
    else:
        runtime_creds = await _main_llm_runtime_creds(
            creds,
            user_id=user.id,
            llm_creds_name="default",
            runtime_litellm_key=token,
            litellm_metadata=metadata,
        )
        credential_source = "user"
    if runtime_creds is None:
        raise HTTPException(
            400,
            "LLM key required. Add an LLM credential in Settings > LLM credentials.",
        )
    return LLMGrantResponse(
        grant=token,
        grant_id=str(payload["grant_id"]),
        expires_at=int(payload["expires_at"]),
        llm_creds=LLMCredsOut(
            base_url=str(runtime_creds.get("base_url") or ""),
            api_key=str(runtime_creds.get("api_key") or ""),
            model=str(runtime_creds.get("model") or ""),
            source=credential_source,
            temperature_mode=str(runtime_creds.get("temperature_mode") or "omit"),
            temperature=runtime_creds.get("temperature"),
            extra_body=(
                runtime_creds.get("extra_body")
                if isinstance(runtime_creds.get("extra_body"), dict)
                else {}
            ),
            metadata=(
                runtime_creds.get("metadata")
                if isinstance(runtime_creds.get("metadata"), dict)
                else {}
            ),
        ),
        account_access=(
            access_decision.public_payload()
            if access_decision is not None
            else None
        ),
    )


def _llm_grant_metadata(
    *,
    body: LLMGrantRequest,
    user: User,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "a2a_user_id": user.id,
            "a2a_user_email": user.email,
            "a2a_thread_id": body.thread_id,
            "session_id": body.thread_id,
            "a2a_grant_id": body.grant_id or payload["grant_id"],
            "a2a_agent_name": body.audience.strip(),
            "a2a_skill_name": body.skill_name,
            "a2a_llm_source": "platform_llm_grant",
        }.items()
        if value is not None
    }
