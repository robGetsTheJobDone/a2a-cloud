import type { AgentMineSummary } from "../api";
import type { AgentLifecycleSummary } from "./AgentLifecycleDashboard";
import type { AgentLifecycleFilterValue } from "./AgentLifecycleFilters";
import { ToolbarButton, ToolbarLink, type ToolbarButtonVariant } from "./DashboardChrome";
import {
  DashboardSurfacePosture,
  type SurfacePostureMetric,
  type SurfacePostureStatus,
} from "./SurfacePosture";
import { agentDetailRouteReference } from "./myAgentRouteState";

const MY_AGENTS_ROUTE = "/my-agents";

type AgentFleetAction =
  | {
      kind: "link";
      label: string;
      detail: string;
      href: string;
      action: string;
      variant: ToolbarButtonVariant;
    }
  | {
      kind: "button";
      label: string;
      detail: string;
      action: string;
      variant: ToolbarButtonVariant;
      onClick: () => void;
      disabled?: boolean;
    };

/**
 * AgentFleetPosture — single compact telemetry header for the My Agents surface.
 *
 * Mandate D: this merges the former 3-panel stack (the 5-card lifecycle
 * dashboard, the oversized "fleet posture" banner, and the standalone filters
 * panel) into one DashboardSurfacePosture strip. It surfaces fleet health as
 * inline metrics plus the single next-best action; selection no longer routes
 * (mandate B/C), so the next action fires its callback in place.
 */
export function AgentFleetPosture({
  search,
  agents,
  filtered,
  summary,
  hasActiveFilters,
  selectedAgentName,
  selectedAgent,
  bulkUpgradeCount,
  bulkUpgradeBusy,
  onClearFilters,
  onShowFailed,
  onShowDeploying,
  onShowProofGaps,
  onShowRuntimeUpdates,
  onUpgradeAll,
  onSelectTopResult,
}: {
  search: string;
  agents: AgentMineSummary[] | null;
  filtered: AgentMineSummary[];
  summary: AgentLifecycleSummary;
  filters: AgentLifecycleFilterValue;
  hasActiveFilters: boolean;
  selectedAgentName: string | null;
  selectedAgent: AgentMineSummary | null;
  bulkUpgradeCount: number;
  bulkUpgradeBusy: boolean;
  onClearFilters: () => void;
  onShowFailed: () => void;
  onShowDeploying: () => void;
  onShowProofGaps: () => void;
  onShowRuntimeUpdates: () => void;
  onUpgradeAll: () => void;
  /** In-place selection of the first filtered result (mandate B/C). */
  onSelectTopResult?: (agent: AgentMineSummary) => void;
}) {
  const proofGapCount =
    summary.proofCoverage.degraded +
    summary.proofCoverage.unverified +
    summary.proofCoverage.missing;
  const nextAction = agentFleetNextAction({
    search,
    agents,
    filtered,
    summary,
    hasActiveFilters,
    selectedAgentName,
    selectedAgent,
    bulkUpgradeCount,
    bulkUpgradeBusy,
    proofGapCount,
    onClearFilters,
    onShowFailed,
    onShowDeploying,
    onShowProofGaps,
    onShowRuntimeUpdates,
    onUpgradeAll,
  });
  const status = agentFleetPostureStatus(agents, summary, bulkUpgradeCount, proofGapCount);
  const metrics = agentFleetPostureMetrics({
    agents,
    filtered,
    summary,
    bulkUpgradeCount,
  });

  // The top-result action is a route link in the pure helper (test contract);
  // intercept it here so selection stays in-place (mandate B/C).
  const topResultClick =
    nextAction.kind === "link" &&
    nextAction.action === "Open agent" &&
    onSelectTopResult &&
    filtered.length > 0
      ? () => onSelectTopResult(filtered[0])
      : null;

  return (
    <DashboardSurfacePosture
      data-onboarding-target="my-agents-posture"
      eyebrow="Agent fleet"
      title={nextAction.label}
      status={status}
      metrics={metrics}
      actions={
        <>
          {hasActiveFilters && nextAction.action !== "Clear filters" && (
            <ToolbarButton onClick={onClearFilters} size="xs">
              Clear filters
            </ToolbarButton>
          )}
          {topResultClick ? (
            <ToolbarButton onClick={topResultClick} variant={nextAction.variant} size="xs">
              {nextAction.action}
            </ToolbarButton>
          ) : nextAction.kind === "link" ? (
            <ToolbarLink href={nextAction.href} variant={nextAction.variant} size="xs">
              {nextAction.action}
            </ToolbarLink>
          ) : (
            <ToolbarButton
              onClick={nextAction.onClick}
              disabled={nextAction.disabled}
              variant={nextAction.variant}
              size="xs"
            >
              {nextAction.action}
            </ToolbarButton>
          )}
        </>
      }
    />
  );
}

