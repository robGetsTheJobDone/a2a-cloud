/* Tiny client for the control plane. Browser OIDC sessions use the
   httpOnly __Host-a2a_session cookie. */

export type Session = {
  id: number;
  email: string;
  source?: "cookie";
};

let memorySession: Session | null = null;

export type Organization = {
  id: number;
  slug: string;
  name: string;
  role: "owner" | "admin" | "member" | string;
  created_at: string;
};

export type OrganizationDomain = {
  id: number;
  domain: string;
  verification_record_name: string;
  verification_record_value: string;
  verified_at: string | null;
  created_at: string;
};

export type OrganizationMember = {
  id: number;
  user_id: number;
  email: string;
  role: "owner" | "admin" | "member" | string;
  active: boolean;
  external_id: string | null;
  created_at: string;
  updated_at: string | null;
};

export type OrganizationAuditLog = {
  id: number;
  actor_user_id: number | null;
  actor: string | null;
  action: string;
  target_type: string;
  target_id: string | null;
  target_email: string | null;
  data: Record<string, unknown>;
  created_at: string;
};

export type OrganizationScimConfig = {
  base_url: string;
  service_provider_config_url: string;
  users_url: string;
};

export type OrganizationScimToken = {
  id: number;
  label: string;
  token_last4: string;
  enabled: boolean;
  created_by_email: string | null;
  last_used_at: string | null;
  created_at: string;
};

export type OrganizationScimTokenCreated = OrganizationScimToken & {
  token: string;
};

type LangfuseServiceAccess = {
  status: string;
  base_url: string;
  login_url: string;
  login_email: string;
  login_password: string | null;
  login_password_ref: string | null;
  account_status: string;
  role: string | null;
  project_name: string | null;
  project_id: string | null;
  project_url: string | null;
  public_key: string | null;
  secret_key_ref: string | null;
  last_error: string | null;
  provisioned_at: string | null;
};

type LiteLLMServiceAccess = {
  status: string;
  base_url: string;
  openai_base_url: string;
  team_id: string | null;
  key_ref: string | null;
  key_configured: boolean;
  last_error: string | null;
};

type GiteaOrganizationAccess = {
  status: string;
  org_name: string | null;
  org_url: string | null;
  last_error: string | null;
  provisioned_at: string | null;
};

export type OrganizationServiceAccess = {
  id: number;
  slug: string;
  name: string;
  role: string;
  gitea: GiteaOrganizationAccess;
  langfuse: LangfuseServiceAccess;
  litellm: LiteLLMServiceAccess;
};

export type GiteaRepositoryAccess = {
  agent_name: string;
  organization_id: number | null;
  owner: string | null;
  repo_url: string;
  agent_url: string | null;
  status: string;
  public: boolean;
};

type GiteaServiceAccess = {
  base_url: string;
  username: string;
  auth_mode: string;
  repositories: GiteaRepositoryAccess[];
};

export type ServiceAccess = {
  user: { email: string };
  organizations: OrganizationServiceAccess[];
  gitea: GiteaServiceAccess;
};

export type FeatureFlags = {
  enabled_keys: string[];
};

export type OnboardingStep = "llm_key" | "tour" | "done";

export type OnboardingState = {
  completed: boolean;
  /** "Skip for now" was used on the BYOK step. Distinct from `completed`: the
   *  wizard stops blocking, the LLM key is collected just-in-time instead. */
  dismissed: boolean;
  current_step: OnboardingStep;
  llm_key_configured: boolean;
  llm_key_step_completed: boolean;
  walkthrough_completed: boolean;
  started_at: string | null;
  llm_key_completed_at: string | null;
  walkthrough_started_at: string | null;
  walkthrough_completed_at: string | null;
  last_seen_step: string | null;
  tour_step_index: number;
  tour_step_total: number;
  dismissed_at: string | null;
  completed_at: string | null;
  updated_at: string | null;
};

export type FileMeta = {
  path: string;
  size: number;
  modified_at: string;
  content_type: string;
  etag?: string;
  is_dir?: boolean;
  writable?: boolean;
};

export type LLMCreds = {
  id: number;
  name: string;
  base_url: string;
  model: string;
  temperature_mode: "omit" | "default" | "custom";
  temperature: number | null;
  extra_body: Record<string, unknown>;
  api_key_redacted: string;
  created_at: string;
  updated_at: string;
};

export type LLMModelCatalogModel = {
  model: string;
  litellm_model: string;
  provider: string;
  mode: string;
  input_cost_per_million_tokens: number | null;
  output_cost_per_million_tokens: number | null;
  max_input_tokens: number | null;
  max_output_tokens: number | null;
  supports_vision: boolean | null;
  supports_function_calling: boolean | null;
  supports_reasoning: boolean | null;
  supports_web_search: boolean | null;
  // litellm's supported request knobs for this model (subset the form renders):
  // temperature, max_tokens, max_completion_tokens, top_p, reasoning_effort, thinking.
  supported_params: string[];
};

export type LLMModelCatalogProvider = {
  id: string;
  label: string;
  provider: string;
  base_url: string;
  default_model: string;
  // litellm's env var for this provider's key (e.g. "GROQ_API_KEY"); "" for custom.
  key_env: string;
  // Whether litellm treats this provider as an OpenAI-compatible endpoint.
  openai_compatible: boolean;
  extra_body: Record<string, unknown>;
  models: LLMModelCatalogModel[];
};

export type LLMModelCatalog = {
  source: string;
  providers: LLMModelCatalogProvider[];
  total_models: number;
  generated_at: string;
};

export type ConsumerSetupField = {
  name: string;
  kind: "config" | "secret" | string;
  label: string;
  description: string;
  required: boolean;
  input_type:
    | "text"
    | "password"
    | "url"
    | "email"
    | "textarea"
    | "number"
    | "boolean"
    | "select"
    | string;
  options: string[];
};

export type ConsumerSetupValue = {
  name: string;
  kind: string;
  configured: boolean;
  source: "user" | "org" | string | null;
  value_redacted: string | null;
  updated_at: string | null;
};

export type ConsumerSetupStatus = {
  declaration: { fields: ConsumerSetupField[] };
  values: ConsumerSetupValue[];
  missing_required: string[];
  complete: boolean;
  organization: {
    id: number;
    slug: string;
    name: string;
    role: string;
  } | null;
  can_manage_org: boolean;
};

export type InstalledAgentSetupValue = ConsumerSetupValue & {
  label: string;
  required: boolean;
  input_type: ConsumerSetupField["input_type"];
  scope: "user" | "org" | string;
  organization: {
    id: number;
    slug: string;
    name: string;
    role: string;
  } | null;
};

export type AgentSkill = {
  name: string;
  description: string;
  tags: string[];
  input_schema: Record<string, unknown>;
  output_schema?: Record<string, unknown>;
};

type TemplateLineage = {
  schema_version?: string;
  template_ref?: string | null;
  template_version?: string | null;
  template_digest?: string | null;
  source_agent?: string | null;
  source_agent_version?: string | null;
  source_repo_url?: string | null;
  source_revision?: string | null;
  instance_id?: string | null;
  instance_version?: string | null;
  update_policy?: "none" | "notify" | "propose" | "auto_patch" | string;
  update_channel?: string | null;
  migration_skill?: string | null;
};

type AgentCardData = {
  name: string;
  description: string;
  version: string;
  capabilities?: Record<string, unknown>;
  skills: AgentSkill[];
  template_lineage?: TemplateLineage | null;
  runtime?: {
    llm_provisioning?:
      | "platform"
      | "platform_or_caller_provided"
      | "caller_provided"
      | "agent_byok";
    account_access?: AccountAccessPolicy;
    tools_used?: string[];
  };
  consumer_setup?: { fields: ConsumerSetupField[] };
};

export type AccountAccessPolicy = {
  required: boolean;
  platform_skill_calls: number;
  after_trial: "byok";
};

export type AgentListing = {
  id: number;
  name: string;
  description: string;
  version: string;
  image: string;
  public: boolean;
  status: string;
  url: string | null;
  card: AgentCardData;
  created_at: string;
};

export type AgentSearchResult = {
  name: string;
  description: string;
  status: string;
  public: boolean;
  url: string | null;
  score: number | null;
  match_source: string;
  llm_provisioning: string | null;
  account_access: AccountAccessPolicy | null;
  setup_required: boolean;
  skills: Array<{
    name: string;
    description: string;
    tags: string[];
    input_fields: Array<{
      name: string;
      type: string | null;
      required: boolean;
    }>;
  }>;
};

export type AgentImportAuthInput =
  | { type: "bearer"; value: string; scheme?: string }
  | {
      type: "api_key";
      value: string;
      location: "header" | "query";
      name: string;
    };

export type AgentImportInput = {
  url: string;
  name?: string;
  public: boolean;
  auth?: AgentImportAuthInput;
};

export type AgentImportResult = AgentListing & {
  source: string;
  card_hash: string;
  mcp: { mode: string; url: string };
};

export type AgentOpenApiGenerateInput = {
  url?: string;
  urls?: string[];
  name?: string;
  description?: string;
  public: boolean;
  base_url?: string;
  base_urls?: Array<string | null>;
  organization_slug?: string;
  refresh_existing?: boolean;
};

export type AgentOpenApiPreview = {
  name: string;
  description: string;
  version: string;
  server_url: string;
  server_urls: string[];
  operation_count: number;
  operations: Array<{
    operation_id: string;
    skill_name: string;
    method: string;
    path: string;
    summary: string;
    tags: string[];
    destructive: boolean;
    parameter_count: number;
    has_request_body: boolean;
    requires_auth: boolean;
  }>;
  consumer_setup: { fields: ConsumerSetupField[] };
  security_schemes: Array<Record<string, unknown>>;
  skills: string[];
  source_files: string[];
  source_openapi_url: string | null;
  source_openapi_urls: string[];
  regenerable: boolean;
  composite: boolean;
  warnings: string[];
};

export type AgentOpenApiGenerateResult = {
  name: string;
  version: string;
  status: string;
  repo_url: string;
  expected_url: string | null;
  deployment_id: string | null;
  head_sha: string;
  preview: AgentOpenApiPreview;
  refreshed_existing: boolean;
};

export type AgentComposeInput = {
  name?: string;
  description?: string;
  version?: string;
  public: boolean;
  manifest: Record<string, unknown>;
  organization_slug?: string;
  refresh_existing?: boolean;
};

type AgentComposePreview = {
  name: string;
  class_name: string;
  description: string;
  version: string;
  skills: string[];
  composition: Record<string, unknown>;
  goal: Record<string, unknown>;
  memory: Record<string, unknown>;
  sub_agents: Array<Record<string, unknown>>;
  source_files: string[];
  warnings: string[];
};

export type AgentComposeResult = {
  name: string;
  version: string;
  status: string;
  repo_url: string;
  expected_url: string | null;
  deployment_id: string | null;
  head_sha: string;
  preview: AgentComposePreview;
  refreshed_existing: boolean;
};

type AgentAuthRequirement = {
  scheme_name: string;
  scheme_type: "api_key" | "http" | "oauth2" | "oidc" | "mtls" | string;
  required: boolean;
  supported: boolean;
  description: string;
  location?: string | null;
  name?: string | null;
  scheme?: string | null;
  authorization_url?: string | null;
  token_url?: string | null;
  open_id_connect_url?: string | null;
  scopes: string[];
};

