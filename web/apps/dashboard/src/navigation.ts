export type DashboardRouteId =
  | "workspace"
  | "trials"
  | "activity"
  | "schedules"
  | "simulations"
  | "my-agents"
  | "marketplace"
  | "studio"
  | "compose"
  | "installed-agents"
  | "bounties"
  | "access"
  | "keys"
  | "organization"
  | "compliance"
  | "runtime";

type DashboardGroupId = "Work" | "Agents" | "Admin";

type Breadcrumb = {
  label: string;
  href?: string;
};

type DashboardRouteLayout = "scroll" | "full-height";
type DashboardRoutePageWidth = "sm" | "md" | "lg" | "xl" | "full";

type DashboardRoutePage = {
  eyebrow?: string;
  title?: string;
  description?: string;
  maxWidth?: DashboardRoutePageWidth;
};

export type DashboardRouteConfig = {
  id: DashboardRouteId;
  label: string;
  group: DashboardGroupId;
  path: string;
  layout?: DashboardRouteLayout;
  page?: DashboardRoutePage;
  routePatterns?: string[];
  redirects?: DashboardRouteRedirect[];
  description: string;
  aliases?: string[];
  featureFlag?: string;
  nav?: boolean;
  command?: boolean;
  match?: (pathname: string) => boolean;
  breadcrumbs?: (pathname: string) => Breadcrumb[];
};

type DashboardRouteRedirect = {
  path: string;
  to?: string;
};

export type DashboardRouteMatch = {
  route: DashboardRouteConfig;
  breadcrumbs: Breadcrumb[];
};

export type DashboardCommandItem = {
  id: DashboardRouteId;
  label: string;
  group: string;
  path: string;
  description: string;
  aliases: string[];
};

type DashboardNavSectionId =
  | "start"
  | "operate"
  | "agents"
  | "runtime"
  | "admin";

export type DashboardNavItem = {
  id: string;
  routeId: DashboardRouteId;
  label: string;
  path: string;
  description: string;
  aliases?: readonly string[];
  featureFlag?: string;
  children?: readonly DashboardNavItem[];
};

export type DashboardNavSection = {
  id: DashboardNavSectionId;
  label: string;
  items: readonly DashboardNavItem[];
};

export const DEFAULT_ROUTE_ID: DashboardRouteId = "workspace";

const WORKSPACE_VIEWS = [
  {
    id: "chat",
    label: "Chat",
    path: "/workspace",
    description: "Main-agent conversation, thread history, approvals, and streaming work.",
  },
  {
    id: "threads",
    label: "Threads",
    path: "/workspace/threads",
    description: "Search, reopen, and manage saved chat threads.",
  },
  {
    id: "settings",
    label: "Settings",
    path: "/workspace/settings",
    description: "Edit thread-scoped budgets, policy gates, and approved agent overrides.",
  },
  {
    id: "files",
    label: "Files",
    path: "/workspace/files",
    description: "Upload, browse, preview, move, and delete workspace files.",
  },
  {
    id: "artifacts",
    label: "Artifacts",
    path: "/workspace/artifacts",
    description: "Scan generated files and downloadable outputs in a gallery.",
  },
  {
    id: "activity",
    label: "Activity",
    path: "/workspace/activity",
    description: "Thread-scoped handoffs, DAG runs, file operations, and LLM usage.",
  },
] as const;

export type WorkspaceViewId = (typeof WORKSPACE_VIEWS)[number]["id"];
export type WorkspaceView = (typeof WORKSPACE_VIEWS)[number];

const WORKSPACE_VIEW_BY_ID = Object.fromEntries(
  WORKSPACE_VIEWS.map((view) => [view.id, view]),
) as Record<WorkspaceViewId, WorkspaceView>;

const WORKSPACE_SETTINGS_SECTION_LABELS = {
  overview: "Overview",
  defaults: "Chat defaults",
  policy: "Policy",
  allowlist: "Allowlist",
} as const;

export const AGENT_DETAIL_SECTIONS = [
  { id: "overview", label: "Overview" },
  { id: "skills", label: "Tools" },
  { id: "proofs", label: "Proofs" },
  { id: "runs", label: "Runs" },
  { id: "runtime", label: "Runtime" },
  { id: "access", label: "Access" },
  { id: "auth", label: "Auth" },
  { id: "secrets", label: "Secrets" },
  { id: "domains", label: "Domains" },
  { id: "mailbox", label: "Email inbox" },
  { id: "insights", label: "Insights" },
  { id: "evidence", label: "Evidence" },
  { id: "deployment", label: "Deployment" },
] as const;

export type AgentDetailSection = (typeof AGENT_DETAIL_SECTIONS)[number]["id"];

const AGENT_DETAIL_SECTION_IDS = new Set<AgentDetailSection>(
  AGENT_DETAIL_SECTIONS.map((section) => section.id),
);

export const RUNTIME_VIEWS = [
  {
    id: "state",
    label: "Overview",
    path: "/runtime",
    description: "Runtime health, live DAG runs, spend posture, and the latest control receipt.",
    aliases: ["control room", "runtime health", "live dags", "runtime state"],
  },
  {
    id: "timeline",
    label: "Timeline",
    path: "/runtime/timeline",
    description: "Live DAGs, receipt streams, routing, spend, file operations, and proof status.",
    aliases: ["runtime ledger", "receipts", "control timeline", "control events"],
  },
  {
    id: "policy",
    label: "Policy",
    path: "/runtime/policy",
    description: "Budget caps, approvals, network posture, PII-safe mode, and agent allowlists.",
    aliases: ["runtime policy", "guardrails", "budget caps", "agent allowlist"],
  },
] as const;

export type RuntimeViewId = (typeof RUNTIME_VIEWS)[number]["id"];
export type RuntimeView = (typeof RUNTIME_VIEWS)[number];

const RUNTIME_VIEW_BY_ID = Object.fromEntries(
  RUNTIME_VIEWS.map((view) => [view.id, view]),
) as Record<RuntimeViewId, RuntimeView>;

const RUNTIME_POLICY_SECTION_LABELS = {
  overview: "Overview",
  budgets: "Budgets",
  gates: "Gates",
  allowlist: "Allowlist",
} as const;

export const TRIAL_ROOM_VIEWS = [
  { id: "overview", label: "Overview" },
  { id: "run", label: "Run" },
  { id: "comparison", label: "Comparison" },
  { id: "receipts", label: "Receipts" },
] as const;

export type TrialRoomViewId = (typeof TRIAL_ROOM_VIEWS)[number]["id"];
type TrialRoomView = (typeof TRIAL_ROOM_VIEWS)[number];

const TRIAL_ROOM_VIEW_BY_ID = Object.fromEntries(
  TRIAL_ROOM_VIEWS.map((view) => [view.id, view]),
) as Record<TrialRoomViewId, TrialRoomView>;

const TRIAL_ROOM_VIEW_IDS = new Set<TrialRoomViewId>(
  TRIAL_ROOM_VIEWS.map((view) => view.id),
);

export const MARKETPLACE_VIEWS = [
  {
    id: "browse",
    label: "Browse",
    path: "/marketplace",
    description: "Search public agents, inspect cards, install setup, and run trials.",
  },
  {
    id: "proofs",
    label: "Proofs",
    path: "/marketplace/proofs",
    description: "Review proof coverage, latest receipts, tool evidence, and verification gaps.",
  },
] as const;

export type MarketplaceViewId = (typeof MARKETPLACE_VIEWS)[number]["id"];
export type MarketplaceView = (typeof MARKETPLACE_VIEWS)[number];

const MARKETPLACE_VIEW_BY_ID = Object.fromEntries(
  MARKETPLACE_VIEWS.map((view) => [view.id, view]),
) as Record<MarketplaceViewId, MarketplaceView>;

export const BOUNTY_VIEWS = [
  { id: "open", label: "Open requests", path: "/bounties/open" },
  { id: "mine", label: "Mine", path: "/bounties/mine" },
  { id: "claimed", label: "Claimed", path: "/bounties/claimed" },
  { id: "fulfilled", label: "Fulfilled", path: "/bounties/fulfilled" },
] as const;

export type BountyViewId = (typeof BOUNTY_VIEWS)[number]["id"];
type BountyView = (typeof BOUNTY_VIEWS)[number];

const BOUNTY_VIEW_BY_ID = Object.fromEntries(
  BOUNTY_VIEWS.map((view) => [view.id, view]),
) as Record<BountyViewId, BountyView>;

const BOUNTY_VIEW_IDS = new Set<BountyViewId>(
  BOUNTY_VIEWS.map((view) => view.id),
);