export function agentFleetNextAction({
  search,
  agents,
  filtered,
  summary,
  hasActiveFilters,
  selectedAgentName,
  selectedAgent,
  bulkUpgradeCount,
  bulkUpgradeBusy,
  proofGapCount,
  onClearFilters,
  onShowFailed,
  onShowDeploying,
  onShowProofGaps,
  onShowRuntimeUpdates,
  onUpgradeAll,
}: {
  search: string;
  agents: AgentMineSummary[] | null;
  filtered: AgentMineSummary[];
  summary: AgentLifecycleSummary;
  hasActiveFilters: boolean;
  selectedAgentName: string | null;
  selectedAgent: AgentMineSummary | null;
  bulkUpgradeCount: number;
  bulkUpgradeBusy: boolean;
  proofGapCount: number;
  onClearFilters: () => void;
  onShowFailed: () => void;
  onShowDeploying: () => void;
  onShowProofGaps: () => void;
  onShowRuntimeUpdates: () => void;
  onUpgradeAll: () => void;
}): AgentFleetAction {
  if (!agents) {
    return {
      kind: "link",
      label: "Load the agent fleet",
      detail: "Fetching owned agents, recent runs, proof receipts, runtime updates, and deployment state.",
      href: myAgentsRoute(null, "overview", search),
      action: "Agents",
      variant: "secondary",
    };
  }
  if (selectedAgentName) {
    return {
      kind: "link",
      label: selectedAgent
        ? `Inspect ${selectedAgent.name}`
        : `Resolve ${agentDetailRouteReference(selectedAgentName)}`,
      detail: selectedAgent
        ? `${selectedAgent.name} is open; review runs, proofs, secrets, domains, and deployment evidence from its detail tabs.`
        : "This route is waiting for the selected agent record to load.",
      href: selectedAgent
        ? myAgentsRoute(selectedAgent.name, "runs", search)
        : myAgentsRoute(null, "overview", search),
      action: selectedAgent ? "Open runs" : "All agents",
      variant: selectedAgent ? "secondary" : "primary",
    };
  }
  if (agents.length === 0) {
    return {
      kind: "link",
      label: "Bring the first agent",
      detail: "Import an A2A endpoint or generate a managed agent from an OpenAPI specification.",
      href: myAgentsImportRoute(search),
      action: "Bring agent",
      variant: "primary",
    };
  }
  if (hasActiveFilters && filtered.length === 0) {
    return {
      kind: "button",
      label: "Broaden the fleet view",
      detail: "No agents match the active filters. Clear them to return to the full fleet.",
      action: "Clear filters",
      variant: "primary",
      onClick: onClearFilters,
    };
  }
  if (summary.failureCount > 0) {
    if (summary.failureBreakdown.deployments > 0) {
      return {
        kind: "button",
        label: "Review failed deployments",
        detail: `${summary.failureBreakdown.deployments} deployment failure${summary.failureBreakdown.deployments === 1 ? "" : "s"} can block public traffic. Filter the fleet to agents with lifecycle failures before redeploying or checking logs.`,
        action: "Show deploy failures",
        variant: "danger",
        onClick: onShowFailed,
      };
    }
    if (summary.failureBreakdown.runs > 0) {
      return {
        kind: "button",
        label: "Review failed runs",
        detail: `${summary.failureBreakdown.runs} run failure${summary.failureBreakdown.runs === 1 ? "" : "s"} need grant, file, or handoff inspection from the affected agent run tabs.`,
        action: "Show run failures",
        variant: "danger",
        onClick: onShowFailed,
      };
    }
    if (summary.failureBreakdown.proofs > 0) {
      return {
        kind: "button",
        label: "Review failed proofs",
        detail: `${summary.failureBreakdown.proofs} proof failure${summary.failureBreakdown.proofs === 1 ? "" : "s"} need receipt review before the agent can be treated as trusted.`,
        action: "Show proof failures",
        variant: "danger",
        onClick: onShowFailed,
      };
    }
    return {
      kind: "button",
      label: "Review agents needing attention",
      detail: `${summary.failureCount} lifecycle failure${summary.failureCount === 1 ? "" : "s"} are present across deployments, runs, or proofs.`,
      action: "Show failures",
      variant: "danger",
      onClick: onShowFailed,
    };
  }
  if (summary.health.deploying > 0) {
    return {
      kind: "button",
      label: "Watch active deployments",
      detail: `${summary.health.deploying} agent${summary.health.deploying === 1 ? "" : "s"} are building, deploying, queued, or verifying right now.`,
      action: "Show deploying",
      variant: "primary",
      onClick: onShowDeploying,
    };
  }
  if (bulkUpgradeCount > 0) {
    return {
      kind: "button",
      label: "Queue runtime updates",
      detail: `${bulkUpgradeCount} a2a-pack runtime update${bulkUpgradeCount === 1 ? "" : "s"} can be queued now.`,
      action: bulkUpgradeBusy ? "Queueing..." : "Queue updates",
      variant: "primary",
      onClick: onUpgradeAll,
      disabled: bulkUpgradeBusy,
    };
  }
  if (summary.updateNeededCount > 0) {
    return {
      kind: "button",
      label: "Find blocked runtime updates",
      detail: `${summary.updateNeededCount} agents report runtime updates; some may be waiting on active deployments or external ownership.`,
      action: "Show updates",
      variant: "secondary",
      onClick: onShowRuntimeUpdates,
    };
  }
  if (proofGapCount > 0) {
    return {
      kind: "button",
      label: "Close proof coverage gaps",
      detail: `${proofGapCount} agent${proofGapCount === 1 ? "" : "s"} need verified public proof evidence.`,
      action: "Show proof gaps",
      variant: "secondary",
      onClick: onShowProofGaps,
    };
  }
  if (filtered.length > 0) {
    const first = filtered[0];
    return {
      kind: "link",
      label: "Inspect the top fleet result",
      detail: `${first.name} is first in the current sort and filter view.`,
      href: myAgentsRoute(first.name, "overview", search),
      action: "Open agent",
      variant: "secondary",
    };
  }
  return {
    kind: "link",
    label: "Bring another agent",
    detail: "Expand the fleet with an imported A2A endpoint or generated OpenAPI-backed agent.",
    href: myAgentsImportRoute(search),
    action: "Bring agent",
    variant: "secondary",
  };
}