export type AgentAuthConnection = {
  id: number;
  agent_name: string;
  scheme_name: string;
  scheme_type: string;
  credential_scope: string;
  status: string;
  expires_at: string | null;
  last_verified_at: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type AgentAuthStatus = {
  agent_name: string;
  status: string;
  requirements: AgentAuthRequirement[];
  connections: AgentAuthConnection[];
};

export type AgentApiToken = {
  id: number;
  agent_name: string;
  name: string;
  token_last4: string;
  scopes: string[];
  enabled: boolean;
  expires_at: string | null;
  last_used_at: string | null;
  created_at: string;
  updated_at: string;
};

export type AgentApiTokenCreated = AgentApiToken & {
  token: string;
};

/**
 * A listed link never ships the secret, so these URLs are always the clean
 * ones. The placement the link was minted with is not persisted server-side,
 * so it is reported only on the create response, where it is known.
 */
export type AgentIntegrationLink = AgentApiToken & {
  openapi_url: string;
  invoke_base_url: string;
  sample_invoke_url: string | null;
  mcp_url: string;
};

export type AgentIntegrationLinkCreated = AgentIntegrationLink & {
  token: string;
  curl_example: string;
  /**
   * "header" (default) means the URLs above carry no secret and callers send
   * `Authorization: Bearer <token>`. "url_query" means the token is embedded in
   * the URLs, which must then be treated as secrets.
   */
  token_placement: "header" | "url_query";
  auth_header: string;
  security_notice: string | null;
};

/**
 * What the integration-link panel may claim about the URLs it is about to
 * show. Ticking the URL-token box puts the live token in every URL, so the
 * "no secret" reassurance must never be static copy — it is derived here so a
 * panel cannot assert it while rendering a URL that carries the credential.
 */
export function integrationLinkUrlSecrecyNote(urlToken: boolean): string {
  return urlToken
    ? "With the URL-token option below, the generated URLs carry the token and must be treated as secrets."
    : "The URLs below hold no secret.";
}

/**
 * Label a URL field on a freshly created integration link. When the caller
 * opted into the URL-token form the URL *is* the credential, so it must not be
 * presented like the ordinary non-secret endpoint URLs.
 */
export function integrationLinkUrlLabel(
  label: string,
  link: Pick<AgentIntegrationLinkCreated, "token_placement">,
): string {
  return link.token_placement === "url_query" ? `${label} (secret)` : label;
}

export type InstalledAgent = {
  agent: AgentListing;
  setup_values: InstalledAgentSetupValue[];
  auth_connections: AgentAuthConnection[];
  missing_required: string[];
  updated_at: string | null;
};

export type AgentAuthConnectInput = {
  scheme_name?: string;
  scheme_type: "api_key" | "http" | "oauth2" | "oidc" | "mtls";
  credential_scope?: "agent" | "user";
  value?: string;
  token?: string;
  scheme?: string;
  location?: "header" | "query";
  name?: string;
  access_token?: string;
  refresh_token?: string;
  token_url?: string;
  client_id?: string;
  client_secret?: string;
  expires_in?: number;
  cert_pem?: string;
  key_pem?: string;
  ca_pem?: string;
};

export type AgentDeploymentEvent = {
  id: number;
  deploy_id: string;
  agent_name: string;
  stage: string;
  status: "pending" | "running" | "passed" | "failed" | string;
  message: string;
  data: Record<string, unknown>;
  created_at: string;
};

export type AgentDeployment = {
  id: number;
  deploy_id: string;
  agent_name: string;
  trigger: string;
  status: "queued" | "building" | "deploying" | "verifying" | "live" | "failed" | string;
  source_repo_url: string | null;
  head_sha: string | null;
  image: string | null;
  agent_url: string | null;
  error: string | null;
  verification: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  completed_at: string | null;
  events: AgentDeploymentEvent[];
};

export type AgentDeploymentLog = {
  stage: string;
  source: "gitea_actions" | "pod" | "argo" | string;
  content: string;
  truncated: boolean;
  byte_len: number;
  created_at: string;
  updated_at: string;
};

export type AgentDeploymentLogsResponse = {
  deploy_id: string;
  agent_name: string;
  logs: AgentDeploymentLog[];
};

export type AgentDeploymentStreamEvent =
  | { type: "snapshot"; deployment: AgentDeployment }
  | { type: "event"; deploy_id?: string; event: AgentDeploymentEvent }
  | { type: "done"; deployment?: AgentDeployment }
  | { type: "error"; message?: string };

type EvidenceProvenanceRef = {
  source: string;
  row_id: string | null;
  timestamp: string | null;
  join_rule: string;
};

type EvidenceWarning = {
  code: string;
  message: string;
  severity: "info" | "warning" | "critical";
  source_ref: string | null;
};

type EvidenceRedaction = {
  view: "owner" | "public" | "operator" | string;
  include_payloads: boolean;
  omitted_classes: string[];
};

type EvidenceWatermark = {
  generated_at: string;
  source_row_count: number;
  projection: string;
};

export type AgentDossier = {
  schema_version: number;
  agent: Record<string, unknown>;
  claims: Array<Record<string, unknown>>;
  current_version: Record<string, unknown>;
  ownership: Record<string, unknown>;
  trust_profile: Record<string, unknown>;
  authority_summary: Record<string, unknown>;
  quality_summary: Record<string, unknown>;
  mutation_summary: Record<string, unknown>;
  risk_summary: Record<string, unknown>;
  graph_refs: Record<string, unknown>;
  warnings: EvidenceWarning[];
  redaction: EvidenceRedaction;
  watermark: EvidenceWatermark;
};

export type EvidenceTimelineLane =
  | "version"
  | "authority"
  | "mutation"
  | "quality"
  | "process"
  | "cost"
  | "control";

export type EvidenceTimelineItem = {
  id: string;
  lane: EvidenceTimelineLane;
  node_id: string;
  type: string;
  label: string;
  summary: string | null;
  status: string | null;
  confidence: "strict" | "inferred" | "none";
  edge_ids: string[];
  payload: Record<string, unknown>;
  provenance: EvidenceProvenanceRef[];
  created_at: string | null;
  updated_at: string | null;
};

export type EvidenceTimeline = {
  schema_version: number;
  agent: Record<string, unknown>;
  items: EvidenceTimelineItem[];
  warnings: EvidenceWarning[];
  redaction: EvidenceRedaction;
  watermark: EvidenceWatermark;
};

export type EvidenceTimelineOptions = {
  limit?: number;
  lane?: EvidenceTimelineLane;
  head_sha?: string;
  skill_name?: string;
  event_type?: string;
  status?: string;
  severity?: string;
  include_inferred?: boolean;
};

export type AgentCustomDomain = {
  id: number;
  agent_name: string;
  hostname: string;
  status: "pending" | "verified" | "active" | string;
  url: string | null;
  verification_record_name: string;
  verification_record_value: string;
  routing_record_type: string;
  routing_record_name: string;
  routing_record_value: string;
  routing_fallback_record_type: string | null;
  routing_fallback_record_name: string | null;
  routing_fallback_record_value: string | null;
  routing_fallback_reason: string | null;
  dns_setup_kind: string;
  is_apex: boolean;
  paired_www_hostname: string | null;
  canonical_hostname: string | null;
  redirect_enabled: boolean;
  redirect_target_url: string | null;
  certificate_status: string;
  verified_at: string | null;
  created_at: string;
  updated_at: string;
  last_error: string | null;
};

export type AgentMailbox = {
  agent_name: string;
  address: string;
  status: "pending" | "provisioning" | "ready" | "disabled" | "failed" | string;
  quota_bytes: number | null;
  allowed_senders: string[];
  daily_send_limit: number | null;
  outbound_count: number;
  disabled_at: string | null;
  created_at: string;
  updated_at: string;
};

export type AgentSecret = {
  id: number;
  agent_name: string;
  key: string;
  value_redacted: string;
  created_at: string;
  updated_at: string;
};

export type AgentCallLog = {
  id: string;
  source: "handoff" | "trial" | "proof" | string;
  agent_name: string;
  skill_name: string;
  status: string;
  badge: string | null;
  summary: string | null;
  error: string | null;
  grant_id: string | null;
  args_preview: Record<string, unknown>;
  result_preview: Record<string, unknown>;
  events: Record<string, unknown>[];
  file_ops: SubagentFileOp[];
  elapsed_ms: number | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  metadata: Record<string, unknown>;
};

export type MyAgentListing = AgentListing & {
  repo_url: string;
  openapi_source: {
    url: string;
    urls: string[];
    regenerable: boolean;
  } | null;
  code_editor: {
    target_agent_name: string;
    enabled: boolean;
    status: string;
    shared_agent_name: string;
    target_repo_url: string | null;
    workspace_key: string | null;
    last_error: string | null;
    enabled_at: string | null;
    updated_at: string | null;
    runtime: Record<string, unknown>;
  };
  self_healing: {
    enabled: boolean;
    consecutive_failures?: number;
    window_seconds?: number;
    cooldown_seconds?: number;
    max_repairs_per_day?: number;
    max_turns?: number;
    deployment_timeout_seconds?: number;
    require_tests?: boolean;
  };
  runtime_upgrade: {
    package: string;
    current_version: string | null;
    latest_version: string;
    update_available: boolean;
    can_redeploy: boolean;
    message: string;
  };
  latest_deployment: AgentDeployment | null;
};

export type AgentMineSummary = Pick<
  AgentListing,
  "id" | "name" | "description" | "version" | "public" | "status" | "url" | "created_at"
> & {
  skill_count: number;
  runtime_upgrade: MyAgentListing["runtime_upgrade"];
  latest_deployment: AgentDeployment | null;
};

export type AgentTemplateUpdateResult = {
  agent_name: string;
  job_id: string;
  status: string;
  update_policy: string;
  template_lineage: TemplateLineage;
  message: string;
};

type AgentSelfHealingEvent = {
  event_id?: string;
  event_type?: string;
  status?: string | null;
  message?: string | null;
  payload?: Record<string, unknown>;
  created_at?: string;
};

export type AgentSelfHealingRun = {
  job_id: string;
  status: string;
  title?: string | null;
  summary?: string | null;
  error?: string | null;
  payload?: Record<string, unknown>;
  result?: Record<string, unknown>;
  created_at?: string;
  completed_at?: string | null;
  events: AgentSelfHealingEvent[];
};

export type AgentSelfHealingHistory = {
  agent_name: string;
  policy: MyAgentListing["self_healing"];
  runs: AgentSelfHealingRun[];
};

export type SubagentFileOp = {
  op: "create" | "update" | "delete" | string;
  path: string;
  size: number;
  content_type?: string | null;
};

type SubagentRunEvent = {
  id: number;
  event_type: string;
  payload: Record<string, unknown>;
  created_at: string;
};

export type SubagentHandoff = {
  id: string;
  from_agent: string | null;
  to_agent: string;
  skill: string;
  grant_id: string;
  status: string;
  summary: string | null;
  scopes: Record<string, unknown>;
  args_preview: Record<string, unknown>;
  created_at: string | null;
  completed_at: string | null;
};

export type SubagentGrant = {
  id: number;
  grant_id: string;
  parent_grant_id: string | null;
  issuer: string;
  audience: string;
  bucket: string;
  mode: string;
  allow_patterns: string[];
  deny_patterns: string[];
  outputs_prefix: string | null;
  ttl_seconds: number;
  decision: string;
  decided_by: string;
  reason: string | null;
  created_at: string;
};

export type SubagentReceipt = {
  id: string;
  kind: "receipt" | "replay" | string;
  label: string;
  event_id: number | null;
  receipt_id: string | null;
  agent_name: string | null;
  skill_name: string | null;
  task_id: string | null;
  status: string | null;
  signed_token: string | null;
  payload: Record<string, unknown>;
  created_at: string | null;
};

export type SubagentRun = {
  grant_id: string;
  rerun_of_grant_id: string | null;
  thread_id: string | null;
  agent_name: string;
  skill_name: string;
  args_json: string;
  scopes: HandoffScopes;
  status: string;
  summary: string | null;
  file_ops: SubagentFileOp[];
  file_ops_count?: number;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  original_request?: Record<string, unknown> | null;
  handoffs?: SubagentHandoff[];
  grants?: SubagentGrant[];
  receipts?: SubagentReceipt[];
  events: SubagentRunEvent[];
};

export type LLMUsage = {
  id: number;
  thread_id: string | null;
  dag_run_id: string | null;
  grant_id: string | null;
  agent_name: string | null;
  skill_name: string | null;
  source: string;
  provider: string | null;
  model: string;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cost_usd: number;
  status: string;
  metadata: Record<string, unknown>;
  created_at: string;
};

type MetaRunPlanNode = {
  id: string;
  agent: string;
  skill: string;
  deps: string[];
  args: Record<string, unknown>;
  expected_outputs: string[];
  [key: string]: unknown;
};

type MetaRunPlan = {
  ok?: boolean | null;
  goal?: string | null;
  max_nodes?: number | null;
  max_parallel?: number | null;
  rounds: string[][];
  nodes: MetaRunPlanNode[];
  [key: string]: unknown;
};

export type MetaAgentRun = {
  run_id: string;
  agent_name: string;
  thread_id: string | null;
  goal: string;
  success_criteria: string[];
  status: string;
  current_plan: MetaRunPlan;
  progress: Array<Record<string, unknown>>;
  state: Record<string, unknown>;
  summary: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
};

export type Page<T> = {
  items: T[];
  next_cursor: string | null;
  has_more?: boolean;
  total?: number | null;
};

export type WorkEvent = {
  id: number;
  job_id: string;
  event_id: string;
  event_type: string;
  type?: string;
  event_seq?: number | null;
  status?: string | null;
  stage?: string | null;
  severity?: string | null;
  message?: string | null;
  payload: Record<string, unknown>;
  created_at: string;
  [key: string]: unknown;
};

export type WorkJob = {
  id: number;
  job_id: string;
  kind: string;
  type?: string;
  title?: string | null;
  status:
    | "queued"
    | "running"
    | "complete"
    | "error"
    | "waiting"
    | "succeeded"
    | "failed"
    | "canceled"
    | "cancelled"
    | string;
  summary: string | null;
  error: string | null;
  thread_id: string | null;
  root_job_id?: string | null;
  parent_job_id?: string | null;
  correlation_id?: string | null;
  grant_id?: string | null;
  dag_run_id?: string | null;
  agent_name?: string | null;
  skill_name?: string | null;
  args_preview?: Record<string, unknown>;
  result_preview?: Record<string, unknown>;
  file_ops?: SubagentFileOp[];
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  completed_at: string | null;
  events?: WorkEvent[];
  [key: string]: unknown;
};

export type ReviewLoopRequest = {
  reviewer_agent_name?: string;
  ref?: string | null;
  head_sha?: string | null;
  loop_budget_cents?: number;
  budget_ceiling_cents?: number;
  ttl_seconds?: number;
  max_iterations?: number;
  canary_plan?: string | null;
  rollback_plan?: string | null;
};

export type ReviewLoop = {
  job: WorkJob;
  events: WorkEvent[];
};

export type CustomKernelSimulationRunRequest = {
  spec?: Record<string, unknown>;
  template_id?: string | null;
  cost_cents?: number;
};

export type CustomKernelSimulationTemplate = {
  template_id: string;
  name: string;
  kind: string;
  risk_class: string;
  description: string;
  required_node_types: string[];
  required_port_types: string[];
  edge_types: string[];
  required_capabilities: string[];
  minted_capabilities: string[];
  consumed_signals: string[];
  emitted_signals: string[];
  proposed_rewrites: string[];
  required_policies: string[];
  budgets: Record<string, number>;
  child_templates: string[];
  exit_conditions: string[];
  ledger_requirements: string[];
  invariants: string[];
  gates: string[];
  simulation_only: boolean;
  proposal_only: boolean;
  active_apply_enabled: boolean;
  spec: Record<string, unknown>;
};

export type CustomKernelSuiteRunRequest = {
  suite?: Record<string, unknown>;
  template_id?: string | null;
  cost_cents?: number;
};

export type CustomKernelSuiteTemplate = {
  template_id: string;
  name: string;
  kind: string;
  risk_class: string;
  description: string;
  ledger_requirements: string[];
  invariants: string[];
  gates: string[];
  suite: Record<string, unknown>;
  simulation_only: boolean;
  proposal_only: boolean;
  active_apply_enabled: boolean;
};

export type ProtocolSimulation = {
  job: WorkJob;
  events: WorkEvent[];
};

export type UserKernelSimulationTemplate = {
  template_id: string;
  name: string;
  simulation_type: string;
  description: string;
  simulation_only: boolean;
  proposal_only: boolean;
  active_apply_enabled: boolean;
  spec: Record<string, unknown>;
};

export type UserKernelSimulationRunRequest = {
  spec?: Record<string, unknown>;
  template_id?: string | null;
  cost_cents?: number;
  thread_id?: string | null;
  idempotency_key?: string | null;
  execution_mode?: "bounded" | "hybrid";
  max_live_calls?: number;
};

export type UserKernelSimulationReplay = {
  job: WorkJob;
  replay_passed: boolean;
  original_summary: Record<string, unknown>;
  replay_summary: Record<string, unknown>;
};

export type KernelEvolutionRunRequest = {
  title?: string;
  base_template_id?: string | null;
  base_spec?: Record<string, unknown> | null;
  participants?: unknown[] | null;
  strategy?: "bounded_score_shift" | "multi_agent_topology" | string | null;
  simulation_type?: string | null;
  seed?: string | null;
  variant_count?: number;
  max_score_delta?: number;
  thread_id?: string | null;
  idempotency_key?: string | null;
};

export type KernelEvolutionRun = {
  job: WorkJob;
  events: WorkEvent[];
};

export type KernelEvolutionReplay = {
  job: WorkJob;
  replay_passed: boolean;
  original_summary: Record<string, unknown>;
  replay_summary: Record<string, unknown>;
};

type DagNodeWire = {
  id: string;
  agent: string;
  skill: string;
  deps: string[];
  args: Record<string, unknown>;
  expected_outputs?: string[];
};

type DagRunNode = {
  node_id: string;
  agent_name: string;
  skill_name: string;
  deps: string[];
  args_json: string;
  status: string;
  summary: string | null;
  result: Record<string, unknown>;
  grant_id: string | null;
  file_ops: SubagentFileOp[];
  elapsed_ms: number | null;
  started_at: string | null;
  completed_at: string | null;
};

export type DagRun = {
  dag_run_id: string;
  thread_id: string | null;
  goal: string;
  status: string;
  summary: string | null;
  nodes_json: DagNodeWire[];
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  nodes: DagRunNode[];
};

export type AgentProofRun = {
  id: number;
  agent_name: string;
  skill_name: string;
  grant_id: string | null;
  status: string;
  badge: "verified" | "degraded" | "unverified" | string;
  summary: string | null;
  error: string | null;
  args_preview: Record<string, unknown>;
  result: Record<string, unknown>;
  events: Record<string, unknown>[];
  file_ops: SubagentFileOp[];
  events_count?: number;
  file_ops_count?: number;
  card_hash: string | null;
  repo_url: string | null;
  head_sha: string | null;
  image: string | null;
  agent_url: string | null;
  elapsed_ms: number | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
};

/**
 * One proof run as the *public* proof surfaces publish it —
 * `PublicAgentProofOut` in control-plane/control_plane/routes/agent_proofs.py.
 *
 * `/v1/public/agent-proofs` answers with a union decided per agent: this shape
 * for anyone who is not an evidence reader of that agent, the full
 * `AgentProofRun` for those who are. The marketplace grid is almost entirely
 * agents the signed-in user does not own, so it must be typed as this arm —
 * the extra fields an owner receives for their own agents are still on the
 * wire, they are simply not something a listing may assume.
 *
 * Note the differences that used to be silent runtime crashes: the id is
 * `proof_id`, not `id`; the counts are `events_count` / `file_ops_count`, and
 * there is no `file_ops` array to take `.length` of.
 */
type PublicAgentProofRun = {
  proof_id: number;
  agent_name: string;
  agent_description: string;
  agent_version: string;
  skill_name: string;
  skill_description: string | null;
  status: string;
  badge: "verified" | "degraded" | "unverified" | string;
  /** Derived by the control plane from status and the counts, never the stored summary. */
  summary: string;
  events_count: number;
  file_ops_count: number;
  card_hash: string | null;
  head_sha: string | null;
  image: string | null;
  agent_url: string | null;
  elapsed_ms: number | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
};

export type PublicAgentProofSummary = {
  agent_name: string;
  badge: "verified" | "degraded" | "unverified" | string;
  latest: PublicAgentProofRun | null;
  total_runs: number;
  passed_runs: number;
  failed_runs: number;
};

export type ControlPolicy = {
  monthly_budget_cents: number;
  run_budget_cents: number;
  max_agent_calls_per_run: number;
  require_approval_for_file_writes: boolean;
  deny_external_network: boolean;
  only_approved_agents: boolean;
  pii_safe_mode: boolean;
  approved_agents: string[];
};

export type ControlSummary = {
  monthly_spend_cents: number;
  monthly_budget_cents: number;
  run_budget_cents: number;
  llm_calls_month: number;
  llm_tokens_month: number;
  agent_runs: number;
  dag_runs: number;
  failures: number;
  files_touched: number;
};

export type ControlTimelineItem = {
  id: string;
  source: "subagent" | "dag" | "proof" | "trial" | "deployment" | "llm" | string;
  title: string;
  status: string;
  summary: string | null;
  agent_name: string | null;
  skill_name: string | null;
  cost_cents: number;
  token_count: number;
  file_ops: SubagentFileOp[];
  receipt_path: string;
  created_at: string;
  completed_at: string | null;
  metadata: Record<string, unknown>;
};

export type ControlRoom = {
  policy: ControlPolicy;
  summary: ControlSummary;
  timeline: ControlTimelineItem[];
};

export type ControlReceipt = {
  receipt_id: string;
  source: string;
  subject: string;
  status: string;
  summary: string | null;
  created_at: string;
  completed_at: string | null;
  ids: Record<string, unknown>;
  scopes: Record<string, unknown>;
  args_preview: Record<string, unknown>;
  result_preview: Record<string, unknown>;
  file_ops: SubagentFileOp[];
  events: Record<string, unknown>[];
  costs: Record<string, unknown>;
  metadata: Record<string, unknown>;
};

export type AgentScheduleTarget = "main_agent" | "agent";

export type AgentSchedule = {
  schedule_id: string;
  name: string;
  enabled: boolean;
  cron: string;
  timezone: string;
  target_type: AgentScheduleTarget;
  prompt: string | null;
  agent_name: string | null;
  skill_name: string | null;
  args_json: string;
  next_run_at: string | null;
  last_run_at: string | null;
  last_run_status: string | null;
  last_run_job_id: string | null;
  last_error: string | null;
  run_count: number;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type AgentScheduleInput = {
  name: string;
  enabled: boolean;
  cron: string;
  timezone: string;
  target_type: AgentScheduleTarget;
  prompt?: string | null;
  agent_name?: string | null;
  skill_name?: string | null;
  args_json?: string;
  metadata?: Record<string, unknown>;
};

export type ChatMessage = { role: "user" | "assistant" | "system"; content: string };
export type MainLlmSource = "platform" | "user";

export type HandoffScopes = {
  bucket: string;
  mode: string;
  allow_patterns: string[];
  deny_patterns: string[];
  outputs_prefix: string | null;
  write_prefixes: string[];
  ttl_seconds: number;
};

export type ScopeRequested = {
  read_patterns: string[];
  write_prefix: string | null;
  write_prefixes: string[];
  mode: string;
  ttl_seconds: number;
};

export type ScopeOriginal = {
  allow_patterns: string[];
  outputs_prefix: string | null;
  write_prefixes: string[];
  mode: string | null;
};

export type ScopeProposed = {
  allow_patterns: string[];
  outputs_prefix: string | null;
  write_prefixes: string[];
  mode: string;
  ttl_seconds: number;
};

export type ChatEvent =
  | { type: "delta"; content: string }
  | { type: "final"; content: string }
  | { type: "tool_call"; id: string; tool: string; args_preview: Record<string, unknown> }
  | { type: "tool_result"; id: string; tool: string; ok: boolean; summary: string }
  | {
      type: "dag_started";
      dag_run_id: string;
      goal: string;
      nodes: DagNodeWire[];
    }
  | {
      type: "dag_node_started";
      dag_run_id: string;
      node_id: string;
      agent: string;
      skill: string;
      deps: string[];
      args_preview: Record<string, unknown>;
    }
  | {
      type: "dag_node_complete";
      dag_run_id: string;
      node_id: string;
      agent: string;
      skill: string;
      ok: boolean;
      grant_id: string | null;
      summary: string;
      result: Record<string, unknown>;
      elapsed_ms: number;
      file_ops?: SubagentFileOp[];
    }
  | {
      type: "dag_node_skipped";
      dag_run_id: string;
      node_id: string;
      agent: string;
      skill: string;
      ok: false;
      grant_id: null;
      summary: string;
      result: Record<string, unknown>;
      elapsed_ms: number;
      file_ops?: SubagentFileOp[];
    }
  | {
      type: "dag_complete";
      dag_run_id: string;
      ok: boolean;
      summary: string;
    }
  | {
      type: "review_loop_event";
      job_id: string;
      agent: string;
      event_type: string;
      status?: string | null;
      severity?: string | null;
      message?: string | null;
      payload?: Record<string, unknown>;
    }
  | {
      type: "evidence_event";
      evidence_key: string;
      evidence_kind: string;
      event_type: string;
      title: string;
      status?: string | null;
      severity?: string | null;
      message?: string | null;
      source?: Record<string, unknown>;
      payload: Record<string, unknown>;
    }
  | {
      type: "agent_handoff";
      from: string;
      to: string;
      skill: string;
      grant_id: string;
      scopes: HandoffScopes;
      args_preview: Record<string, unknown>;
    }
  | {
      type: "agent_setup_required";
      agent: string;
      skill: string;
      setup: ConsumerSetupStatus;
      missing_required: string[];
    }
  | {
      type: "approval_required";
      approval_id: string;
      handoff: {
        from: string;
        to: string;
        skill: string;
        grant_id: string;
        scopes: HandoffScopes;
        args_preview: Record<string, unknown>;
      };
    }
  | {
      type: "agent_progress";
      grant_id: string;
      kind: string;
      payload: Record<string, unknown>;
    }
  | {
      type: "agent_question";
      grant_id: string;
      question_id: string;
      prompt: string;
    }
  | {
      type: "agent_question_answered";
      grant_id: string;
      question_id: string;
      answer: string;
    }
  | {
      type: "agent_input_request";
      grant_id: string;
      request_id: string;
      title: string;
      reason: string;
      schema: JsonSchema;
      ui_schema: Record<string, unknown>;
    }
  | {
      type: "agent_input_submitted";
      grant_id: string;
      request_id: string;
      ok: boolean;
      value_preview: Record<string, unknown>;
    }
  | {
      type: "agent_input_timeout";
      grant_id: string;
      request_id: string;
    }
  | {
      type: "scope_request";
      grant_id: string;
      request_id: string;
      reason: string;
      requested: ScopeRequested;
      original_scopes: ScopeOriginal;
      decision: "auto_approve" | "ask_user" | "deny";
    }
  | {
      type: "scope_approval_required";
      grant_id: string;
      request_id: string;
      approval_id: string;
      reason: string;
      policy_reason: string;
      requested: ScopeRequested;
      original_scopes: ScopeOriginal;
      proposed_grant: ScopeProposed;
    }
  | {
      type: "scope_grant";
      grant_id: string;
      request_id: string;
      new_grant_id: string;
      ok: boolean;
      decided_by: string;
      scopes: ScopeProposed;
    }
  | {
      type: "scope_denied";
      grant_id: string;
      request_id: string;
      reason: string;
      decided_by: string;
    }
  | {
      type: "handoff_complete";
      grant_id: string;
      ok: boolean;
      summary: string;
      file_ops: SubagentFileOp[];
    }
  | { type: "handoff_denied"; grant_id: string }
  | {
      type: "thread";
      id: string;
      title: string;
      is_new: boolean;
      settings?: ChatThreadSettings;
      job_id?: string;
    }
  | { type: "error"; message: string };

// ---------- Chat threads ----------

export type ChatThread = {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  settings?: ChatThreadSettings;
};

export type ChatThreadSettings = {
  run_budget_cents?: number;
  max_agent_calls_per_run?: number;
  require_approval_for_file_writes?: boolean;
  deny_external_network?: boolean;
  only_approved_agents?: boolean;
  pii_safe_mode?: boolean;
  approved_agents?: string[];
  /** Set by the control plane for threads created from inbound email. */
  source?: string;
  remote_address?: string;
  mailbox_address?: string;
  agent_name?: string;
};

export type ThreadMessages = {
  id: string;
  title?: string;
  settings?: ChatThreadSettings;
  messages: { role: "user" | "assistant" | "system"; content: string }[];
};

export type ThreadActivity = {
  id: string;
  title?: string;
  events: ChatEvent[];
  last_seq?: number | null;
};

export function listThreads(): Promise<ChatThread[]>;
export function listThreads(opts: {
  cursor?: string | null;
  limit?: number;
}): Promise<Page<ChatThread>>;
export async function listThreads(opts?: {
  cursor?: string | null;
  limit?: number;
}): Promise<ChatThread[] | Page<ChatThread>> {
  const suffix = opts ? querySuffix({
    page: true,
    cursor: opts.cursor,
    limit: opts.limit,
  }) : "";
  const r = await fetch(`/v1/me/threads${suffix}`, { headers: authHeader() });
  const out = await jsonOrThrow<ChatThread[] | Page<ChatThread>>(r);
  return opts ? toPage(out) : (Array.isArray(out) ? out : out.items);
}

export async function updateThreadSettings(id: string, settings: ChatThreadSettings): Promise<ChatThread> {
  const r = await fetch(`/v1/me/threads/${id}`, {
    method: "PATCH",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify({ settings }),
  });
  return jsonOrThrow<ChatThread>(r);
}

export async function deleteThread(id: string): Promise<void> {
  const r = await fetch(`/v1/me/threads/${id}`, {
    method: "DELETE", headers: authHeader(),
  });
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
}

export async function getThreadMessages(id: string): Promise<ThreadMessages> {
  const r = await fetch(`/v1/me/threads/${id}/messages`, { headers: authHeader() });
  return jsonOrThrow<ThreadMessages>(r);
}

export async function getThreadActivity(
  id: string,
  opts: { after_seq?: number | null; limit?: number } = {},
): Promise<ThreadActivity> {
  const suffix = querySuffix({
    after_seq: opts.after_seq,
    limit: opts.limit,
  });
  const r = await fetch(`/v1/me/threads/${id}/activity${suffix}`, { headers: authHeader() });
  return jsonOrThrow<ThreadActivity>(r);
}

export async function listActivity(opts: {
  cursor?: string | null;
  limit?: number;
  status?: string;
  type?: string;
  kind?: string;
  source?: string;
  thread_id?: string;
  agent?: string;
  grant?: string;
  q?: string;
} = {}): Promise<Page<WorkJob>> {
  const suffix = querySuffix({
    cursor: opts.cursor,
    limit: opts.limit,
    status: opts.status,
    type: opts.type,
    kind: opts.kind,
    source: opts.source,
    thread_id: opts.thread_id,
    agent: opts.agent,
    grant: opts.grant,
    q: opts.q,
  });
  const r = await fetch(`/v1/me/activity${suffix}`, { headers: authHeader() });
  return toPage(await jsonOrThrow<Page<WorkJob> | WorkJob[]>(r));
}

export async function getJob(jobId: string): Promise<WorkJob> {
  const r = await fetch(`/v1/me/jobs/${encodeURIComponent(jobId)}`, {
    headers: authHeader(),
  });
  return jsonOrThrow<WorkJob>(r);
}

export async function cancelJob(jobId: string, reason?: string): Promise<WorkJob> {
  const r = await fetch(`/v1/me/jobs/${encodeURIComponent(jobId)}/cancel`, {
    method: "POST",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify({ reason: reason || "canceled from dashboard" }),
  });
  return jsonOrThrow<WorkJob>(r);
}

export async function listJobEvents(
  jobId: string,
  opts: {
    cursor?: string | null;
    limit?: number;
    after_id?: string | number | null;
  } = {},
): Promise<Page<WorkEvent>> {
  const cursor = opts.cursor ?? (
    opts.after_id === undefined || opts.after_id === null
      ? undefined
      : String(opts.after_id)
  );
  const suffix = querySuffix({
    cursor,
    limit: opts.limit,
  });
  const r = await fetch(
    `/v1/me/jobs/${encodeURIComponent(jobId)}/events${suffix}`,
    { headers: authHeader() },
  );
  return toPage(await jsonOrThrow<Page<WorkEvent> | WorkEvent[]>(r));
}

export async function listSubagentRuns(opts: {
  agent?: string;
  thread_id?: string;
  limit?: number;
} = {}): Promise<SubagentRun[]> {
  const qs = new URLSearchParams();
  if (opts.agent) qs.set("agent", opts.agent);
  if (opts.thread_id) qs.set("thread_id", opts.thread_id);
  if (opts.limit) qs.set("limit", String(opts.limit));
  const suffix = qs.toString() ? `?${qs}` : "";
  const r = await fetch(`/v1/me/subagent-runs${suffix}`, { headers: authHeader() });
  return jsonOrThrow<SubagentRun[]>(r);
}

export async function getSubagentRun(grantId: string): Promise<SubagentRun> {
  const r = await fetch(`/v1/me/subagent-runs/${grantId}`, { headers: authHeader() });
  return jsonOrThrow<SubagentRun>(r);
}

export async function rerunSubagentRun(grantId: string): Promise<SubagentRun> {
  const r = await fetch(`/v1/me/subagent-runs/${grantId}/rerun`, {
    method: "POST",
    headers: authHeader(),
  });
  return jsonOrThrow<SubagentRun>(r);
}

export async function listLlmUsage(opts: {
  thread_id?: string;
  grant_id?: string;
  dag_run_id?: string;
  agent?: string;
  limit?: number;
} = {}): Promise<LLMUsage[]> {
  const qs = new URLSearchParams();
  if (opts.thread_id) qs.set("thread_id", opts.thread_id);
  if (opts.grant_id) qs.set("grant_id", opts.grant_id);
  if (opts.dag_run_id) qs.set("dag_run_id", opts.dag_run_id);
  if (opts.agent) qs.set("agent", opts.agent);
  if (opts.limit) qs.set("limit", String(opts.limit));
  const suffix = qs.toString() ? `?${qs}` : "";
  const r = await fetch(`/v1/me/llm-usage${suffix}`, { headers: authHeader() });
  return jsonOrThrow<LLMUsage[]>(r);
}

export async function listDagRuns(opts: {
  thread_id?: string;
  limit?: number;
} = {}): Promise<DagRun[]> {
  const qs = new URLSearchParams();
  if (opts.thread_id) qs.set("thread_id", opts.thread_id);
  if (opts.limit) qs.set("limit", String(opts.limit));
  const suffix = qs.toString() ? `?${qs}` : "";
  const r = await fetch(`/v1/me/dag-runs${suffix}`, { headers: authHeader() });
  return jsonOrThrow<DagRun[]>(r);
}

export async function listAgentProofs(opts: {
  agent?: string;
  limit?: number;
  compact?: boolean;
} = {}): Promise<AgentProofRun[]> {
  const qs = new URLSearchParams();
  if (opts.agent) qs.set("agent", opts.agent);
  if (opts.limit) qs.set("limit", String(opts.limit));
  if (opts.compact) qs.set("compact", "true");
  const suffix = qs.toString() ? `?${qs}` : "";
  const r = await fetch(`/v1/me/agent-proofs${suffix}`, { headers: authHeader() });
  return jsonOrThrow<AgentProofRun[]>(r);
}

export async function runAgentProof(
  agentName: string,
  input: { skill_name?: string; args?: Record<string, unknown>; args_json?: string },
): Promise<AgentProofRun> {
  const r = await fetch(`/v1/me/agent-proofs/${encodeURIComponent(agentName)}/run`, {
    method: "POST",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify(input),
  });
  return jsonOrThrow<AgentProofRun>(r);
}

/**
 * The public proof feed, typed as the arm a listing may rely on.
 *
 * This request *is* authenticated: dashboard auth is the httpOnly
 * `__Host-a2a_session` cookie, carried automatically on this same-origin `/v1`
 * path, so `optional_current_user` resolves the signed-in user and their own
 * agents come back in full. Adding an `Authorization` header would change
 * nothing — `authHeader()` returns `{}`. What it cannot do is widen the
 * entries for the agents the user does not own, which is most of the grid.
 * Read those through `PublicAgentProofRun`, or use `listMyAgentProofs` when
 * the full record is what you need.
 */
export async function listPublicAgentProofs(opts: {
  limit?: number;
  compact?: boolean;
} = {}): Promise<PublicAgentProofSummary[]> {
  const qs = new URLSearchParams();
  if (opts.limit) qs.set("limit", String(opts.limit));
  if (opts.compact ?? true) qs.set("compact", "true");
  const suffix = qs.toString() ? `?${qs}` : "";
  const r = await fetch(`/v1/public/agent-proofs${suffix}`);
  return jsonOrThrow<PublicAgentProofSummary[]>(r);
}

type ReceiptVerifyingKey = {
  kid: string;
  alg: string;
  /** Base64 of the raw 32-byte Ed25519 public key. */
  public_key: string;
  use: string;
};

export type ReceiptKeys = {
  active_kid: string;
  keys: ReceiptVerifyingKey[];
};

/**
 * The published receipt verifying key(s). Unauthenticated by design: anyone
 * holding a receipt token must be able to check it. 503 when the control plane
 * has no signing key configured — it never invents one.
 */
export async function getReceiptKeys(): Promise<ReceiptKeys> {
  const r = await fetch("/v1/public/receipt-keys");
  return jsonOrThrow<ReceiptKeys>(r);
}

export async function getControlRoom(options: { limit?: number } = {}): Promise<ControlRoom> {
  const qs = new URLSearchParams();
  if (options.limit) qs.set("limit", String(options.limit));
  const suffix = qs.toString() ? `?${qs}` : "";
  const r = await fetch(`/v1/me/control-room${suffix}`, { headers: authHeader() });
  return jsonOrThrow<ControlRoom>(r);
}

export async function updateControlPolicy(
  input: Partial<ControlPolicy>,
): Promise<ControlPolicy> {
  const r = await fetch("/v1/me/control-room/policy", {
    method: "PATCH",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify(input),
  });
  return jsonOrThrow<ControlPolicy>(r);
}

export async function getControlReceipt(path: string): Promise<ControlReceipt> {
  const r = await fetch(`/v1/me/control-room/receipts/${pathParam(path)}`, {
    headers: authHeader(),
  });
  return jsonOrThrow<ControlReceipt>(r);
}

export async function listSchedules(): Promise<AgentSchedule[]> {
  const r = await fetch("/v1/me/schedules", { headers: authHeader() });
  return jsonOrThrow<AgentSchedule[]>(r);
}

export async function createSchedule(
  input: AgentScheduleInput,
): Promise<AgentSchedule> {
  const r = await fetch("/v1/me/schedules", {
    method: "POST",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify(input),
  });
  return jsonOrThrow<AgentSchedule>(r);
}

export async function updateSchedule(
  scheduleId: string,
  input: Partial<AgentScheduleInput>,
): Promise<AgentSchedule> {
  const r = await fetch(`/v1/me/schedules/${encodeURIComponent(scheduleId)}`, {
    method: "PATCH",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify(input),
  });
  return jsonOrThrow<AgentSchedule>(r);
}

export async function runScheduleNow(scheduleId: string): Promise<AgentSchedule> {
  const r = await fetch(
    `/v1/me/schedules/${encodeURIComponent(scheduleId)}/run`,
    { method: "POST", headers: authHeader() },
  );
  return jsonOrThrow<AgentSchedule>(r);
}

export async function deleteSchedule(scheduleId: string): Promise<void> {
  const r = await fetch(`/v1/me/schedules/${encodeURIComponent(scheduleId)}`, {
    method: "DELETE",
    headers: authHeader(),
  });
  await noContentOrThrow(r);
}

export async function approveHandoff(
  approvalId: string,
  decision: "approve" | "deny",
): Promise<void> {
  const r = await fetch(`/v1/me/chat/approvals/${approvalId}`, {
    method: "POST",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify({ decision }),
  });
  await noContentOrThrow(r);
}

export async function answerQuestion(
  questionId: string,
  answer: string,
): Promise<void> {
  const r = await fetch(`/v1/me/chat/questions/${questionId}`, {
    method: "POST",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify({ answer }),
  });
  await noContentOrThrow(r);
}

export type JsonSchema = {
  type?: string | string[];
  title?: string;
  description?: string;
  format?: string;
  enum?: unknown[];
  const?: unknown;
  default?: unknown;
  properties?: Record<string, JsonSchema>;
  required?: string[];
  items?: JsonSchema;
  additionalProperties?: boolean | JsonSchema;
  contentMediaType?: string;
  [key: string]: unknown;
};

export async function submitInputRequest(
  requestId: string,
  value: Record<string, unknown>,
): Promise<void> {
  const r = await fetch(`/v1/me/chat/input-requests/${requestId}`, {
    method: "POST",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify({ value }),
  });
  await noContentOrThrow(r);
}

export async function approveScopeRequest(
  approvalId: string,
  decision: "approve" | "deny",
): Promise<void> {
  const r = await fetch(`/v1/me/chat/scope-approvals/${approvalId}`, {
    method: "POST",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify({ decision }),
  });
  await noContentOrThrow(r);
}

export const session = {
  load(): Session | null {
    return memorySession;
  },
  save(s: Session) {
    memorySession = s;
  },
  clear() {
    memorySession = null;
  },
};

function authHeader(): Record<string, string> {
  return {};
}

async function jsonOrThrow<T>(r: Response): Promise<T> {
  if (!r.ok) {
    throw await responseError(r);
  }
  return (await r.json()) as T;
}

async function noContentOrThrow(r: Response): Promise<void> {
  if (!r.ok && r.status !== 204) {
    throw await responseError(r);
  }
}

async function responseError(r: Response): Promise<Error> {
  let detail = r.statusText;
  try {
    const text = await r.text();
    if (text) {
      try {
        const j = JSON.parse(text);
        detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail ?? j);
      } catch {
        detail = text;
      }
    }
  } catch {
    /* ignore */
  }
  return new Error(`${r.status}: ${detail || r.statusText}`);
}

function pathParam(path: string): string {
  return path
    .split("/")
    .map((part) => encodeURIComponent(part))
    .join("/");
}

function querySuffix(params: Record<string, string | number | boolean | null | undefined>): string {
  const qs = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") {
      qs.set(key, String(value));
    }
  }
  return qs.toString() ? `?${qs}` : "";
}

