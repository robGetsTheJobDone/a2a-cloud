from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from main_agent.config import load_settings as load_runtime_settings

from ..auth import current_user
from ..config import settings
from ..db import get_session
from ..gitea import GITEA_PUBLIC, GITEA_USER, repo_web_url
from ..models import (
    Agent,
    Organization,
    OrganizationGiteaWorkspace,
    OrganizationLangfuseWorkspace,
    OrganizationMember,
    User,
    UserLangfuseAccount,
)
from ..repo_mounts import parse_repo_mounts
from ..secret_crypto import decrypt_secret


router = APIRouter(prefix="/v1/me/service-access", tags=["service-access"])


class ServiceAccessUserOut(BaseModel):
    email: str


class LangfuseServiceOut(BaseModel):
    status: str
    base_url: str
    login_url: str
    login_email: str
    login_password: str | None = None
    login_password_ref: str | None = None
    account_status: str
    role: str | None = None
    project_name: str | None = None
    project_id: str | None = None
    project_url: str | None = None
    public_key: str | None = None
    secret_key_ref: str | None = None
    last_error: str | None = None
    provisioned_at: datetime | None = None


class LiteLLMServiceOut(BaseModel):
    status: str
    base_url: str
    openai_base_url: str
    team_id: str | None = None
    key_ref: str | None = None
    key_configured: bool = False
    last_error: str | None = None


class GiteaOrganizationOut(BaseModel):
    status: str
    org_name: str | None = None
    org_url: str | None = None
    last_error: str | None = None
    provisioned_at: datetime | None = None


class OrganizationServiceAccessOut(BaseModel):
    id: int
    slug: str
    name: str
    role: str
    gitea: GiteaOrganizationOut
    langfuse: LangfuseServiceOut
    litellm: LiteLLMServiceOut


class GiteaRepositoryOut(BaseModel):
    agent_name: str
    repo: str | None = None
    mount_path: str | None = None
    kind: str = "agent"
    organization_id: int | None = None
    owner: str | None = None
    repo_url: str
    agent_url: str | None = None
    status: str
    public: bool


class GiteaServiceOut(BaseModel):
    base_url: str
    username: str
    auth_mode: str
    repositories: list[GiteaRepositoryOut]


class ServiceAccessOut(BaseModel):
    user: ServiceAccessUserOut
    organizations: list[OrganizationServiceAccessOut]
    gitea: GiteaServiceOut


