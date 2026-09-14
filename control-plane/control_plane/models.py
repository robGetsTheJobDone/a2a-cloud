from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )

    agents: Mapped[list["Agent"]] = relationship(back_populates="owner", cascade="all, delete-orphan")




class AgentAccountAccessUsage(Base):
    """Lifetime platform-funded trial usage for one account and agent."""

    __tablename__ = "agent_account_access_usage"
    __table_args__ = (
        UniqueConstraint(
            "agent_id",
            "user_id",
            name="uq_agent_account_access_usage_agent_user",
        ),
        Index("ix_agent_account_access_usage_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    platform_skill_calls_used: Mapped[int] = mapped_column(Integer, default=0)
    last_skill_name: Mapped[str] = mapped_column(
        String(128), default="", server_default=""
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )



class FeatureFlag(Base):
    __tablename__ = "feature_flags"

    key: Mapped[str] = mapped_column(String(96), primary_key=True)
    label: Mapped[str] = mapped_column(String(160))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class UserFeatureFlag(Base):
    __tablename__ = "user_feature_flags"
    __table_args__ = (
        UniqueConstraint("user_id", "flag_key", name="uq_user_feature_flags_user_flag"),
        Index("ix_user_feature_flags_flag_enabled", "flag_key", "enabled"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    flag_key: Mapped[str] = mapped_column(
        ForeignKey("feature_flags.key", ondelete="CASCADE"), index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )


class UserOnboardingState(Base):
    __tablename__ = "user_onboarding_states"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_user_onboarding_states_user"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    current_step: Mapped[str] = mapped_column(
        String(32), default="llm_key", server_default="llm_key"
    )
    llm_key_step_completed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    walkthrough_completed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    llm_key_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    walkthrough_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    walkthrough_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_seen_step: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tour_step_index: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    tour_step_total: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    # "Skip for now" on the BYOK step. Distinct from completed_at: onboarding is
    # not finished, it just stops blocking the app. NULL on every pre-existing
    # row, so users mid-onboarding keep the behaviour they had.
    dismissed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )


class KeycloakIdentity(Base):
    """Maps a Keycloak token ``sub`` to a control-plane :class:`User` (E1-P2).

    A new table (not a column on ``users``) so the create_all bootstrap picks
    it up without an ALTER on the existing prod DB. Provision-on-first-login
    writes one row the first time a Keycloak access token reaches the API.
    """

    __tablename__ = "keycloak_identities"

    id: Mapped[int] = mapped_column(primary_key=True)
    keycloak_sub: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )


class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_organizations_slug"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(96), index=True)
    name: Mapped[str] = mapped_column(String(160))
    created_by_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )


class OrganizationMember(Base):
    __tablename__ = "organization_members"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", name="uq_org_members_user"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(32), default="member", index=True)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    active: Mapped[bool] = mapped_column(default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )


class OrganizationScimToken(Base):
    __tablename__ = "organization_scim_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_org_scim_token_hash"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    label: Mapped[str] = mapped_column(String(160))
    token_hash: Mapped[str] = mapped_column(String(64), index=True)
    token_last4: Mapped[str] = mapped_column(String(4))
    enabled: Mapped[bool] = mapped_column(default=True, index=True)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )


class OrganizationAuditLog(Base):
    __tablename__ = "organization_audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    actor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    action: Mapped[str] = mapped_column(String(96), index=True)
    target_type: Mapped[str] = mapped_column(String(64), index=True)
    target_id: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    target_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), index=True
    )


class OrganizationLangfuseWorkspace(Base):
    """Local provisioning intent for an org-scoped Langfuse workspace."""

    __tablename__ = "organization_langfuse_workspaces"
    __table_args__ = (
        UniqueConstraint("organization_id", name="uq_org_langfuse_workspace_org"),
        Index("ix_org_langfuse_workspace_status", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    deployment_mode: Mapped[str] = mapped_column(String(32), default="shared_instance")
    langfuse_base_url: Mapped[str] = mapped_column(String(512))
    langfuse_org_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    langfuse_project_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    project_name: Mapped[str] = mapped_column(String(160))
    public_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    secret_key_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    secret_key_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    litellm_team_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    litellm_key_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    litellm_key_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    provisioned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )


class UserLangfuseAccount(Base):
    """Local provisioning intent for a user's Langfuse access within an org."""

    __tablename__ = "user_langfuse_accounts"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "user_id",
            name="uq_user_langfuse_account_org_user",
        ),
        Index("ix_user_langfuse_account_status", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(String(255), index=True)
    role: Mapped[str] = mapped_column(String(32), default="owner")
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    langfuse_user_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    login_password_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    login_password_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    provisioned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )


