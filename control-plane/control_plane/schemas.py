from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field


class UserOut(BaseModel):
    id: int
    email: EmailStr
    created_at: datetime

    class Config:
        from_attributes = True


class UserSessionOut(BaseModel):
    id: int
    email: EmailStr


class AuthSessionOut(BaseModel):
    authenticated: bool
    user: UserSessionOut | None = None


class AuthLogoutOut(AuthSessionOut):
    logout_url: str | None = None


class AuthCliSessionCreateIn(BaseModel):
    redirect_to: str | None = Field(default="/", max_length=512)


class AuthCliSessionCreateOut(BaseModel):
    redeem_url: str
    expires_in: int


class AgentSessionExchangeIn(BaseModel):
    """Posted server-side by an agent pod redeeming a browser hand-off code."""

    code: str = Field(min_length=32, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    # The agent names itself. The code is bound to one agent at issue time, so
    # a mismatch means the caller is not the agent the user was sent to.
    audience: str = Field(min_length=1, max_length=128)


class AgentSessionExchangeOut(BaseModel):
    token: str
    expires_at: int
    user_id: int
    email: str




class AgentRegisterIn(BaseModel):
    name: str
    description: str = ""
    version: str
    image: str
    public: bool = True
    card: dict[str, Any]


class AgentImportAuthIn(BaseModel):
    type: str = Field(default="none", max_length=32)
    value: str | None = Field(default=None, max_length=32768)
    scheme: str | None = Field(default=None, max_length=32)
    location: str | None = Field(default=None, max_length=16)
    name: str | None = Field(default=None, max_length=128)


class AgentImportIn(BaseModel):
    url: str = Field(max_length=400)
    name: str | None = Field(default=None, max_length=128)
    public: bool = False
    auth: AgentImportAuthIn | None = None
    organization_slug: str | None = Field(default=None, max_length=96)


class AgentMCPOut(BaseModel):
    mode: str
    url: str
    connector_url: str | None = None
    endpoints: dict[str, Any] = Field(default_factory=dict)


class AgentImportOut(BaseModel):
    id: int
    name: str
    description: str
    version: str
    image: str
    public: bool
    status: str
    url: str | None
    card: dict[str, Any]
    created_at: datetime
    source: str = "external_a2a"
    card_hash: str
    mcp: AgentMCPOut


class AgentFromSourceIn(BaseModel):
    """Provision a user-editable source repo for an agent.

    Build/deploy files live in the platform runtime repo created by the
    tarball deploy path.
    """

    name: str
    description: str = ""
    version: str
    public: bool = True
    organization_slug: str | None = Field(default=None, max_length=96)


class AgentFromSourceOut(BaseModel):
    name: str
    push_url: str  # user-safe HTTPS git URL; credentials are never embedded
    repo_url: str  # user-safe repository URL
    expected_url: str | None  # public URL once ready
    deployment_id: str | None = None


class AgentFromTarballOut(BaseModel):
    """Result of a tarball deploy. The user never sees gitea internals."""

    name: str
    version: str
    status: str
    url: str | None
    head_sha: str
    deployment_id: str | None = None


class AgentOpenAPIGenerateIn(BaseModel):
    """Generate an editable A2APack source repo from an OpenAPI document."""

    url: str | None = Field(default=None, max_length=1000)
    urls: list[str] = Field(default_factory=list, max_length=12)
    name: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=1024)
    public: bool = True
    base_url: str | None = Field(default=None, max_length=1000)
    base_urls: list[str | None] = Field(default_factory=list, max_length=12)
    organization_slug: str | None = Field(default=None, max_length=96)
    refresh_existing: bool = False