function toPage<T>(out: Page<T> | T[]): Page<T> {
  return Array.isArray(out) ? { items: out, next_cursor: null } : out;
}

function sseData(event: string): string | null {
  const dataLines = event
    .split("\n")
    .filter((l) => l.startsWith("data:"))
    .map((l) => l.slice(5).trimStart());
  return dataLines.length ? dataLines.join("\n") : null;
}

export async function browserSession(): Promise<Session | null> {
  const r = await fetch("/v1/auth/session", { headers: authHeader() });
  const j = await jsonOrThrow<{
    authenticated: boolean;
    user: { id: number; email: string } | null;
  }>(r);
  return j.authenticated && j.user
    ? { id: j.user.id, email: j.user.email, source: "cookie" }
    : null;
}

export function startOidcLogin(redirectTo?: string, loginHint?: string): void {
  const qs = new URLSearchParams();
  if (redirectTo) qs.set("redirect_to", redirectTo);
  if (loginHint) qs.set("login_hint", loginHint);
  window.location.href = `/v1/auth/oidc/start${qs.toString() ? `?${qs}` : ""}`;
}

export async function logout(redirectTo?: string): Promise<{ logout_url?: string | null }> {
  const qs = new URLSearchParams();
  if (redirectTo) qs.set("redirect_to", redirectTo);
  const r = await fetch(`/v1/auth/logout${qs.toString() ? `?${qs}` : ""}`, {
    method: "POST",
    headers: authHeader(),
  });
  return jsonOrThrow<{ authenticated: boolean; logout_url?: string | null }>(r);
}

