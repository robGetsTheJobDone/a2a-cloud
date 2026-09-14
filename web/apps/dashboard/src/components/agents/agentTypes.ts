import {
  type AgentDeployment,
  type AgentMineSummary,
  type AgentProofRun,
  type MyAgentListing,
  type SubagentRun,
} from "../../api";
import { isPlatformAgentHost } from "../../lib/platform";
import {
  type AgentDetailSection,
} from "../../navigation";
import { agentSearchWithRequestedAgent } from "../myAgentRouteState";
import { isActiveDeploymentStatus, isTransientAgentStatus } from "../myAgentsFleet";
import { type RunDetailsView } from "../RunDetailsPanel";

const MY_AGENTS_ROUTE = "/my-agents";

export type AgentIndexAgent = AgentMineSummary;
export type MyAgentsResource = {
  agents: AgentIndexAgent[];
  runs: SubagentRun[];
  proofs: AgentProofRun[];
};
export type AgentRunDetailView = Exclude<RunDetailsView, "all">;

export const AGENT_RUN_DETAIL_VIEWS: Array<{
  id: AgentRunDetailView;
  label: string;
  description: string;
}> = [
  {
    id: "overview",
    label: "Overview",
    description: "Run status, grant identity, handoff counts, and final summary.",
  },
  {
    id: "files",
    label: "Files",
    description: "Workspace files created, updated, or deleted by this run.",
  },
  {
    id: "payload",
    label: "Payload",
    description: "Request args, scopes, result payload, grants, handoffs, and receipts.",
  },
  {
    id: "timeline",
    label: "Timeline",
    description: "Execution nodes and recorded run events in chronological order.",
  },
];

export function normalizeAgentRunDetailView(
  value: string | null | undefined,
): AgentRunDetailView {
  return AGENT_RUN_DETAIL_VIEWS.some((view) => view.id === value)
    ? (value as AgentRunDetailView)
    : "overview";
}

export function myAgentsRoute(
  agentName: string | null,
  section: AgentDetailSection = "overview",
  search = "",
) {
  const pathname = agentName
    ? `${MY_AGENTS_ROUTE}/${encodeURIComponent(agentName)}${
        section === "overview" ? "" : `/${section}`
      }`
    : MY_AGENTS_ROUTE;
  return `${pathname}${search}`;
}

export function myAgentRunRoute(
  agentName: string,
  grantId: string,
  search = "",
  view: AgentRunDetailView = "overview",
) {
  const suffix = view === "overview" ? "" : `/${view}`;
  return `${MY_AGENTS_ROUTE}/${encodeURIComponent(agentName)}/runs/${encodeURIComponent(grantId)}${suffix}${search}`;
}

export function myAgentsImportRoute(search = "") {
  return `${MY_AGENTS_ROUTE}/import${search}`;
}

export function myAgentsRequestedImportRoute(
  search: string,
  requestedAgentName: string,
) {
  return myAgentsImportRoute(
    agentSearchWithRequestedAgent(search, requestedAgentName),
  );
}

export type AgentEvidenceView = "overview" | "timeline" | "review" | "simulations";

const AGENT_EVIDENCE_VIEWS: Array<{
  id: AgentEvidenceView;
  label: string;
  description: string;
}> = [
  {
    id: "overview",
    label: "Overview",
    description: "Trust posture, evidence metrics, and dossier warnings.",
  },
  {
    id: "timeline",
    label: "Timeline",
    description: "Evidence lanes, provenance rows, inferred signals, and audit chronology.",
  },
  {
    id: "review",
    label: "Review loops",
    description: "Adversarial reviewer loops, proposed fixes, freezes, and reviewer events.",
  },
  {
    id: "simulations",
    label: "Simulations",
    description: "Custom kernel simulations and arena suite scoreboards.",
  },
];

export function normalizeAgentEvidenceView(
  value: string | null | undefined,
): AgentEvidenceView {
  return AGENT_EVIDENCE_VIEWS.some((view) => view.id === value)
    ? (value as AgentEvidenceView)
    : "overview";
}