export const ACCESS_VIEWS = [
  {
    id: "overview",
    label: "Overview",
    path: "/access",
    description: "Service readiness, managed account links, and repository coverage.",
  },
  {
    id: "langfuse",
    label: "Langfuse",
    path: "/access/langfuse",
    description: "Observability projects, login identities, project links, and credential refs.",
  },
  {
    id: "litellm",
    label: "LiteLLM",
    path: "/access/litellm",
    description: "OpenAI-compatible gateway teams, keys, base URLs, and setup errors.",
  },
  {
    id: "gitea",
    label: "Gitea orgs",
    path: "/access/gitea",
    description: "Managed source-control organizations and account-level Gitea access.",
  },
  {
    id: "repositories",
    label: "Repositories",
    path: "/access/repositories",
    description: "Agent repositories, visibility, source links, and marketplace agent links.",
  },
] as const;

export type AccessViewId = (typeof ACCESS_VIEWS)[number]["id"];
export type AccessView = (typeof ACCESS_VIEWS)[number];

const ACCESS_VIEW_BY_ID = Object.fromEntries(
  ACCESS_VIEWS.map((view) => [view.id, view]),
) as Record<AccessViewId, AccessView>;

const ACCESS_VIEW_IDS = new Set<AccessViewId>(
  ACCESS_VIEWS.map((view) => view.id),
);

export const ORGANIZATION_VIEWS = [
  { id: "overview", label: "Overview" },
  { id: "domains", label: "Domains" },
  { id: "scim", label: "SCIM" },
  { id: "members", label: "Members" },
  { id: "audit", label: "Audit" },
] as const;

export type OrganizationViewId = (typeof ORGANIZATION_VIEWS)[number]["id"];
export type OrganizationView = (typeof ORGANIZATION_VIEWS)[number];

const ORGANIZATION_VIEW_BY_ID = Object.fromEntries(
  ORGANIZATION_VIEWS.map((view) => [view.id, view]),
) as Record<OrganizationViewId, OrganizationView>;

const ORGANIZATION_VIEW_IDS = new Set<OrganizationViewId>(
  ORGANIZATION_VIEWS.map((view) => view.id),
);

export const COMPLIANCE_VIEWS = [
  { id: "records", label: "Decision Records" },
  { id: "policy", label: "Policy" },
  { id: "agents", label: "Agent Risk" },
] as const;

export type ComplianceViewId = (typeof COMPLIANCE_VIEWS)[number]["id"];
export type ComplianceView = (typeof COMPLIANCE_VIEWS)[number];

const COMPLIANCE_VIEW_BY_ID = Object.fromEntries(
  COMPLIANCE_VIEWS.map((view) => [view.id, view]),
) as Record<ComplianceViewId, ComplianceView>;

const COMPLIANCE_VIEW_IDS = new Set<ComplianceViewId>(
  COMPLIANCE_VIEWS.map((view) => view.id),
);

const LLM_KEY_VIEWS = [
  { id: "overview", label: "Overview", path: "/llm-keys" },
  { id: "saved", label: "Saved keys", path: "/llm-keys/saved" },
  { id: "add", label: "Add key", path: "/llm-keys/add" },
] as const;

type LlmKeyViewId = (typeof LLM_KEY_VIEWS)[number]["id"];
export type LlmKeyView = (typeof LLM_KEY_VIEWS)[number];

const LLM_KEY_VIEW_BY_ID = Object.fromEntries(
  LLM_KEY_VIEWS.map((view) => [view.id, view]),
) as Record<LlmKeyViewId, LlmKeyView>;

const LLM_KEY_VIEW_IDS = new Set<LlmKeyViewId>(
  LLM_KEY_VIEWS.map((view) => view.id),
);

export const INSTALLED_AGENT_VIEWS = [
  { id: "setup", label: "Setup" },
  { id: "auth", label: "Auth" },
  { id: "links", label: "Links" },
] as const;

export type InstalledAgentViewId = (typeof INSTALLED_AGENT_VIEWS)[number]["id"];
type InstalledAgentView = (typeof INSTALLED_AGENT_VIEWS)[number];

const INSTALLED_AGENT_VIEW_BY_ID = Object.fromEntries(
  INSTALLED_AGENT_VIEWS.map((view) => [view.id, view]),
) as Record<InstalledAgentViewId, InstalledAgentView>;

const INSTALLED_AGENT_VIEW_IDS = new Set<InstalledAgentViewId>(
  INSTALLED_AGENT_VIEWS.map((view) => view.id),
);

const SCHEDULE_VIEWS = [
  { id: "overview", label: "Overview", path: "/schedules" },
  { id: "list", label: "Schedules", path: "/schedules/list" },
  { id: "create", label: "Create", path: "/schedules/create" },
] as const;

type ScheduleViewId = (typeof SCHEDULE_VIEWS)[number]["id"];
export type ScheduleView = (typeof SCHEDULE_VIEWS)[number];

const SCHEDULE_VIEW_BY_ID = Object.fromEntries(
  SCHEDULE_VIEWS.map((view) => [view.id, view]),
) as Record<ScheduleViewId, ScheduleView>;

const SCHEDULE_VIEW_IDS = new Set<ScheduleViewId>(
  SCHEDULE_VIEWS.map((view) => view.id),
);

export const SIMULATION_VIEWS = [
  {
    id: "builder",
    label: "Builder",
    path: "/simulations",
    description: "Build a live-agent simulation spec from selected agents.",
  },
  {
    id: "spec",
    label: "Spec",
    path: "/simulations/spec",
    description: "Pick templates, edit JSON, choose execution mode, and run a simulation.",
  },
  {
    id: "runs",
    label: "Runs",
    path: "/simulations/runs",
    description: "Inspect recent simulation runs, replay evidence, live calls, and traces.",
  },
  {
    id: "evolution",
    label: "Evolution lab",
    path: "/simulations/evolution",
    description: "Configure and run topology evolution experiments.",
  },
  {
    id: "evolution-results",
    label: "Evolution results",
    path: "/simulations/evolution-results",
    description: "Inspect evolution variants, winners, replay results, and disabled proposals.",
  },
] as const;

export type SimulationViewId = (typeof SIMULATION_VIEWS)[number]["id"];
export type SimulationView = (typeof SIMULATION_VIEWS)[number];

const SIMULATION_VIEW_BY_ID = Object.fromEntries(
  SIMULATION_VIEWS.map((view) => [view.id, view]),
) as Record<SimulationViewId, SimulationView>;

export const COMPOSE_STEPS = [
  {
    id: "select",
    label: "Catalog",
    path: "/compose/select",
    description: "Browse public and owned agents with declared tools.",
  },
  {
    id: "selection",
    label: "Selected tools",
    path: "/compose/selection",
    description: "Review the sub-agents and tools that the planner may call.",
  },
  {
    id: "identity",
    label: "Identity",
    path: "/compose/identity",
    description: "Name, describe, version, and visibility settings for the composed agent.",
  },
  {
    id: "controls",
    label: "Controls",
    path: "/compose/controls",
    description: "Goal, success criteria, memory tiers, and execution limits.",
  },
  {
    id: "manifest",
    label: "Manifest",
    path: "/compose/manifest",
    description: "Review the generated manifest and deploy the composed agent.",
  },
  {
    id: "deployment",
    label: "Deployment",
    path: "/compose/deployment",
    description: "Watch repository and deployment progress for the composed agent.",
  },
  {
    id: "runs",
    label: "Runs",
    path: "/compose/runs",
    description: "Inspect persisted goal runs after the composed agent is deployed.",
  },
] as const;

export type ComposeStepId = (typeof COMPOSE_STEPS)[number]["id"];
export type ComposeStep = (typeof COMPOSE_STEPS)[number];

const COMPOSE_STEP_BY_ID = Object.fromEntries(
  COMPOSE_STEPS.map((step) => [step.id, step]),
) as Record<ComposeStepId, ComposeStep>;

const COMPOSE_STEP_IDS = new Set<ComposeStepId>(
  COMPOSE_STEPS.map((step) => step.id),
);