@router.get("", response_model=ServiceAccessOut)
async def list_service_access(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ServiceAccessOut:
    org_rows = (
        await session.execute(
            select(Organization, OrganizationMember)
            .join(
                OrganizationMember,
                OrganizationMember.organization_id == Organization.id,
            )
            .where(OrganizationMember.user_id == user.id)
            .where(OrganizationMember.active == True)  # noqa: E712
            .order_by(Organization.name.asc(), Organization.id.asc())
        )
    ).all()
    org_ids = [org.id for org, _member in org_rows]

    workspace_by_org = {}
    account_by_org = {}
    gitea_by_org = {}
    if org_ids:
        workspaces = (
            await session.execute(
                select(OrganizationLangfuseWorkspace).where(
                    OrganizationLangfuseWorkspace.organization_id.in_(org_ids)
                )
            )
        ).scalars().all()
        workspace_by_org = {workspace.organization_id: workspace for workspace in workspaces}

        gitea_workspaces = (
            await session.execute(
                select(OrganizationGiteaWorkspace).where(
                    OrganizationGiteaWorkspace.organization_id.in_(org_ids)
                )
            )
        ).scalars().all()
        gitea_by_org = {
            workspace.organization_id: workspace for workspace in gitea_workspaces
        }

        accounts = (
            await session.execute(
                select(UserLangfuseAccount).where(
                    UserLangfuseAccount.organization_id.in_(org_ids),
                    UserLangfuseAccount.user_id == user.id,
                )
            )
        ).scalars().all()
        account_by_org = {account.organization_id: account for account in accounts}

    agents = (
        await session.execute(
            select(Agent)
            .where(Agent.owner_id == user.id)
            .order_by(Agent.name.asc(), Agent.id.asc())
        )
    ).scalars().all()
    litellm_url = load_runtime_settings().litellm_url.rstrip("/")

    return ServiceAccessOut(
        user=ServiceAccessUserOut(email=user.email),
        organizations=[
            _organization_access_out(
                org,
                member,
                user,
                workspace_by_org.get(org.id),
                account_by_org.get(org.id),
                gitea_by_org.get(org.id),
                litellm_url=litellm_url,
            )
            for org, member in org_rows
        ],
        gitea=GiteaServiceOut(
            base_url=GITEA_PUBLIC.rstrip("/"),
            username=GITEA_USER,
            auth_mode="platform_managed",
            repositories=[
                GiteaRepositoryOut(
                    agent_name=agent.name,
                    repo=agent.name,
                    mount_path=f"agents/{agent.name}/",
                    organization_id=agent.organization_id,
                    owner=agent.gitea_owner,
                    repo_url=repo_web_url(agent.name, owner=agent.gitea_owner),
                    agent_url=agent.url,
                    status=agent.status,
                    public=agent.public,
                )
                for agent in agents
                if agent.gitea_owner
            ]
            + ([
                GiteaRepositoryOut(
                    agent_name="",
                    repo=mount.repo,
                    mount_path=mount.mount_path,
                    kind="repo",
                    owner=mount.owner,
                    repo_url=repo_web_url(mount.repo, owner=mount.owner),
                    agent_url=None,
                    status="mounted",
                    public=False,
                )
                for mount in parse_repo_mounts(settings.repo_mounts, default_owner=GITEA_USER)
            ] if user.is_admin else []),
        ),
    )


def _organization_access_out(
    org: Organization,
    member: OrganizationMember,
    user: User,
    workspace: OrganizationLangfuseWorkspace | None,
    account: UserLangfuseAccount | None,
    gitea_workspace: OrganizationGiteaWorkspace | None,
    litellm_url: str,
) -> OrganizationServiceAccessOut:
    return OrganizationServiceAccessOut(
        id=org.id,
        slug=org.slug,
        name=org.name,
        role=member.role,
        gitea=_gitea_out(gitea_workspace),
        langfuse=_langfuse_out(user, workspace, account),
        litellm=LiteLLMServiceOut(
            status=_litellm_status(workspace),
            base_url=litellm_url,
            openai_base_url=f"{litellm_url}/v1",
            team_id=workspace.litellm_team_id if workspace else None,
            key_ref=workspace.litellm_key_ref if workspace else None,
            key_configured=bool(workspace and workspace.litellm_key_ref),
            last_error=workspace.last_error if workspace else None,
        ),
    )


def _gitea_out(workspace: OrganizationGiteaWorkspace | None) -> GiteaOrganizationOut:
    return GiteaOrganizationOut(
        status=workspace.status if workspace else "missing",
        org_name=workspace.gitea_org_name if workspace else None,
        org_url=workspace.org_url if workspace else None,
        last_error=workspace.last_error if workspace else None,
        provisioned_at=workspace.provisioned_at if workspace else None,
    )


def _langfuse_out(
    user: User,
    workspace: OrganizationLangfuseWorkspace | None,
    account: UserLangfuseAccount | None,
) -> LangfuseServiceOut:
    base_url = (workspace.langfuse_base_url if workspace else settings.langfuse_base_url).rstrip("/")
    project_url = None
    if workspace and workspace.langfuse_project_id:
        project_url = f"{base_url}/project/{quote(workspace.langfuse_project_id, safe='')}"
    return LangfuseServiceOut(
        status=workspace.status if workspace else "missing",
        base_url=base_url,
        login_url=base_url,
        login_email=account.email if account else user.email,
        login_password=(
            decrypt_secret(account.login_password_ciphertext)
            if account and account.login_password_ciphertext
            else None
        ),
        login_password_ref=account.login_password_ref if account else None,
        account_status=account.status if account else "missing",
        role=account.role if account else None,
        project_name=workspace.project_name if workspace else None,
        project_id=workspace.langfuse_project_id if workspace else None,
        project_url=project_url,
        public_key=workspace.public_key if workspace else None,
        secret_key_ref=workspace.secret_key_ref if workspace else None,
        last_error=(
            workspace.last_error
            if workspace and workspace.last_error
            else account.last_error if account else None
        ),
        provisioned_at=workspace.provisioned_at if workspace else None,
    )


def _litellm_status(workspace: OrganizationLangfuseWorkspace | None) -> str:
    if workspace is None:
        return "missing"
    if not settings.litellm_team_logging_enabled:
        return "disabled"
    if workspace.litellm_key_ref:
        return "configured"
    return workspace.status