// ---------- Organizations ----------

export async function listOrganizations(opts?: {
  purpose?: "default" | "chat";
  limit?: number;
}): Promise<Organization[]> {
  const suffix = querySuffix({
    purpose: opts?.purpose && opts.purpose !== "default" ? opts.purpose : undefined,
    limit: opts?.limit,
  });
  const r = await fetch(`/v1/me/organizations${suffix}`, { headers: authHeader() });
  return jsonOrThrow<Organization[]>(r);
}

export async function getServiceAccess(): Promise<ServiceAccess> {
  const r = await fetch("/v1/me/service-access", { headers: authHeader() });
  return jsonOrThrow<ServiceAccess>(r);
}

export async function getFeatureFlags(): Promise<FeatureFlags> {
  const r = await fetch("/v1/me/feature-flags", { headers: authHeader() });
  return jsonOrThrow<FeatureFlags>(r);
}

export async function getOnboardingState(): Promise<OnboardingState> {
  const r = await fetch("/v1/me/onboarding", { headers: authHeader() });
  return jsonOrThrow<OnboardingState>(r);
}

export async function updateOnboardingState(input: {
  current_step?: OnboardingStep;
  llm_key_step_completed?: boolean;
  walkthrough_completed?: boolean;
  completed?: boolean;
  dismissed?: boolean;
  last_seen_step?: string | null;
  tour_step_index?: number;
  tour_step_total?: number;
}): Promise<OnboardingState> {
  const r = await fetch("/v1/me/onboarding", {
    method: "PATCH",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify(input),
  });
  return jsonOrThrow<OnboardingState>(r);
}

export async function createOrganization(input: {
  name: string;
  slug?: string;
}): Promise<Organization> {
  const r = await fetch("/v1/me/organizations", {
    method: "POST",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify(input),
  });
  return jsonOrThrow<Organization>(r);
}