export const DASHBOARD_ROUTES: DashboardRouteConfig[] = [
  {
    id: "workspace",
    label: "Workspace",
    group: "Work",
    path: "/workspace",
    layout: "full-height",
    routePatterns: [
      "/workspace/:view",
      "/workspace/settings/:settingsSection",
      "/workspace/files/file/*",
      "/workspace/artifacts/file/*",
      "/workspace/activity/:detailKind/:detailId",
      "/workspace/activity/:detailKind/:detailId/:detailSection",
    ],
    description: "Execution cockpit with files, chat, threads, controls, activity, and artifacts.",
    match: (pathname) => startsWithSegment(pathname, "/workspace"),
    breadcrumbs: workspaceBreadcrumbs,
  },
  {
    id: "trials",
    label: "Trials",
    group: "Work",
    path: "/trials",
    layout: "full-height",
    routePatterns: [
      "/trials/new",
      "/trials/:roomSlug",
      "/trials/:roomSlug/runs/:runId",
      "/trials/:roomSlug/runs/:runId/:section",
      "/trials/:roomSlug/:view",
    ],
    description: "Compare candidate agents against the same goal, files, scores, and receipts.",
    match: (pathname) => startsWithSegment(pathname, "/trials"),
    breadcrumbs: trialBreadcrumbs,
  },
  {
    id: "activity",
    label: "Activity",
    group: "Work",
    path: "/activity",
    layout: "full-height",
    routePatterns: [
      "/activity/:jobId",
      "/activity/:jobId/:section",
    ],
    description: "Global work ledger for jobs, status, timelines, receipts, and artifacts.",
    page: {
      eyebrow: "Activity",
      title: "Global work ledger",
      description: "Search recent jobs across agents, trials, proofs, deployments, and LLM work.",
      maxWidth: "xl",
    },
    match: (pathname) => startsWithSegment(pathname, "/activity"),
    breadcrumbs: activityBreadcrumbs,
  },
  {
    id: "schedules",
    label: "Schedules",
    group: "Work",
    path: "/schedules",
    routePatterns: [
      "/schedules/:view",
      "/schedules/list/:scheduleId",
      "/schedules/list/:scheduleId/:section",
    ],
    description: "Create and operate recurring orchestrator or agent runs.",
    page: {
      eyebrow: "Schedules",
      title: "Cron runs",
      description: "Run the orchestrator or a selected agent from a saved schedule.",
      maxWidth: "xl",
    },
    match: (pathname) => startsWithSegment(pathname, "/schedules"),
    breadcrumbs: schedulesBreadcrumbs,
  },
  {
    id: "simulations",
    label: "Simulations",
    group: "Work",
    path: "/simulations",
    aliases: ["/kernel-simulations"],
    redirects: [{ path: "/kernel-simulations" }],
    routePatterns: [
      ...SIMULATION_VIEWS.filter((view) => view.id !== "builder").map((view) => view.path),
      "/simulations/runs/:jobId",
      "/simulations/runs/:jobId/:section",
      "/simulations/evolution-results/:jobId",
    ],
    description: "Experimental protocol lab for specs, replay evidence, and evolution inspection.",
    page: {
      eyebrow: "Experimental",
      title: "Simulation lab",
      description: "Build agent simulation specs, run bounded or live-agent evidence, replay receipts, and inspect evolution proposals.",
      maxWidth: "xl",
    },
    featureFlag: "dashboard.simulations",
    nav: false,
    match: (pathname) =>
      startsWithSegment(pathname, "/simulations") ||
      startsWithSegment(pathname, "/kernel-simulations"),
    breadcrumbs: simulationBreadcrumbs,
  },
  {
    id: "my-agents",
    label: "My Agents",
    group: "Agents",
    path: "/my-agents",
    routePatterns: [
      "/my-agents/import",
      "/my-agents/:agentName",
      "/my-agents/:agentName/:section",
      "/my-agents/:agentName/runs/:grantId",
      "/my-agents/:agentName/runs/:grantId/:runView",
      "/my-agents/:agentName/calls/:callId",
      "/my-agents/:agentName/proofs/:proofId",
      "/my-agents/:agentName/domains/:domainHostname",
      "/my-agents/:agentName/evidence/:evidenceView",
    ],
    description: "Lifecycle command center for owned agents, proofs, health, deployments, and updates.",
    page: {
      eyebrow: "My agents",
      title: "Agents you built",
      description: "Your deployed agents, source repos, public endpoints, and declared runtime economics in one place. Run proof receipts so buyers can trust what the agent does.",
      maxWidth: "lg",
    },
    match: (pathname) => startsWithSegment(pathname, "/my-agents"),
    breadcrumbs: myAgentsBreadcrumbs,
  },
  {
    id: "marketplace",
    label: "Marketplace",
    group: "Agents",
    path: "/marketplace",
    routePatterns: [
      "/marketplace/:view",
      "/marketplace/agents/:agentName",
      "/marketplace/agents/:agentName/install",
      "/marketplace/agents/:agentName/trial",
      "/marketplace/agents/:agentName/:section",
    ],
    description: "Evaluate public agents by proof state, running state, tools, install, and trial.",
    page: {
      eyebrow: "Marketplace",
      title: "Agents you can prove",
      description: "Every public agent the control plane knows about, with declared tools and proof history. Run candidates in Agent Trials before you depend on one.",
      maxWidth: "xl",
    },
    match: (pathname) => startsWithSegment(pathname, "/marketplace"),
    breadcrumbs: marketplaceBreadcrumbs,
  },
  {
    id: "studio",
    label: "Studio",
    group: "Agents",
    path: "/studio",
    routePatterns: ["/studio/runs/:runId"],
    description: "Describe an agent once; a crew builds, reviews, improves, and ships it.",
    page: {
      eyebrow: "Agent Studio",
      title: "Build an agent",
      description: "State the goal in plain language. The builder scaffolds it, the reviewer finds flaws, the editor fixes them, and it deploys with a live URL, a connector, and a signed proof.",
      maxWidth: "xl",
    },
    match: (pathname) => startsWithSegment(pathname, "/studio"),
    breadcrumbs: studioBreadcrumbs,
  },
  {
    id: "compose",
    label: "Compose",
    group: "Agents",
    path: "/compose",
    routePatterns: [
      "/compose/:step",
      "/compose/runs/:agentName",
      "/compose/runs/:agentName/:runId",
      "/compose/runs/:agentName/:runId/:runSection",
    ],
    description: "Build a composed agent by selecting agents, defining controls, and deploying a manifest.",
    page: {
      eyebrow: "Meta-agents",
      title: "Compose agents",
      description: "Select public or owned agents, expose the tools a planner may call, and deploy a manifest-backed coordinator.",
      maxWidth: "xl",
    },
    match: (pathname) => startsWithSegment(pathname, "/compose"),
    breadcrumbs: composeBreadcrumbs,
  },
  {
    id: "installed-agents",
    label: "Installed Setup",
    group: "Agents",
    path: "/installed-setup",
    routePatterns: [
      "/installed-setup/:agentName",
      "/installed-setup/:agentName/:view",
      "/installed-setup/:agentName/manage/:setupScope",
    ],
    aliases: ["/installed-agents"],
    redirects: [{ path: "/installed-agents" }],
    description: "Manage setup health, saved values, imported auth, and integration links.",
    page: {
      eyebrow: "Installed agents",
      title: "Saved agent setup",
      description: "Agents with setup values or imported auth credentials available to your account.",
      maxWidth: "xl",
    },
    match: (pathname) =>
      startsWithSegment(pathname, "/installed-setup") ||
      startsWithSegment(pathname, "/installed-agents"),
    breadcrumbs: installedAgentsBreadcrumbs,
  },
  {
    id: "bounties",
    label: "Bounties",
    group: "Agents",
    path: "/bounties",
    routePatterns: [
      "/bounties/new",
      "/bounties/:view",
      "/bounties/:view/:bountySlug",
      "/bounties/:view/:bountySlug/:section",
    ],
    description: "Request workflow for open, owned, claimed, and fulfilled agent work.",
    page: {
      eyebrow: "Bounties",
      title: "Demand-side requests",
      description: "Post what you need an agent to do. Someone with the relevant expertise wires up an agent that solves it. For private files or buyer acceptance checks, move the candidates into Agent Trials.",
      maxWidth: "lg",
    },
    featureFlag: "dashboard.bounties",
    nav: false,
    match: (pathname) => startsWithSegment(pathname, "/bounties"),
    breadcrumbs: bountyBreadcrumbs,
  },
  {
    id: "access",
    label: "Access",
    group: "Admin",
    path: "/access",
    routePatterns: [
      "/access/:view",
      "/access/langfuse/:orgSlug",
      "/access/litellm/:orgSlug",
      "/access/gitea/:orgSlug",
      "/access/repositories/:agentName",
    ],
    description: "Service inventory for Langfuse, LiteLLM, Gitea, copy values, and external links.",
    page: {
      eyebrow: "Admin",
      title: "Access",
      description: "Service links, login identities, and managed credential refs for the current account.",
      maxWidth: "lg",
    },
    match: (pathname) => startsWithSegment(pathname, "/access"),
    breadcrumbs: accessBreadcrumbs,
  },
  {
    id: "keys",
    label: "LLM Keys",
    group: "Admin",
    path: "/llm-keys",
    routePatterns: [
      "/llm-keys/:view",
      "/llm-keys/saved/:credName",
      "/llm-keys/saved/:credName/:section",
    ],
    aliases: ["/keys"],
    redirects: [{ path: "/keys" }],
    description: "Saved provider keys, default key, model health, and add or replace controls.",
    page: {
      eyebrow: "Settings",
      title: "LLM credentials",
      description: "Chat and LLM-capable agents run on your saved provider credentials. The platform routes them through LiteLLM for observability and policy.",
      maxWidth: "md",
    },
    match: (pathname) =>
      startsWithSegment(pathname, "/llm-keys") ||
      startsWithSegment(pathname, "/keys"),
    breadcrumbs: keysBreadcrumbs,
  },
  {
    id: "organization",
    label: "Organization",
    group: "Admin",
    path: "/organization",
    routePatterns: [
      "/organization/:orgSlug",
      "/organization/:orgSlug/:view",
      "/organization/:orgSlug/domains/:domainName",
      "/organization/:orgSlug/scim/tokens/:scimTokenId",
      "/organization/:orgSlug/members/:memberId",
      "/organization/:orgSlug/audit/:auditLogId",
      "/organization/*",
    ],
    description: "Organizations, governance, domains, SCIM, members, and audit history.",
    page: {
      eyebrow: "Identity",
      title: "Enterprise governance",
      description: "Organization access, verified domains, SCIM provisioning, and audit history.",
      maxWidth: "xl",
    },
    match: (pathname) => startsWithSegment(pathname, "/organization"),
    breadcrumbs: organizationBreadcrumbs,
  },
  {
    id: "compliance",
    label: "Compliance",
    group: "Admin",
    path: "/compliance",
    routePatterns: [
      "/compliance/:orgSlug",
      "/compliance/:orgSlug/:view",
      "/compliance/*",
    ],
    description: "EU AI Act readiness, decision records, retention policy, and agent risk classification.",
    page: {
      eyebrow: "Governance",
      title: "Compliance",
      description: "EU AI Act readiness for the organization: tamper-evident decision records, retention policy, evidence export, and agent risk classification.",
      maxWidth: "xl",
    },
    match: (pathname) => startsWithSegment(pathname, "/compliance"),
    breadcrumbs: complianceBreadcrumbs,
  },
  {
    id: "runtime",
    label: "Runtime",
    group: "Admin",
    path: "/runtime",
    layout: "full-height",
    routePatterns: [
      ...RUNTIME_VIEWS.filter((view) => view.id !== "state").map((view) => view.path),
      "/runtime/policy/:policySection",
      "/runtime/timeline/receipts/:receiptToken",
      "/runtime/timeline/receipts/:receiptToken/:receiptSection",
    ],
    aliases: ["/control-room"],
    redirects: [{ path: "/control-room" }],
    description: "Runtime state, live DAG runs, routing, and policy internals.",
    match: (pathname) =>
      startsWithSegment(pathname, "/runtime") ||
      startsWithSegment(pathname, "/control-room"),
    breadcrumbs: runtimeBreadcrumbs,
  },
];

