import {
  type AgentProofRun,
} from "../../api";
import {
  AGENT_DETAIL_SECTIONS,
  type AgentDetailSection,
} from "../../navigation";
import {
  agentRouteRecoveryMatches,
  agentSearchWithoutRequestedAgent,
  agentDetailRouteReference,
  agentDetailRouteStatus,
  type AgentRouteRecoveryAgent,
} from "../myAgentRouteState";
import {
  InlineAlert,
  SegmentedButton,
  SegmentedControl,
  SelectableSurfaceLink,
  SurfacePanel,
  ToolbarButton,
  ToolbarLink,
} from "../DashboardChrome";
import { StateBadge, StatusBadge } from "../StatusPillAdapters";
import { cx } from "@a2a/design-system";
import { Icon } from "../Icon";
import { type AgentIndexAgent, fmtDate, myAgentsRoute } from "./agentTypes";
import { DeploymentPill, RuntimePill } from "./AgentPills";

/**
 * AgentIndexRow — compact left-rail row for the DualPaneResourceBrowser. Selects
 * the agent in place (mandate B/C); it no longer navigates to a detail route.
 */
export function AgentIndexRow({
  agent,
  selected,
  onSelect,
  runCount,
  latestProof,
}: {
  agent: AgentIndexAgent;
  selected: boolean;
  onSelect: () => void;
  runCount: number;
  latestProof: AgentProofRun | null;
}) {
  const deployment = agent.latest_deployment;
  const upgrade = agent.runtime_upgrade;
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-current={selected ? "true" : undefined}
      data-selected={selected ? "true" : undefined}
      className={cx(
        "group flex w-full flex-col gap-2 rounded-lg border px-3 py-2.5 text-left transition",
        selected
          ? "border-signal-protocol-strong/45 bg-signal-protocol-strong/10"
          : "border-runtime-line-soft/60 bg-runtime-panel/40 hover:border-runtime-line-strong hover:bg-runtime-panel/80",
      )}
    >
      <div className="flex min-w-0 items-center gap-2">
        <span className="truncate font-mono text-sm font-semibold text-ink">
          {agent.name}
        </span>
        {upgrade?.update_available && <RuntimePill />}
      </div>
      <div className="flex flex-wrap items-center gap-1.5">
        <StateBadge
          status={agent.status}
          live={agent.status === "running" || agent.status === "ready"}
          size="xs"
        />
        {deployment && <DeploymentPill status={deployment.status} />}
        <StateBadge
          status={latestProof?.badge || "unverified"}
          aria-label={`Proof status: ${latestProof?.badge || "unverified"}`}
          size="xs"
        />
        <span className="rounded-full border border-runtime-line-soft/60 px-1.5 py-0.5 text-[10px] text-ink-muted">
          {agent.public ? "public" : "private"}
        </span>
      </div>
      <div className="flex flex-wrap gap-x-2 gap-y-0.5 text-[11px] text-ink-faint">
        <span>{agent.skill_count} skills</span>
        <span>{runCount} runs</span>
        <span>created {fmtDate(agent.created_at)}</span>
      </div>
    </button>
  );
}

export function AgentDetailRouteState({
  agentName,
  loading,
  error,
  onBack,
  onRetry,
  importHref,
  agents,
  search,
}: {
  agentName: string;
  loading: boolean;
  error: string | null;
  onBack: () => void;
  onRetry: () => void;
  importHref: string;
  agents: AgentRouteRecoveryAgent[] | null;
  search: string;
}) {
  const status = agentDetailRouteStatus({ loading, error });
  const routeReference = agentDetailRouteReference(agentName);
  const recoverySearch = agentSearchWithoutRequestedAgent(search);
  const matches = agents
    ? agentRouteRecoveryMatches(agentName, agents)
    : [];
  const statusCopy =
    status === "loading"
      ? "Looking up the owned-agent record, deployment card, and proof posture."
      : status === "missing"
        ? "No owned agent matched this route. Refresh after a deployment completes or bring the agent into this fleet."
        : status === "error"
          ? "The agent detail lookup failed. The fleet stays available while you retry this route."
          : "Preparing the agent detail view.";
  const statusTone = status === "missing" || status === "error"
    ? "red"
    : status === "loading"
      ? "amber"
      : "neutral";

  return (
    <SurfacePanel as="section" aria-busy={loading} className="bg-runtime-bg p-6">
      <div className="flex flex-col gap-5 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="break-all font-mono text-base font-semibold text-ink">
              {agentName}
            </h2>
            <StateBadge status={status} tone={statusTone} size="xs" />
          </div>
          <p className="mt-2 max-w-2xl text-sm leading-relaxed text-ink-muted">
            {statusCopy}
          </p>
          <div className="mt-4 rounded-md border border-runtime-line-soft/70 bg-runtime-panel/45 p-3">
            <div className="text-[10px] font-semibold uppercase tracking-wider text-ink-faint">
              Requested route
            </div>
            <div className="mt-1 break-all font-mono text-sm text-ink">
              {routeReference}
            </div>
            <div className="mt-2 break-all text-xs text-ink-muted">
              Exact agent id: <span className="font-mono text-ink-soft">{agentName}</span>
            </div>
          </div>
          {error && (
            <InlineAlert tone="red" className="mt-3 text-sm">
              {error}
            </InlineAlert>
          )}
          <AgentDetailRecoveryMatches
            agentName={agentName}
            agents={agents}
            matches={matches}
            search={recoverySearch}
          />
        </div>
        <div className="flex w-full flex-col gap-2 sm:w-auto sm:flex-row lg:justify-end">
          <ToolbarButton
            type="button"
            onClick={onRetry}
            disabled={loading}
            size="md"
            className="w-full sm:w-auto"
          >
            Refresh lookup
          </ToolbarButton>
          <ToolbarLink
            href={importHref}
            variant="primary"
            size="md"
            className="w-full sm:w-auto"
          >
            Bring this agent
          </ToolbarLink>
          <ToolbarButton
            type="button"
            onClick={onBack}
            size="md"
            className="w-full sm:w-auto"
          >
            Back to agents
          </ToolbarButton>
        </div>
      </div>
    </SurfacePanel>
  );
}