class OrganizationGiteaWorkspace(Base):
    """Local provisioning intent for an org-scoped Gitea organization."""

    __tablename__ = "organization_gitea_workspaces"
    __table_args__ = (
        UniqueConstraint("organization_id", name="uq_org_gitea_workspace_org"),
        UniqueConstraint("gitea_org_name", name="uq_org_gitea_workspace_name"),
        Index("ix_org_gitea_workspace_status", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    gitea_base_url: Mapped[str] = mapped_column(String(512))
    gitea_org_name: Mapped[str] = mapped_column(String(96), index=True)
    gitea_org_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    org_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    provisioned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )


class OrganizationDomain(Base):
    __tablename__ = "organization_domains"
    __table_args__ = (
        UniqueConstraint("domain", name="uq_organization_domains_domain"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    domain: Mapped[str] = mapped_column(String(255), index=True)
    verification_token: Mapped[str] = mapped_column(String(96))
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )


class Agent(Base):
    __tablename__ = "agents"
    __table_args__ = (UniqueConstraint("name", name="uq_agents_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    organization_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    gitea_owner: Mapped[str | None] = mapped_column(String(96), nullable=True, index=True)
    source_agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(128), index=True)
    description: Mapped[str] = mapped_column(String(1024), default="")
    version: Mapped[str] = mapped_column(String(64))
    image: Mapped[str] = mapped_column(String(512))
    public: Mapped[bool] = mapped_column(default=True)
    # Source reuse is deliberately separate from public card visibility.
    # private | organization | public
    fork_policy: Mapped[str] = mapped_column(
        String(32), default="organization", server_default="organization", index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="pending")
    url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    card: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, server_default=func.now()
    )

    owner: Mapped[User] = relationship(back_populates="agents")


class AgentSeoProfile(Base):
    """Saved, public marketing copy derived from allowlisted Agent Card facts."""

    __tablename__ = "agent_seo_profiles"
    __table_args__ = (
        UniqueConstraint("agent_id", name="uq_agent_seo_profiles_agent_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), unique=True, index=True
    )
    card_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    content: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    model: Mapped[str | None] = mapped_column(String(160), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )


class AgentApiToken(Base):
    __tablename__ = "agent_api_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_agent_api_tokens_token_hash"),
        Index("ix_agent_api_tokens_agent_user", "agent_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    agent_name: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(160))
    token_hash: Mapped[str] = mapped_column(String(64), index=True)
    token_last4: Mapped[str] = mapped_column(String(4))
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(default=True, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, server_default=func.now()
    )


class AgentCustomDomain(Base):
    __tablename__ = "agent_custom_domains"
    __table_args__ = (
        UniqueConstraint("hostname", name="uq_agent_custom_domains_hostname"),
        Index("ix_agent_custom_domains_agent_status", "agent_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    agent_name: Mapped[str] = mapped_column(String(128), index=True)
    hostname: Mapped[str] = mapped_column(String(255), index=True)
    verification_token: Mapped[str] = mapped_column(String(96))
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    canonical_hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    redirect_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), index=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )


class AgentCodeEditorOptIn(Base):
    __tablename__ = "agent_code_editor_opt_ins"
    __table_args__ = (
        UniqueConstraint("agent_id", name="uq_agent_code_editor_opt_ins_agent_id"),
        Index("ix_agent_code_editor_opt_ins_user_status", "user_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="enabled", index=True)
    shared_agent_name: Mapped[str] = mapped_column(String(128), default="code-editor-agent")
    workspace_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )


class AgentDeployment(Base):
    """One deploy attempt for an agent, independent of the steady-state agent row."""

    __tablename__ = "agent_deployments"
    __table_args__ = (
        UniqueConstraint("deploy_id", name="uq_agent_deployments_deploy_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    deploy_id: Mapped[str] = mapped_column(String(64), index=True)
    agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=True, index=True
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    agent_name: Mapped[str] = mapped_column(String(128), index=True)
    trigger: Mapped[str] = mapped_column(String(64), default="deploy", index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    source_repo_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    image: Mapped[str | None] = mapped_column(String(512), nullable=True)
    agent_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    verification: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AgentDeploymentEvent(Base):
    """Human-readable timeline event for an :class:`AgentDeployment`."""

    __tablename__ = "agent_deployment_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    deployment_id: Mapped[int] = mapped_column(
        ForeignKey("agent_deployments.id", ondelete="CASCADE"), index=True
    )
    deploy_id: Mapped[str] = mapped_column(String(64), index=True)
    agent_name: Mapped[str] = mapped_column(String(128), index=True)
    stage: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )


class AgentDeploymentLog(Base):
    """Raw build/runtime log tail attached to a deployment stage.

    Proxies Gitea Actions, image build, Argo sync, and pod readiness output so
    users can debug failed deploys without cluster or Grafana access. One row
    per (deployment, stage, source); content is bounded and secret-scrubbed and
    replaced in place as the deploy progresses.
    """

    __tablename__ = "agent_deployment_logs"
    __table_args__ = (
        UniqueConstraint(
            "deployment_id", "stage", "source",
            name="uq_agent_deployment_logs_stage_source",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    deployment_id: Mapped[int] = mapped_column(
        ForeignKey("agent_deployments.id", ondelete="CASCADE"), index=True
    )
    deploy_id: Mapped[str] = mapped_column(String(64), index=True)
    agent_name: Mapped[str] = mapped_column(String(128), index=True)
    stage: Mapped[str] = mapped_column(String(64), index=True)
    # gitea_actions | pod | argo
    source: Mapped[str] = mapped_column(String(32), index=True)
    content: Mapped[str] = mapped_column(Text, default="")
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    byte_len: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )


class AgentStudioResolution(Base):
    """A short-lived, immutable snapshot of Studio reuse candidates."""

    __tablename__ = "agent_studio_resolutions"
    __table_args__ = (
        UniqueConstraint("plan_id", name="uq_agent_studio_resolutions_plan_id"),
        Index("ix_agent_studio_resolutions_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    organization_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    brief: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    candidates: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(
        String(32), default="resolved", server_default="resolved", index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )


class AgentStudioRun(Base):
    """One Agent Studio build run: describe → build → review → improve → deploy.

    Drives the deployed agent-studio coordinator and mirrors its progress so the
    dashboard can stream a live run view. One row per run; events live in
    :class:`AgentStudioRunEvent`.
    """

    __tablename__ = "agent_studio_runs"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_agent_studio_runs_run_id"),
        UniqueConstraint(
            "user_id",
            "idempotency_key",
            name="uq_agent_studio_runs_user_idempotency",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    organization_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(
        String(32), default="build_new", server_default="build_new", index=True
    )
    source_agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_card_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plan_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    authorization_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    agent_name: Mapped[str] = mapped_column(String(128), index=True)
    # queued | building | evaluating | reviewing | improving | deploying | live | failed
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    stop_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    brief: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    budget_cents: Mapped[int] = mapped_column(Integer, default=0)
    budget_spent_cents: Mapped[int] = mapped_column(Integer, default=0)
    iteration: Mapped[int] = mapped_column(Integer, default=0)
    max_iterations: Mapped[int] = mapped_column(Integer, default=3)
    deploy_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )


class AgentStudioRunEvent(Base):
    """One streamed progress event for an :class:`AgentStudioRun`."""

    __tablename__ = "agent_studio_run_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    studio_run_id: Mapped[int] = mapped_column(
        ForeignKey("agent_studio_runs.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    # build | evaluate | review | improve | deploy
    phase: Mapped[str] = mapped_column(String(32), index=True)
    # builder | reviewer | editor | deployer | coordinator
    actor: Mapped[str] = mapped_column(String(32), default="coordinator")
    status: Mapped[str] = mapped_column(String(32), default="running")
    message: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )


class AgentLineage(Base):
    """Auditable parent/version relationship for every Studio reuse action."""

    __tablename__ = "agent_lineage"
    __table_args__ = (
        Index("ix_agent_lineage_parent_created", "parent_agent_id", "created_at"),
        Index("ix_agent_lineage_child_created", "child_agent_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    parent_agent_id: Mapped[int] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    child_agent_id: Mapped[int] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    studio_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_studio_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String(32), index=True)
    parent_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parent_source_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parent_card_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    authorization_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )


class AgentStudioAutopilotPolicy(Base):
    """Per-user opt-in for the daily read-only Agent Studio review."""

    __tablename__ = "agent_studio_autopilot_policies"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_agent_studio_autopilot_user"),
        Index("ix_agent_studio_autopilot_due", "enabled", "next_run_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), index=True
    )
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    daily_hour: Mapped[int] = mapped_column(Integer, default=9)
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_run_status: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_email_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )


class AgentStudioUpgradeProposal(Base):
    """One review-backed idea awaiting an owner's accept/reject decision."""

    __tablename__ = "agent_studio_upgrade_proposals"
    __table_args__ = (
        UniqueConstraint("proposal_id", name="uq_agent_studio_upgrade_proposal_id"),
        Index(
            "ix_agent_studio_upgrade_user_status_created",
            "user_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_agent_studio_upgrade_agent_source",
            "agent_id",
            "source_head_sha",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    agent_id: Mapped[int] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    agent_name: Mapped[str] = mapped_column(String(128), index=True)
    source_head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), default="pending", server_default="pending", index=True
    )
    severity: Mapped[str] = mapped_column(String(32), default="info", index=True)
    category: Mapped[str] = mapped_column(String(64), default="ergonomics", index=True)
    title: Mapped[str] = mapped_column(String(240))
    idea: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    upgrade_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    upgrade_report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    emailed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )


class AgentSecret(Base):
    """Metadata for an environment secret stored in Kubernetes, not in SQL."""

    __tablename__ = "agent_secrets"
    __table_args__ = (
        UniqueConstraint("agent_id", "key", name="uq_agent_secrets_agent_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    agent_name: Mapped[str] = mapped_column(String(128), index=True)
    key: Mapped[str] = mapped_column(String(128), index=True)
    value_redacted: Mapped[str] = mapped_column(String(64), default="***")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )


class AgentMemoryEntry(Base):
    """Transactional per-user memory for an agent."""

    __tablename__ = "agent_memory_entries"
    __table_args__ = (
        UniqueConstraint(
            "agent_id",
            "user_id",
            "namespace",
            "key",
            name="uq_agent_memory_agent_user_namespace_key",
        ),
        Index(
            "ix_agent_memory_agent_user_namespace_updated",
            "agent_id",
            "user_id",
            "namespace",
            "updated_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    agent_name: Mapped[str] = mapped_column(String(128), index=True)
    namespace: Mapped[str] = mapped_column(String(96), default="notes", index=True)
    key: Mapped[str] = mapped_column(String(512), index=True)
    value: Mapped[Any] = mapped_column(JSON)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )


class AgentAuthConnection(Base):
    """Managed credentials for an imported A2A agent.

    Secret material is encrypted in ``secret_ciphertext``. Public card
    metadata stays in ``metadata_json`` so dashboards and CLIs can show
    scheme status without exposing credentials.
    """

    __tablename__ = "agent_auth_connections"
    __table_args__ = (
        UniqueConstraint(
            "agent_id",
            "user_id",
            "scheme_name",
            name="uq_agent_auth_agent_user_scheme",
        ),
        Index("ix_agent_auth_connections_status_updated_at", "status", "updated_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    agent_name: Mapped[str] = mapped_column(String(128), index=True)
    scheme_name: Mapped[str] = mapped_column(String(128), index=True)
    scheme_type: Mapped[str] = mapped_column(String(32), index=True)
    credential_scope: Mapped[str] = mapped_column(String(32), default="agent")
    status: Mapped[str] = mapped_column(String(32), default="needs_setup", index=True)
    secret_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )


class AgentConsumerSetupValue(Base):
    """Per-caller values for an agent's A2APack consumer_setup contract."""

    __tablename__ = "agent_consumer_setup_values"
    __table_args__ = (
        Index("ix_agent_consumer_setup_agent_scope", "agent_id", "scope"),
        Index("ix_agent_consumer_setup_user", "user_id", "agent_id"),
        Index("ix_agent_consumer_setup_org", "organization_id", "agent_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    agent_name: Mapped[str] = mapped_column(String(128), index=True)
    field_name: Mapped[str] = mapped_column(String(128), index=True)
    field_kind: Mapped[str] = mapped_column(String(16), default="config", index=True)
    scope: Mapped[str] = mapped_column(String(16), index=True)  # user|org
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    organization_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True
    )
    value_json: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    secret_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_redacted: Mapped[str] = mapped_column(String(128), default="configured")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )


class AgentInstall(Base):
    """Explicit user install for marketplace agents without setup material."""

    __tablename__ = "agent_installs"
    __table_args__ = (
        UniqueConstraint("agent_id", "user_id", name="uq_agent_installs_agent_user"),
        Index("ix_agent_installs_user", "user_id", "agent_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    agent_name: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )


class DatabaseCluster(Base):
    """Physical database service cell that can host user/org projects."""

    __tablename__ = "database_clusters"
    __table_args__ = (
        UniqueConstraint("name", name="uq_database_clusters_name"),
        Index("ix_database_clusters_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(96), index=True)
    status: Mapped[str] = mapped_column(String(32), default="ready", index=True)
    region: Mapped[str | None] = mapped_column(String(96), nullable=True)
    storage_class: Mapped[str | None] = mapped_column(String(128), nullable=True)
    api_endpoint: Mapped[str | None] = mapped_column(String(512), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )


class DatabaseProject(Base):
    """User/org-owned logical database project."""

    __tablename__ = "database_projects"
    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_database_projects_user_name"),
        UniqueConstraint(
            "organization_id", "name", name="uq_database_projects_org_name"
        ),
        Index("ix_database_projects_owner_status", "owner_id", "status"),
        Index("ix_database_projects_org_status", "organization_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    organization_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True
    )
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    cluster_id: Mapped[int | None] = mapped_column(
        ForeignKey("database_clusters.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(96), index=True)
    display_name: Mapped[str] = mapped_column(String(160))
    engine: Mapped[str] = mapped_column(String(32), default="postgres", index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    project_ref: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    default_branch_name: Mapped[str] = mapped_column(String(96), default="main")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )


class DatabaseBranch(Base):
    """Branch/timeline within a database project."""

    __tablename__ = "database_branches"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_database_branches_project_name"),
        Index("ix_database_branches_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("database_projects.id", ondelete="CASCADE"), index=True
    )
    parent_branch_id: Mapped[int | None] = mapped_column(
        ForeignKey("database_branches.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(96), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    branch_ref: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    protected: Mapped[bool] = mapped_column(Boolean, default=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )


class DatabaseRole(Base):
    """Scoped Postgres role managed by the platform."""

    __tablename__ = "database_roles"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_database_roles_project_name"),
        Index("ix_database_roles_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("database_projects.id", ondelete="CASCADE"), index=True
    )
    branch_id: Mapped[int | None] = mapped_column(
        ForeignKey("database_branches.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(96), index=True)
    username: Mapped[str] = mapped_column(String(128), index=True)
    database_name: Mapped[str] = mapped_column(String(128))
    access_mode: Mapped[str] = mapped_column(String(32), default="read_write", index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    secret_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    connection_uri_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )


class AgentDatabaseBinding(Base):
    """Agent runtime binding to a platform-managed database role."""

    __tablename__ = "agent_database_bindings"
    __table_args__ = (
        UniqueConstraint("agent_id", "binding_name", name="uq_agent_db_binding_name"),
        Index("ix_agent_db_bindings_project", "database_project_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    organization_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True
    )
    database_project_id: Mapped[int] = mapped_column(
        ForeignKey("database_projects.id", ondelete="CASCADE"), index=True
    )
    database_branch_id: Mapped[int | None] = mapped_column(
        ForeignKey("database_branches.id", ondelete="SET NULL"), nullable=True, index=True
    )
    database_role_id: Mapped[int | None] = mapped_column(
        ForeignKey("database_roles.id", ondelete="SET NULL"), nullable=True, index=True
    )
    agent_name: Mapped[str] = mapped_column(String(128), index=True)
    binding_name: Mapped[str] = mapped_column(String(96), index=True)
    env_var: Mapped[str] = mapped_column(String(128), default="DATABASE_URL")
    access_mode: Mapped[str] = mapped_column(String(32), default="read_write", index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    declaration_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )


class DatabaseProvisionEvent(Base):
    """Audit/reconcile event for database lifecycle operations."""

    __tablename__ = "database_provision_events"
    __table_args__ = (
        Index("ix_database_events_project_created", "database_project_id", "created_at"),
        Index("ix_database_events_agent_created", "agent_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    database_project_id: Mapped[int | None] = mapped_column(
        ForeignKey("database_projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=True, index=True
    )
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(96), index=True)
    status: Mapped[str] = mapped_column(String(32), default="info", index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), index=True
    )


class AgentMailbox(Base):
    """Platform-managed Mailu mailbox for an agent (pro plans only).

    One mailbox per agent at ``<agent-name>@<agent mail domain>``. The
    mailbox provisioner reconciles rows against the Mailu API and projects
    IMAP/SMTP credentials into the agent runtime secret. The mail ingress
    worker polls ready mailboxes and bridges messages into chat threads.
    """

    __tablename__ = "agent_mailboxes"
    __table_args__ = (
        UniqueConstraint("agent_id", name="uq_agent_mailbox_agent"),
        UniqueConstraint("address", name="uq_agent_mailbox_address"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    agent_name: Mapped[str] = mapped_column(String(128), index=True)
    address: Mapped[str] = mapped_column(String(320), index=True)
    # pending -> provisioning -> ready; disabled (plan lapsed, mail kept),
    # removed (delete requested), failed (retried by the provisioner).
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    quota_bytes: Mapped[int] = mapped_column(BigInteger, default=100 * 1024 * 1024)
    # Senders allowed to open threads with the agent. Owner email is always
    # implicitly allowed; empty list means owner-only.
    allowed_senders_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    password_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    # IMAP UID watermark for the ingress poller (per-mailbox monotonic).
    last_imap_uid: Mapped[int] = mapped_column(Integer, default=0)
    outbound_count: Mapped[int] = mapped_column(Integer, default=0)
    outbound_period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    disabled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    declaration_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )


class MailboxProvisionEvent(Base):
    """Audit/reconcile event for agent mailbox lifecycle + mail bridging."""

    __tablename__ = "mailbox_provision_events"
    __table_args__ = (
        Index("ix_mailbox_events_mailbox_created", "mailbox_id", "created_at"),
        Index("ix_mailbox_events_agent_created", "agent_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    mailbox_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_mailboxes.id", ondelete="CASCADE"), nullable=True, index=True
    )
    agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=True, index=True
    )
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(96), index=True)
    status: Mapped[str] = mapped_column(String(32), default="info", index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), index=True
    )


class UserLLMCreds(Base):
    """Caller-provided LLM endpoint + key.

    Stored per-user; forwarded by the orchestrator's ``call_agent``
    when a callee's Card declares ``llm_provisioning=caller_provided`` or
    ``platform_or_caller_provided``.

    API key is stored verbatim for now (single-replica, trusted env).
    Encrypt at rest before going multi-tenant: write a Fernet key via a
    k8s Secret and wrap/unwrap on read/write here.
    """

    __tablename__ = "user_llm_creds"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_user_llm_creds_user_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(64), default="default")
    base_url: Mapped[str] = mapped_column(String(512))
    api_key: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(128))
    temperature_mode: Mapped[str] = mapped_column(
        String(16), default="omit", server_default="omit"
    )
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    extra_body: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )


class UserControlPolicy(Base):
    """Per-user control-room budget and runtime policy settings.

    Org-wide policy can layer on top once agents/runs are org-owned. Today
    the durable run tables are user-scoped, so this is the enforceable
    policy boundary.
    """

    __tablename__ = "user_control_policies"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_user_control_policies_user"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    monthly_budget_cents: Mapped[int] = mapped_column(Integer, default=5000)
    run_budget_cents: Mapped[int] = mapped_column(Integer, default=500)
    max_agent_calls_per_run: Mapped[int] = mapped_column(Integer, default=8)
    require_approval_for_file_writes: Mapped[bool] = mapped_column(Boolean, default=False)
    deny_external_network: Mapped[bool] = mapped_column(Boolean, default=False)
    only_approved_agents: Mapped[bool] = mapped_column(Boolean, default=False)
    pii_safe_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_agents: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )


class AgentSchedule(Base):
    """User-owned cron schedule for main-agent or direct agent work."""

    __tablename__ = "agent_schedules"
    __table_args__ = (
        UniqueConstraint("schedule_id", name="uq_agent_schedules_schedule_id"),
        Index("ix_agent_schedules_due", "enabled", "next_run_at"),
        Index("ix_agent_schedules_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    schedule_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(160))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    cron: Mapped[str] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    target_type: Mapped[str] = mapped_column(String(32), index=True)
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_name: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    skill_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    args_json: Mapped[str] = mapped_column(Text, default="{}")
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_run_status: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    last_run_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_count: Mapped[int] = mapped_column(Integer, default=0)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )


class LLMUsageEvent(Base):
    """Attribution row for platform and direct user-provider LLM calls.

    LiteLLM keeps raw proxy logs for platform-routed calls. This row is the
    app-local join table that connects cost/tokens to users, threads, runs,
    agents, and skills, including direct OpenAI-compatible provider calls.
    """

    __tablename__ = "llm_usage_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    thread_id: Mapped[str | None] = mapped_column(
        ForeignKey("chat_threads.id", ondelete="SET NULL"), nullable=True, index=True
    )
    dag_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    grant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    agent_name: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    skill_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source: Mapped[str] = mapped_column(String(64), default="control_plane_chat", index=True)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str] = mapped_column(String(128), default="", index=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), index=True
    )


class Bounty(Base):
    """A user-posted request for an agent to solve a problem.

    A bounty captures demand discovery: anyone with a deployed public agent
    can claim it, and the poster marks it fulfilled once the agent works.
    """

    __tablename__ = "bounties"
    __table_args__ = (UniqueConstraint("slug", name="uq_bounties_slug"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(96), index=True)
    title: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text)
    example_input: Mapped[str] = mapped_column(Text, default="")
    example_output: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)

    posted_by_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # open -> claimed -> fulfilled. cancelled is terminal from any state.
    status: Mapped[str] = mapped_column(String(32), default="open", index=True)

    claimed_agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    claimed_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    fulfilled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )


class PlatformSetting(Base):
    """Global key/value settings driven by the admin panel.

    One row per logical knob. ``value`` is JSON-typed so the same table
    serves bools, strings, ints, and structured config without growing
    columns. Reads are not hot-pathed — these settings gate slow async
    work (review enqueue, sweeper intervals, etc.), so a per-call DB
    lookup is fine for v1.
    """

    __tablename__ = "platform_settings"
    __table_args__ = (UniqueConstraint("key", name="uq_platform_settings_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(96), index=True)
    value: Mapped[Any] = mapped_column(JSON)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(96), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )


class ProtocolRegistryPack(Base):
    """Durable protocol-pack registry row.

    Registry signatures prove provenance of the stored metadata. They are not
    runtime grants and cannot enable active graph mutation by themselves.
    """

    __tablename__ = "protocol_registry_packs"
    __table_args__ = (
        UniqueConstraint(
            "protocol_id",
            "protocol_version",
            name="uq_protocol_registry_pack_version",
        ),
        Index("ix_protocol_registry_pack_enabled", "enabled", "protocol_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    protocol_id: Mapped[str] = mapped_column(String(64), index=True)
    protocol_version: Mapped[int] = mapped_column(Integer, default=1, index=True)
    display_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    risk_class: Mapped[str] = mapped_column(String(64), default="simulation_only", index=True)
    enabled: Mapped[bool] = mapped_column(default=True, index=True)
    simulation_only: Mapped[bool] = mapped_column(default=True)
    proposal_only: Mapped[bool] = mapped_column(default=True)
    active_apply_enabled: Mapped[bool] = mapped_column(default=False)
    enabled_for: Mapped[list[str]] = mapped_column(JSON, default=list)
    policy_refs: Mapped[list[str]] = mapped_column(JSON, default=list)
    simulation_refs: Mapped[list[str]] = mapped_column(JSON, default=list)
    protocol_class: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    canonical_digest: Mapped[str] = mapped_column(String(64), index=True)
    signature: Mapped[str] = mapped_column(String(128))
    signature_algorithm: Mapped[str] = mapped_column(String(64), default="hmac-sha256")
    signed_by: Mapped[str] = mapped_column(String(96), default="control-plane")
    deprecated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    retired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )


class AgentReviewRun(Base):
    """One pre-deploy review of an agent's source by the reviewer meta-agent.

    Lifecycle is fully advisory in v1: deploys never wait for and never get
    blocked by a review. Findings are stored here and surfaced through the
    deployment timeline so callers can read them after the fact.

    Status values:

    * ``queued`` — task created, reviewer not yet called.
    * ``running`` — reviewer call in flight.
    * ``passed`` — review completed with zero critical findings.
    * ``warning`` — review completed, no critical, but warnings present.
    * ``failed`` — review completed with at least one critical finding.
    * ``errored`` — reviewer call itself failed (timeout, 5xx, etc.). No
      verdict was reached.
    * ``skipped`` — reviewer was disabled or unreachable; advisory bypass.
    """

    __tablename__ = "agent_review_runs"
    __table_args__ = (
        UniqueConstraint("review_id", name="uq_agent_review_runs_review_id"),
        Index("ix_agent_review_runs_agent_status", "agent_name", "status"),
        Index("ix_agent_review_runs_user_created_at", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    review_id: Mapped[str] = mapped_column(String(64), index=True)
    deploy_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=True, index=True
    )
    agent_name: Mapped[str] = mapped_column(String(128), index=True)
    ref: Mapped[str] = mapped_column(String(128), default="main")
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    findings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    critical_count: Mapped[int] = mapped_column(Integer, default=0)
    warning_count: Mapped[int] = mapped_column(Integer, default=0)
    info_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class GiteaTokenAudit(Base):
    """Durable record for every Gitea token minted via the platform endpoint.

    Tracks active issuance so the background sweeper can revoke expired
    tokens after a control-plane restart, the release endpoint can verify
    the caller is the original issuer, and operators can audit who got
    access to which repo.

    A token is "active" iff ``revoked_at IS NULL`` and ``expires_at > NOW()``.
    ``token_secret_hash`` stores a short fingerprint (sha256 of the secret,
    first 32 chars) so admins can correlate a leaked token to its row
    without storing the secret itself.
    """

    __tablename__ = "gitea_token_audit"
    __table_args__ = (
        UniqueConstraint("token_name", name="uq_gitea_token_audit_name"),
        Index(
            "ix_gitea_token_audit_active_expires",
            "revoked_at",
            "expires_at",
        ),
        Index(
            "ix_gitea_token_audit_user_repo",
            "issued_by_user_id",
            "owner",
            "repo",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    token_name: Mapped[str] = mapped_column(String(128), index=True)
    username: Mapped[str] = mapped_column(String(96), index=True)
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    owner: Mapped[str] = mapped_column(String(96), index=True)
    repo: Mapped[str] = mapped_column(String(128), index=True)
    permission: Mapped[str] = mapped_column(String(16), index=True)  # read|write
    issued_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    purpose: Mapped[str | None] = mapped_column(String(255), nullable=True)
    token_secret_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class GrantAudit(Base):
    """One row per grant the control plane mints, plus its decision.

    The chain of custody for runtime scope negotiation lives here:
    ``parent_grant_id`` links a superseding grant to the one it replaces,
    so an auditor can reconstruct the full negotiation for a session.

    The token wire format itself is unchanged (the SDK's :class:`Grant`
    is ``extra="forbid"``). All chain metadata stays server-side.
    """

    __tablename__ = "grant_audit"
    __table_args__ = (UniqueConstraint("grant_id", name="uq_grant_audit_grant_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    grant_id: Mapped[str] = mapped_column(String(64), index=True)
    parent_grant_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    issuer: Mapped[str] = mapped_column(String(255))
    audience: Mapped[str] = mapped_column(String(255))
    bucket: Mapped[str] = mapped_column(String(255))
    mode: Mapped[str] = mapped_column(String(32))
    allow_patterns: Mapped[list[str]] = mapped_column(JSON, default=list)
    deny_patterns: Mapped[list[str]] = mapped_column(JSON, default=list)
    outputs_prefix: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ttl_seconds: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decision: Mapped[str] = mapped_column(String(32))  # auto_approve|user_approve|user_deny|hard_deny
    decided_by: Mapped[str] = mapped_column(String(64))  # "auto"|"user"|"policy"
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )


class SubagentRun(Base):
    """Durable record for one cross-agent handoff run.

    ``grant_id`` is the stable join key shared with live SSE cards and
    ``GrantAudit``. File snapshots are metadata-only; full file bytes stay
    in MinIO.
    """

    __tablename__ = "subagent_runs"
    __table_args__ = (
        UniqueConstraint("grant_id", name="uq_subagent_runs_grant_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    grant_id: Mapped[str] = mapped_column(String(64), index=True)
    rerun_of_grant_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    thread_id: Mapped[str | None] = mapped_column(
        ForeignKey("chat_threads.id", ondelete="SET NULL"), nullable=True, index=True
    )
    agent_name: Mapped[str] = mapped_column(String(128), index=True)
    skill_name: Mapped[str] = mapped_column(String(128))
    args_json: Mapped[str] = mapped_column(Text, default="{}")
    scopes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_ops: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    start_files: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class SubagentRunEvent(Base):
    """Ordered event timeline for a :class:`SubagentRun`."""

    __tablename__ = "subagent_run_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("subagent_runs.id", ondelete="CASCADE"), index=True
    )
    grant_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )


class AgentProofRun(Base):
    """Evidence that a public agent was invoked successfully by the platform."""

    __tablename__ = "agent_proof_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    agent_name: Mapped[str] = mapped_column(String(128), index=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    skill_name: Mapped[str] = mapped_column(String(128), index=True)
    grant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    args_json: Mapped[str] = mapped_column(Text, default="{}")
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    events: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    file_ops: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    card_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    repo_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    image: Mapped[str | None] = mapped_column(String(512), nullable=True)
    agent_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AgentReceipt(Base):
    """Signed execution receipt persisted for query + audit.

    Mirrors :class:`a2a_pack.receipts.ExecutionReceipt`. The full signed
    wire token is stored verbatim (``signed_token``) so any downstream
    verifier can re-check the signature without re-encoding; ``payload``
    holds the verified JSON for indexable querying.
    """

    __tablename__ = "agent_receipts"
    __table_args__ = (
        Index("ix_agent_receipts_agent_started", "agent_id", "started_at"),
        Index("ix_agent_receipts_caller_started", "caller", "started_at"),
    )

    receipt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    agent_name: Mapped[str] = mapped_column(String(128), index=True)
    agent_version: Mapped[str] = mapped_column(String(64), default="")
    caller: Mapped[str] = mapped_column(String(255), default="", index=True)
    task_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    skill_name: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32), default="ok", index=True)
    eval_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)
    signed_token: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )


class AgentSession(Base):
    """Signed replay session header persisted for query + audit.

    Mirrors :class:`a2a_pack.replay.ReplaySession`. The events tuple is
    *not* stored inline — a session can hold hundreds of LLM/tool/workspace
    records and JSONB scans get expensive fast. Instead we write the events
    to the object store as newline-delimited JSON at ``events_object_key``
    and keep only the header fields here for index/pagination scans.

    The full signed wire token is stored verbatim (``signed_token``) so a
    forensic reader can re-verify and reconstruct even when the object
    store is unreachable; the row is the durable index, the object is the
    durable payload.
    """

    __tablename__ = "agent_sessions"
    __table_args__ = (
        Index("ix_agent_sessions_agent_started", "agent_id", "started_at"),
        Index("ix_agent_sessions_caller_started", "caller", "started_at"),
    )

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    agent_name: Mapped[str] = mapped_column(String(128), index=True)
    agent_version: Mapped[str] = mapped_column(String(64), default="")
    caller: Mapped[str] = mapped_column(String(255), default="", index=True)
    task_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    skill_name: Mapped[str] = mapped_column(String(128), index=True)
    receipt_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    started_at: Mapped[int] = mapped_column(Integer, default=0, index=True)
    ended_at: Mapped[int] = mapped_column(Integer, default=0)
    event_count: Mapped[int] = mapped_column(Integer, default=0)
    signed_token: Mapped[str] = mapped_column(Text)
    events_object_key: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )


class DagRun(Base):
    """Durable execution record for a main-agent multi-agent DAG."""

    __tablename__ = "dag_runs"
    __table_args__ = (
        UniqueConstraint("dag_run_id", name="uq_dag_runs_dag_run_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    dag_run_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    thread_id: Mapped[str | None] = mapped_column(
        ForeignKey("chat_threads.id", ondelete="SET NULL"), nullable=True, index=True
    )
    goal: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    nodes_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class MetaAgentRun(Base):
    """Durable goal/plan/progress state for a composable meta-agent."""

    __tablename__ = "meta_agent_runs"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_meta_agent_runs_run_id"),
        Index(
            "ix_meta_agent_runs_agent_user_status_updated",
            "agent_id",
            "user_id",
            "status",
            "updated_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    agent_id: Mapped[int] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    agent_name: Mapped[str] = mapped_column(String(128), index=True)
    thread_id: Mapped[str | None] = mapped_column(String(96), nullable=True, index=True)
    goal: Mapped[str] = mapped_column(Text, default="")
    success_criteria: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="planning", index=True)
    current_plan: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    progress: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class DagRunNode(Base):
    """One executable node inside a :class:`DagRun`."""

    __tablename__ = "dag_run_nodes"
    __table_args__ = (
        UniqueConstraint("dag_run_id", "node_id", name="uq_dag_run_nodes_node"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    dag_run_id: Mapped[str] = mapped_column(String(64), index=True)
    node_id: Mapped[str] = mapped_column(String(96), index=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    agent_name: Mapped[str] = mapped_column(String(128), index=True)
    skill_name: Mapped[str] = mapped_column(String(128))
    deps: Mapped[list[str]] = mapped_column(JSON, default=list)
    args_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    grant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    file_ops: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class TrialRoom(Base):
    """Private buyer trial where agents compete on the same task and files."""

    __tablename__ = "trial_rooms"
    __table_args__ = (UniqueConstraint("slug", name="uq_trial_rooms_slug"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(96), index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(180))
    goal: Mapped[str] = mapped_column(Text)
    acceptance_criteria: Mapped[str] = mapped_column(Text, default="")
    input_paths: Mapped[list[str]] = mapped_column(JSON, default=list)
    output_schema: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    max_cost_cents: Mapped[int] = mapped_column(Integer, default=0)
    max_runtime_seconds: Mapped[int] = mapped_column(Integer, default=300)
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    selected_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deployed_agent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )


class TrialRun(Base):
    """One agent attempt inside a :class:`TrialRoom`."""

    __tablename__ = "trial_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    trial_room_id: Mapped[int] = mapped_column(
        ForeignKey("trial_rooms.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    agent_name: Mapped[str] = mapped_column(String(128), index=True)
    skill_name: Mapped[str] = mapped_column(String(128), index=True)
    grant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    score: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    evaluator_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    args_json: Mapped[str] = mapped_column(Text, default="{}")
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    events: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    file_ops: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    receipt_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ChatThread(Base):
    """Sidebar metadata for a chat conversation.

    The actual message + tool-call state lives in LangGraph's postgres
    checkpoint tables, keyed by the same ``id``. This table exists
    only so we can list a user's threads with titles + timestamps; the
    checkpointer doesn't have a user-scoped index of its own.
    """

    __tablename__ = "chat_threads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(255), default="New chat")
    settings_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )


class ChatThreadMessage(Base):
    """Durable message history for a chat thread.

    LangGraph checkpoints are still used to resume the agent state, but this
    table is the UI-readable transcript so old messages survive checkpoint
    shape changes or checkpointer outages.
    """

    __tablename__ = "chat_thread_messages"
    __table_args__ = (
        Index("ix_chat_thread_messages_thread_id_id", "thread_id", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    thread_id: Mapped[str] = mapped_column(
        ForeignKey("chat_threads.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(24))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )


class ChatThreadEvent(Base):
    """Durable ordered copy of chat stream events for transcript replay."""

    __tablename__ = "chat_thread_events"
    __table_args__ = (
        UniqueConstraint("thread_id", "seq", name="uq_chat_thread_events_thread_seq"),
        Index("ix_chat_thread_events_thread_id_seq", "thread_id", "seq"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    thread_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("chat_threads.id", ondelete="CASCADE"), index=True,
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )


class ChatThreadEmailLink(Base):
    """Maps one email conversation to one chat thread.

    The mail ingress worker keys inbound messages by the root of their
    ``References`` chain (falling back to ``Message-ID``); each distinct email
    conversation lands in its own ``ChatThread`` and agent replies go back out
    with correct ``In-Reply-To``/``References`` so they thread in the remote
    client.
    """

    __tablename__ = "chat_thread_email_links"
    __table_args__ = (
        UniqueConstraint("mailbox_id", "thread_key", name="uq_email_link_mailbox_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    thread_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("chat_threads.id", ondelete="CASCADE"), index=True,
    )
    mailbox_id: Mapped[int] = mapped_column(
        ForeignKey("agent_mailboxes.id", ondelete="CASCADE"), index=True
    )
    agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=True, index=True
    )
    thread_key: Mapped[str] = mapped_column(String(998), index=True)
    remote_address: Mapped[str] = mapped_column(String(320), index=True)
    subject: Mapped[str] = mapped_column(String(998), default="")
    last_message_id: Mapped[str | None] = mapped_column(String(998), nullable=True)
    references_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )


class WorkJob(Base):
    """Generic durable work ledger entry.

    This is intentionally broader than the existing run-specific tables so
    chats, handoffs, DAG nodes, proofs, trials, deploys, and file work can
    share one audit/query surface while specialized tables continue to exist.
    """

    __tablename__ = "work_jobs"
    __table_args__ = (
        UniqueConstraint("job_id", name="uq_work_jobs_job_id"),
        Index("ix_work_jobs_kind_status_created_at", "kind", "status", "created_at"),
        Index("ix_work_jobs_queue_status_priority", "queue", "status", "priority"),
        Index("ix_work_jobs_thread_created_at", "thread_id", "created_at"),
        Index("ix_work_jobs_parent_created_at", "parent_job_id", "created_at"),
        Index("ix_work_jobs_subject", "subject_type", "subject_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    queue: Mapped[str] = mapped_column(String(64), default="default", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=1)

    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    thread_id: Mapped[str | None] = mapped_column(
        ForeignKey("chat_threads.id", ondelete="SET NULL"), nullable=True, index=True
    )
    root_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    parent_job_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    correlation_id: Mapped[str | None] = mapped_column(
        String(96), nullable=True, index=True
    )
    idempotency_key: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )

    source_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    subject_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subject_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    worker_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    worker_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)

    input_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    artifact_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    proof_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    leased_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    events: Mapped[list["WorkEvent"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


class WorkEvent(Base):
    """Append-only event row for a :class:`WorkJob` timeline."""

    __tablename__ = "work_events"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_work_events_event_id"),
        UniqueConstraint("job_id", "event_seq", name="uq_work_events_job_seq"),
        Index("ix_work_events_job_created_at", "job_id", "created_at"),
        Index("ix_work_events_type_created_at", "event_type", "created_at"),
        Index("ix_work_events_source", "source_type", "source_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(64), index=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("work_jobs.job_id", ondelete="CASCADE"), index=True
    )
    event_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parent_event_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    correlation_id: Mapped[str | None] = mapped_column(
        String(96), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    stage: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    severity: Mapped[str] = mapped_column(String(16), default="info", index=True)
    message: Mapped[str] = mapped_column(Text, default="")

    actor_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    source_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_id: Mapped[str | None] = mapped_column(String(160), nullable=True)

    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    artifact_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    proof_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), index=True
    )

    job: Mapped[WorkJob] = relationship(back_populates="events")


class IdempotencyRecord(Base):
    """Request/result cache for deduping work creation and side effects."""

    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("scope", "idempotency_key", name="uq_idempotency_scope_key"),
        Index("ix_idempotency_records_status_expires_at", "status", "expires_at"),
        Index("ix_idempotency_records_user_created_at", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(128), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), index=True)
    request_hash: Mapped[str] = mapped_column(String(96), index=True)
    status: Mapped[str] = mapped_column(String(32), default="in_progress", index=True)

    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    thread_id: Mapped[str | None] = mapped_column(
        ForeignKey("chat_threads.id", ondelete="SET NULL"), nullable=True, index=True
    )
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("work_jobs.job_id", ondelete="SET NULL"), nullable=True, index=True
    )
    result_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    response_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class OrganizationCompliancePolicy(Base):
    """Org-scoped AI-compliance policy.

    ``retention_days`` is the log-retention window for decision records
    (receipts, grant audits, org audit logs). While ``eu_ai_act`` is in
    ``frameworks`` the floor is 180 days (EU AI Act Art. 19); the purge
    path re-checks the floor so a stale row can never authorize an early
    delete. ``legal_hold`` blocks all purging regardless of age.
    """

    __tablename__ = "organization_compliance_policies"
    __table_args__ = (
        UniqueConstraint("organization_id", name="uq_org_compliance_policy_org"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    frameworks: Mapped[list[str]] = mapped_column(JSON, default=list)
    retention_days: Mapped[int] = mapped_column(Integer, default=180)
    legal_hold: Mapped[bool] = mapped_column(default=False)
    legal_hold_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )


class AgentComplianceProfile(Base):
    """Per-agent EU AI Act risk classification + procurement metadata.

    ``risk_tier`` follows the Act's taxonomy (minimal|limited|high|
    unacceptable, plus "unclassified" until an org admin decides).
    ``intended_purpose`` and ``human_oversight`` feed the ISO/IEC 42001
    procurement dossier in the compliance evidence export.
    """

    __tablename__ = "agent_compliance_profiles"
    __table_args__ = (
        UniqueConstraint("agent_id", name="uq_agent_compliance_profile_agent"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    organization_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True
    )
    risk_tier: Mapped[str] = mapped_column(
        String(32), default="unclassified", index=True
    )
    intended_purpose: Mapped[str] = mapped_column(Text, default="")
    human_oversight: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    updated_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=func.now(),
    )