export const ROUTE_BY_ID = Object.fromEntries(
  DASHBOARD_ROUTES.map((route) => [route.id, route]),
) as Record<DashboardRouteId, DashboardRouteConfig>;

export const DASHBOARD_NAV_SECTIONS: readonly DashboardNavSection[] = [
  {
    id: "start",
    label: "Start",
    items: [
      {
        id: "chat",
        routeId: "workspace",
        label: "Chat",
        path: "/workspace",
        description: "Main-agent conversation, approvals, and streaming work.",
        aliases: ["workspace", "main agent"],
      },
      {
        id: "threads",
        routeId: "workspace",
        label: "Threads",
        path: "/workspace/threads",
        description: "Search, reopen, and manage saved chat threads.",
      },
      {
        id: "files",
        routeId: "workspace",
        label: "Files",
        path: "/workspace/files",
        description: "Upload, browse, preview, move, and delete workspace files.",
      },
      {
        id: "artifacts",
        routeId: "workspace",
        label: "Artifacts",
        path: "/workspace/artifacts",
        description: "Scan generated files and downloadable outputs.",
      },
      {
        id: "thread-settings",
        routeId: "workspace",
        label: "Thread settings",
        path: "/workspace/settings",
        description: "Thread-scoped budgets, policy gates, and approved agent overrides.",
        aliases: ["chat defaults", "thread controls"],
      },
      {
        id: "thread-activity",
        routeId: "workspace",
        label: "Thread activity",
        path: "/workspace/activity",
        description: "Thread-scoped handoffs, DAG runs, file operations, and LLM usage.",
      },
    ],
  },
  {
    id: "operate",
    label: "Operate",
    items: [
      {
        id: "runs",
        routeId: "activity",
        label: "Runs",
        path: "/activity",
        description: "Global work ledger for jobs, timelines, receipts, and artifacts.",
        aliases: ["activity", "work ledger"],
      },
      {
        id: "trials",
        routeId: "trials",
        label: "Trials",
        path: "/trials",
        description: "Compare candidate agents against the same goal, files, scores, and receipts.",
      },
      {
        id: "schedules",
        routeId: "schedules",
        label: "Schedules",
        path: "/schedules",
        description: "Create and operate recurring orchestrator or agent runs.",
        children: [
          {
            id: "schedules-list",
            routeId: "schedules",
            label: "All schedules",
            path: "/schedules/list",
            description: "Inspect recurring runs, owners, targets, cadence, and health.",
          },
          {
            id: "schedules-create",
            routeId: "schedules",
            label: "New schedule",
            path: "/schedules/create",
            description: "Create a recurring orchestrator or agent run with saved inputs.",
          },
        ],
      },
    ],
  },
  {
    id: "agents",
    label: "Build",
    items: [
      {
        id: "studio",
        routeId: "studio",
        label: "Studio",
        path: "/studio",
        description: "Describe an agent once; a crew builds, reviews, improves, and ships it.",
        aliases: ["new agent", "describe an agent", "build an agent", "agent studio"],
      },
      {
        id: "my-agents",
        routeId: "my-agents",
        label: "My agents",
        path: "/my-agents",
        description: "Owned agents, proofs, health, deployments, and updates.",
      },
      {
        id: "marketplace",
        routeId: "marketplace",
        label: "Marketplace",
        path: "/marketplace",
        description: "Evaluate public agents by proof state, tools, install, and trial.",
        children: [
          {
            id: "marketplace-proofs",
            routeId: "marketplace",
            label: "Proofs",
            path: "/marketplace/proofs",
            description: "Review proof coverage, latest receipts, tool evidence, and verification gaps.",
          },
        ],
      },
      {
        id: "compose",
        routeId: "compose",
        label: "Compose",
        path: "/compose",
        description: "Build a composed agent by selecting agents, defining controls, and deploying a manifest.",
        aliases: ["meta agent"],
      },
      {
        id: "installed-setup",
        routeId: "installed-agents",
        label: "Installed setup",
        path: "/installed-setup",
        description: "Manage setup health, saved values, imported auth, and integration links.",
      },
      {
        id: "bounties",
        routeId: "bounties",
        label: "Bounties",
        path: "/bounties/open",
        description: "Demand-side requests ready for an agent solution.",
        featureFlag: "dashboard.bounties",
      },
    ],
  },
  {
    id: "runtime",
    label: "Runtime",
    items: [
      {
        id: "runtime-overview",
        routeId: "runtime",
        label: "Overview",
        path: "/runtime",
        description: "Runtime health, live DAG runs, spend posture, and the latest receipt.",
        aliases: ["control room", "runtime health"],
      },
      {
        id: "runtime-timeline",
        routeId: "runtime",
        label: "Timeline",
        path: "/runtime/timeline",
        description: "Live DAGs, receipt streams, routing, spend, file operations, and proof status.",
        aliases: ["runtime ledger", "receipts"],
      },
      {
        id: "runtime-policy",
        routeId: "runtime",
        label: "Policy",
        path: "/runtime/policy",
        description: "Budget caps, approvals, network posture, PII-safe mode, and agent allowlists.",
        aliases: ["guardrails", "budget caps"],
      },
      {
        id: "simulations",
        routeId: "simulations",
        label: "Simulations",
        path: "/simulations",
        description: "Simulation specs, replay evidence, and evolution inspection.",
        featureFlag: "dashboard.simulations",
      },
    ],
  },
  {
    id: "admin",
    label: "Settings",
    items: [
      {
        id: "access",
        routeId: "access",
        label: "Access",
        path: "/access",
        description: "Service links, login identities, and managed credential refs.",
        children: [
          {
            id: "access-langfuse",
            routeId: "access",
            label: "Langfuse",
            path: "/access/langfuse",
            description: "Observability projects, login identities, project links, and credential refs.",
          },
          {
            id: "access-litellm",
            routeId: "access",
            label: "LiteLLM",
            path: "/access/litellm",
            description: "OpenAI-compatible gateway teams, keys, base URLs, and setup errors.",
          },
          {
            id: "access-gitea",
            routeId: "access",
            label: "Gitea",
            path: "/access/gitea",
            description: "Managed source-control organizations and account-level Gitea access.",
          },
          {
            id: "access-repositories",
            routeId: "access",
            label: "Repositories",
            path: "/access/repositories",
            description: "Agent repositories, visibility, source links, and marketplace agent links.",
          },
        ],
      },
      {
        id: "llm-keys",
        routeId: "keys",
        label: "LLM keys",
        path: "/llm-keys",
        description: "Saved provider keys, default key, model health, and add or replace controls.",
        aliases: ["credentials", "provider key"],
        children: [
          {
            id: "llm-keys-saved",
            routeId: "keys",
            label: "Saved keys",
            path: "/llm-keys/saved",
            description: "Manage saved provider keys, credential refs, and replacement flows.",
          },
          {
            id: "llm-keys-add",
            routeId: "keys",
            label: "Add key",
            path: "/llm-keys/add",
            description: "Add or replace a provider key for chat and LLM-capable agents.",
          },
        ],
      },
      {
        id: "organization",
        routeId: "organization",
        label: "Organization",
        path: "/organization",
        description: "Organization access, verified domains, SCIM provisioning, and audit history.",
      },
      {
        id: "compliance",
        routeId: "compliance",
        label: "Compliance",
        path: "/compliance",
        description: "EU AI Act readiness, decision records, retention policy, and agent risk tiers.",
        aliases: ["eu ai act", "decision records", "evidence pack", "retention"],
      },
    ],
  },
];