function AgentDetailRecoveryMatches({
  agentName,
  agents,
  matches,
  search,
}: {
  agentName: string;
  agents: AgentRouteRecoveryAgent[] | null;
  matches: AgentRouteRecoveryAgent[];
  search: string;
}) {
  if (!agents) {
    return (
      <div className="mt-3 rounded-md border border-runtime-line-soft/70 bg-runtime-panel/35 p-3">
        <div className="text-[10px] font-semibold uppercase tracking-wider text-ink-faint">
          Fleet matches
        </div>
        <p className="mt-1 text-xs leading-relaxed text-ink-muted">
          Checking owned-agent summaries before suggesting nearby records.
        </p>
      </div>
    );
  }

  if (matches.length === 0) {
    return (
      <div className="mt-3 rounded-md border border-runtime-line-soft/70 bg-runtime-panel/35 p-3">
        <div className="text-[10px] font-semibold uppercase tracking-wider text-ink-faint">
          Fleet matches
        </div>
        <p className="mt-1 text-xs leading-relaxed text-ink-muted">
          No nearby owned agents match <span className="font-mono text-ink-soft">{agentName}</span>.
          Refresh the lookup or bring this agent into the fleet.
        </p>
      </div>
    );
  }

  return (
    <div className="mt-3 rounded-md border border-runtime-line-soft/70 bg-runtime-panel/35 p-3">
      <div className="text-[10px] font-semibold uppercase tracking-wider text-ink-faint">
        Nearby fleet matches
      </div>
      <div className="mt-2 grid gap-2">
        {matches.map((agent) => (
          <SelectableSurfaceLink
            key={agent.name}
            href={myAgentsRoute(agent.name, "overview", search)}
            className="p-3 hover:border-runtime-line-strong hover:bg-runtime-panel/80"
          >
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="break-all font-mono text-sm font-semibold text-ink">
                    {agent.name}
                  </span>
                  {agent.status && (
                    <StateBadge
                      status={agent.status}
                      live={agent.status === "running" || agent.status === "ready"}
                      size="xs"
                    />
                  )}
                  {agent.latest_deployment?.status && (
                    <StatusBadge className="uppercase">
                      deploy {agent.latest_deployment.status}
                    </StatusBadge>
                  )}
                  <span className="rounded-full border border-runtime-line-soft/60 px-2 py-0.5 text-[11px] text-ink-muted">
                    {agent.public ? "public" : "private"}
                  </span>
                </div>
                <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-ink-muted">
                  {agent.description || "No description."}
                </p>
              </div>
              <span className="flex shrink-0 items-center gap-2 text-xs text-ink-dim">
                Open match
                <Icon name="arrow-right" size={13} />
              </span>
            </div>
          </SelectableSurfaceLink>
        ))}
      </div>
    </div>
  );
}

/**
 * AgentDetailNav — in-place tab navigation for the open agent's detail sheet.
 * Tabs switch local state (mandate B/C) instead of routing to a detail URL, so
 * the fleet list behind the sheet stays mounted.
 */
export function AgentDetailNav({
  agent,
  latestProof,
  activeSection,
  onBack,
  onSectionChange,
}: {
  agent: AgentIndexAgent;
  latestProof: AgentProofRun | null;
  activeSection: AgentDetailSection;
  onBack: () => void;
  onSectionChange: (section: AgentDetailSection) => void;
}) {
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <ToolbarButton
          type="button"
          onClick={onBack}
          variant="ghost"
          size="xs"
        >
          <Icon name="chevron-left" size={13} />
          All agents
        </ToolbarButton>
        <StateBadge
          status={agent.status}
          live={agent.status === "running" || agent.status === "ready"}
          size="xs"
        />
        <StateBadge
          status={latestProof?.badge || "unverified"}
          aria-label={`Proof status: ${latestProof?.badge || "unverified"}`}
          size="xs"
        />
      </div>
      <SegmentedControl
        role="tablist"
        aria-label={`${agent.name} sections`}
        className="flex gap-1 overflow-x-auto bg-runtime-panel/70"
      >
        {AGENT_DETAIL_SECTIONS.map((section) => (
          <SegmentedButton
            key={section.id}
            role="tab"
            selected={activeSection === section.id}
            aria-selected={activeSection === section.id}
            onClick={() => onSectionChange(section.id)}
            className="whitespace-nowrap"
          >
            {section.label}
          </SegmentedButton>
        ))}
      </SegmentedControl>
    </div>
  );
}