export function normalizeAgentSummary(agent: AgentIndexAgent): AgentIndexAgent {
  const deploymentStatus = agent.latest_deployment?.status;
  if (!deploymentStatus || !isTransientAgentStatus(agent.status)) return agent;
  if (deploymentStatus === "live") return { ...agent, status: "running" };
  if (deploymentStatus === "failed") return { ...agent, status: "failed" };
  return agent;
}

export function normalizeAgentListing(agent: MyAgentListing): MyAgentListing {
  const deploymentStatus = agent.latest_deployment?.status;
  if (!deploymentStatus || !isTransientAgentStatus(agent.status)) return agent;
  if (deploymentStatus === "live") return { ...agent, status: "running" };
  if (deploymentStatus === "failed") return { ...agent, status: "failed" };
  return agent;
}

export function shouldApplyDeploymentUpdate(
  current: AgentDeployment | null | undefined,
  next: AgentDeployment,
): boolean {
  if (!current) return true;
  if (current.deploy_id === next.deploy_id) return true;
  return next.id >= current.id;
}

export function agentSummaryFromListing(agent: MyAgentListing): AgentIndexAgent {
  return normalizeAgentSummary({
    id: agent.id,
    name: agent.name,
    description: agent.description,
    version: agent.version,
    public: agent.public,
    status: agent.status,
    url: agent.url,
    created_at: agent.created_at,
    skill_count: agent.card?.skills?.length || 0,
    runtime_upgrade: agent.runtime_upgrade,
    latest_deployment: agent.latest_deployment,
  });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function splitOpenApiUrls(value: string): string[] {
  return value
    .split(/[\r\n,]+/)
    .map((part) => part.trim())
    .filter(Boolean);
}

function inferA2AOpenApiUrl(defaultBaseUrl: string): string | null {
  try {
    const url = new URL(defaultBaseUrl);
    if (!isPlatformAgentHost(url.hostname)) return null;
    url.pathname = `${url.pathname.replace(/\/$/, "")}/openapi.json`;
    url.search = "";
    url.hash = "";
    return url.toString();
  } catch {
    return null;
  }
}

export function openApiSourceUrlsForAgent(agent: MyAgentListing): string[] {
  if (agent.openapi_source && agent.openapi_source.regenerable !== false) {
    if (agent.openapi_source.urls?.length) {
      return agent.openapi_source.urls;
    }
    if (agent.openapi_source.url) {
      return [agent.openapi_source.url];
    }
  }
  const capability = agent.card?.capabilities?.openapi_auto_agent;
  if (isRecord(capability)) {
    if (capability.regenerable === false) return [];
    const sourceUrls = capability.source_openapi_urls;
    if (Array.isArray(sourceUrls)) {
      const urls = sourceUrls
        .map((value) => (typeof value === "string" ? value.trim() : ""))
        .filter(Boolean);
      if (urls.length) return urls;
    }
    const sourceUrl = capability.source_openapi_url;
    if (typeof sourceUrl === "string" && sourceUrl.trim()) {
      return [sourceUrl.trim()];
    }
    const defaultBaseUrl = capability.default_base_url;
    if (typeof defaultBaseUrl === "string") {
      const inferred = inferA2AOpenApiUrl(defaultBaseUrl);
      if (inferred) return [inferred];
    }
  }
  for (const event of agent.latest_deployment?.events || []) {
    const values = event.data.openapi_urls;
    if (Array.isArray(values)) {
      const urls = values
        .map((value) => (typeof value === "string" ? value.trim() : ""))
        .filter(Boolean);
      if (urls.length) return urls;
    }
    const value = event.data.openapi_url;
    if (typeof value === "string" && value.trim()) {
      return [value.trim()];
    }
  }
  return [];
}

export function frontendUrlForAgent(
  agent: MyAgentListing,
  fallbackBase: string | null,
): string | null {
  // The agent advertises a packed frontend via card.capabilities.ui.url
  // (set by frontend_ui_metadata when a static frontend is deployed).
  const ui = agent.card?.capabilities?.ui;
  if (!isRecord(ui)) return null;
  const raw =
    typeof ui.url === "string" && ui.url.trim()
      ? ui.url.trim()
      : typeof ui.entry === "string" && ui.entry.trim()
        ? ui.entry.trim()
        : null;
  if (!raw) return null;
  if (/^https?:\/\//i.test(raw)) return raw;
  if (!fallbackBase) return null;
  return `${fallbackBase.replace(/\/$/, "")}/${raw.replace(/^\//, "")}`;
}

function absoluteControlPlaneUrl(path: string): string {
  const clean = path.startsWith("/") ? path : `/${path}`;
  if (typeof window === "undefined") return clean;
  return new URL(clean, window.location.origin).toString();
}

export function agentApiUrls(agent: MyAgentListing) {
  const encoded = encodeURIComponent(agent.name);
  const firstSkill = agent.card?.skills?.[0]?.name || null;
  return {
    openapi: absoluteControlPlaneUrl(`/v1/agents/${encoded}/api/openapi.json`),
    invokeBase: absoluteControlPlaneUrl(`/v1/agents/${encoded}/api/invoke`),
    sampleInvoke: firstSkill
      ? absoluteControlPlaneUrl(
          `/v1/agents/${encoded}/api/invoke/${encodeURIComponent(firstSkill)}`,
        )
      : null,
    publicRegistry: agent.public
      ? absoluteControlPlaneUrl(`/marketplace?agent=${encoded}`)
      : null,
  };
}

export type AgentApiUrls = ReturnType<typeof agentApiUrls>;

export type TemplateLineageView = NonNullable<MyAgentListing["card"]["template_lineage"]>;

export function summarizeRuns(runs: SubagentRun[]) {
  return runs.reduce(
    (acc, run) => {
      if (run.status === "error" || run.status === "denied") acc.failures += 1;
      return acc;
    },
    { failures: 0 },
  );
}

export function runtimeUpgradeMessage(
  upgrade: MyAgentListing["runtime_upgrade"] | undefined,
  deployment: MyAgentListing["latest_deployment"],
  deploymentActive: boolean,
): string {
  if (deployment?.trigger === "runtime_upgrade" && isActiveDeploymentStatus(deployment.status)) {
    return `Redeploy ${deployment.deploy_id} is ${deployment.status}. The deployment timeline will update until the rebuilt agent is live.`;
  }
  if (upgrade && deploymentActive) {
    return `${upgrade.message} Finish the active deployment before queueing another runtime rebuild.`;
  }
  if (upgrade) {
    return `${upgrade.message} Redeploy to rebuild from the latest base image.`;
  }
  return "Runtime redeploy is waiting for deployment status.";
}

export function runtimeUpgradeButtonLabel({
  busy,
  active,
  blocked,
  canRedeploy,
}: {
  busy: boolean;
  active: boolean;
  blocked: boolean;
  canRedeploy: boolean;
}): string {
  if (busy) return "queueing redeploy...";
  if (active) return "redeploy queued";
  if (blocked) return "deployment active";
  if (!canRedeploy) return "redeploy unavailable";
  return "redeploy latest";
}

export function templateUpdatePolicy(lineage: TemplateLineageView): string {
  return String(lineage.update_policy || "none").trim().toLowerCase() || "none";
}

export function templateLineageMiniValue(
  lineage?: MyAgentListing["card"]["template_lineage"] | null,
): string {
  if (!lineage) return "none";
  if (lineage.template_version) return `v${lineage.template_version}`;
  if (lineage.source_agent_version) return `v${lineage.source_agent_version}`;
  if (lineage.template_ref || lineage.source_agent) return "tracked";
  return templateUpdatePolicy(lineage);
}

export function templateUpdateButtonLabel({
  busy,
  blocked,
  policy,
  queued,
}: {
  busy: boolean;
  blocked: boolean;
  policy: string;
  queued: boolean;
}): string {
  if (busy) return "queueing update...";
  if (queued) return "update queued";
  if (blocked) return "deployment active";
  if (policy === "none") return "updates off";
  return "update template";
}

export function fmtDate(value: string): string {
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "unknown";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