export function normalizePathname(pathname: string) {
  const normalized = pathname.replace(/\/+$/, "") || "/";
  return normalized === "/" ? "/" : normalized;
}

export function workspaceViewForPath(pathname: string): WorkspaceView {
  const normalized = normalizePathname(pathname);
  const routedView = WORKSPACE_VIEWS
    .filter((view) => view.id !== "chat")
    .find((view) => startsWithSegment(normalized, view.path));
  return routedView || WORKSPACE_VIEW_BY_ID.chat;
}

export function workspaceActivityHref(threadId?: string | null) {
  return threadId
    ? `/workspace/activity?thread=${encodeURIComponent(threadId)}`
    : "/workspace/activity";
}

export function runtimeControlReceiptHref(
  receiptPath: string,
  section = "summary",
) {
  const suffix = section && section !== "summary" ? `/${encodeURIComponent(section)}` : "";
  return `/runtime/timeline/receipts/${encodeRuntimeControlReceiptToken(receiptPath)}${suffix}`;
}

function encodeRuntimeControlReceiptToken(value: string) {
  const bytes = new TextEncoder().encode(value);
  let binary = "";
  bytes.forEach((byte) => {
    binary += String.fromCharCode(byte);
  });
  return btoa(binary)
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

export type WorkspaceArtifactRouteView = "files" | "artifacts";

export function workspaceArtifactHref(
  path: string,
  view: WorkspaceArtifactRouteView = "artifacts",
  search = "",
) {
  const cleanPath = path.trim().replace(/^\/+/, "");
  const encodedPath = cleanPath
    .split("/")
    .filter(Boolean)
    .map((segment) => encodeURIComponent(segment))
    .join("/");
  const base = view === "files" ? "/workspace/files/file" : "/workspace/artifacts/file";
  return `${base}/${encodedPath}${search}`;
}

export function workspaceArtifactPathFromPathname(
  pathname: string,
  view: WorkspaceArtifactRouteView,
) {
  const normalized = normalizePathname(pathname);
  const prefix =
    view === "files"
      ? "/workspace/files/file/"
      : "/workspace/artifacts/file/";
  if (!normalized.startsWith(prefix)) return null;
  const encodedPath = normalized.slice(prefix.length);
  if (!encodedPath) return null;
  return encodedPath
    .split("/")
    .filter(Boolean)
    .map((segment) => decodeSegment(segment))
    .join("/");
}

export function decodeRouteSegment(segment: string | null | undefined): string | null {
  if (!segment) return null;
  try {
    return decodeURIComponent(segment);
  } catch {
    return segment;
  }
}

function isAgentDetailSection(
  value: string | null | undefined,
): value is AgentDetailSection {
  return AGENT_DETAIL_SECTION_IDS.has(value as AgentDetailSection);
}

export function normalizeAgentDetailSection(
  value: string | null | undefined,
): AgentDetailSection {
  return isAgentDetailSection(value) ? value : "overview";
}

function agentDetailSectionLabel(value: string | null | undefined) {
  return AGENT_DETAIL_SECTIONS.find((section) => section.id === value)?.label
    ?? titleFromSegment(value || "");
}

export function runtimeViewForPath(pathname: string): RuntimeView {
  const normalized = normalizePathname(pathname);
  const routedView = RUNTIME_VIEWS
    .filter((view) => view.id !== "state")
    .find((view) => startsWithSegment(normalized, view.path));
  if (routedView) return routedView;
  return RUNTIME_VIEW_BY_ID.state;
}

function isTrialRoomViewId(
  value: string | null | undefined,
): value is TrialRoomViewId {
  return TRIAL_ROOM_VIEW_IDS.has(value as TrialRoomViewId);
}

export function normalizeTrialRoomViewId(
  value: string | null | undefined,
): TrialRoomViewId {
  return isTrialRoomViewId(value) ? value : "overview";
}

function trialRoomViewForPath(pathname: string): TrialRoomView {
  const viewId = normalizeTrialRoomViewId(pathSegments(pathname)[2]);
  return TRIAL_ROOM_VIEW_BY_ID[viewId];
}

export function marketplaceViewForPath(pathname: string): MarketplaceView {
  const normalized = normalizePathname(pathname);
  const routedView = MARKETPLACE_VIEWS
    .filter((view) => view.id !== "browse")
    .find((view) => startsWithSegment(normalized, view.path));
  return routedView || MARKETPLACE_VIEW_BY_ID.browse;
}

function isBountyViewId(
  value: string | null | undefined,
): value is BountyViewId {
  return BOUNTY_VIEW_IDS.has(value as BountyViewId);
}

export function normalizeBountyViewId(value: string | null | undefined): BountyViewId {
  return isBountyViewId(value) ? value : "open";
}

function bountyViewForPath(pathname: string): BountyView {
  const viewId = normalizeBountyViewId(pathSegments(pathname)[1]);
  return BOUNTY_VIEW_BY_ID[viewId];
}

function isAccessViewId(
  value: string | null | undefined,
): value is AccessViewId {
  return ACCESS_VIEW_IDS.has(value as AccessViewId);
}

function normalizeAccessViewId(
  value: string | null | undefined,
): AccessViewId {
  return isAccessViewId(value) ? value : "overview";
}

export function accessViewForPath(pathname: string): AccessView {
  const viewId = normalizeAccessViewId(pathSegments(pathname)[1]);
  return ACCESS_VIEW_BY_ID[viewId];
}

function isOrganizationViewId(
  value: string | null | undefined,
): value is OrganizationViewId {
  return ORGANIZATION_VIEW_IDS.has(value as OrganizationViewId);
}

function normalizeOrganizationViewId(
  value: string | null | undefined,
): OrganizationViewId {
  return isOrganizationViewId(value) ? value : "overview";
}

export function organizationViewForPath(pathname: string): OrganizationView {
  const viewId = normalizeOrganizationViewId(pathSegments(pathname)[2]);
  return ORGANIZATION_VIEW_BY_ID[viewId];
}

function isComplianceViewId(
  value: string | null | undefined,
): value is ComplianceViewId {
  return COMPLIANCE_VIEW_IDS.has(value as ComplianceViewId);
}

function normalizeComplianceViewId(
  value: string | null | undefined,
): ComplianceViewId {
  return isComplianceViewId(value) ? value : "records";
}

export function complianceViewForPath(pathname: string): ComplianceView {
  const viewId = normalizeComplianceViewId(pathSegments(pathname)[2]);
  return COMPLIANCE_VIEW_BY_ID[viewId];
}

function isLlmKeyViewId(
  value: string | null | undefined,
): value is LlmKeyViewId {
  return LLM_KEY_VIEW_IDS.has(value as LlmKeyViewId);
}

function normalizeLlmKeyViewId(
  value: string | null | undefined,
): LlmKeyViewId {
  return isLlmKeyViewId(value) ? value : "overview";
}

export function llmKeyViewForPath(pathname: string): LlmKeyView {
  const segments = pathSegments(pathname);
  const viewId = normalizeLlmKeyViewId(
    segments[0] === "keys" ? segments[1] : segments[1],
  );
  return LLM_KEY_VIEW_BY_ID[viewId];
}

function isInstalledAgentViewId(
  value: string | null | undefined,
): value is InstalledAgentViewId {
  return INSTALLED_AGENT_VIEW_IDS.has(value as InstalledAgentViewId);
}

export function normalizeInstalledAgentViewId(
  value: string | null | undefined,
): InstalledAgentViewId {
  return isInstalledAgentViewId(value) ? value : "setup";
}

function installedAgentViewForPath(pathname: string): InstalledAgentView {
  const viewId = normalizeInstalledAgentViewId(pathSegments(pathname)[2]);
  return INSTALLED_AGENT_VIEW_BY_ID[viewId];
}

function isScheduleViewId(
  value: string | null | undefined,
): value is ScheduleViewId {
  return SCHEDULE_VIEW_IDS.has(value as ScheduleViewId);
}

function normalizeScheduleViewId(
  value: string | null | undefined,
): ScheduleViewId {
  return isScheduleViewId(value) ? value : "overview";
}

export function scheduleViewForPath(pathname: string): ScheduleView {
  const viewId = normalizeScheduleViewId(pathSegments(pathname)[1]);
  return SCHEDULE_VIEW_BY_ID[viewId];
}

export function simulationViewForPath(pathname: string): SimulationView {
  const normalized = normalizePathname(pathname);
  const routedView = SIMULATION_VIEWS
    .filter((view) => view.id !== "builder")
    .find((view) => startsWithSegment(normalized, view.path));
  return routedView || SIMULATION_VIEW_BY_ID.builder;
}

function isComposeStepId(
  value: string | null | undefined,
): value is ComposeStepId {
  return COMPOSE_STEP_IDS.has(value as ComposeStepId);
}

export function normalizeComposeStepId(value: string | null | undefined): ComposeStepId {
  if (value === "review") return "manifest";
  return isComposeStepId(value) ? value : "select";
}

function composeStepForPath(pathname: string): ComposeStep {
  const stepId = normalizeComposeStepId(pathSegments(pathname)[1]);
  return COMPOSE_STEP_BY_ID[stepId];
}

export function isRouteEnabled(
  route: DashboardRouteConfig,
  enabledFeatureFlags: Set<string>,
) {
  return !route.featureFlag || enabledFeatureFlags.has(route.featureFlag);
}

export function visibleDashboardNavSections(
  enabledFeatureFlags: Set<string>,
): DashboardNavSection[] {
  return DASHBOARD_NAV_SECTIONS.map((section) => ({
    ...section,
    items: section.items
      .map((item) => visibleDashboardNavItem(item, enabledFeatureFlags))
      .filter((item): item is DashboardNavItem => item !== null),
  })).filter((section) => section.items.length > 0);
}

export function flattenDashboardNavItems(
  sections: readonly DashboardNavSection[],
): DashboardNavItem[] {
  return flattenDashboardNavEntries(sections).map((entry) => entry.item);
}

export function activeDashboardNavItem(
  pathname: string,
  enabledFeatureFlags: Set<string>,
): DashboardNavItem | null {
  const normalized = normalizePathname(pathname);
  const sections = visibleDashboardNavSections(enabledFeatureFlags);
  const items = flattenDashboardNavItems(sections);
  const direct = [...items]
    .sort((a, b) => normalizePathname(b.path).length - normalizePathname(a.path).length)
    .find((item) => startsWithSegment(normalized, item.path));
  if (direct) return direct;

  const route = matchDashboardRoute(normalized)?.route;
  if (!route) return null;
  return items.find((item) => item.routeId === route.id) ?? null;
}

export function commandNavigationItems(
  enabledFeatureFlags: Set<string>,
): DashboardCommandItem[] {
  const sections = visibleDashboardNavSections(enabledFeatureFlags);
  const commands = flattenDashboardNavEntries(sections).map((entry) =>
    navEntryToCommandItem(entry),
  );
  return uniqueCommandItems(commands);
}

export function searchCommandNavigationItems(
  items: DashboardCommandItem[],
  query: string,
): DashboardCommandItem[] {
  const normalizedQuery = normalizeSearchQuery(query);
  if (!normalizedQuery) return items;
  const terms = normalizedQuery.split(" ");

  return items
    .map((item, index) => ({
      item,
      index,
      score: commandSearchScore(item, normalizedQuery, terms),
    }))
    .filter((entry): entry is {
      item: DashboardCommandItem;
      index: number;
      score: number;
    } => entry.score !== null)
    .sort((a, b) => b.score - a.score || a.index - b.index)
    .map((entry) => entry.item);
}

export function matchDashboardRoute(pathname: string): DashboardRouteMatch | null {
  const normalized = normalizePathname(pathname);
  const route = DASHBOARD_ROUTES.find((candidate) =>
    candidate.match
      ? candidate.match(normalized)
      : matchesRoutePath(normalized, candidate.path, candidate.aliases, candidate.redirects),
  );
  if (!route) return null;
  return {
    route,
    breadcrumbs: route.breadcrumbs?.(normalized) ?? [{ label: route.label, href: route.path }],
  };
}

type DashboardNavEntry = {
  section: DashboardNavSection;
  item: DashboardNavItem;
  parents: DashboardNavItem[];
};

function visibleDashboardNavItem(
  item: DashboardNavItem,
  enabledFeatureFlags: Set<string>,
): DashboardNavItem | null {
  const route = ROUTE_BY_ID[item.routeId];
  const itemFeatureEnabled =
    !item.featureFlag || enabledFeatureFlags.has(item.featureFlag);
  if (!itemFeatureEnabled || !isRouteEnabled(route, enabledFeatureFlags)) return null;

  const children = item.children
    ?.map((child) => visibleDashboardNavItem(child, enabledFeatureFlags))
    .filter((child): child is DashboardNavItem => child !== null);

  return children ? { ...item, children } : { ...item };
}

function flattenDashboardNavEntries(
  sections: readonly DashboardNavSection[],
): DashboardNavEntry[] {
  const entries: DashboardNavEntry[] = [];
  for (const section of sections) {
    for (const item of section.items) {
      pushDashboardNavEntry(entries, section, item, []);
    }
  }
  return entries;
}

function pushDashboardNavEntry(
  entries: DashboardNavEntry[],
  section: DashboardNavSection,
  item: DashboardNavItem,
  parents: DashboardNavItem[],
) {
  entries.push({ section, item, parents });
  for (const child of item.children ?? []) {
    pushDashboardNavEntry(entries, section, child, [...parents, item]);
  }
}

function navEntryToCommandItem({
  section,
  item,
  parents,
}: DashboardNavEntry): DashboardCommandItem {
  const route = ROUTE_BY_ID[item.routeId];
  const labels = [...parents.map((parent) => parent.label), item.label];
  return {
    id: item.routeId,
    label: labels.join(" / "),
    group: section.label,
    path: item.path,
    description: item.description,
    aliases: [
      section.label,
      route.label,
      ...parents.map((parent) => parent.label),
      item.label,
      ...(route.aliases ?? []),
      ...(item.aliases ?? []),
    ],
  };
}

function uniqueCommandItems(items: DashboardCommandItem[]): DashboardCommandItem[] {
  const seenPaths = new Set<string>();
  return items.filter((item) => {
    const key = normalizePathname(item.path);
    if (seenPaths.has(key)) return false;
    seenPaths.add(key);
    return true;
  });
}

function normalizeSearchQuery(value: string) {
  return value.trim().toLowerCase().replace(/\s+/g, " ");
}

function commandSearchScore(
  item: DashboardCommandItem,
  query: string,
  terms: string[],
) {
  const label = item.label.toLowerCase();
  const group = item.group.toLowerCase();
  const path = item.path.toLowerCase();
  const description = item.description.toLowerCase();
  const aliases = item.aliases.map((alias) => alias.toLowerCase());
  const searchText = [label, group, path, description, ...aliases].join(" ");

  if (!terms.every((term) => searchText.includes(term))) return null;

  let score = 0;
  if (path === query) score += 150;
  if (label === query) score += 130;
  if (path.startsWith(query)) score += 110;
  if (label.startsWith(query)) score += 100;
  if (aliases.some((alias) => alias === query)) score += 90;
  if (aliases.some((alias) => alias.startsWith(query))) score += 70;
  if (path.includes(query)) score += 55;
  if (label.includes(query)) score += 45;
  if (description.includes(query)) score += 30;
  if (group.includes(query)) score += 10;

  for (const term of terms) {
    if (path.includes(term)) score += 12;
    if (label.includes(term)) score += 10;
    if (aliases.some((alias) => alias.includes(term))) score += 8;
    if (description.includes(term)) score += 4;
    if (group.includes(term)) score += 2;
  }

  return score;
}

function matchesRoutePath(
  pathname: string,
  path: string,
  aliases: string[] = [],
  redirects: DashboardRouteRedirect[] = [],
) {
  return [path, ...aliases, ...redirects.map((redirect) => redirect.path)].some(
    (candidate) => normalizePathname(candidate) === pathname,
  );
}

function startsWithSegment(pathname: string, basePath: string) {
  const normalized = normalizePathname(pathname);
  const base = normalizePathname(basePath);
  return normalized === base || normalized.startsWith(`${base}/`);
}

function workspaceBreadcrumbs(pathname: string): Breadcrumb[] {
  const segments = pathSegments(pathname);
  const crumbs: Breadcrumb[] = [{ label: "Workspace", href: "/workspace" }];
  const view = workspaceViewForPath(pathname);
  if (view.id !== "chat") {
    crumbs.push({ label: view.label, href: view.path });
  }
  if (view.id === "settings" && segments[2]) {
    const settingsSectionLabel =
      WORKSPACE_SETTINGS_SECTION_LABELS[
        segments[2] as keyof typeof WORKSPACE_SETTINGS_SECTION_LABELS
      ];
    if (settingsSectionLabel) {
      crumbs.push({
        label: settingsSectionLabel,
        href: `/workspace/settings/${segments[2]}`,
      });
    }
  }
  if (
    (view.id === "files" || view.id === "artifacts") &&
    segments[2] === "file" &&
    segments[3]
  ) {
    const artifactPath = segments.slice(3).map(decodeSegment).join("/");
    crumbs.push({
      label: shortenMiddle(artifactPath, 28),
      href: workspaceArtifactHref(artifactPath, view.id),
    });
  }
  if (view.id === "activity" && segments[2] && segments[3]) {
    crumbs.push({
      label: `${titleFromSegment(segments[2])}: ${shortenMiddle(decodeSegment(segments[3]), 22)}`,
      href: `/workspace/activity/${segments[2]}/${segments[3]}`,
    });
    if (segments[4]) {
      crumbs.push({
        label: titleFromSegment(segments[4]),
        href: `/workspace/activity/${segments[2]}/${segments[3]}/${segments[4]}`,
      });
    }
  }
  return crumbs;
}

function myAgentsBreadcrumbs(pathname: string): Breadcrumb[] {
  const segments = pathSegments(pathname);
  const crumbs: Breadcrumb[] = [{ label: "My Agents", href: "/my-agents" }];
  const encodedAgent = segments[1];
  if (!encodedAgent) return crumbs;
  if (encodedAgent === "import") {
    crumbs.push({ label: "Import", href: "/my-agents/import" });
    return crumbs;
  }

  const agent = decodeSegment(encodedAgent);
  const agentHref = `/my-agents/${encodedAgent}`;
  crumbs.push({ label: agent, href: agentHref });

  const section = segments[2];
  if (!section) return crumbs;
  if (section === "domains") {
    crumbs.push({ label: "Domains", href: `${agentHref}/domains` });
    if (segments[3]) {
      crumbs.push({
        label: shortenMiddle(decodeSegment(segments[3]), 28),
        href: `${agentHref}/domains/${segments[3]}`,
      });
    }
    return crumbs;
  }
  if (isAgentDetailSection(section)) {
    crumbs.push({ label: agentDetailSectionLabel(section), href: `${agentHref}/${section}` });
  }
  if (section === "runs" && segments[3]) {
    crumbs.push({
      label: shortenMiddle(decodeSegment(segments[3]), 24),
      href: `${agentHref}/runs/${segments[3]}`,
    });
    if (segments[4]) {
      crumbs.push({
        label: titleFromSegment(segments[4]),
        href: `${agentHref}/runs/${segments[3]}/${segments[4]}`,
      });
    }
  }
  if (section === "calls" && segments[3]) {
    crumbs.push({ label: "Insights", href: `${agentHref}/insights` });
    crumbs.push({
      label: `Call ${shortenMiddle(decodeSegment(segments[3]), 18)}`,
      href: `${agentHref}/calls/${segments[3]}`,
    });
  }
  if (section === "proofs" && segments[3]) {
    crumbs.push({
      label: `Proof ${shortenMiddle(decodeSegment(segments[3]), 18)}`,
      href: `${agentHref}/proofs/${segments[3]}`,
    });
  }
  if (section === "evidence" && segments[3]) {
    crumbs.push({
      label: titleFromSegment(segments[3]),
      href: `${agentHref}/evidence/${segments[3]}`,
    });
  }
  return crumbs;
}

function activityBreadcrumbs(pathname: string): Breadcrumb[] {
  const segments = pathSegments(pathname);
  const crumbs: Breadcrumb[] = [{ label: "Activity", href: "/activity" }];
  const encodedJobId = segments[1];
  if (!encodedJobId) return crumbs;
  crumbs.push({
    label: shortenMiddle(decodeSegment(encodedJobId), 28),
    href: `/activity/${encodedJobId}`,
  });
  if (segments[2]) {
    crumbs.push({
      label: titleFromSegment(segments[2]),
      href: `/activity/${encodedJobId}/${segments[2]}`,
    });
  }
  return crumbs;
}

function keysBreadcrumbs(pathname: string): Breadcrumb[] {
  const segments = pathSegments(pathname);
  const crumbs: Breadcrumb[] = [{ label: "LLM Keys", href: "/llm-keys" }];
  const view = llmKeyViewForPath(pathname);
  if (view.id !== "overview") {
    crumbs.push({ label: view.label, href: view.path });
  }
  if (segments[1] === "saved" && segments[2]) {
    crumbs.push({
      label: shortenMiddle(decodeSegment(segments[2]), 28),
      href: `/llm-keys/saved/${segments[2]}`,
    });
    if (segments[3]) {
      crumbs.push({
        label: titleFromSegment(segments[3]),
        href: `/llm-keys/saved/${segments[2]}/${segments[3]}`,
      });
    }
  }
  return crumbs;
}

function installedAgentsBreadcrumbs(pathname: string): Breadcrumb[] {
  const segments = pathSegments(pathname);
  const crumbs: Breadcrumb[] = [{ label: "Installed Setup", href: "/installed-setup" }];
  if (segments[1]) {
    crumbs.push({
      label: shortenMiddle(decodeSegment(segments[1]), 28),
      href: `/installed-setup/${segments[1]}`,
    });
  }
  if (segments[2]) {
    if (segments[2] === "manage") {
      crumbs.push({
        label: "Manage setup",
        href: `/installed-setup/${segments[1]}/manage`,
      });
      if (segments[3]) {
        crumbs.push({
          label: segments[3] === "org" ? "Organization" : "For me",
          href: `/installed-setup/${segments[1]}/manage/${segments[3]}`,
        });
      }
      return crumbs;
    }
    const view = installedAgentViewForPath(pathname);
    if (view.id !== "setup") {
      crumbs.push({ label: view.label, href: `/installed-setup/${segments[1]}/${view.id}` });
    }
  }
  return crumbs;
}

function schedulesBreadcrumbs(pathname: string): Breadcrumb[] {
  const segments = pathSegments(pathname);
  const crumbs: Breadcrumb[] = [{ label: "Schedules", href: "/schedules" }];
  const view = scheduleViewForPath(pathname);
  if (view.id !== "overview") {
    crumbs.push({ label: view.label, href: view.path });
  }
  if (segments[1] === "list" && segments[2]) {
    crumbs.push({
      label: shortenMiddle(decodeSegment(segments[2]), 24),
      href: `/schedules/list/${segments[2]}`,
    });
  }
  if (segments[1] === "list" && segments[2] && segments[3]) {
    crumbs.push({
      label: titleFromSegment(segments[3]),
      href: `/schedules/list/${segments[2]}/${segments[3]}`,
    });
  }
  return crumbs;
}

function simulationBreadcrumbs(pathname: string): Breadcrumb[] {
  const segments = pathSegments(pathname);
  const crumbs: Breadcrumb[] = [{ label: "Simulations", href: "/simulations" }];
  const view = simulationViewForPath(pathname);
  if (view.id !== "builder") {
    crumbs.push({ label: view.label, href: view.path });
  }
  if (segments[1] === "runs" && segments[2]) {
    crumbs.push({
      label: shortenMiddle(decodeSegment(segments[2]), 24),
      href: `/simulations/runs/${segments[2]}`,
    });
  }
  if (segments[1] === "runs" && segments[2] && segments[3]) {
    crumbs.push({
      label: titleFromSegment(segments[3]),
      href: `/simulations/runs/${segments[2]}/${segments[3]}`,
    });
  }
  return crumbs;
}

function trialBreadcrumbs(pathname: string): Breadcrumb[] {
  const segments = pathSegments(pathname);
  const crumbs: Breadcrumb[] = [{ label: "Trials", href: "/trials" }];
  if (segments[1] === "new") {
    crumbs.push({ label: "New", href: "/trials/new" });
    return crumbs;
  }
  if (segments[1]) {
    crumbs.push({
      label: shortenMiddle(decodeSegment(segments[1]), 28),
      href: `/trials/${segments[1]}`,
    });
  }
  if (segments[2] === "runs" && segments[3]) {
    crumbs.push({
      label: `Run ${shortenMiddle(decodeSegment(segments[3]), 18)}`,
      href: `/trials/${segments[1]}/runs/${segments[3]}`,
    });
    if (segments[4]) {
      crumbs.push({
        label: titleFromSegment(segments[4]),
        href: `/trials/${segments[1]}/runs/${segments[3]}/${segments[4]}`,
      });
    }
    return crumbs;
  }
  if (segments[2]) {
    const view = trialRoomViewForPath(pathname);
    if (view.id !== "overview") {
      crumbs.push({
        label: view.label,
        href: `/trials/${segments[1]}/${view.id}`,
      });
    }
  }
  return crumbs;
}

function marketplaceBreadcrumbs(pathname: string): Breadcrumb[] {
  const segments = pathSegments(pathname);
  const crumbs: Breadcrumb[] = [{ label: "Marketplace", href: "/marketplace" }];
  if (segments[1] === "agents" && segments[2]) {
    crumbs.push({
      label: shortenMiddle(decodeSegment(segments[2]), 28),
      href: `/marketplace/agents/${segments[2]}`,
    });
    if (segments[3] === "install") {
      crumbs.push({
        label: "Install",
        href: `/marketplace/agents/${segments[2]}/install`,
      });
    }
    if (segments[3] === "trial") {
      crumbs.push({
        label: "Run trial",
        href: `/marketplace/agents/${segments[2]}/trial`,
      });
    }
    if (segments[3] && segments[3] !== "install" && segments[3] !== "trial") {
      crumbs.push({
        label: titleFromSegment(segments[3]),
        href: `/marketplace/agents/${segments[2]}/${segments[3]}`,
      });
    }
    return crumbs;
  }
  const view = marketplaceViewForPath(pathname);
  if (view.id !== "browse") {
    crumbs.push({ label: view.label, href: view.path });
  }
  return crumbs;
}

function bountyBreadcrumbs(pathname: string): Breadcrumb[] {
  const segments = pathSegments(pathname);
  const crumbs: Breadcrumb[] = [{ label: "Bounties", href: "/bounties" }];
  if (segments[1] === "new") {
    crumbs.push({ label: "New", href: "/bounties/new" });
    return crumbs;
  }
  const view = bountyViewForPath(pathname);
  if (view.id !== "open") {
    crumbs.push({ label: view.label, href: view.path });
  }
  if (segments[2]) {
    crumbs.push({
      label: shortenMiddle(decodeSegment(segments[2]), 28),
      href: `/bounties/${view.id}/${segments[2]}`,
    });
  }
  if (segments[3]) {
    crumbs.push({
      label: titleFromSegment(segments[3]),
      href: `/bounties/${view.id}/${segments[2]}/${segments[3]}`,
    });
  }
  return crumbs;
}

function accessBreadcrumbs(pathname: string): Breadcrumb[] {
  const segments = pathSegments(pathname);
  const crumbs: Breadcrumb[] = [{ label: "Access", href: "/access" }];
  const view = accessViewForPath(pathname);
  if (view.id !== "overview") {
    crumbs.push({ label: view.label, href: view.path });
  }
  if (segments[1] === "langfuse" && segments[2]) {
    crumbs.push({
      label: shortenMiddle(decodeSegment(segments[2]), 24),
      href: `/access/langfuse/${segments[2]}`,
    });
  }
  if (segments[1] === "litellm" && segments[2]) {
    crumbs.push({
      label: shortenMiddle(decodeSegment(segments[2]), 24),
      href: `/access/litellm/${segments[2]}`,
    });
  }
  if (segments[1] === "gitea" && segments[2]) {
    crumbs.push({
      label: shortenMiddle(decodeSegment(segments[2]), 24),
      href: `/access/gitea/${segments[2]}`,
    });
  }
  if (segments[1] === "repositories" && segments[2]) {
    crumbs.push({
      label: shortenMiddle(decodeSegment(segments[2]), 24),
      href: `/access/repositories/${segments[2]}`,
    });
  }
  return crumbs;
}

function studioBreadcrumbs(pathname: string): Breadcrumb[] {
  const segments = pathSegments(pathname);
  const crumbs: Breadcrumb[] = [{ label: "Studio", href: "/studio" }];
  if (segments[1] === "runs" && segments[2]) {
    crumbs.push({
      label: shortenMiddle(decodeSegment(segments[2]), 24),
      href: `/studio/runs/${segments[2]}`,
    });
  }
  return crumbs;
}

function composeBreadcrumbs(pathname: string): Breadcrumb[] {
  const segments = pathSegments(pathname);
  const crumbs: Breadcrumb[] = [{ label: "Compose", href: "/compose" }];
  const step = composeStepForPath(pathname);
  if (step.id !== "select") {
    crumbs.push({ label: step.label, href: step.path });
  }
  if (segments[1] === "runs" && segments[2]) {
    crumbs.push({
      label: shortenMiddle(decodeSegment(segments[2]), 24),
      href: `/compose/runs/${segments[2]}`,
    });
  }
  if (segments[1] === "runs" && segments[2] && segments[3]) {
    crumbs.push({
      label: shortenMiddle(decodeSegment(segments[3]), 24),
      href: `/compose/runs/${segments[2]}/${segments[3]}`,
    });
    if (segments[4]) {
      crumbs.push({
        label: titleFromSegment(segments[4]),
        href: `/compose/runs/${segments[2]}/${segments[3]}/${segments[4]}`,
      });
    }
  }
  return crumbs;
}

function organizationBreadcrumbs(pathname: string): Breadcrumb[] {
  const segments = pathSegments(pathname);
  const crumbs: Breadcrumb[] = [{ label: "Organization", href: "/organization" }];
  if (segments[1]) {
    const org = decodeSegment(segments[1]);
    crumbs.push({ label: org, href: `/organization/${segments[1]}` });
  }
  if (segments[2]) {
    const view = organizationViewForPath(pathname);
    if (view.id !== "overview") {
      crumbs.push({ label: view.label, href: `/organization/${segments[1]}/${view.id}` });
    }
  }
  if (segments[2] === "domains" && segments[3]) {
    crumbs.push({
      label: shortenMiddle(decodeSegment(segments[3]), 28),
      href: `/organization/${segments[1]}/domains/${segments[3]}`,
    });
  }
  if (segments[2] === "scim" && segments[3] === "tokens" && segments[4]) {
    crumbs.push({
      label: `Token ${shortenMiddle(decodeSegment(segments[4]), 18)}`,
      href: `/organization/${segments[1]}/scim/tokens/${segments[4]}`,
    });
  }
  if (segments[2] === "audit" && segments[3]) {
    crumbs.push({
      label: `Entry ${shortenMiddle(decodeSegment(segments[3]), 18)}`,
      href: `/organization/${segments[1]}/audit/${segments[3]}`,
    });
  }
  if (segments[2] === "members" && segments[3]) {
    crumbs.push({
      label: `Member ${shortenMiddle(decodeSegment(segments[3]), 18)}`,
      href: `/organization/${segments[1]}/members/${segments[3]}`,
    });
  }
  return crumbs;
}

function complianceBreadcrumbs(pathname: string): Breadcrumb[] {
  const segments = pathSegments(pathname);
  const crumbs: Breadcrumb[] = [{ label: "Compliance", href: "/compliance" }];
  if (segments[1]) {
    const org = decodeSegment(segments[1]);
    crumbs.push({ label: org, href: `/compliance/${segments[1]}` });
  }
  if (segments[2]) {
    const view = complianceViewForPath(pathname);
    if (view.id !== "records") {
      crumbs.push({ label: view.label, href: `/compliance/${segments[1]}/${view.id}` });
    }
  }
  return crumbs;
}

function runtimeBreadcrumbs(pathname: string): Breadcrumb[] {
  const crumbs: Breadcrumb[] = [{ label: "Runtime", href: "/runtime" }];
  const segments = pathSegments(pathname);
  const view = runtimeViewForPath(pathname);
  if (view.id !== "state") {
    crumbs.push({ label: view.label, href: view.path });
  }
  if (view.id === "policy" && segments[2]) {
    const policySectionLabel =
      RUNTIME_POLICY_SECTION_LABELS[
        segments[2] as keyof typeof RUNTIME_POLICY_SECTION_LABELS
      ];
    if (policySectionLabel) {
      crumbs.push({
        label: policySectionLabel,
        href: `/runtime/policy/${segments[2]}`,
      });
    }
  }
  if (segments[1] === "timeline" && segments[2] === "receipts" && segments[3]) {
    crumbs.push({
      label: "Receipt",
      href: `/runtime/timeline/receipts/${segments[3]}`,
    });
    if (segments[4]) {
      crumbs.push({
        label: titleFromSegment(segments[4]),
        href: `/runtime/timeline/receipts/${segments[3]}/${segments[4]}`,
      });
    }
  }
  return crumbs;
}

function pathSegments(pathname: string) {
  return normalizePathname(pathname).split("/").filter(Boolean);
}

function decodeSegment(segment: string) {
  return decodeRouteSegment(segment) ?? segment;
}

function titleFromSegment(segment: string) {
  return decodeSegment(segment)
    .split(/[-_]/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function shortenMiddle(value: string, maxLength: number) {
  if (value.length <= maxLength) return value;
  const keep = Math.max(6, Math.floor((maxLength - 1) / 2));
  return `${value.slice(0, keep)}...${value.slice(-keep)}`;
}