export async function listOrganizationDomains(
  slug: string,
): Promise<OrganizationDomain[]> {
  const r = await fetch(`/v1/me/organizations/${encodeURIComponent(slug)}/domains`, {
    headers: authHeader(),
  });
  return jsonOrThrow<OrganizationDomain[]>(r);
}

export async function addOrganizationDomain(
  slug: string,
  domain: string,
): Promise<OrganizationDomain> {
  const r = await fetch(`/v1/me/organizations/${encodeURIComponent(slug)}/domains`, {
    method: "POST",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify({ domain }),
  });
  return jsonOrThrow<OrganizationDomain>(r);
}

export async function verifyOrganizationDomain(
  slug: string,
  domain: string,
): Promise<OrganizationDomain> {
  const r = await fetch(
    `/v1/me/organizations/${encodeURIComponent(slug)}/domains/${encodeURIComponent(domain)}/verify`,
    {
      method: "POST",
      headers: authHeader(),
    },
  );
  return jsonOrThrow<OrganizationDomain>(r);
}

export async function deleteOrganizationDomain(
  slug: string,
  domain: string,
): Promise<void> {
  const r = await fetch(
    `/v1/me/organizations/${encodeURIComponent(slug)}/domains/${encodeURIComponent(domain)}`,
    {
      method: "DELETE",
      headers: authHeader(),
    },
  );
  if (!r.ok && r.status !== 204) {
    let detail = r.statusText;
    try {
      const j = await r.json();
      detail = j.detail ?? JSON.stringify(j);
    } catch {
      /* ignore */
    }
    throw new Error(`${r.status}: ${detail}`);
  }
}

export async function listOrganizationMembers(
  slug: string,
): Promise<OrganizationMember[]> {
  const r = await fetch(`/v1/me/organizations/${encodeURIComponent(slug)}/members`, {
    headers: authHeader(),
  });
  return jsonOrThrow<OrganizationMember[]>(r);
}

export async function listOrganizationAuditLogs(
  slug: string,
  limit = 100,
): Promise<OrganizationAuditLog[]> {
  const qs = new URLSearchParams({ limit: String(limit) });
  const r = await fetch(
    `/v1/me/organizations/${encodeURIComponent(slug)}/audit-log?${qs}`,
    { headers: authHeader() },
  );
  return jsonOrThrow<OrganizationAuditLog[]>(r);
}

export async function getOrganizationScimConfig(
  slug: string,
): Promise<OrganizationScimConfig> {
  const r = await fetch(`/v1/me/organizations/${encodeURIComponent(slug)}/scim`, {
    headers: authHeader(),
  });
  return jsonOrThrow<OrganizationScimConfig>(r);
}

export async function listOrganizationScimTokens(
  slug: string,
): Promise<OrganizationScimToken[]> {
  const r = await fetch(`/v1/me/organizations/${encodeURIComponent(slug)}/scim/tokens`, {
    headers: authHeader(),
  });
  return jsonOrThrow<OrganizationScimToken[]>(r);
}

export async function createOrganizationScimToken(
  slug: string,
  label: string,
): Promise<OrganizationScimTokenCreated> {
  const r = await fetch(`/v1/me/organizations/${encodeURIComponent(slug)}/scim/tokens`, {
    method: "POST",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify({ label }),
  });
  return jsonOrThrow<OrganizationScimTokenCreated>(r);
}

export async function revokeOrganizationScimToken(
  slug: string,
  tokenId: number,
): Promise<void> {
  const r = await fetch(
    `/v1/me/organizations/${encodeURIComponent(slug)}/scim/tokens/${tokenId}`,
    {
      method: "DELETE",
      headers: authHeader(),
    },
  );
  if (!r.ok && r.status !== 204) {
    let detail = r.statusText;
    try {
      const j = await r.json();
      detail = j.detail ?? JSON.stringify(j);
    } catch {
      /* ignore */
    }
    throw new Error(`${r.status}: ${detail}`);
  }
}

// ---------- Organization compliance ----------

export type ComplianceFramework = "eu_ai_act" | "nist_ai_rmf_1_1" | "iso_42001";

export type CompliancePolicy = {
  frameworks: ComplianceFramework[];
  retention_days: number;
  retention_floor_days: number;
  legal_hold: boolean;
  legal_hold_reason: string | null;
  updated_at: string | null;
  persisted: boolean;
};

export type CompliancePolicyInput = {
  frameworks: ComplianceFramework[];
  retention_days: number;
  legal_hold: boolean;
  legal_hold_reason?: string;
};

type ComplianceRecordCounts = {
  skill_execution: number;
  authorization: number;
  admin_action: number;
};

export type ComplianceStatus = {
  policy: CompliancePolicy;
  retention_ok: boolean;
  enforcement_date: string;
  days_until_enforcement: number;
  record_counts: ComplianceRecordCounts;
  oldest_record_at: string | null;
  agents_total: number;
  agents_classified: number;
  agents_high_risk: number;
};

export type ComplianceDecisionRecordKind =
  | "skill_execution"
  | "authorization"
  | "admin_action";

export type ComplianceDecisionRecord = {
  record_id: string;
  kind: ComplianceDecisionRecordKind;
  recorded_at: string;
  occurred_at: string;
  agent_name: string | null;
  actor: string;
  action: string;
  outcome: string;
  verifiable: boolean;
  details: Record<string, unknown>;
};

export type ComplianceSignatureStatus = "valid" | "invalid" | "not_applicable";

export type ComplianceDecisionRecordDetail = ComplianceDecisionRecord & {
  signed_token: string | null;
  signature_status: ComplianceSignatureStatus;
  payload: Record<string, unknown>;
};

export type ComplianceDecisionRecordFilters = {
  kind?: ComplianceDecisionRecordKind | "";
  agent?: string;
  outcome?: string;
  from_at?: string;
  to_at?: string;
  limit?: number;
};

export type ComplianceRiskTier =
  | "unclassified"
  | "minimal"
  | "limited"
  | "high"
  | "unacceptable";

export type ComplianceAgentClassification = {
  agent_name: string;
  agent_version: string | null;
  risk_tier: ComplianceRiskTier;
  intended_purpose: string | null;
  human_oversight: string | null;
  notes: string | null;
  updated_at: string | null;
};

export type ComplianceAgentClassificationInput = {
  risk_tier: ComplianceRiskTier;
  intended_purpose: string;
  human_oversight: string;
  notes: string;
};

export type ComplianceExportInput = {
  from_at?: string;
  to_at?: string;
  kinds?: ComplianceDecisionRecordKind[];
  max_records?: number;
  verify_signatures?: boolean;
};

export type ComplianceRetentionEnforceResult = {
  cutoff: string;
  receipts_deleted: number;
  admin_actions_deleted: number;
  note: string;
};

function complianceBase(slug: string): string {
  return `/v1/me/organizations/${encodeURIComponent(slug)}/compliance`;
}

export async function getComplianceStatus(slug: string): Promise<ComplianceStatus> {
  const r = await fetch(`${complianceBase(slug)}/status`, { headers: authHeader() });
  return jsonOrThrow<ComplianceStatus>(r);
}

export async function updateCompliancePolicy(
  slug: string,
  input: CompliancePolicyInput,
): Promise<CompliancePolicy> {
  const r = await fetch(`${complianceBase(slug)}/policy`, {
    method: "PUT",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify(input),
  });
  return jsonOrThrow<CompliancePolicy>(r);
}

export async function listComplianceDecisionRecords(
  slug: string,
  filters?: ComplianceDecisionRecordFilters,
): Promise<ComplianceDecisionRecord[]> {
  const suffix = querySuffix({
    kind: filters?.kind,
    agent: filters?.agent,
    outcome: filters?.outcome,
    from_at: filters?.from_at,
    to_at: filters?.to_at,
    limit: filters?.limit,
  });
  const r = await fetch(`${complianceBase(slug)}/decision-records${suffix}`, {
    headers: authHeader(),
  });
  return jsonOrThrow<ComplianceDecisionRecord[]>(r);
}

export async function getComplianceDecisionRecord(
  slug: string,
  kind: ComplianceDecisionRecordKind,
  recordId: string,
): Promise<ComplianceDecisionRecordDetail> {
  const r = await fetch(
    `${complianceBase(slug)}/decision-records/${encodeURIComponent(kind)}/${encodeURIComponent(recordId)}`,
    { headers: authHeader() },
  );
  return jsonOrThrow<ComplianceDecisionRecordDetail>(r);
}

export async function listComplianceAgents(
  slug: string,
): Promise<ComplianceAgentClassification[]> {
  const r = await fetch(`${complianceBase(slug)}/agents`, { headers: authHeader() });
  return jsonOrThrow<ComplianceAgentClassification[]>(r);
}

export async function updateComplianceAgent(
  slug: string,
  agentName: string,
  input: ComplianceAgentClassificationInput,
): Promise<ComplianceAgentClassification> {
  const r = await fetch(
    `${complianceBase(slug)}/agents/${encodeURIComponent(agentName)}`,
    {
      method: "PUT",
      headers: { "content-type": "application/json", ...authHeader() },
      body: JSON.stringify(input),
    },
  );
  return jsonOrThrow<ComplianceAgentClassification>(r);
}

export async function exportComplianceEvidence(
  slug: string,
  input: ComplianceExportInput,
): Promise<Record<string, unknown>> {
  const r = await fetch(`${complianceBase(slug)}/export`, {
    method: "POST",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify(input),
  });
  return jsonOrThrow<Record<string, unknown>>(r);
}

export async function enforceComplianceRetention(
  slug: string,
): Promise<ComplianceRetentionEnforceResult> {
  const r = await fetch(`${complianceBase(slug)}/retention/enforce`, {
    method: "POST",
    headers: authHeader(),
  });
  return jsonOrThrow<ComplianceRetentionEnforceResult>(r);
}

export type ListFilesOptions = {
  limit?: number;
  cursor?: string | null;
};

export type FileListPage = {
  items: FileMeta[];
  nextCursor: string | null;
};

export type WorkspaceFilePresence = {
  has_files: boolean;
  file_count?: number;
  has_more?: boolean;
  next_cursor?: string | null;
  limit?: number;
  status?: "empty" | "ready" | "truncated";
};

export async function listFiles(
  prefix?: string,
  opts: ListFilesOptions = {},
): Promise<FileMeta[]> {
  const params = new URLSearchParams();
  if (prefix !== undefined) params.set("prefix", prefix);
  if (opts.limit) params.set("limit", String(opts.limit));
  if (opts.cursor) params.set("cursor", opts.cursor);
  const url = params.toString() ? `/v1/me/files?${params}` : "/v1/me/files";
  const r = await fetch(url, { headers: authHeader() });
  return jsonOrThrow<FileMeta[]>(r);
}

export async function listFilesPage(
  prefix?: string,
  opts: ListFilesOptions = {},
): Promise<FileListPage> {
  const params = new URLSearchParams();
  if (prefix !== undefined) params.set("prefix", prefix);
  if (opts.limit) params.set("limit", String(opts.limit));
  if (opts.cursor) params.set("cursor", opts.cursor);
  const url = params.toString() ? `/v1/me/files?${params}` : "/v1/me/files";
  const r = await fetch(url, { headers: authHeader() });
  const items = await jsonOrThrow<FileMeta[]>(r);
  return {
    items,
    nextCursor: r.headers.get("x-a2a-next-cursor"),
  };
}

export async function listAllFilesPaged(pageSize = 250): Promise<FileMeta[]> {
  const files: FileMeta[] = [];
  let cursor: string | null = null;
  do {
    const page = await listFilesPage(undefined, {
      limit: pageSize,
      cursor,
    });
    files.push(...page.items);
    cursor = page.nextCursor;
  } while (cursor);
  return files;
}

export async function getWorkspaceFilePresence(limit = 100): Promise<WorkspaceFilePresence> {
  const params = new URLSearchParams();
  params.set("limit", String(limit));
  params.set("summary", "true");
  const r = await fetch(`/v1/me/files?${params}`, {
    headers: authHeader(),
  });
  return jsonOrThrow<WorkspaceFilePresence>(r);
}

export async function hasWorkspaceFiles(): Promise<boolean> {
  const presence = await getWorkspaceFilePresence(1);
  return Boolean(presence.has_files);
}

export async function fetchFile(path: string): Promise<Blob> {
  const r = await fetch(`/v1/me/files/${pathParam(path)}`, {
    headers: authHeader(),
  });
  if (!r.ok) {
    throw await responseError(r);
  }
  return await r.blob();
}

export async function uploadFile(file: File, prefix?: string): Promise<FileMeta> {
  const fd = new FormData();
  fd.append("file", file);
  if (prefix) {
    const clean = prefix.replace(/^\/+|\/+$/g, "");
    fd.append("path", clean ? `${clean}/${file.name}` : file.name);
  }
  const r = await fetch("/v1/me/files", {
    method: "POST",
    headers: authHeader(),
    body: fd,
  });
  return jsonOrThrow<FileMeta>(r);
}

export async function deleteFile(path: string): Promise<void> {
  const r = await fetch(`/v1/me/files/${pathParam(path)}`, {
    method: "DELETE",
    headers: authHeader(),
  });
  await noContentOrThrow(r);
}

export async function moveFile(from: string, to: string): Promise<void> {
  const r = await fetch("/v1/me/files/move", {
    method: "POST",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify({ from, to }),
  });
  if (!r.ok) {
    let detail = r.statusText;
    try {
      const j = await r.json();
      detail = j.detail ?? JSON.stringify(j);
    } catch {
      /* ignore */
    }
    throw new Error(`${r.status}: ${detail}`);
  }
}

// ---------- LLM creds ----------

export async function listLlmCreds(): Promise<LLMCreds[]> {
  const r = await fetch("/v1/me/llm-creds", { headers: authHeader() });
  return jsonOrThrow<LLMCreds[]>(r);
}

export async function listLlmModelCatalog(): Promise<LLMModelCatalog> {
  const r = await fetch("/v1/me/llm-creds/catalog", { headers: authHeader() });
  return jsonOrThrow<LLMModelCatalog>(r);
}

export async function upsertLlmCreds(input: {
  name: string;
  base_url: string;
  api_key: string;
  model: string;
  temperature_mode: "omit" | "default" | "custom";
  temperature?: number | null;
  extra_body?: Record<string, unknown>;
}): Promise<LLMCreds> {
  const r = await fetch("/v1/me/llm-creds", {
    method: "POST",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify(input),
  });
  return jsonOrThrow<LLMCreds>(r);
}