class AgentOpenAPIPreviewOut(BaseModel):
    name: str
    description: str
    version: str
    server_url: str
    server_urls: list[str] = Field(default_factory=list)
    operation_count: int
    operations: list[dict[str, Any]]
    consumer_setup: dict[str, Any]
    security_schemes: list[dict[str, Any]]
    skills: list[str]
    routing_mode: str = "direct_operation_skills"
    direct_operation_skill_limit: int | None = None
    route_groups: list[dict[str, Any]] = Field(default_factory=list)
    deepagent_skills: list[str] = Field(default_factory=list)
    source_files: list[str]
    source_openapi_url: str | None = None
    source_openapi_urls: list[str] = Field(default_factory=list)
    regenerable: bool = False
    composite: bool = False
    warnings: list[str] = []


class AgentFromOpenAPIOut(BaseModel):
    """Result of generating and deploying an editable OpenAPI-backed agent."""

    name: str
    version: str
    status: str
    repo_url: str
    expected_url: str | None = None
    deployment_id: str | None = None
    head_sha: str
    preview: AgentOpenAPIPreviewOut
    refreshed_existing: bool = False


class AgentComposeIn(BaseModel):
    """Generate and deploy an editable manifest-backed meta-agent."""

    name: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=1024)
    version: str = Field(default="0.1.0", max_length=64)
    public: bool = True
    manifest: dict[str, Any] | None = None
    composition: Any | None = None
    goal: Any | None = None
    memory: Any | None = None
    organization_slug: str | None = Field(default=None, max_length=96)
    refresh_existing: bool = False


class AgentComposePreviewOut(BaseModel):
    name: str
    class_name: str
    description: str
    version: str
    skills: list[str]
    composition: dict[str, Any]
    goal: dict[str, Any]
    memory: dict[str, Any]
    sub_agents: list[dict[str, Any]]
    source_files: list[str]
    warnings: list[str] = []


class AgentFromComposeOut(BaseModel):
    """Result of generating and deploying an editable meta-agent."""

    name: str
    version: str
    status: str
    repo_url: str
    expected_url: str | None = None
    deployment_id: str | None = None
    head_sha: str
    preview: AgentComposePreviewOut
    refreshed_existing: bool = False


class AgentCustomDomainIn(BaseModel):
    hostname: str = Field(min_length=3, max_length=255)
    include_www: bool = False
    canonical_hostname: str | None = Field(default=None, min_length=3, max_length=255)


class AgentCustomDomainOut(BaseModel):
    id: int
    agent_name: str
    hostname: str
    status: str
    url: str | None
    verification_record_name: str
    verification_record_value: str
    routing_record_type: str
    routing_record_name: str
    routing_record_value: str
    routing_fallback_record_type: str | None = None
    routing_fallback_record_name: str | None = None
    routing_fallback_record_value: str | None = None
    routing_fallback_reason: str | None = None
    dns_setup_kind: str
    is_apex: bool
    paired_www_hostname: str | None = None
    canonical_hostname: str | None = None
    redirect_enabled: bool
    redirect_target_url: str | None = None
    certificate_status: str
    verified_at: datetime | None
    created_at: datetime
    updated_at: datetime
    last_error: str | None = None


class AgentSeoProfileOut(BaseModel):
    status: str
    card_hash: str
    content: dict[str, Any]
    model: str | None = None
    generated_at: datetime | None = None

    class Config:
        from_attributes = True


class AgentOut(BaseModel):
    id: int
    name: str
    description: str
    version: str
    image: str
    public: bool
    fork_policy: Literal["private", "organization", "public"] = "organization"
    status: str
    url: str | None
    source_url: str | None = None
    card: dict[str, Any]
    created_at: datetime
    seo_profile: AgentSeoProfileOut | None = None
    #: True only when the control plane knows a2a runs this agent for itself —
    #: a build specialist from ``PLATFORM_TOOLCHAIN_AGENTS``, or an agent owned
    #: by one of the platform's own service principals. See
    #: ``control_plane.platform_internal``. False means "no such signal", which
    #: is the honest answer both for a seller's listing and for a surface that
    #: never evaluated it; only the public registry routes populate it.
    platform_internal: bool = False

    class Config:
        from_attributes = True


class AgentDetailOut(AgentOut):
    pass