function agentFleetPostureStatus(
  agents: AgentMineSummary[] | null,
  summary: AgentLifecycleSummary,
  bulkUpgradeCount: number,
  proofGapCount: number,
): SurfacePostureStatus {
  if (!agents) return { label: "loading", tone: "neutral", dot: true };
  if (summary.totalAgents === 0) return { label: "empty", tone: "neutral", dot: true };
  if (summary.failureCount > 0 || summary.health.attention > 0) {
    return { label: "needs review", tone: "danger", dot: true };
  }
  if (summary.health.deploying > 0) {
    return { label: "deploying", tone: "live", pulse: true, dot: true };
  }
  if (bulkUpgradeCount > 0) return { label: "updates ready", tone: "authority", dot: true };
  if (proofGapCount > 0) return { label: "proof gaps", tone: "authority", dot: true };
  return { label: "healthy", tone: "protocol", dot: true };
}

function agentFleetPostureMetrics({
  agents,
  filtered,
  summary,
  bulkUpgradeCount,
}: {
  agents: AgentMineSummary[] | null;
  filtered: AgentMineSummary[];
  summary: AgentLifecycleSummary;
  bulkUpgradeCount: number;
}): SurfacePostureMetric[] {
  return [
    {
      label: "shown",
      value: agents
        ? `${filtered.length.toLocaleString()}/${summary.totalAgents.toLocaleString()}`
        : "—",
    },
    {
      label: "healthy",
      value: `${summary.health.healthy.toLocaleString()}/${summary.totalAgents.toLocaleString()}`,
      tone: summary.health.attention > 0 ? "danger" : "live",
    },
    {
      label: "live",
      value: summary.deployLiveCount.toLocaleString(),
      tone: summary.deployLiveCount > 0 ? "live" : "neutral",
    },
    {
      label: "updates",
      value: `${summary.updateNeededCount.toLocaleString()} (${bulkUpgradeCount.toLocaleString()})`,
      tone: summary.updateNeededCount > 0 ? "authority" : "neutral",
    },
    {
      label: "failures",
      value: summary.failureCount.toLocaleString(),
      tone: summary.failureCount > 0 ? "danger" : "neutral",
    },
    {
      label: "proof",
      value: `${summary.proofCoverage.percent}%`,
      tone: summary.proofCoverage.percent === 100 ? "proof" : "authority",
    },
  ];
}

function myAgentsRoute(
  agentName: string | null,
  section: "overview" | "runs" = "overview",
  search = "",
) {
  const pathname = agentName
    ? `${MY_AGENTS_ROUTE}/${encodeURIComponent(agentName)}${
        section === "overview" ? "" : `/${section}`
      }`
    : MY_AGENTS_ROUTE;
  return `${pathname}${search}`;
}

function myAgentsImportRoute(search = "") {
  return `${MY_AGENTS_ROUTE}/import${search}`;
}