export async function deleteLlmCreds(name: string): Promise<void> {
  const r = await fetch(`/v1/me/llm-creds/${encodeURIComponent(name)}`, {
    method: "DELETE",
    headers: authHeader(),
  });
  await noContentOrThrow(r);
}

// ---------- Marketplace ----------

export async function listAgents(): Promise<AgentListing[]> {
  const r = await fetch("/v1/agents", { headers: authHeader() });
  return jsonOrThrow<AgentListing[]>(r);
}

export async function searchAgents(input: {
  q: string;
  tags?: string[];
  skill?: string;
  limit?: number;
}): Promise<AgentSearchResult[]> {
  const params = new URLSearchParams();
  const q = input.q.trim();
  if (q) params.set("q", q);
  for (const tag of input.tags || []) {
    const clean = tag.trim();
    if (clean) params.append("tag", clean);
  }
  if (input.skill?.trim()) params.set("skill", input.skill.trim());
  if (input.limit) params.set("limit", String(input.limit));
  const qs = params.toString();
  const r = await fetch(`/v1/agents/search${qs ? `?${qs}` : ""}`, {
    headers: authHeader(),
  });
  return jsonOrThrow<AgentSearchResult[]>(r);
}

export async function listPublicAgents(opts: {
  limit?: number;
  cursor?: string | number;
} = {}): Promise<AgentListing[]> {
  const qs = new URLSearchParams();
  if (opts.limit) qs.set("limit", String(opts.limit));
  if (opts.cursor) qs.set("cursor", String(opts.cursor));
  const suffix = qs.toString() ? `?${qs}` : "";
  const r = await fetch(`/v1/public/agents${suffix}`);
  return jsonOrThrow<AgentListing[]>(r);
}

export async function listMyAgents(): Promise<MyAgentListing[]> {
  const r = await fetch("/v1/agents/mine", { headers: authHeader() });
  return jsonOrThrow<MyAgentListing[]>(r);
}

export async function listMyAgentSummaries(): Promise<AgentMineSummary[]> {
  const r = await fetch("/v1/agents/mine/summary", { headers: authHeader() });
  return jsonOrThrow<AgentMineSummary[]>(r);
}

export async function getMyAgent(name: string): Promise<MyAgentListing> {
  const r = await fetch(`/v1/agents/mine/${encodeURIComponent(name)}`, {
    headers: authHeader(),
  });
  return jsonOrThrow<MyAgentListing>(r);
}

export async function updateAgentVisibility(
  name: string,
  isPublic: boolean,
): Promise<MyAgentListing> {
  const r = await fetch(
    `/v1/agents/mine/${encodeURIComponent(name)}/visibility`,
    {
      method: "PATCH",
      headers: { "content-type": "application/json", ...authHeader() },
      body: JSON.stringify({ public: isPublic }),
    },
  );
  return jsonOrThrow<MyAgentListing>(r);
}

// Hosted agents idle to zero pods, so list/detail reads serve the cached card
// and never wake them. This explicitly re-reads the live card — it cold-starts
// the agent on purpose — for an owner who wants the freshest card. The server
// persists the live card back to the DB + cache, so a subsequent list reflects
// it without waking the agent again.
export async function refreshAgentCard(name: string): Promise<void> {
  const r = await fetch(
    `/v1/agents/${encodeURIComponent(name)}?refresh=true`,
    { headers: authHeader() },
  );
  await jsonOrThrow<unknown>(r);
}

export async function listAgentApiTokens(name: string): Promise<AgentApiToken[]> {
  const r = await fetch(`/v1/agents/${encodeURIComponent(name)}/api-tokens`, {
    headers: authHeader(),
  });
  return jsonOrThrow<AgentApiToken[]>(r);
}

export async function createAgentApiToken(
  name: string,
  label: string,
): Promise<AgentApiTokenCreated> {
  const r = await fetch(`/v1/agents/${encodeURIComponent(name)}/api-tokens`, {
    method: "POST",
    headers: { ...authHeader(), "Content-Type": "application/json" },
    body: JSON.stringify({ name: label.trim() || "API token", scopes: ["invoke"] }),
  });
  return jsonOrThrow<AgentApiTokenCreated>(r);
}

export async function revokeAgentApiToken(
  name: string,
  tokenId: number,
): Promise<void> {
  const r = await fetch(
    `/v1/agents/${encodeURIComponent(name)}/api-tokens/${encodeURIComponent(tokenId)}`,
    {
      method: "DELETE",
      headers: authHeader(),
    },
  );
  await noContentOrThrow(r);
}

export async function listAgentIntegrationLinks(
  name: string,
): Promise<AgentIntegrationLink[]> {
  const r = await fetch(`/v1/agents/${encodeURIComponent(name)}/integration-links`, {
    headers: authHeader(),
  });
  return jsonOrThrow<AgentIntegrationLink[]>(r);
}

export async function createAgentIntegrationLink(
  name: string,
  options: { urlToken?: boolean } = {},
): Promise<AgentIntegrationLinkCreated> {
  const r = await fetch(`/v1/agents/${encodeURIComponent(name)}/integration-links`, {
    method: "POST",
    headers: { ...authHeader(), "Content-Type": "application/json" },
    body: JSON.stringify({ url_token: Boolean(options.urlToken) }),
  });
  return jsonOrThrow<AgentIntegrationLinkCreated>(r);
}

export async function revokeAgentIntegrationLink(
  name: string,
  linkId: number,
): Promise<void> {
  const r = await fetch(
    `/v1/agents/${encodeURIComponent(name)}/integration-links/${encodeURIComponent(linkId)}`,
    {
      method: "DELETE",
      headers: authHeader(),
    },
  );
  await noContentOrThrow(r);
}

export async function enableAgentCodeEditor(
  name: string,
): Promise<MyAgentListing> {
  const r = await fetch(`/v1/agents/${encodeURIComponent(name)}/code-editor`, {
    method: "POST",
    headers: authHeader(),
  });
  return jsonOrThrow<MyAgentListing>(r);
}

export async function disableAgentCodeEditor(
  name: string,
): Promise<MyAgentListing> {
  const r = await fetch(`/v1/agents/${encodeURIComponent(name)}/code-editor`, {
    method: "DELETE",
    headers: authHeader(),
  });
  return jsonOrThrow<MyAgentListing>(r);
}

export async function listAgentSelfHealing(
  name: string,
): Promise<AgentSelfHealingHistory> {
  const r = await fetch(`/v1/agents/${encodeURIComponent(name)}/self-healing`, {
    headers: authHeader(),
  });
  return jsonOrThrow<AgentSelfHealingHistory>(r);
}

export async function listInstalledAgents(): Promise<InstalledAgent[]> {
  const r = await fetch("/v1/installed-agents", { headers: authHeader() });
  return jsonOrThrow<InstalledAgent[]>(r);
}

export async function installMarketplaceAgent(name: string): Promise<InstalledAgent> {
  const r = await fetch(`/v1/installed-agents/${encodeURIComponent(name)}`, {
    method: "POST",
    headers: authHeader(),
  });
  return jsonOrThrow<InstalledAgent>(r);
}

export async function importAgent(input: AgentImportInput): Promise<AgentImportResult> {
  const r = await fetch("/v1/agents/import", {
    method: "POST",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify(input),
  });
  return jsonOrThrow<AgentImportResult>(r);
}

export async function previewOpenApiAgent(
  input: AgentOpenApiGenerateInput,
): Promise<AgentOpenApiPreview> {
  const r = await fetch("/v1/agents/openapi/preview", {
    method: "POST",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify(input),
  });
  return jsonOrThrow<AgentOpenApiPreview>(r);
}

export async function generateOpenApiAgent(
  input: AgentOpenApiGenerateInput,
): Promise<AgentOpenApiGenerateResult> {
  const r = await fetch("/v1/agents/from-openapi", {
    method: "POST",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify(input),
  });
  return jsonOrThrow<AgentOpenApiGenerateResult>(r);
}

export async function composeAgent(
  input: AgentComposeInput,
): Promise<AgentComposeResult> {
  const r = await fetch("/v1/agents/compose", {
    method: "POST",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify(input),
  });
  return jsonOrThrow<AgentComposeResult>(r);
}

export async function getAgentAuth(name: string): Promise<AgentAuthStatus> {
  const r = await fetch(`/v1/agents/${encodeURIComponent(name)}/auth`, {
    headers: authHeader(),
  });
  return jsonOrThrow<AgentAuthStatus>(r);
}

export async function connectAgentAuth(
  name: string,
  input: AgentAuthConnectInput,
): Promise<AgentAuthConnection> {
  const r = await fetch(`/v1/agents/${encodeURIComponent(name)}/auth`, {
    method: "POST",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify(input),
  });
  return jsonOrThrow<AgentAuthConnection>(r);
}

export async function deleteAgentAuth(
  name: string,
  connectionId: number,
): Promise<void> {
  const r = await fetch(
    `/v1/agents/${encodeURIComponent(name)}/auth/${connectionId}`,
    {
      method: "DELETE",
      headers: authHeader(),
    },
  );
  await noContentOrThrow(r);
}

export async function getConsumerSetup(
  name: string,
  organizationSlug?: string | null,
): Promise<ConsumerSetupStatus> {
  const qs = organizationSlug
    ? `?organization_slug=${encodeURIComponent(organizationSlug)}`
    : "";
  const r = await fetch(
    `/v1/agents/${encodeURIComponent(name)}/consumer-setup${qs}`,
    { headers: authHeader() },
  );
  return jsonOrThrow<ConsumerSetupStatus>(r);
}

export async function upsertConsumerSetup(
  name: string,
  values: Record<string, unknown>,
  organizationSlug?: string | null,
): Promise<ConsumerSetupStatus> {
  const r = await fetch(`/v1/agents/${encodeURIComponent(name)}/consumer-setup`, {
    method: "PUT",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify({
      values,
      organization_slug: organizationSlug || undefined,
    }),
  });
  return jsonOrThrow<ConsumerSetupStatus>(r);
}

export async function upsertOrgConsumerSetup(
  name: string,
  values: Record<string, unknown>,
  organizationSlug?: string | null,
): Promise<ConsumerSetupStatus> {
  const r = await fetch(
    `/v1/agents/${encodeURIComponent(name)}/consumer-setup/org`,
    {
      method: "PUT",
      headers: { "content-type": "application/json", ...authHeader() },
      body: JSON.stringify({
        values,
        organization_slug: organizationSlug || undefined,
      }),
    },
  );
  return jsonOrThrow<ConsumerSetupStatus>(r);
}

export async function deleteConsumerSetup(
  name: string,
  fieldName: string,
  scope: "user" | "org" = "user",
  organizationSlug?: string | null,
): Promise<ConsumerSetupStatus> {
  const qs = new URLSearchParams({ scope });
  if (organizationSlug) {
    qs.set("organization_slug", organizationSlug);
  }
  const r = await fetch(
    `/v1/agents/${encodeURIComponent(name)}/consumer-setup/${encodeURIComponent(fieldName)}?${qs.toString()}`,
    {
      method: "DELETE",
      headers: authHeader(),
    },
  );
  return jsonOrThrow<ConsumerSetupStatus>(r);
}

export async function upgradeAgentRuntime(name: string): Promise<MyAgentListing> {
  const r = await fetch(`/v1/agents/${encodeURIComponent(name)}/runtime-upgrade`, {
    method: "POST",
    headers: authHeader(),
  });
  return jsonOrThrow<MyAgentListing>(r);
}

export async function requestAgentTemplateUpdate(
  name: string,
): Promise<AgentTemplateUpdateResult> {
  const r = await fetch(`/v1/agents/${encodeURIComponent(name)}/template-update`, {
    method: "POST",
    headers: authHeader(),
  });
  return jsonOrThrow<AgentTemplateUpdateResult>(r);
}

export async function getAgentDossier(name: string): Promise<AgentDossier> {
  const r = await fetch(`/v1/agents/${encodeURIComponent(name)}/dossier`, {
    headers: authHeader(),
  });
  return jsonOrThrow<AgentDossier>(r);
}

export async function getAgentEvidenceTimeline(
  name: string,
  opts: EvidenceTimelineOptions = {},
): Promise<EvidenceTimeline> {
  const qs = new URLSearchParams();
  if (opts.limit) qs.set("limit", String(opts.limit));
  if (opts.lane) qs.set("lane", opts.lane);
  if (opts.head_sha) qs.set("head_sha", opts.head_sha);
  if (opts.skill_name) qs.set("skill_name", opts.skill_name);
  if (opts.event_type) qs.set("event_type", opts.event_type);
  if (opts.status) qs.set("status", opts.status);
  if (opts.severity) qs.set("severity", opts.severity);
  if (opts.include_inferred !== undefined) {
    qs.set("include_inferred", String(opts.include_inferred));
  }
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  const r = await fetch(
    `/v1/agents/${encodeURIComponent(name)}/evidence-timeline${suffix}`,
    { headers: authHeader() },
  );
  return jsonOrThrow<EvidenceTimeline>(r);
}

export async function listReviewLoops(name: string, limit = 20): Promise<ReviewLoop[]> {
  const qs = new URLSearchParams({ limit: String(limit) });
  const r = await fetch(
    `/v1/agents/${encodeURIComponent(name)}/review-loops?${qs.toString()}`,
    { headers: authHeader() },
  );
  return jsonOrThrow<ReviewLoop[]>(r);
}

export async function createReviewLoop(
  name: string,
  input: ReviewLoopRequest,
): Promise<ReviewLoop> {
  const r = await fetch(`/v1/agents/${encodeURIComponent(name)}/review-loops`, {
    method: "POST",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify(input),
  });
  return jsonOrThrow<ReviewLoop>(r);
}

export async function stopReviewLoop(
  name: string,
  jobId: string,
  reason = "owner requested stop",
): Promise<ReviewLoop> {
  const r = await fetch(
    `/v1/agents/${encodeURIComponent(name)}/review-loops/${encodeURIComponent(jobId)}/stop`,
    {
      method: "POST",
      headers: { "content-type": "application/json", ...authHeader() },
      body: JSON.stringify({ reason }),
    },
  );
  return jsonOrThrow<ReviewLoop>(r);
}

export async function listCustomKernelSimulationTemplates(
  name: string,
): Promise<CustomKernelSimulationTemplate[]> {
  const r = await fetch(
    `/v1/agents/${encodeURIComponent(name)}/protocol-simulations/custom-templates`,
    { headers: authHeader() },
  );
  return jsonOrThrow<CustomKernelSimulationTemplate[]>(r);
}