class AgentSearchInputFieldOut(BaseModel):
    name: str
    type: str | None = None
    required: bool = False


class AgentSearchSkillOut(BaseModel):
    name: str
    description: str = ""
    tags: list[str] = []
    input_fields: list[AgentSearchInputFieldOut] = []


class AgentSearchOut(BaseModel):
    name: str
    description: str
    status: str
    public: bool
    url: str | None
    score: float | None = None
    match_source: str
    llm_provisioning: str | None = None
    account_access: dict[str, Any] | None = None
    setup_required: bool = False
    skills: list[AgentSearchSkillOut] = []


class AgentDeploymentEventOut(BaseModel):
    id: int
    deploy_id: str
    agent_name: str
    stage: str
    status: str
    message: str
    data: dict[str, Any]
    created_at: datetime

    class Config:
        from_attributes = True


class AgentDeploymentOut(BaseModel):
    id: int
    deploy_id: str
    agent_name: str
    trigger: str
    status: str
    source_repo_url: str | None
    head_sha: str | None
    image: str | None
    agent_url: str | None
    error: str | None
    verification: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    events: list[AgentDeploymentEventOut] = []

    class Config:
        from_attributes = True


class AgentDeploymentLogOut(BaseModel):
    stage: str
    source: str
    content: str
    truncated: bool
    byte_len: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AgentDeploymentLogsOut(BaseModel):
    deploy_id: str
    agent_name: str
    logs: list[AgentDeploymentLogOut] = []


class AgentRuntimeUpgradeStatus(BaseModel):
    package: str
    current_version: str | None
    latest_version: str
    update_available: bool
    can_redeploy: bool
    message: str


class AgentTemplateUpdateOut(BaseModel):
    agent_name: str
    job_id: str
    status: str
    update_policy: str
    template_lineage: dict[str, Any]
    message: str


class AgentOpenAPISourceOut(BaseModel):
    url: str
    urls: list[str] = Field(default_factory=list)
    regenerable: bool = True


class AgentCodeEditorOut(BaseModel):
    target_agent_name: str
    enabled: bool
    status: str
    shared_agent_name: str
    target_repo_url: str | None = None
    workspace_key: str | None = None
    last_error: str | None = None
    enabled_at: datetime | None = None
    updated_at: datetime | None = None
    runtime: dict[str, Any] = Field(default_factory=dict)


class AgentMineOut(AgentOut):
    repo_url: str
    runtime_upgrade: AgentRuntimeUpgradeStatus
    latest_deployment: AgentDeploymentOut | None = None
    openapi_source: AgentOpenAPISourceOut | None = None
    code_editor: AgentCodeEditorOut
    self_healing: dict[str, Any] = Field(default_factory=dict)


class AgentMineSummaryOut(BaseModel):
    id: int
    name: str
    description: str
    version: str
    public: bool
    status: str
    url: str | None
    created_at: datetime
    skill_count: int = 0
    runtime_upgrade: AgentRuntimeUpgradeStatus
    latest_deployment: AgentDeploymentOut | None = None


class BountyIn(BaseModel):
    title: str = Field(min_length=4, max_length=160)
    description: str = Field(min_length=10, max_length=8000)
    example_input: str = Field(default="", max_length=4000)
    example_output: str = Field(default="", max_length=4000)
    tags: list[str] = Field(default_factory=list, max_length=8)


class BountyClaimIn(BaseModel):
    agent_name: str


class BountyOut(BaseModel):
    id: int
    slug: str
    title: str
    description: str
    example_input: str
    example_output: str
    tags: list[str]
    status: str
    posted_by_email: str | None = None
    claimed_agent_name: str | None = None
    claimed_agent_status: str | None = None
    claimed_agent_url: str | None = None
    claimed_agent_version: str | None = None
    claimed_agent_card: dict[str, Any] | None = None
    claimed_at: datetime | None = None
    fulfilled_at: datetime | None = None
    created_at: datetime

    class Config:
        from_attributes = True
