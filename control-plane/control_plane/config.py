from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="A2A_CP_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://control_plane:control_plane@control-plane-postgres.control-plane.svc.cluster.local:5432/control_plane"
    redis_url: str | None = "redis://control-plane-redis.control-plane.svc.cluster.local:6379/0"
    jwt_secret: str = "change-me-in-prod"
    jwt_alg: str = "HS256"
    jwt_ttl_seconds: int = 60 * 60 * 24 * 7  # 7 days

    # Keycloak OAuth identity bridge (E1-P2). When enabled, ``current_user``
    # also accepts Keycloak-issued RS256 access tokens, validated via the realm
    # JWKS, and provisions a control-plane User on first login (keyed by the
    # token ``sub``, linked by verified email when one already exists). Browser
    # sessions use short-lived CP HS256 cookies minted by the OIDC callback.
    keycloak_enabled: bool = True
    keycloak_issuer: str = "https://auth.a2acloud.io/realms/a2acloud"
    keycloak_backend_url: str | None = None
    keycloak_jwks_url: str = (
        "https://auth.a2acloud.io/realms/a2acloud/protocol/openid-connect/certs"
    )
    keycloak_browser_client_id: str = "a2acloud-dashboard"
    keycloak_browser_client_secret: str | None = None
    keycloak_admin_client_id: str = "a2acloud-admin"
    oidc_state_cookie_name: str = "a2a_oidc_state"
    oidc_state_ttl_seconds: int = 10 * 60
    cli_session_exchange_ttl_seconds: int = Field(default=60, ge=10, le=300)
    cli_session_confirmation_cookie_name: str = "__Host-a2a_cli_confirm"

    agents_namespace: str = "agents"
    ingress_host_template: str = "{name}.a2acloud.io"
    # Knative scale-to-zero defaults for hosted user agents. Ordinary agents
    # idle down to zero pods; ``agents_max_scale`` bounds burst. Platform-
    # critical agents listed in ``always_on_agents`` (comma-separated names)
    # stay pinned at ``min_scale >= 1`` and never scale to zero.
    agents_min_scale: int = 0
    # One pod per agent: a single replica soaks all concurrent requests (see
    # the high ``containerConcurrency`` default) and still scales to zero when
    # idle. Caps redeploy overlap to ~2 pods on the single node, so old
    # revisions don't crowd while draining their long (15-min) requests.
    agents_max_scale: int = 1
    always_on_agents: str = ""
    # Registry auto-detection is disabled while the registry requires
    # method-scoped BasicAuth. Pin ``A2A_CP_A2A_PACK_VERSION`` instead; a future
    # auth-aware client can explicitly re-enable detection.
    a2a_pack_registry_auto_detect: bool = False
    a2a_pack_registry_url: str = "http://registry.registry.svc.cluster.local:5000"
    a2a_pack_image_repo: str = "a2a/a2a-pack-base"
    # Default Knative request timeout for hosted agents when neither a runtime
    # nor skill-level timeout is declared. Keep this aligned with the cluster's
    # max-revision-timeout-seconds so durable API runs are not cut at 10m.
    agents_default_timeout_seconds: int = 1800
    # Public URL the deployed agent pods call back to when verifying
    # caller JWTs against /v1/me. Cluster-internal default works for
    # agents in the same cluster; override in prod if pods are remote.
    public_cp_url: str = "http://control-plane.control-plane.svc.cluster.local"
    # Browser-facing dashboard origin used by OIDC redirects and auth errors.
    dashboard_url: str = "https://app.a2acloud.io"
    session_cookie_name: str = "__Host-a2a_session"
    session_cookie_secure: bool = True
    # Hosted agent origins never receive the dashboard's host-only session.
    # They get their own origin-bound cookie instead, minted by exchanging a
    # single-use code at /v1/auth/agent-session/* for an agent-scoped token
    # (see control_plane/agent_frontend_session.py). That gateway ships, so
    # packed frontends may declare frontend.auth=platform.
    allow_platform_frontend_auth: bool = True
    # Hand-off codes are redeemed by the agent immediately on redirect.
    agent_session_exchange_ttl_seconds: int = Field(default=60, ge=10, le=300)
    # Host-only cookie set by the agent on its *own* origin. Deliberately a
    # different name from the dashboard session so the two can never be
    # confused for one another.
    agent_session_cookie_name: str = "__Host-a2a_agent_session"
    # Lifetime of the agent-origin session cookie. Kept well under the platform
    # session so a token that does leak stops working quickly.
    agent_session_ttl_seconds: int = Field(default=12 * 60 * 60, ge=300)

    langfuse_base_url: str = "https://langfuse.a2acloud.io"
    langfuse_provisioning_enabled: bool = True
    langfuse_provisioning_mode: str = "shared_instance"
    langfuse_org_public_key: str | None = None
    langfuse_org_secret_key: str | None = None
    langfuse_provisioner_interval_seconds: float = 10.0
    # When the Langfuse OSS install lacks an EE license, ``/api/public/projects``,
    # ``/api/public/projects/{id}/apiKeys`` and ``/api/public/scim/Users`` return
    # 401/403. Set this DSN (postgresql+asyncpg://...) to let the provisioner
    # fall back to direct writes against Langfuse's Postgres. Leave unset to keep
    # provisioning API-only and surface the 403 as a hard error.
    langfuse_database_url: str | None = None
    # Single shared Langfuse organization id every control-plane org maps to in
    # ``shared_instance`` mode.
    langfuse_shared_org_id: str = "a2a"
    litellm_team_logging_enabled: bool = True
    litellm_url: str = Field(
        default="http://litellm.llm.svc.cluster.local:4000",
        validation_alias="A2A_LITELLM_URL",
    )
    litellm_key: str = Field(default="", validation_alias="A2A_LITELLM_KEY")
    litellm_model: str = Field(default="gpt-5.5", validation_alias="A2A_LITELLM_MODEL")
    litellm_usage_reconcile_enabled: bool = True
    litellm_usage_reconcile_window_seconds: int = 300
    # Arbitrary endpoints require an FQDN-aware egress proxy. Keep disabled on
    # the shared platform; generated provider origins remain available.
    allow_custom_llm_base_urls: bool = False
    litellm_usage_reconcile_page_size: int = 100
    # User/generated agents declare ``llm_provisioning=platform`` to receive the
    # caller's saved LLM credential through LiteLLM.
    allow_user_platform_llm: bool = True
    gitea_provisioning_enabled: bool = True
    gitea_provisioner_interval_seconds: float = 10.0
    gitea_oauth_linking_enabled: bool = True
    gitea_database_url: str | None = None
    gitea_oauth_auth_source_name: str = "A2A Cloud"
    gitea_oauth_provider: str = "openidConnect"
    gitea_source_webhooks_enabled: bool = False
    gitea_source_webhook_url: str | None = None
    gitea_source_webhook_secret: str | None = None
    gitea_runtime_webhooks_enabled: bool = False
    gitea_runtime_webhook_url: str | None = None
    gitea_runtime_webhook_secret: str | None = None
    # Repository-scoped Gitea Actions credentials used only on hidden runtime
    # repos to publish agent images to the private registry.
    registry_actions_username: str | None = None
    registry_actions_password: str | None = None
    # First-party internal repos mounted into the orchestrator workspace as
    # repos/<repo>/... . Entries are comma-separated repo names or owner/repo.
    repo_mounts: str = "control-plane,dashboard"
    database_provisioning_enabled: bool = False
    database_namespace: str = "database"
    database_storage_class: str = "longhorn-replica-2"
    database_default_idle_suspend_seconds: int = 300
    database_provisioner_interval_seconds: float = 10.0
    database_admin_url: str | None = None
    database_neon_pageserver_url: str | None = None
    database_pg_version: int = 16
    database_runtime_host: str | None = None
    database_runtime_port: int | None = None
    database_sslmode: str | None = None
    # Provisioning backend. "postgres_admin" (default) = CREATE DATABASE in one
    # shared cluster. "neon_operator" = per-agent operator tenant + hybrid
    # compute + wake-proxy (see neon_operator_backend.py). Keep default until the
    # hybrid is integration-tested + agents migrated (H5).
    database_backend: str = "postgres_admin"
    database_operator_namespace: str = "neon-v2"
    database_operator_cluster: str = "agents-cluster"
    database_operator_pageserver_url: str | None = None
    database_operator_storcon_url: str | None = None
    database_operator_safekeepers: str | None = None
    database_operator_compute_image: str | None = None
    database_operator_wrapper_configmap: str = "h2-compute"
    database_operator_wake_proxy_image: str | None = None
    database_operator_wake_proxy_configmap: str = "h2-wake-proxy"
    database_operator_wake_proxy_sa: str = "h2-wake-proxy"
    # cloud_admin ships in the compute image as a network-reachable superuser
    # (verified in the spike). Override with a strong password in production.
    database_operator_admin_user: str = "cloud_admin"
    database_operator_admin_password: str | None = "cloud_admin"
    database_operator_admin_secret: str = "h2-compute-auth"
    database_operator_idle_seconds: int = 300
    # 0 = compute scales to zero (real scale-to-zero; a wake occasionally hits
    # the ~30s self-healing walproposer restart crash). 1 = always-warm compute
    # -> that crash is impossible. Default to scale-to-zero; pin critical/always-on
    # agents to 1 per-agent via the wake-proxy COMPUTE_MIN_REPLICAS env.
    database_operator_compute_min_replicas: int = 0
    source_push_deploy_worker_enabled: bool = True
    source_push_deploy_worker_interval_seconds: float = 5.0
    source_push_deploy_debounce_seconds: float = 15.0
    agent_api_worker_enabled: bool = True
    agent_api_worker_interval_seconds: float = 2.0
    agent_api_worker_lease_seconds: int = 30 * 60
    subagent_stale_closer_enabled: bool = True
    subagent_stale_closer_interval_seconds: float = 60.0
    subagent_stale_after_seconds: int = 30 * 60
    subagent_stale_batch_size: int = 100
    template_update_worker_enabled: bool = True
    template_update_worker_interval_seconds: float = 5.0
    template_update_timeout_seconds: int = 3600
    template_update_max_turns: int = 50
    self_healing_enabled: bool = True
    self_healing_worker_interval_seconds: float = 5.0
    agent_scheduler_enabled: bool = True
    agent_scheduler_interval_seconds: float = 30.0
    agent_studio_worker_enabled: bool = True
    agent_studio_worker_interval_seconds: float = 2.0
    agent_studio_worker_lease_seconds: int = 120
    agent_studio_stale_after_seconds: int = 180
    agent_studio_autopilot_worker_enabled: bool = False
    agent_studio_autopilot_interval_seconds: float = 30.0
    agent_studio_autopilot_max_agents_per_run: int = 100
    agent_studio_autopilot_max_proposals_per_agent: int = 3
    agent_studio_autopilot_proposal_ttl_days: int = 14
    agent_studio_autopilot_review_budget_usd: float = 0.50
    agent_studio_harness_cleanup_enabled: bool = True
    agent_studio_harness_cleanup_interval_seconds: float = 3600.0
    agent_studio_harness_cleanup_ttl_seconds: int = 24 * 60 * 60
    agent_studio_harness_cleanup_owner_email: str = "agent-studio-harness@a2acloud.io"
    agent_studio_harness_cleanup_name_prefix: str = "studio-harness-"
    agent_studio_harness_cleanup_max_per_run: int = 10
    gitea_token_sweeper_enabled: bool = True
    gitea_token_sweeper_interval_seconds: float = 30.0
    code_editor_runtime_enabled: bool = True
    code_editor_agent_name: str = "code-editor-agent"
    agent_search_qdrant_url: str | None = None
    agent_search_qdrant_api_key: str | None = None
    agent_search_qdrant_collection: str = "agents"
    agent_search_qdrant_timeout_seconds: float = 5.0
    agent_search_embedding_model: str = "BAAI/bge-small-en-v1.5"
    agent_search_embedding_dimension: int = 384
    agent_search_score_threshold: float | None = None
    memory_embedding_provider: str = "local"
    memory_embedding_model: str = "text-embedding-3-small"
    memory_embedding_dimension: int = 1536
    memory_embedding_timeout_seconds: float = 20.0
    memory_qdrant_url: str | None = None
    memory_qdrant_api_key: str | None = None
    memory_qdrant_collection: str = "agent_memory"
    memory_qdrant_timeout_seconds: float = 5.0
    memory_semantic_score_threshold: float | None = None
    # Internal audit signature for persisted protocol-pack registry rows.
    # These signatures prove control-plane provenance; they do not grant live
    # runtime mutation authority.
    protocol_registry_signing_secret: str | None = None

    # Agent mailboxes (pro plans): Mailu-backed per-agent inboxes on the
    # dedicated agent mail domain. The provisioner reconciles AgentMailbox
    # rows against the Mailu API and projects IMAP/SMTP creds into the agent
    # runtime secret; the ingress worker polls IMAP and bridges mail into
    # chat threads. Both loops are off unless explicitly enabled.
    mailbox_provisioning_enabled: bool = False
    mailbox_provisioner_interval_seconds: float = 15.0
    mailu_api_url: str = "https://mail.a2acloud.io/api/v1"
    mailu_api_token: str | None = None
    # Separate domain from a2acloud.io so agent addresses can never collide
    # with human/system senders (postmaster@, no-reply@, ...) and a
    # deliverability hit on agent mail stays off the primary domain.
    agent_mail_domain: str = "agents.a2acloud.io"
    agent_mail_imap_host: str = "mailu-front.mailu.svc.cluster.local"
    agent_mail_imap_port: int = 993
    agent_mail_smtp_host: str = "mailu-front.mailu.svc.cluster.local"
    agent_mail_smtp_port: int = 587
    # Mailu front serves the public mail.a2acloud.io cert; the cluster-local
    # service name won't match it, so hostname verification stays off for
    # in-cluster IMAP/SMTP connections.
    agent_mail_tls_verify: bool = False
    agent_mailbox_quota_bytes: int = 100 * 1024 * 1024
    agent_mail_daily_send_limit: int = 50
    agent_mail_max_body_bytes: int = 256 * 1024
    # Attachments handed to on_email skills: inlined as base64 up to this
    # size, metadata-only above it.
    agent_mail_max_attachment_bytes: int = 1024 * 1024
    agent_mail_max_attachments: int = 10
    # Downgraded (plan-lapsed) mailboxes are disabled, kept this long, then
    # removed by the provisioner.
    mailbox_disabled_retention_days: int = 30
    mail_ingress_enabled: bool = False
    mail_ingress_interval_seconds: float = 30.0
    # Timeout for the A2A message/send turn triggered by an inbound email.
    mail_ingress_agent_timeout_seconds: float = 300.0

    # HTTP rate limiting (control_plane/rate_limit.py). Only the endpoint
    # classes named in that module's policy table are limited; everything else
    # is untouched. The limiter is Redis-backed (``redis_url`` above) so the
    # count is shared across every replica and uvicorn worker, and it FAILS
    # OPEN: a Redis outage degrades to "no limit", never to a 5xx.
    rate_limit_enabled: bool = True
    # Deliberately sub-second. A limiter that is slow is worse than a limiter
    # that is absent, so a Redis that does not answer promptly is treated as
    # unavailable and the request is allowed through.
    rate_limit_redis_timeout_seconds: float = 0.25
    # Requests whose client address is loopback/private are first-party
    # in-cluster callers (cronjobs, agent-to-agent, the SDK
    # runtime): they are exempt from the IP-keyed limits. This does NOT rest on
    # Traefik stripping inbound X-Forwarded-* - the limiter reads the rightmost
    # forwarded hop that is not ours, so a caller cannot present a private
    # address and be exempted. Flip this off to bucket in-cluster callers too.
    rate_limit_exempt_private_clients: bool = True
    # Comma-separated CIDRs that are *our* infrastructure despite being
    # globally routable - a load balancer that appends its own public address
    # to X-Forwarded-For, or the cluster's egress IP. Everything
    # non-globally-routable is already treated this way; this is only for the
    # cases that property cannot express. Left empty because guessing at it
    # would be worse than leaving it out: watch
    # ``a2a_control_plane_rate_limit_client_source_total{source="forwarded_ambiguous"}``
    # to find out whether any hop needs to go here.
    rate_limit_trusted_proxy_cidrs: str = ""

    in_cluster: bool = True
    kubeconfig: str | None = None

    @field_validator(
        "keycloak_browser_client_secret",
        "keycloak_backend_url",
        "langfuse_org_public_key",
        "langfuse_org_secret_key",
        "langfuse_database_url",
        "gitea_database_url",
        "gitea_source_webhook_url",
        "gitea_source_webhook_secret",
        "gitea_runtime_webhook_url",
        "gitea_runtime_webhook_secret",
        "registry_actions_username",
        "registry_actions_password",
        "database_admin_url",
        "database_neon_pageserver_url",
        "database_runtime_host",
        "database_runtime_port",
        "database_sslmode",
        "agent_search_qdrant_url",
        "agent_search_qdrant_api_key",
        "agent_search_score_threshold",
        "memory_qdrant_url",
        "memory_qdrant_api_key",
        "memory_semantic_score_threshold",
        "protocol_registry_signing_secret",
        "mailu_api_token",
        "kubeconfig",
        mode="before",
    )
    @classmethod
    def _empty_string_as_none(cls, value):
        if isinstance(value, str) and value.strip().lower() in {"", "none", "null"}:
            return None
        return value


settings = Settings()

# https://www.sparkxyz.io/projects/23814