export async function listCustomKernelSuiteTemplates(
  name: string,
): Promise<CustomKernelSuiteTemplate[]> {
  const r = await fetch(
    `/v1/agents/${encodeURIComponent(name)}/protocol-simulations/custom-suite-templates`,
    { headers: authHeader() },
  );
  return jsonOrThrow<CustomKernelSuiteTemplate[]>(r);
}

export async function runCustomProtocolSimulation(
  name: string,
  input: CustomKernelSimulationRunRequest,
): Promise<ProtocolSimulation> {
  const r = await fetch(
    `/v1/agents/${encodeURIComponent(name)}/protocol-simulations/custom-runs`,
    {
      method: "POST",
      headers: { "content-type": "application/json", ...authHeader() },
      body: JSON.stringify(input),
    },
  );
  return jsonOrThrow<ProtocolSimulation>(r);
}

export async function runCustomProtocolSuite(
  name: string,
  input: CustomKernelSuiteRunRequest,
): Promise<ProtocolSimulation> {
  const r = await fetch(
    `/v1/agents/${encodeURIComponent(name)}/protocol-simulations/custom-suites`,
    {
      method: "POST",
      headers: { "content-type": "application/json", ...authHeader() },
      body: JSON.stringify(input),
    },
  );
  return jsonOrThrow<ProtocolSimulation>(r);
}

export async function listUserKernelSimulationTemplates(): Promise<UserKernelSimulationTemplate[]> {
  const r = await fetch("/v1/me/kernel-simulations/templates", { headers: authHeader() });
  return jsonOrThrow<UserKernelSimulationTemplate[]>(r);
}

export async function listUserKernelSimulationRuns(limit = 20): Promise<ProtocolSimulation[]> {
  const qs = new URLSearchParams({ limit: String(limit) });
  const r = await fetch(`/v1/me/kernel-simulations/runs?${qs.toString()}`, {
    headers: authHeader(),
  });
  return jsonOrThrow<ProtocolSimulation[]>(r);
}

export async function getUserKernelSimulationRun(jobId: string): Promise<ProtocolSimulation> {
  const r = await fetch(
    `/v1/me/kernel-simulations/runs/${encodeURIComponent(jobId)}`,
    { headers: authHeader() },
  );
  return jsonOrThrow<ProtocolSimulation>(r);
}

export async function runUserKernelSimulation(
  input: UserKernelSimulationRunRequest,
): Promise<ProtocolSimulation> {
  const r = await fetch("/v1/me/kernel-simulations/runs", {
    method: "POST",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify(input),
  });
  return jsonOrThrow<ProtocolSimulation>(r);
}

export async function replayUserKernelSimulation(jobId: string): Promise<UserKernelSimulationReplay> {
  const r = await fetch(
    `/v1/me/kernel-simulations/runs/${encodeURIComponent(jobId)}/replay`,
    {
      method: "POST",
      headers: authHeader(),
    },
  );
  return jsonOrThrow<UserKernelSimulationReplay>(r);
}

export async function listKernelEvolutionRuns(limit = 20): Promise<KernelEvolutionRun[]> {
  const qs = new URLSearchParams({ limit: String(limit) });
  const r = await fetch(`/v1/me/kernel-evolution/runs?${qs.toString()}`, {
    headers: authHeader(),
  });
  return jsonOrThrow<KernelEvolutionRun[]>(r);
}

export async function getKernelEvolutionRun(jobId: string): Promise<KernelEvolutionRun> {
  const r = await fetch(
    `/v1/me/kernel-evolution/runs/${encodeURIComponent(jobId)}`,
    { headers: authHeader() },
  );
  return jsonOrThrow<KernelEvolutionRun>(r);
}

export async function runKernelEvolution(
  input: KernelEvolutionRunRequest,
): Promise<KernelEvolutionRun> {
  const r = await fetch("/v1/me/kernel-evolution/runs", {
    method: "POST",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify(input),
  });
  return jsonOrThrow<KernelEvolutionRun>(r);
}

export async function replayKernelEvolution(jobId: string): Promise<KernelEvolutionReplay> {
  const r = await fetch(
    `/v1/me/kernel-evolution/runs/${encodeURIComponent(jobId)}/replay`,
    {
      method: "POST",
      headers: authHeader(),
    },
  );
  return jsonOrThrow<KernelEvolutionReplay>(r);
}

export async function listMetaAgentRuns(
  name: string,
  opts: { limit?: number; status?: string; thread_id?: string } = {},
): Promise<MetaAgentRun[]> {
  const qs = new URLSearchParams();
  if (opts.limit) qs.set("limit", String(opts.limit));
  if (opts.status) qs.set("status", opts.status);
  if (opts.thread_id) qs.set("thread_id", opts.thread_id);
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  const r = await fetch(
    `/v1/agents/${encodeURIComponent(name)}/meta-runs${suffix}`,
    { headers: authHeader() },
  );
  return jsonOrThrow<MetaAgentRun[]>(r);
}

export async function getAgentDeployment(
  name: string,
  deployId: string,
): Promise<AgentDeployment> {
  const r = await fetch(
    `/v1/agents/${encodeURIComponent(name)}/deployments/${encodeURIComponent(deployId)}`,
    { headers: authHeader() },
  );
  return jsonOrThrow<AgentDeployment>(r);
}

export async function getAgentDeploymentLogs(
  name: string,
  deployId: string,
): Promise<AgentDeploymentLogsResponse> {
  const r = await fetch(
    `/v1/agents/${encodeURIComponent(name)}/deployments/${encodeURIComponent(deployId)}/logs`,
    { headers: authHeader() },
  );
  return jsonOrThrow<AgentDeploymentLogsResponse>(r);
}

export async function* streamAgentDeployment(
  name: string,
  deployId: string,
  signal?: AbortSignal,
): AsyncGenerator<AgentDeploymentStreamEvent, void, unknown> {
  const r = await fetch(
    `/v1/agents/${encodeURIComponent(name)}/deployments/${encodeURIComponent(deployId)}/stream`,
    {
      headers: { accept: "text/event-stream", ...authHeader() },
      signal,
    },
  );
  if (!r.ok || !r.body) {
    throw await responseError(r);
  }

  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) !== -1) {
      const event = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const payload = sseData(event);
      if (!payload) continue;
      if (payload === "[DONE]") return;
      try {
        yield JSON.parse(payload) as AgentDeploymentStreamEvent;
      } catch {
        /* tolerate stray pings */
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Agent Studio — one bounded run that builds, reviews, improves, and deploys an
// agent from a plain-language brief. Backed by the deployed agent-studio
// coordinator (see control-plane routes/agent_studio.py).
// ---------------------------------------------------------------------------

export type StudioReviewDepth = "light" | "standard" | "strict";
type StudioStartupRecipe =
  | "auto"
  | "csv_tool"
  | "document_generator"
  | "email_assistant"
  | "scheduled_monitor"
  | "calculator"
  | "approval_workflow"
  | "dashboard"
  | "custom";
export type StudioReuseAction =
  | "use_existing"
  | "compose"
  | "fork"
  | "edit_existing"
  | "build_new";

export type StudioRunBrief = {
  name: string;
  goal: string;
  inputs: string[];
  integrations: string[];
  frontend: boolean;
  budget_cents: number;
  public: boolean;
  recipe: StudioStartupRecipe;
  account_trial_calls: number;
  review: StudioReviewDepth;
  organization_slug?: string | null;
  plan_id?: string | null;
  action?: StudioReuseAction;
  candidate_agent_id?: number | null;
  expected_version?: string | null;
  expected_card_hash?: string | null;
  expected_source_sha?: string | null;
  idempotency_key?: string | null;
  confirmed_edit?: boolean;
};

type StudioCandidateAction = {
  action: string;
  allowed: boolean;
  reason: string;
  basis: string;
  organization_role?: string | null;
};

export type StudioReuseCandidate = {
  candidate_id: string;
  agent_id: number;
  name: string;
  description: string;
  version: string;
  card_hash: string;
  source_sha: string | null;
  public: boolean;
  fork_policy: "private" | "organization" | "public";
  status: string;
  url: string | null;
  healthy: boolean;
  score: number;
  match_source: string;
  skills: { name: string; description: string; tags: string[] }[];
  setup: { complete: boolean; missing_required: string[]; setup_url: string };
  actions: Record<Exclude<StudioReuseAction, "build_new">, StudioCandidateAction>;
};

export type StudioResolution = {
  plan_id: string;
  expires_at: string;
  brief: StudioRunBrief;
  recommended_action: StudioReuseAction;
  candidates: StudioReuseCandidate[];
  build_new: { allowed: boolean; reason: string };
};

type StudioRunPhase =
  | "queued"
  | "building"
  | "evaluating"
  | "reviewing"
  | "improving"
  | "deploying"
  | "live"
  | "failed"
  | string;

export type StudioRunEvent = {
  id: number;
  run_id: string;
  phase: string;
  actor: "builder" | "reviewer" | "editor" | "deployer" | "coordinator" | string;
  status: "running" | "passed" | "failed" | "info" | string;
  message: string;
  data: Record<string, unknown>;
  created_at: string;
};

type StudioRunReport = {
  agent_name: string;
  agent_url: string | null;
  mcp_url: string | null;
  frontend_url?: string | null;
  public?: boolean;
  public_url?: string | null;
  source_url?: string | null;
  cli?: string | null;
  distribution?: Record<string, unknown>;
  status: string;
  iterations: number;
  tests_passed: number;
  tests_total: number;
  findings: number;
  receipt_id: string | null;
  deploy_id: string | null;
  next_actions: string[];
};

export type StudioRun = {
  run_id: string;
  agent_name: string;
  action: StudioReuseAction;
  source_agent_id: number | null;
  plan_id: string | null;
  status: StudioRunPhase;
  stop_reason: string | null;
  brief: StudioRunBrief;
  budget_spent_cents: number;
  iteration: number;
  max_iterations: number;
  deploy_id: string | null;
  report: StudioRunReport | null;
  events: StudioRunEvent[];
  created_at: string;
  updated_at: string;
};

export type StudioRunStreamEvent =
  | { type: "snapshot"; run: StudioRun }
  | { type: "event"; run_id?: string; event: StudioRunEvent }
  | { type: "done"; run?: StudioRun }
  | { type: "error"; message?: string };

type StudioStartupIdea = {
  rank: number;
  idea_id: string;
  name: string;
  title: string;
  goal: string;
  audience: string;
  recipe: StudioStartupRecipe;
  mcp_tools: string[];
  scores: {
    usefulness: number;
    recurrence: number;
    viability: number;
    platform_fit: number;
    buildability: number;
    total: number;
  };
};

export type StudioIdeaFactoryResult = {
  ok: boolean;
  theme: string;
  ideas: StudioStartupIdea[];
  top_three: StudioStartupIdea[];
  runs: StudioRun[];
  builds_started: number;
  total_budget_cents: number;
};

export async function runStudioIdeaFactory(
  theme: string,
  buildTopThree = false,
  idempotencyKey?: string,
): Promise<StudioIdeaFactoryResult> {
  const r = await fetch("/v1/agents/studio/idea-factory", {
    method: "POST",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify({
      theme,
      build_top_three: buildTopThree,
      total_budget_cents: 3000,
      public: buildTopThree,
      account_trial_calls: buildTopThree ? 3 : 0,
      idempotency_key: idempotencyKey,
    }),
  });
  return jsonOrThrow<StudioIdeaFactoryResult>(r);
}

export async function resolveStudioRun(brief: StudioRunBrief): Promise<StudioResolution> {
  const r = await fetch("/v1/agents/studio/resolve", {
    method: "POST",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify(brief),
  });
  return jsonOrThrow<StudioResolution>(r);
}

export async function startStudioRun(brief: StudioRunBrief): Promise<StudioRun> {
  const r = await fetch("/v1/agents/studio/runs", {
    method: "POST",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify(brief),
  });
  return jsonOrThrow<StudioRun>(r);
}

export async function claimGuestStudioRun(token: string): Promise<{
  ok: boolean;
  agent_name: string;
  agent_url: string | null;
  public: boolean;
}> {
  const r = await fetch("/v1/agents/studio/guest-runs/claim", {
    method: "POST",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify({ token }),
  });
  return jsonOrThrow(r);
}

export async function getStudioRun(runId: string): Promise<StudioRun> {
  const r = await fetch(`/v1/agents/studio/runs/${encodeURIComponent(runId)}`, {
    headers: authHeader(),
  });
  return jsonOrThrow<StudioRun>(r);
}

export async function* streamStudioRun(
  runId: string,
  signal?: AbortSignal,
): AsyncGenerator<StudioRunStreamEvent, void, unknown> {
  const r = await fetch(
    `/v1/agents/studio/runs/${encodeURIComponent(runId)}/stream`,
    { headers: { accept: "text/event-stream", ...authHeader() }, signal },
  );
  if (!r.ok || !r.body) {
    throw await responseError(r);
  }
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) !== -1) {
      const event = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const payload = sseData(event);
      if (!payload) continue;
      if (payload === "[DONE]") return;
      try {
        yield JSON.parse(payload) as StudioRunStreamEvent;
      } catch {
        /* tolerate stray pings */
      }
    }
  }
}

export type StudioAutopilotPolicy = {
  enabled: boolean;
  timezone: string;
  daily_hour: number;
  next_run_at: string | null;
  last_run_at: string | null;
  last_run_status: string | null;
  last_error: string | null;
  last_email_at: string | null;
};

export type StudioUpgradeProposal = {
  proposal_id: string;
  agent_name: string;
  source_head_sha: string | null;
  status: string;
  severity: string;
  category: string;
  title: string;
  idea: string;
  rationale: string;
  evidence: Record<string, unknown>;
  upgrade_run_id: string | null;
  upgrade_report: Record<string, unknown> | null;
  error: string | null;
  emailed_at: string | null;
  decided_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  expires_at: string;
  created_at: string;
};

export async function getStudioAutopilot(): Promise<StudioAutopilotPolicy> {
  const r = await fetch("/v1/agents/studio/autopilot", { headers: authHeader() });
  return jsonOrThrow<StudioAutopilotPolicy>(r);
}

export async function updateStudioAutopilot(input: {
  enabled: boolean;
  timezone: string;
  daily_hour: number;
}): Promise<StudioAutopilotPolicy> {
  const r = await fetch("/v1/agents/studio/autopilot", {
    method: "PUT",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify(input),
  });
  return jsonOrThrow<StudioAutopilotPolicy>(r);
}

export async function runStudioAutopilotNow(): Promise<StudioAutopilotPolicy> {
  const r = await fetch("/v1/agents/studio/autopilot/run", {
    method: "POST",
    headers: authHeader(),
  });
  return jsonOrThrow<StudioAutopilotPolicy>(r);
}

export async function listStudioUpgradeProposals(): Promise<StudioUpgradeProposal[]> {
  const r = await fetch("/v1/agents/studio/autopilot/proposals", {
    headers: authHeader(),
  });
  return jsonOrThrow<StudioUpgradeProposal[]>(r);
}

export async function getStudioUpgradeProposal(
  proposalId: string,
): Promise<StudioUpgradeProposal> {
  const r = await fetch(
    `/v1/agents/studio/autopilot/proposals/${encodeURIComponent(proposalId)}`,
    { headers: authHeader() },
  );
  return jsonOrThrow<StudioUpgradeProposal>(r);
}

export async function decideStudioUpgradeProposal(
  proposalId: string,
  decision: "accept" | "reject",
): Promise<StudioUpgradeProposal> {
  const r = await fetch(
    `/v1/agents/studio/autopilot/proposals/${encodeURIComponent(proposalId)}/decision`,
    {
      method: "POST",
      headers: { "content-type": "application/json", ...authHeader() },
      body: JSON.stringify({ decision }),
    },
  );
  return jsonOrThrow<StudioUpgradeProposal>(r);
}

export async function listAgentDomains(name: string): Promise<AgentCustomDomain[]> {
  const r = await fetch(`/v1/agents/${encodeURIComponent(name)}/domains`, {
    headers: authHeader(),
  });
  return jsonOrThrow<AgentCustomDomain[]>(r);
}

export async function addAgentDomain(
  name: string,
  hostname: string,
  options: { include_www?: boolean; canonical_hostname?: string } = {},
): Promise<AgentCustomDomain> {
  const r = await fetch(`/v1/agents/${encodeURIComponent(name)}/domains`, {
    method: "POST",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify({ hostname, ...options }),
  });
  return jsonOrThrow<AgentCustomDomain>(r);
}

export async function verifyAgentDomain(
  name: string,
  hostname: string,
): Promise<AgentCustomDomain> {
  const r = await fetch(
    `/v1/agents/${encodeURIComponent(name)}/domains/${encodeURIComponent(hostname)}/verify`,
    {
      method: "POST",
      headers: authHeader(),
    },
  );
  return jsonOrThrow<AgentCustomDomain>(r);
}

export async function deleteAgentDomain(name: string, hostname: string): Promise<void> {
  const r = await fetch(
    `/v1/agents/${encodeURIComponent(name)}/domains/${encodeURIComponent(hostname)}`,
    {
      method: "DELETE",
      headers: authHeader(),
    },
  );
  await noContentOrThrow(r);
}

export async function getAgentMailbox(name: string): Promise<AgentMailbox | null> {
  const r = await fetch(`/v1/agents/${encodeURIComponent(name)}/mailbox`, {
    headers: authHeader(),
  });
  if (r.status === 404) return null;
  return jsonOrThrow<AgentMailbox>(r);
}

export async function createAgentMailbox(name: string): Promise<AgentMailbox> {
  const r = await fetch(`/v1/agents/${encodeURIComponent(name)}/mailbox`, {
    method: "POST",
    headers: authHeader(),
  });
  return jsonOrThrow<AgentMailbox>(r);
}

export async function updateAgentMailbox(
  name: string,
  update: { allowed_senders: string[] },
): Promise<AgentMailbox> {
  const r = await fetch(`/v1/agents/${encodeURIComponent(name)}/mailbox`, {
    method: "PATCH",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify(update),
  });
  return jsonOrThrow<AgentMailbox>(r);
}

export async function deleteAgentMailbox(name: string): Promise<void> {
  const r = await fetch(`/v1/agents/${encodeURIComponent(name)}/mailbox`, {
    method: "DELETE",
    headers: authHeader(),
  });
  await noContentOrThrow(r);
}

export async function listAgentSecrets(name: string): Promise<AgentSecret[]> {
  const r = await fetch(`/v1/agents/${encodeURIComponent(name)}/secrets`, {
    headers: authHeader(),
  });
  return jsonOrThrow<AgentSecret[]>(r);
}

export async function upsertAgentSecret(
  name: string,
  input: { key: string; value: string },
): Promise<AgentSecret> {
  const r = await fetch(`/v1/agents/${encodeURIComponent(name)}/secrets`, {
    method: "POST",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify(input),
  });
  return jsonOrThrow<AgentSecret>(r);
}

export async function deleteAgentSecret(name: string, key: string): Promise<void> {
  const r = await fetch(
    `/v1/agents/${encodeURIComponent(name)}/secrets/${encodeURIComponent(key)}`,
    {
      method: "DELETE",
      headers: authHeader(),
    },
  );
  if (!r.ok && r.status !== 204) {
    let detail = r.statusText;
    try {
      const j = await r.json();
      detail = j.detail ?? JSON.stringify(j);
    } catch {
      /* ignore */
    }
    throw new Error(`${r.status}: ${detail}`);
  }
}

export async function listAgentCallLogs(name: string): Promise<AgentCallLog[]> {
  const r = await fetch(`/v1/agents/${encodeURIComponent(name)}/insights/calls`, {
    headers: authHeader(),
  });
  return jsonOrThrow<AgentCallLog[]>(r);
}

export async function deleteAgent(name: string): Promise<void> {
  const r = await fetch(`/v1/agents/${encodeURIComponent(name)}`, {
    method: "DELETE",
    headers: authHeader(),
  });
  if (!r.ok && r.status !== 204) {
    let detail = r.statusText;
    try {
      const j = await r.json();
      detail = j.detail ?? JSON.stringify(j);
    } catch {
      /* ignore */
    }
    throw new Error(`${r.status}: ${detail}`);
  }
}

// ---------- Trial rooms ----------

type TrialRoomStatus = "open" | "evaluating" | "deployed" | "archived" | string;

export type TrialRun = {
  id: number;
  agent_id: number | null;
  agent_name: string;
  skill_name: string;
  grant_id: string | null;
  status: "running" | "passed" | "failed" | string;
  score: number;
  summary: string | null;
  evaluator_notes: string | null;
  error: string | null;
  args_preview: Record<string, unknown>;
  result: Record<string, unknown>;
  events: Record<string, unknown>[];
  file_ops: SubagentFileOp[];
  receipt_json: Record<string, unknown>;
  elapsed_ms: number | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
};

export type TrialRoom = {
  id: number;
  slug: string;
  title: string;
  goal: string;
  acceptance_criteria: string;
  input_paths: string[];
  output_schema: Record<string, unknown>;
  max_cost_cents: number;
  max_runtime_seconds: number;
  status: TrialRoomStatus;
  selected_run_id: number | null;
  deployed_agent_id: number | null;
  created_at: string;
  updated_at: string;
  runs: TrialRun[];
};

export type TrialRoomDraft = {
  title: string;
  goal: string;
  acceptance_criteria?: string;
  input_paths?: string[];
  output_schema?: Record<string, unknown>;
  max_cost_cents?: number;
  max_runtime_seconds?: number;
};

export async function listTrialRooms(opts: {
  status?: string;
  limit?: number;
} = {}): Promise<TrialRoom[]> {
  const qs = new URLSearchParams();
  if (opts.status) qs.set("status", opts.status);
  if (opts.limit) qs.set("limit", String(opts.limit));
  const suffix = qs.toString() ? `?${qs}` : "";
  const r = await fetch(`/v1/me/trial-rooms${suffix}`, { headers: authHeader() });
  return jsonOrThrow<TrialRoom[]>(r);
}

export async function getTrialRoom(slug: string): Promise<TrialRoom> {
  const r = await fetch(`/v1/me/trial-rooms/${encodeURIComponent(slug)}`, {
    headers: authHeader(),
  });
  return jsonOrThrow<TrialRoom>(r);
}

export async function createTrialRoom(draft: TrialRoomDraft): Promise<TrialRoom> {
  const r = await fetch("/v1/me/trial-rooms", {
    method: "POST",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify(draft),
  });
  return jsonOrThrow<TrialRoom>(r);
}

export async function runTrialAgent(
  slug: string,
  input: {
    agent_name: string;
    skill_name?: string;
    args?: Record<string, unknown>;
    args_json?: string;
  },
): Promise<TrialRoom> {
  const r = await fetch(`/v1/me/trial-rooms/${encodeURIComponent(slug)}/runs`, {
    method: "POST",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify(input),
  });
  return jsonOrThrow<TrialRoom>(r);
}

export async function rateTrialRun(
  slug: string,
  runId: number,
  input: { score: number; evaluator_notes?: string; passed?: boolean | null },
): Promise<TrialRoom> {
  const r = await fetch(
    `/v1/me/trial-rooms/${encodeURIComponent(slug)}/runs/${runId}/rate`,
    {
      method: "POST",
      headers: { "content-type": "application/json", ...authHeader() },
      body: JSON.stringify(input),
    },
  );
  return jsonOrThrow<TrialRoom>(r);
}

export async function selectTrialWinner(
  slug: string,
  runId: number,
): Promise<TrialRoom> {
  const r = await fetch(
    `/v1/me/trial-rooms/${encodeURIComponent(slug)}/runs/${runId}/select`,
    {
      method: "POST",
      headers: authHeader(),
    },
  );
  return jsonOrThrow<TrialRoom>(r);
}

// ---------- Bounties ----------

export type BountyStatus = "open" | "claimed" | "fulfilled" | "cancelled";

export type Bounty = {
  id: number;
  slug: string;
  title: string;
  description: string;
  example_input: string;
  example_output: string;
  tags: string[];
  status: BountyStatus;
  posted_by_email: string | null;
  claimed_agent_name: string | null;
  claimed_agent_status: string | null;
  claimed_agent_url: string | null;
  claimed_agent_version: string | null;
  claimed_agent_card: Record<string, unknown> | null;
  claimed_at: string | null;
  fulfilled_at: string | null;
  created_at: string;
};

export type BountyDraft = {
  title: string;
  description: string;
  example_input?: string;
  example_output?: string;
  tags?: string[];
};

export type ListBountiesParams = {
  mine?: boolean;
  status?: BountyStatus;
  limit?: number;
  offset?: number;
};

export type BountyListPage = {
  items: Bounty[];
  nextOffset: number | null;
};

export async function listBountiesPage(
  params: ListBountiesParams = {},
): Promise<BountyListPage> {
  const q = new URLSearchParams();
  if (params.mine) q.set("mine", "true");
  if (params.status) q.set("status", params.status);
  if (params.limit) q.set("limit", String(params.limit));
  if (params.offset) q.set("offset", String(params.offset));
  const url = q.toString() ? `/v1/bounties?${q}` : "/v1/bounties";
  const r = await fetch(url, { headers: authHeader() });
  const items = await jsonOrThrow<Bounty[]>(r);
  const nextOffsetHeader = r.headers.get("x-a2a-next-offset");
  const nextOffset =
    nextOffsetHeader === null ? NaN : Number.parseInt(nextOffsetHeader, 10);
  return {
    items,
    nextOffset: Number.isFinite(nextOffset) ? nextOffset : null,
  };
}

export async function listBounties(
  params: ListBountiesParams = {},
): Promise<Bounty[]> {
  if (params.limit || params.offset) {
    return (await listBountiesPage(params)).items;
  }

  const out: Bounty[] = [];
  let offset = 0;
  for (;;) {
    const page = await listBountiesPage({ ...params, limit: 100, offset });
    out.push(...page.items);
    if (page.nextOffset === null || page.nextOffset <= offset) {
      return out;
    }
    offset = page.nextOffset;
  }
}

export async function createBounty(draft: BountyDraft): Promise<Bounty> {
  const r = await fetch("/v1/bounties", {
    method: "POST",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify(draft),
  });
  return jsonOrThrow<Bounty>(r);
}

export async function claimBounty(
  slug: string,
  agentName: string,
): Promise<Bounty> {
  const r = await fetch(`/v1/bounties/${encodeURIComponent(slug)}/claim`, {
    method: "POST",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify({ agent_name: agentName }),
  });
  return jsonOrThrow<Bounty>(r);
}

export async function fulfillBounty(slug: string): Promise<Bounty> {
  const r = await fetch(`/v1/bounties/${encodeURIComponent(slug)}/fulfill`, {
    method: "POST",
    headers: authHeader(),
  });
  return jsonOrThrow<Bounty>(r);
}

export async function cancelBounty(slug: string): Promise<Bounty> {
  const r = await fetch(`/v1/bounties/${encodeURIComponent(slug)}`, {
    method: "DELETE",
    headers: authHeader(),
  });
  return jsonOrThrow<Bounty>(r);
}

// ---------- Chat ----------

/** Stream chat from the control plane. Yields typed ChatEvent objects. */
export async function* chatStream(
  messages: ChatMessage[],
  signal?: AbortSignal,
  approvalMode: boolean = false,
  mainLlmSource: MainLlmSource = "user",
  llmCredsName?: string,
  threadId?: string,
  policyOverrides?: ChatThreadSettings,
  organizationSlug?: string,
): AsyncGenerator<ChatEvent, void, unknown> {
  const body: Record<string, unknown> = {
    messages,
    stream: true,
    approval_mode: approvalMode,
    main_llm_source: mainLlmSource,
  };
  if (llmCredsName) body.llm_creds_name = llmCredsName;
  if (threadId) body.thread_id = threadId;
  if (policyOverrides) body.policy_overrides = policyOverrides;
  if (organizationSlug) body.organization_slug = organizationSlug;
  const r = await fetch("/v1/me/chat", {
    method: "POST",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify(body),
    signal,
  });
  if (!r.ok || !r.body) {
    throw await responseError(r);
  }
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) !== -1) {
      const event = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const dataLines = event
        .split("\n")
        .filter((l) => l.startsWith("data:"))
        .map((l) => l.slice(5).trimStart());
      if (!dataLines.length) continue;
      const payload = dataLines.join("\n");
      if (payload === "[DONE]") return;
      try {
        const j = JSON.parse(payload);
        // OpenAI-style content delta (back-compat path).
        const delta = j?.choices?.[0]?.delta?.content;
        if (typeof delta === "string" && delta.length) {
          yield { type: "delta", content: delta };
          continue;
        }
        if (typeof j?.type === "string") {
          yield j as ChatEvent;
        }
      } catch {
        /* tolerate stray pings */
      }
    }
  }
  throw new Error("chat stream ended before [DONE]");
}
