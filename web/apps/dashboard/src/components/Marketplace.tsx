import { publicAgentUrl } from "../lib/publicAgentUrl";
import { useCallback, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import type { StatusPillProps } from "@a2a/design-system";
import {
  getConsumerSetup,
  installMarketplaceAgent,
  listAgents,
  listInstalledAgents,
  listPublicAgentProofs,
  searchAgents,
  upsertConsumerSetup,
  upsertOrgConsumerSetup,
  type AccountAccessPolicy,
  type AgentListing,
  type PublicAgentProofSummary,
  type AgentSearchResult,
  type AgentSkill,
  type ConsumerSetupField,
  type ConsumerSetupStatus,
  type InstalledAgent,
} from "../api";
import {
  CompactFact,
  EmptyState,
  InlineAlert,
  LoadingState,
  SegmentedControl,
  SelectInput,
  SummaryMetric,
  SummaryStrip,
  SurfacePanel,
  TextInput,
  ToolbarButton,
  ToolbarLink,
} from "./DashboardChrome";
import { StateBadge, StatusBadge } from "./StatusPillAdapters";
import {
  DASHBOARD_SECTION_CACHE_KEYS,
  useDashboardSectionResource,
} from "./DashboardSectionCache";
import { ConsumerSetupFieldInput } from "./ConsumerSetupFieldInput";
import { MarketplaceTrialLauncher } from "./MarketplaceTrialLauncher";
import { RoutePageShell } from "./RoutePageShell";
import { DashboardSurfacePosture } from "./SurfacePosture";
import { DetailSheet } from "./ListDetailLayout";
import {
  MARKETPLACE_VIEWS,
  decodeRouteSegment,
  marketplaceViewForPath,
  type MarketplaceViewId,
} from "../navigation";

type ProofFilter = "all" | "verified" | "needs-proof";
type SurfacePostureStatusTone = StatusPillProps["tone"];

type MarketplaceQueryState = {
  q: string;
  agent: string;
  skill: string;
  proof: ProofFilter;
};

type MarketplaceResource = {
  agents: AgentListing[];
  proofs: PublicAgentProofSummary[];
  installedAgents: InstalledAgent[];
};

type RuntimeProvisioning =
  | "platform"
  | "platform_or_caller_provided"
  | "caller_provided"
  | "agent_byok";

const MARKETPLACE_AGENT_SECTIONS = [
  { id: "overview", label: "Overview" },
  { id: "skills", label: "Tools" },
  { id: "proof", label: "Proof" },
  { id: "endpoints", label: "Endpoints" },
] as const;

type MarketplaceAgentSectionId = (typeof MARKETPLACE_AGENT_SECTIONS)[number]["id"];

const PROOF_FILTERS: { id: ProofFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "verified", label: "Verified" },
  { id: "needs-proof", label: "Needs proof" },
];

const EMPTY_MARKETPLACE_QUERY: MarketplaceQueryState = {
  q: "",
  agent: "",
  skill: "",
  proof: "all",
};

type MarketplaceDetailMode = "card" | "install" | "trial";

type MarketplaceDetailState = {
  agentName: string;
  mode: MarketplaceDetailMode;
  section: MarketplaceAgentSectionId;
};

function readRouteDetailState(
  pathname: string,
  encodedRouteAgentName: string | undefined,
  routeAgentSection: string | undefined,
): MarketplaceDetailState | null {
  const agentName = decodeRouteSegment(encodedRouteAgentName);
  if (!agentName) return null;
  const trimmed = pathname.replace(/\/+$/, "");
  if (trimmed.endsWith("/install")) {
    return { agentName, mode: "install", section: "overview" };
  }
  if (trimmed.endsWith("/trial")) {
    return { agentName, mode: "trial", section: "overview" };
  }
  return {
    agentName,
    mode: "card",
    section: normalizeMarketplaceAgentSection(routeAgentSection),
  };
}

export function Marketplace() {
  const location = useLocation();
  const navigate = useNavigate();
  const {
    agentName: encodedRouteAgentName,
    section: routeAgentSection,
  } = useParams<{ agentName?: string; section?: string }>();
  // Detail/install/trial is an in-place right-side sheet (mandate C). The route
  // still hydrates it on mount and stays shareable, but the list/grid below is
  // never unmounted while a sheet is open (mandate E).
  const [detail, setDetail] = useState<MarketplaceDetailState | null>(() =>
    readRouteDetailState(location.pathname, encodedRouteAgentName, routeAgentSection),
  );
  const loadMarketplaceResource = useCallback(async (): Promise<MarketplaceResource> => {
    const [nextAgents, nextProofs, nextInstalled] = await Promise.all([
      listAgents(),
      listPublicAgentProofs(),
      listInstalledAgents(),
    ]);
    return {
      agents: nextAgents,
      proofs: nextProofs,
      installedAgents: nextInstalled,
    };
  }, []);
  const {
    data: marketplaceData,
    error: loadErr,
    setData: setMarketplaceData,
  } = useDashboardSectionResource(
    DASHBOARD_SECTION_CACHE_KEYS.agents.marketplace,
    loadMarketplaceResource,
  );
  const agents = marketplaceData?.agents ?? null;
  const proofs = marketplaceData?.proofs ?? [];
  const installedAgents = marketplaceData?.installedAgents ?? [];
  const [searchMatches, setSearchMatches] = useState<AgentSearchResult[] | null>(null);
  const [searchLoading, setSearchLoading] = useState(false);
  const [actionErr, setActionErr] = useState<string | null>(null);
  const [quickInstalling, setQuickInstalling] = useState<string | null>(null);
  const err = actionErr ?? loadErr;
  const marketplaceQuery = useMemo(
    () => readMarketplaceQueryState(location.search),
    [location.search],
  );
  const marketplaceView = useMemo(
    () => marketplaceViewForPath(location.pathname),
    [location.pathname],
  );
  const activeView = marketplaceView.id;

  // Re-hydrate the detail sheet whenever the route changes (deep links, back/
  // forward) without unmounting the list. A bare view path closes the sheet.
  useEffect(() => {
    setDetail(
      readRouteDetailState(location.pathname, encodedRouteAgentName, routeAgentSection),
    );
  }, [location.pathname, encodedRouteAgentName, routeAgentSection]);

  useEffect(() => {
    const q = marketplaceQuery.q.trim();
    if (!q) {
      setSearchMatches(null);
      setSearchLoading(false);
      return;
    }

    let active = true;
    setSearchLoading(true);
    searchAgents({ q, limit: 50 })
      .then((matches) => {
        if (active) {
          setSearchMatches(matches);
          setActionErr(null);
        }
      })
      .catch((ex) => {
        if (!active) return;
        setSearchMatches(null);
        setActionErr(ex instanceof Error ? ex.message : String(ex));
      })
      .finally(() => {
        if (active) setSearchLoading(false);
      });
    return () => {
      active = false;
    };
  }, [marketplaceQuery.q]);

  const proofsByAgent = useMemo(
    () => new Map(proofs.map((proof) => [proof.agent_name, proof])),
    [proofs],
  );

  const stats = useMemo(
    () => summarizeAgents(agents || [], proofsByAgent),
    [agents, proofsByAgent],
  );

  const searchOrder = useMemo(() => {
    if (!marketplaceQuery.q.trim() || !searchMatches) return null;
    return new Map(searchMatches.map((match, index) => [match.name.toLowerCase(), index]));
  }, [marketplaceQuery.q, searchMatches]);

  const filtered = useMemo(() => {
    const candidates = searchOrder
      ? (agents || []).filter((agent) => searchOrder.has(agent.name.toLowerCase()))
      : agents || [];
    const query = searchOrder ? { ...marketplaceQuery, q: "" } : marketplaceQuery;
    return filterAgents(candidates, proofsByAgent, query).sort((left, right) => {
      if (searchOrder) {
        return (
          (searchOrder.get(left.name.toLowerCase()) ?? Number.MAX_SAFE_INTEGER) -
            (searchOrder.get(right.name.toLowerCase()) ?? Number.MAX_SAFE_INTEGER) ||
          left.name.localeCompare(right.name)
        );
      }
      return (
        agentRank(right, proofsByAgent.get(right.name) || null) -
          agentRank(left, proofsByAgent.get(left.name) || null) ||
        left.name.localeCompare(right.name)
      );
    });
  }, [activeView, agents, marketplaceQuery, proofsByAgent, searchOrder]);

  const installedNames = useMemo(
    () => new Set(installedAgents.map((item) => item.agent.name.toLowerCase())),
    [installedAgents],
  );
  const detailAgent = useMemo(() => {
    if (!detail || !agents) return null;
    return (
      agents.find(
        (agent) => agent.name.toLowerCase() === detail.agentName.toLowerCase(),
      ) || null
    );
  }, [agents, detail]);

  const hasActiveFilters =
    Boolean(marketplaceQuery.q.trim()) ||
    Boolean(marketplaceQuery.agent.trim()) ||
    Boolean(marketplaceQuery.skill.trim()) ||
    marketplaceQuery.proof !== "all";

  function updateMarketplaceQuery(next: MarketplaceQueryState) {
    navigate(marketplaceQueryPath(next, marketplaceView.path), { replace: true });
  }

  function updateSearch(value: string) {
    updateMarketplaceQuery({
      ...marketplaceQuery,
      q: value,
      agent: "",
      skill: "",
    });
  }

  function updateProofFilter(proof: ProofFilter) {
    updateMarketplaceQuery({ ...marketplaceQuery, proof });
  }

  function clearFilters() {
    updateMarketplaceQuery(EMPTY_MARKETPLACE_QUERY);
  }

  // Opening detail keeps the list mounted and pushes a shareable URL; the route
  // effect above keeps `detail` in sync, so we can update state immediately too.
  const openAgent = useCallback(
    (
      agentName: string,
      mode: MarketplaceDetailMode = "card",
      opts: { section?: MarketplaceAgentSectionId; skill?: string } = {},
    ) => {
      const section = opts.section ?? "overview";
      setDetail({ agentName, mode, section });
      const params: Record<string, string | undefined> = { skill: opts.skill };
      const href =
        mode === "install"
          ? marketplaceAgentInstallRoute(agentName)
          : mode === "trial"
            ? marketplaceAgentTrialRoute(agentName, params)
            : marketplaceAgentSectionRoute(agentName, section, params);
      navigate(href);
    },
    [navigate],
  );

  const closeDetail = useCallback(() => {
    setDetail(null);
    navigate(marketplaceQueryPath(marketplaceQuery, marketplaceView.path));
  }, [navigate, marketplaceQuery, marketplaceView.path]);

  const refreshInstalled = useCallback(async () => {
    const next = await listInstalledAgents();
    setMarketplaceData((current) => ({
      agents: current?.agents || agents || [],
      proofs: current?.proofs || proofs,
      installedAgents: next,
    }));
  }, [agents, proofs, setMarketplaceData]);

  const handleInstall = useCallback(
    async (agent: AgentListing) => {
      if (requiredSetupFields(agent).length > 0) {
        openAgent(agent.name, "install");
        return;
      }
      setQuickInstalling(agent.name);
      setActionErr(null);
      try {
        await installMarketplaceAgent(agent.name);
        await refreshInstalled();
      } catch (ex) {
        setActionErr(messageFromError(ex));
      } finally {
        setQuickInstalling(null);
      }
    },
    [openAgent, refreshInstalled],
  );

  const detailInstalled = detailAgent
    ? installedNames.has(detailAgent.name.toLowerCase())
    : false;

  return (
    <RoutePageShell
      routeId="marketplace"
      data-onboarding-target="marketplace-page"
      actions={
        <label data-onboarding-target="marketplace-search" className="block w-full sm:w-80">
          <span className="sr-only">Search marketplace agents</span>
          <TextInput
            type="search"
            value={marketplaceQuery.q}
            onChange={(e) => updateSearch(e.target.value)}
            placeholder="Filter by name, tool, or tag"
          />
        </label>
      }
    >
      {err && <InlineAlert tone="red">{err}</InlineAlert>}

      <MarketplacePosture
        agents={agents}
        stats={stats}
        filteredCount={filtered.length}
        installedCount={installedAgents.length}
        activeView={activeView}
        searchLoading={searchLoading}
        searchMatches={searchMatches}
        hasActiveFilters={hasActiveFilters}
        firstAgent={filtered[0] || null}
        onClearFilters={clearFilters}
        onOpenAgent={openAgent}
      />

      <div className="flex flex-col gap-3 text-xs text-ink-muted sm:flex-row sm:items-center sm:justify-between">
        <div className="flex flex-wrap items-center gap-3">
          <span role="status" aria-live="polite">
            Showing {filtered.length} of {agents?.length ?? 0}
            {searchLoading ? " searching..." : ""}
          </span>
          {searchMatches && marketplaceQuery.q.trim() && searchMatches[0]?.match_source && (
            <span>{searchMatches[0].match_source} search</span>
          )}
          {marketplaceQuery.agent && (
            <span className="font-mono text-ink-dim">
              {marketplaceQuery.agent}
              {marketplaceQuery.skill ? `.${marketplaceQuery.skill}` : ""}
            </span>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2 sm:justify-end">
          {(
            <label
              data-onboarding-target="marketplace-filters"
              className="flex items-center gap-2"
            >
              <span className="font-mono text-[10px] uppercase tracking-wide text-ink-faint">
                Proof
              </span>
              <SelectInput
                compact
                value={marketplaceQuery.proof}
                onChange={(event) => updateProofFilter(event.target.value as ProofFilter)}
                aria-label="Proof filter"
                className="w-36"
              >
                {PROOF_FILTERS.map((filter) => (
                  <option key={filter.id} value={filter.id}>
                    {filter.label}
                  </option>
                ))}
              </SelectInput>
            </label>
          )}
          {hasActiveFilters && (
            <ToolbarButton
              type="button"
              onClick={clearFilters}
              size="xs"
            >
              Clear
            </ToolbarButton>
          )}
        </div>
      </div>

      {activeView === "proofs" ? (
        <MarketplaceProofsView
          agents={agents}
          filtered={filtered}
          proofsByAgent={proofsByAgent}
          hasActiveFilters={hasActiveFilters}
          onOpenAgent={openAgent}
        />
      ) : (
        <MarketplaceBrowseView
          agents={agents}
          filtered={filtered}
          proofsByAgent={proofsByAgent}
          marketplaceQuery={marketplaceQuery}
          installedNames={installedNames}
          installingAgentName={quickInstalling}
          hasActiveFilters={hasActiveFilters}
          onInstall={handleInstall}
          onOpenAgent={openAgent}
        />
      )}

      <DetailSheet
        open={Boolean(detail)}
        onClose={closeDetail}
        size="xl"
        title={
          detail
            ? detail.mode === "install"
              ? `Install ${detail.agentName}`
              : detail.mode === "trial"
                ? `Trial ${detail.agentName}`
                : detail.agentName
            : ""
        }
        description={
          detail?.mode === "install"
            ? "Configure consumer setup before installing."
            : detail?.mode === "trial"
              ? "Scope inputs and run this marketplace agent in a private trial room."
              : undefined
        }
      >
        {detail?.mode === "trial" ? (
          <MarketplaceAgentTrialView
            agent={detailAgent}
            loading={agents === null}
            requestedAgentName={detail.agentName}
            skillName={marketplaceQuery.skill}
          />
        ) : detail?.mode === "install" ? (
          <MarketplaceAgentInstallView
            agent={detailAgent}
            loading={agents === null}
            requestedAgentName={detail.agentName}
            installed={detailInstalled}
            onBackToCard={() => openAgent(detail.agentName, "card")}
            onInstalled={refreshInstalled}
          />
        ) : detail ? (
          <MarketplaceAgentDetailView
            agent={detailAgent}
            loading={agents === null}
            requestedAgentName={detail.agentName}
            proof={detailAgent ? proofsByAgent.get(detailAgent.name) || null : null}
            installed={detailInstalled}
            installing={detailAgent ? quickInstalling === detailAgent.name : false}
            onInstall={detailAgent ? () => handleInstall(detailAgent) : undefined}
            activeSection={detail.section}
            onSelectSection={(section) => openAgent(detail.agentName, "card", { section })}
            onOpenInstall={() => openAgent(detail.agentName, "install")}
            onOpenTrial={(skill) => openAgent(detail.agentName, "trial", { skill })}
          />
        ) : null}
      </DetailSheet>
    </RoutePageShell>
  );
}

function MarketplacePosture({
  agents,
  stats,
  filteredCount,
  installedCount,
  activeView,
  searchLoading,
  searchMatches,
  hasActiveFilters,
  firstAgent,
  onClearFilters,
  onOpenAgent,
}: {
  agents: AgentListing[] | null;
  stats: ReturnType<typeof summarizeAgents>;
  filteredCount: number;
  installedCount: number;
  activeView: MarketplaceViewId;
  searchLoading: boolean;
  searchMatches: AgentSearchResult[] | null;
  hasActiveFilters: boolean;
  firstAgent: AgentListing | null;
  onClearFilters: () => void;
  onOpenAgent: (agentName: string, mode?: MarketplaceDetailMode) => void;
}) {
  const nextAction = marketplaceNextAction({
    agents,
    stats,
    filteredCount,
    activeView,
    hasActiveFilters,
    firstAgent,
  });
  const tone = marketplacePostureTone(agents, stats);
  const label = marketplacePostureLabel(agents, stats);
  const detail = marketplacePostureDetail({
    agents,
    filteredCount,
    activeView,
    hasActiveFilters,
    searchLoading,
    searchMatches,
  });

  let action: ReactNode = null;
  if (nextAction.kind === "clear") {
    action = (
      <ToolbarButton onClick={onClearFilters} variant="primary" size="sm">
        {nextAction.action}
      </ToolbarButton>
    );
  } else if (nextAction.kind === "open-agent") {
    action = (
      <ToolbarButton
        onClick={() => onOpenAgent(nextAction.agentName)}
        variant={nextAction.variant}
        size="sm"
      >
        {nextAction.action}
      </ToolbarButton>
    );
  } else {
    action = (
      <ToolbarLink href={nextAction.href} variant={nextAction.variant} size="sm">
        {nextAction.action}
      </ToolbarLink>
    );
  }

  return (
    <DashboardSurfacePosture
      data-onboarding-target="marketplace-posture"
      eyebrow="Marketplace"
      title={nextAction.label}
      status={{ tone, label, dot: tone !== "neutral" }}
      metrics={[
        {
          label: "agents",
          value: agents ? stats.total.toLocaleString() : "...",
          tone: stats.running > 0 ? "live" : "neutral",
        },
        {
          label: "verified",
          value: stats.verified.toLocaleString(),
          tone: stats.verified > 0 ? "proof" : "danger",
        },
        {
          label: "installed",
          value: installedCount.toLocaleString(),
          tone: installedCount > 0 ? "live" : "neutral",
        },
      ]}
      actions={
        <>
          <span className="hidden max-w-[20rem] truncate text-xs text-ink-muted lg:inline">
            {detail}
          </span>
          {action}
        </>
      }
    />
  );
}

function marketplacePostureTone(
  agents: AgentListing[] | null,
  stats: ReturnType<typeof summarizeAgents>,
): SurfacePostureStatusTone {
  if (!agents) return "neutral";
  if (stats.total === 0) return "neutral";
  if (stats.running > 0 && stats.verified > 0) return "live";
  if (stats.running > 0 || stats.verified > 0) return "authority";
  return "neutral";
}

function marketplaceAgentRoute(agentName: string, params: Record<string, string | undefined> = {}) {
  return buildRoute(`/marketplace/agents/${encodeURIComponent(agentName)}`, params);
}

function marketplaceAgentSectionRoute(
  agentName: string,
  section: MarketplaceAgentSectionId = "overview",
  params: Record<string, string | undefined> = {},
) {
  const base = `/marketplace/agents/${encodeURIComponent(agentName)}`;
  return buildRoute(section === "overview" ? base : `${base}/${section}`, params);
}

function normalizeMarketplaceAgentSection(
  section: string | null | undefined,
): MarketplaceAgentSectionId {
  return MARKETPLACE_AGENT_SECTIONS.some((item) => item.id === section)
    ? (section as MarketplaceAgentSectionId)
    : "overview";
}

function marketplaceAgentInstallRoute(agentName: string) {
  return `/marketplace/agents/${encodeURIComponent(agentName)}/install`;
}

function marketplaceAgentTrialRoute(
  agentName: string,
  params: Record<string, string | undefined> = {},
) {
  return buildRoute(`/marketplace/agents/${encodeURIComponent(agentName)}/trial`, params);
}

function MarketplaceBrowseView({
  agents,
  filtered,
  proofsByAgent,
  marketplaceQuery,
  installedNames,
  installingAgentName,
  hasActiveFilters,
  onInstall,
  onOpenAgent,
}: {
  agents: AgentListing[] | null;
  filtered: AgentListing[];
  proofsByAgent: Map<string, PublicAgentProofSummary>;
  marketplaceQuery: MarketplaceQueryState;
  installedNames: Set<string>;
  installingAgentName: string | null;
  hasActiveFilters: boolean;
  onInstall: (agent: AgentListing) => void;
  onOpenAgent: (
    agentName: string,
    mode?: MarketplaceDetailMode,
    opts?: { section?: MarketplaceAgentSectionId; skill?: string },
  ) => void;
}) {
  return (
    <div data-onboarding-target="marketplace-results">
      {agents === null ? (
        <LoadingState label="Loading marketplace..." />
      ) : filtered.length === 0 ? (
        <EmptyState
          title={hasActiveFilters ? "No agents match those filters" : "No public agents yet"}
          description={
            hasActiveFilters
              ? "Try a broader name, tool, tag, or proof filter."
              : "Deploy and publish an agent to make it available for evaluation."
          }
        />
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {filtered.map((agent) => (
            <AgentCard
              key={agent.id}
              agent={agent}
              proof={proofsByAgent.get(agent.name) || null}
              focused={
                marketplaceQuery.agent.toLowerCase() === agent.name.toLowerCase()
              }
              installed={installedNames.has(agent.name.toLowerCase())}
              installing={installingAgentName === agent.name}
              onInstall={() => onInstall(agent)}
              onOpenAgent={onOpenAgent}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function MarketplaceAgentDetailView({
  agent,
  loading,
  requestedAgentName,
  proof,
  installed,
  installing,
  onInstall,
  activeSection,
  onSelectSection,
  onOpenInstall,
  onOpenTrial,
}: {
  agent: AgentListing | null;
  loading: boolean;
  requestedAgentName: string;
  proof: PublicAgentProofSummary | null;
  installed: boolean;
  installing: boolean;
  onInstall?: () => void;
  activeSection: MarketplaceAgentSectionId;
  onSelectSection: (section: MarketplaceAgentSectionId) => void;
  onOpenInstall: () => void;
  onOpenTrial: (skill?: string) => void;
}) {
  if (loading) {
    return <LoadingState label="Loading marketplace agent..." />;
  }
  if (!agent) {
    return (
      <EmptyState
        title="Marketplace agent not found"
        description={`${requestedAgentName} is no longer available or the link is stale.`}
        action={<ToolbarLink href="/marketplace">Back to marketplace</ToolbarLink>}
      />
    );
  }

  const runtime = agent.card?.runtime;
  const provisioning = runtime?.llm_provisioning || "platform";
  const skills = agent.card?.skills || [];
  const tools = runtime?.tools_used || [];
  const primarySkill = skills[0] || null;
  const description = agent.card?.description || agent.description || "No description.";
  const setupFieldCount = requiredSetupFields(agent).length;

  return (
    <div data-onboarding-target="marketplace-results" className="space-y-4">
      <SurfacePanel as="section" className="bg-runtime-bg p-4">
        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_auto]">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="break-all font-mono text-xl font-semibold text-ink">
                {agent.name || requestedAgentName}
              </h2>
              <StateBadge
                status={agent.status}
                live={agent.status === "running" || agent.status === "ready"}
              />
              <StateBadge
                status={proof?.badge || "unverified"}
                aria-label={`Proof status: ${proof?.badge || "unverified"}`}
                size="xs"
              />
              <ProvisioningBadge provisioning={provisioning} />
              <AccountTrialBadge access={runtime?.account_access} />
            </div>
            <p className="mt-3 max-w-3xl text-sm leading-relaxed text-ink-dim">
              {description}
            </p>
          </div>
          <div className="flex flex-wrap gap-2 lg:justify-end">
            {installed ? (
              <ToolbarLink href="/installed-setup" variant="primary">
                Installed
              </ToolbarLink>
            ) : (
              <ToolbarButton
                type="button"
                onClick={onInstall}
                disabled={installing || !onInstall}
                variant="success"
              >
                {installing ? "Installing..." : "Install"}
              </ToolbarButton>
            )}
            {agent.status === "running" && primarySkill ? (
              <ToolbarButton
                type="button"
                onClick={() => onOpenTrial(primarySkill.name)}
                variant="primary"
              >
                Run trial
              </ToolbarButton>
            ) : (
              <DisabledAction
                label="Run trial"
                reason={
                  agent.status === "running"
                    ? "This agent has no declared tools"
                    : `This agent is ${agent.status}`
                }
              />
            )}
            <ToolbarLink href={buildRoute("/activity", {
              agent: agent.name,
              source: proof?.latest ? "proof" : undefined,
            })}>
              {proof?.latest ? "Proof activity" : "Activity"}
            </ToolbarLink>
            {agent.url && (
              <ToolbarLink href={agent.url} external>
                Agent API
              </ToolbarLink>
            )}
          </div>
        </div>

        <MarketplaceAgentDecisionPanel
          agent={agent}
          proof={proof}
          installed={installed}
          primarySkill={primarySkill}
          provisioning={provisioning}
          setupFieldCount={setupFieldCount}
          onOpenInstall={onOpenInstall}
          onOpenTrial={onOpenTrial}
          onSelectSection={onSelectSection}
        />
      </SurfacePanel>

      <MarketplaceAgentDetailNav
        activeSection={activeSection}
        onSelectSection={onSelectSection}
      />

      {activeSection === "skills" ? (
        <MarketplaceAgentSkillsPanel skills={skills} tools={tools} />
      ) : activeSection === "proof" ? (
        <MarketplaceAgentProofPanel proof={proof} skills={skills} />
      ) : activeSection === "endpoints" ? (
        <MarketplaceAgentEndpointsPanel agent={agent} />
      ) : (
        <MarketplaceAgentOverviewPanel
          agent={agent}
          proof={proof}
          skills={skills}
          tools={tools}
          provisioning={provisioning}
          installed={installed}
        />
      )}
    </div>
  );
}

function MarketplaceAgentDecisionPanel({
  agent,
  proof,
  installed,
  primarySkill,
  provisioning,
  setupFieldCount,
  onOpenInstall,
  onOpenTrial,
  onSelectSection,
}: {
  agent: AgentListing;
  proof: PublicAgentProofSummary | null;
  installed: boolean;
  primarySkill: AgentSkill | null;
  provisioning: RuntimeProvisioning;
  setupFieldCount: number;
  onOpenInstall: () => void;
  onOpenTrial: (skill?: string) => void;
  onSelectSection: (section: MarketplaceAgentSectionId) => void;
}) {
  const nextAction = marketplaceAgentNextAction({
    agent,
    proof,
    installed,
    primarySkill,
    setupFieldCount,
  });

  const actionButton =
    nextAction.kind === "install" ? (
      <ToolbarButton type="button" onClick={onOpenInstall} variant={nextAction.variant} size="md">
        {nextAction.action}
      </ToolbarButton>
    ) : nextAction.kind === "trial" ? (
      <ToolbarButton
        type="button"
        onClick={() => onOpenTrial(primarySkill?.name)}
        variant={nextAction.variant}
        size="md"
      >
        {nextAction.action}
      </ToolbarButton>
    ) : nextAction.kind === "activity" ? (
      <ToolbarLink href={nextAction.href} variant={nextAction.variant} size="md">
        {nextAction.action}
      </ToolbarLink>
    ) : (
      <ToolbarButton
        type="button"
        onClick={() => onSelectSection("proof")}
        variant={nextAction.variant}
        size="md"
      >
        {nextAction.action}
      </ToolbarButton>
    );

  return (
    <div className="mt-4 grid gap-4 border-t border-runtime-line-soft/70 pt-4">
      <SummaryStrip className="lg:grid-cols-5" aria-label={`${agent.name} marketplace decision summary`}>
        <SummaryMetric
          label="runtime"
          value={agent.status}
          detail={agent.status === "running" || agent.status === "ready" ? "available" : "not runnable"}
          tone={agent.status === "running" || agent.status === "ready" ? "emerald" : "amber"}
          size="compact"
        />
        <SummaryMetric
          label="tools"
          value={String(agent.card?.skills?.length || 0)}
          detail={primarySkill?.name || "no primary tool"}
          tone={primarySkill ? "emerald" : "amber"}
          size="compact"
        />
        <SummaryMetric
          label="proof"
          value={proof?.badge || "unverified"}
          detail={proof?.latest?.status || "no receipt"}
          tone={proof?.badge === "verified" ? "emerald" : "amber"}
          size="compact"
        />
        <SummaryMetric
          label="LLM"
          value={formatProvisioning(provisioning)}
          size="compact"
        />
        <SummaryMetric
          label="setup"
          value={setupFieldCount > 0 ? `${setupFieldCount} req` : "none"}
          detail={installed ? "installed" : "before install"}
          tone={installed || setupFieldCount === 0 ? "emerald" : "amber"}
          size="compact"
        />
      </SummaryStrip>

      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="min-w-0">
          <div className="text-[10px] font-medium uppercase tracking-wide text-ink-faint">
            Decision cue
          </div>
          <div className="mt-1 text-sm font-medium text-ink">
            {nextAction.label}
          </div>
          <p className="mt-1 max-w-2xl text-xs leading-relaxed text-ink-muted">
            {nextAction.detail}
          </p>
        </div>
        {actionButton}
      </div>
    </div>
  );
}

function MarketplaceAgentDetailNav({
  activeSection,
  onSelectSection,
}: {
  activeSection: MarketplaceAgentSectionId;
  onSelectSection: (section: MarketplaceAgentSectionId) => void;
}) {
  return (
    <SegmentedControl
      role="tablist"
      aria-label="Marketplace agent sections"
      className="flex gap-1 overflow-x-auto bg-runtime-panel/60"
    >
      {MARKETPLACE_AGENT_SECTIONS.map((section) => (
        <ToolbarButton
          key={section.id}
          type="button"
          role="tab"
          aria-selected={activeSection === section.id}
          active={activeSection === section.id}
          onClick={() => onSelectSection(section.id)}
          size="xs"
          className="whitespace-nowrap"
        >
          {section.label}
        </ToolbarButton>
      ))}
    </SegmentedControl>
  );
}

function MarketplaceAgentOverviewPanel({
  agent,
  proof,
  skills,
  tools,
  provisioning,
  installed,
}: {
  agent: AgentListing;
  proof: PublicAgentProofSummary | null;
  skills: AgentSkill[];
  tools: string[];
  provisioning: RuntimeProvisioning;
  installed: boolean;
}) {
  return (
    <SurfacePanel as="section" className="bg-runtime-bg p-4">
      <div className="text-[10px] uppercase text-ink-faint">
        Overview
      </div>
      <dl className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        <CompactFact label="version" value={`v${agent.card?.version || agent.version}`} mono />
        <CompactFact label="visibility" value={agent.public ? "public" : "private"} mono />
        <CompactFact label="install" value={installed ? "installed" : "not installed"} mono />
        <CompactFact label="proof" value={proof?.badge || "unverified"} mono />
        <CompactFact label="tools" value={String(skills.length)} mono />
        <CompactFact label="tools" value={String(tools.length)} mono />
        <CompactFact label="LLM" value={formatProvisioning(provisioning)} mono />
      </dl>
      <p className="mt-4 text-sm leading-relaxed text-ink-dim">
        {agent.card?.description || agent.description || "No description."}
      </p>
    </SurfacePanel>
  );
}

function MarketplaceAgentSkillsPanel({
  skills,
  tools,
}: {
  skills: AgentSkill[];
  tools: string[];
}) {
  return (
    <SurfacePanel as="section" className="bg-runtime-bg p-4">
      <div className="text-[10px] uppercase text-ink-faint">
        Skills and runtime
      </div>
      <SkillPreview
        skills={skills}
        tags={uniqueStrings(skills.flatMap((skill) => skill.tags || [])).slice(0, 8)}
      />
      {tools.length > 0 ? (
        <section className="mt-4 border-t border-runtime-line-soft/60 pt-3">
          <h3 className="text-[10px] uppercase text-ink-faint">
            Tools
          </h3>
          <TagList values={tools} />
        </section>
      ) : (
        <EmptyState title="No runtime tools declared" size="compact" className="mt-4" />
      )}
    </SurfacePanel>
  );
}

function MarketplaceAgentProofPanel({
  proof,
  skills,
}: {
  proof: PublicAgentProofSummary | null;
  skills: AgentSkill[];
}) {
  return (
    <SurfacePanel as="section" className="bg-runtime-bg p-4">
      <ProofEvidence proof={proof} skills={skills.map((skill) => skill.name)} />
    </SurfacePanel>
  );
}

function MarketplaceAgentEndpointsPanel({ agent }: { agent: AgentListing }) {
  return (
    <SurfacePanel as="section" className="bg-runtime-bg p-4">
      <div className="text-[10px] uppercase text-ink-faint">
        Public endpoints
      </div>
      <div className="mt-3 flex flex-wrap gap-2">
        <ToolbarLink href={buildAgentOpenApiHref(agent.name)} external>
          OpenAPI
        </ToolbarLink>
        <ToolbarLink href={buildPublicAgentHref(agent.name)} external>
          Public page
        </ToolbarLink>
        {agent.url && (
          <ToolbarLink href={agent.url} external>
            Agent API
          </ToolbarLink>
        )}
        <ToolbarLink href="/marketplace">
          Back to browse
        </ToolbarLink>
      </div>
    </SurfacePanel>
  );
}

function MarketplaceAgentTrialView({
  agent,
  loading,
  requestedAgentName,
  skillName,
}: {
  agent: AgentListing | null;
  loading: boolean;
  requestedAgentName: string;
  skillName: string;
}) {
  if (loading) {
    return <LoadingState label="Loading marketplace trial..." />;
  }
  if (!agent) {
    return (
      <EmptyState
        title="Marketplace agent not found"
        description={`${requestedAgentName} is no longer available or the link is stale.`}
        action={<ToolbarLink href="/marketplace">Back to marketplace</ToolbarLink>}
      />
    );
  }

  return (
    <div data-onboarding-target="marketplace-results">
      <MarketplaceTrialLauncher
        agent={agent}
        skill={skillName || agent.card?.skills?.[0] || null}
        backHref={marketplaceAgentRoute(agent.name)}
      />
    </div>
  );
}

function MarketplaceProofsView({
  agents,
  filtered,
  proofsByAgent,
  hasActiveFilters,
  onOpenAgent,
}: {
  agents: AgentListing[] | null;
  filtered: AgentListing[];
  proofsByAgent: Map<string, PublicAgentProofSummary>;
  hasActiveFilters: boolean;
  onOpenAgent: (
    agentName: string,
    mode?: MarketplaceDetailMode,
    opts?: { section?: MarketplaceAgentSectionId; skill?: string },
  ) => void;
}) {
  if (agents === null) {
    return (
      <div data-onboarding-target="marketplace-results">
        <LoadingState label="Loading marketplace proofs..." />
      </div>
    );
  }

  if (filtered.length === 0) {
    return (
      <div data-onboarding-target="marketplace-results">
        <EmptyState
          title={hasActiveFilters ? "No proof rows match those filters" : "No public agents yet"}
          description={
            hasActiveFilters
              ? "Try a broader name, tool, tag, or proof filter."
              : "Proof rows appear here once public agents are available."
          }
        />
      </div>
    );
  }

  const verified = filtered.filter((agent) => proofsByAgent.get(agent.name)?.badge === "verified").length;
  const withReceipts = filtered.filter((agent) => Boolean(proofsByAgent.get(agent.name)?.latest)).length;

  return (
    <div data-onboarding-target="marketplace-results" className="space-y-4">
      <SummaryStrip aria-label="Proof summary">
        <SummaryMetric label="matching agents" value={filtered.length} />
        <SummaryMetric label="verified" value={verified} />
        <SummaryMetric label="with receipts" value={withReceipts} />
        <SummaryMetric label="needs proof" value={filtered.length - verified} />
      </SummaryStrip>

      <div className="space-y-3" role="list" aria-label="Marketplace proof rows">
        {filtered.map((agent) => {
          const proof = proofsByAgent.get(agent.name) || null;
          const latest = proof?.latest || null;
          const skills = agent.card?.skills || [];
          const primarySkill = skills[0] || null;
          const activityHref = buildRoute("/activity", {
            agent: agent.name,
            source: latest ? "proof" : undefined,
          });
          const passedRuns = proof?.passed_runs ?? (latest?.status === "passed" ? 1 : 0);
          const totalRuns = proof?.total_runs ?? (latest ? 1 : 0);

          return (
            <SurfacePanel
              key={agent.id}
              as="article"
              role="listitem"
              className="bg-runtime-bg p-4"
            >
              <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_auto]">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <h2 className="break-all font-mono text-sm font-semibold text-ink">
                      {agent.name}
                    </h2>
                    <StateBadge status={proof?.badge || "unverified"} />
                    <StateBadge
                      status={latest?.status || "no_receipt"}
                      label={latest?.status || "no receipt"}
                      size="xs"
                    />
                  </div>
                  <p className="mt-2 line-clamp-2 text-sm leading-relaxed text-ink-muted">
                    {latest?.summary || agent.card?.description || agent.description || "No proof summary available."}
                  </p>
                </div>
                <div className="flex flex-wrap gap-2 lg:justify-end">
                  <ToolbarLink href={activityHref}>
                    {latest ? "Proof activity" : "Activity"}
                  </ToolbarLink>
                  <ToolbarButton
                    type="button"
                    onClick={() =>
                      onOpenAgent(agent.name, "card", { skill: primarySkill?.name })
                    }
                  >
                    Open card
                  </ToolbarButton>
                </div>
              </div>

              <dl className="mt-4 grid gap-2 sm:grid-cols-3 lg:grid-cols-6">
                <CompactFact label="tool" value={latest?.skill_name || primarySkill?.name || "-"} mono />
                <CompactFact label="proofs" value={totalRuns ? `${passedRuns}/${totalRuns}` : "0"} mono />
                <CompactFact label="files" value={String(latest?.file_ops_count ?? 0)} mono />
                <CompactFact label="runtime" value={latest ? fmtMs(latest.elapsed_ms) : "-"} mono />
                <CompactFact label="evidence" value={latest ? proofEvidenceId(latest) : "-"} mono />
                <CompactFact label="created" value={latest ? fmtDate(latest.created_at) : "-"} mono />
              </dl>
            </SurfacePanel>
          );
        })}
      </div>
    </div>
  );
}

function AgentCard({
  agent,
  proof,
  focused,
  installed,
  installing,
  onInstall,
  onOpenAgent,
}: {
  agent: AgentListing;
  proof: PublicAgentProofSummary | null;
  focused: boolean;
  installed: boolean;
  installing: boolean;
  onInstall: () => void;
  onOpenAgent: (
    agentName: string,
    mode?: MarketplaceDetailMode,
    opts?: { section?: MarketplaceAgentSectionId; skill?: string },
  ) => void;
}) {
  const runtime = agent.card?.runtime;
  const provisioning = runtime?.llm_provisioning || "platform";
  const skills = agent.card?.skills || [];
  const tools = runtime?.tools_used || [];
  const primarySkill = skills[0] || null;
  const featuredTags = uniqueStrings(
    skills.flatMap((skill) => skill.tags || []),
  ).slice(0, 5);
  const description = agent.card?.description || agent.description || "No description.";

  return (
    <SurfacePanel
      as="article"
      className={
        "flex h-full min-w-0 flex-col bg-runtime-bg p-4 transition " +
        (focused
          ? "border-signal-authority/45 ring-1 ring-signal-authority/25"
          : "border-runtime-line-soft/60 hover:border-runtime-line-mid")
      }
      aria-labelledby={`marketplace-agent-${agent.id}`}
    >
      <header className="min-w-0">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2
              id={`marketplace-agent-${agent.id}`}
              className="truncate font-mono text-base font-semibold text-ink"
            >
              {agent.name}
            </h2>
            <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-ink-muted">
              <span>v{agent.card?.version || agent.version}</span>
              <StateBadge
                status={agent.status}
                live={agent.status === "running" || agent.status === "ready"}
              />
              <span className="rounded-full border border-runtime-line-soft/60 px-2 py-0.5">
                {agent.public ? "public" : "private"}
              </span>
            </div>
          </div>
          <div className="flex shrink-0 flex-col items-end gap-2">
            <StateBadge
              status={proof?.badge || "unverified"}
              aria-label={`Proof status: ${proof?.badge || "unverified"}`}
              size="xs"
            />
            <ProvisioningBadge provisioning={provisioning} />
            <AccountTrialBadge access={runtime?.account_access} />
          </div>
        </div>

        <p className="mt-3 line-clamp-3 text-sm leading-relaxed text-ink-dim">
          {description}
        </p>
      </header>

      <dl className="mt-4 grid grid-cols-3 gap-3 border-y border-runtime-line-soft/60 py-3">
        <CompactFact label="tools" value={String(skills.length)} mono />
        <CompactFact label="proof" value={proof?.latest ? proof.latest.status : "none"} mono />
        <CompactFact
          label="runtime"
          value={formatProvisioning(provisioning)}
          mono
        />
      </dl>

      <SkillPreview skills={skills} tags={featuredTags} />
      <ProofEvidence proof={proof} skills={skills.map((skill) => skill.name)} />

      {tools.length > 0 && (
        <section className="mt-4 border-t border-runtime-line-soft/60 pt-3">
          <h3 className="text-[10px] uppercase text-ink-faint">
            Tools
          </h3>
          <TagList values={tools.slice(0, 6)} />
        </section>
      )}

      <div className="mt-auto flex flex-wrap gap-2 pt-4 text-xs">
        {installed ? (
          <ToolbarLink
            href="/installed-setup"
            aria-label={`Open installed setup for ${agent.name}`}
            variant="primary"
          >
            Installed
          </ToolbarLink>
        ) : (
          <ToolbarButton
            type="button"
            onClick={onInstall}
            disabled={installing}
            variant="success"
          >
            {installing ? "Installing..." : "Install"}
          </ToolbarButton>
        )}
        {agent.status === "running" && primarySkill ? (
          <ToolbarButton
            type="button"
            onClick={() =>
              onOpenAgent(agent.name, "trial", { skill: primarySkill.name })
            }
          >
            Run trial
          </ToolbarButton>
        ) : null}
        <ToolbarButton
          type="button"
          onClick={() =>
            onOpenAgent(agent.name, "card", { skill: primarySkill?.name })
          }
          aria-label={`Open the marketplace card for ${agent.name}`}
        >
          Details
        </ToolbarButton>
      </div>
    </SurfacePanel>
  );
}

function MarketplaceAgentInstallView({
  agent,
  loading: agentLoading,
  requestedAgentName,
  installed,
  onInstalled,
  onBackToCard,
}: {
  agent: AgentListing | null;
  loading: boolean;
  requestedAgentName: string;
  installed: boolean;
  onInstalled: () => void | Promise<void>;
  onBackToCard: () => void;
}) {
  const [status, setStatus] = useState<ConsumerSetupStatus | null>(null);
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState<"user" | "org" | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!agent) {
      setStatus(null);
      setValues({});
      setErr(null);
      setSaving(null);
      return;
    }
    let active = true;
    setLoading(true);
    setErr(null);
    getConsumerSetup(agent.name)
      .then((next) => {
        if (!active) return;
        setStatus(next);
        setValues(initialSetupValues(next));
      })
      .catch((ex) => {
        if (active) setErr(messageFromError(ex));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [agent]);

  async function save(scope: "user" | "org") {
    if (!agent || !status) return;
    const fields = status.declaration.fields;
    const payload = payloadSetupValues(fields, values);
    const missing = fields.filter(
      (field) =>
        field.required &&
        !status.values.some((value) => value.name === field.name && value.configured) &&
        !fieldValuePresent(values[field.name]),
    );
    if (missing.length > 0) {
      setErr(`Add required setup: ${missing.map((field) => field.label || field.name).join(", ")}`);
      return;
    }
    if (Object.keys(payload).length === 0 && !status.complete) {
      setErr("Enter the required setup values before installing.");
      return;
    }

    setSaving(scope);
    setErr(null);
    try {
      const next =
        scope === "org"
          ? await upsertOrgConsumerSetup(agent.name, payload, status.organization?.slug)
          : await upsertConsumerSetup(agent.name, payload);
      setStatus(next);
      setValues(initialSetupValues(next));
      if (!next.complete) {
        setErr(`Still missing required setup: ${next.missing_required.join(", ")}`);
        return;
      }
      await installMarketplaceAgent(agent.name);
      await onInstalled();
      setErr(null);
    } catch (ex) {
      setErr(messageFromError(ex));
    } finally {
      setSaving(null);
    }
  }

  const fields = status?.declaration.fields || requiredSetupFields(agent);
  const valueByName = new Map((status?.values || []).map((value) => [value.name, value]));
  const missing = new Set(status?.missing_required || []);
  const busy = loading || saving !== null;

  if (agentLoading) {
    return <LoadingState label="Loading install setup..." />;
  }
  if (!agent) {
    return (
      <EmptyState
        title="Marketplace agent not found"
        description="This public agent is no longer available or the link is stale."
        action={<ToolbarLink href="/marketplace">Back to marketplace</ToolbarLink>}
      />
    );
  }

  return (
    <div data-onboarding-target="marketplace-results" className="space-y-4">
      <SurfacePanel as="section" className="bg-runtime-bg p-4">
        <div className="flex flex-col gap-3 border-b border-runtime-line-soft/60 pb-4 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <div className="text-xs uppercase text-ink-muted">
              Marketplace install
            </div>
            <h2 className="mt-1 break-all font-mono text-2xl font-semibold text-ink">
              {agent.name || requestedAgentName}
            </h2>
            <p className="mt-2 max-w-2xl text-sm leading-relaxed text-ink-muted">
              Add the consumer setup this agent requires before it can run for your account.
            </p>
          </div>
          <div className="flex shrink-0 flex-wrap gap-2">
            <ToolbarButton type="button" onClick={onBackToCard}>
              Back to card
            </ToolbarButton>
            <ToolbarButton
              variant="primary"
              onClick={() => save("user")}
              disabled={busy || installed || (!status && fields.length === 0)}
            >
              {saving === "user"
                ? "Installing..."
                : installed
                  ? "Installed"
                  : fields.length === 0
                    ? "Install"
                    : "Save and install"}
            </ToolbarButton>
            {status?.can_manage_org && (
              <ToolbarButton
                onClick={() => save("org")}
                disabled={busy || installed || (!status && fields.length === 0)}
              >
                {saving === "org"
                  ? "Installing..."
                  : `Save for ${status.organization?.name || "org"}`}
              </ToolbarButton>
            )}
          </div>
        </div>

        <div className="mt-4 space-y-4">
          {loading && <LoadingState label="Loading setup..." />}
          {err && <InlineAlert tone="red">{err}</InlineAlert>}
          {installed && <InlineAlert tone="emerald">This agent is installed.</InlineAlert>}

          {!loading && fields.length === 0 && (
            <InlineAlert tone="neutral">
              This agent does not require consumer setup. You can install it directly.
            </InlineAlert>
          )}

          {fields.length > 0 && (
            <div className="space-y-4">
              <div className="flex flex-wrap items-center gap-2">
                <StatusBadge tone={status?.complete ? "emerald" : "amber"}>
                  {status?.complete ? "setup complete" : `${missing.size} missing`}
                </StatusBadge>
                {status?.organization && <StatusBadge>{status.organization.name}</StatusBadge>}
              </div>

              <SurfacePanel as="div" className="divide-y divide-runtime-line-soft">
                {fields.map((field) => {
                  const current = valueByName.get(field.name);
                  return (
                    <div key={field.name} className="p-4">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-sm font-medium text-ink">
                          {field.label || field.name}
                        </span>
                        {field.required && <StatusBadge tone="amber">required</StatusBadge>}
                        {field.kind === "secret" && <StatusBadge>secret</StatusBadge>}
                        {missing.has(field.name) && <StatusBadge tone="amber">missing</StatusBadge>}
                      </div>
                      {field.description && (
                        <p className="mt-1 text-xs leading-relaxed text-ink-muted">
                          {field.description}
                        </p>
                      )}
                      {current?.configured && (
                        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-ink-muted">
                          <span>{current.source || "configured"}</span>
                          <span className="rounded-md border border-runtime-line-soft/60 bg-runtime-panel/50 px-2 py-1 font-mono text-ink-soft">
                            {current.value_redacted || "configured"}
                          </span>
                        </div>
                      )}
                      <ConsumerSetupFieldInput
                        field={field}
                        value={values[field.name]}
                        attention={missing.has(field.name)}
                        disabled={busy}
                        onChange={(value) =>
                          setValues((currentValues) => ({
                            ...currentValues,
                            [field.name]: value,
                          }))
                        }
                      />
                    </div>
                  );
                })}
              </SurfacePanel>
            </div>
          )}
        </div>
      </SurfacePanel>
    </div>
  );
}

function SkillPreview({
  skills,
  tags,
}: {
  skills: AgentSkill[];
  tags: string[];
}) {
  if (skills.length === 0) {
    return (
      <section className="mt-4">
        <h3 className="text-[10px] uppercase text-ink-faint">
          Skills
        </h3>
        <p className="mt-2 text-xs text-ink-muted">No declared tools.</p>
      </section>
    );
  }

  return (
    <section className="mt-4">
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-[10px] uppercase text-ink-faint">
          Skills
        </h3>
        {tags.length > 0 && <TagList values={tags} compact />}
      </div>
      <ul className="mt-2 space-y-3">
        {skills.slice(0, 3).map((skill) => (
          <li key={skill.name} className="min-w-0">
            <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
              <span className="truncate font-mono text-xs font-medium text-ink-soft">
                {skill.name}
              </span>
              {skill.tags?.slice(0, 3).map((tag) => (
                <span key={tag} className="text-[10px] text-ink-faint">
                  {tag}
                </span>
              ))}
            </div>
            {skill.description && (
              <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-ink-muted">
                {skill.description}
              </p>
            )}
          </li>
        ))}
      </ul>
      {skills.length > 3 && (
        <p className="mt-2 text-xs text-ink-faint">
          + {skills.length - 3} more skills
        </p>
      )}
    </section>
  );
}

function ProofEvidence({
  proof,
  skills,
}: {
  proof: PublicAgentProofSummary | null;
  skills: string[];
}) {
  const latest = proof?.latest || null;
  if (!latest) {
    return (
      <section className="mt-4 border-t border-runtime-line-soft/60 pt-3">
        <h3 className="text-[10px] uppercase text-ink-faint">
          Proof evidence
        </h3>
        <p className="mt-2 text-xs text-ink-muted">No public proof receipt.</p>
      </section>
    );
  }

  const declaredSkill = skills.includes(latest.skill_name);
  const evidenceId = latest.card_hash || latest.head_sha || "";
  const totalRuns = proof?.total_runs ?? 1;
  const passedRuns = proof?.passed_runs ?? (latest.status === "passed" ? 1 : 0);

  return (
    <section className="mt-4 border-t border-runtime-line-soft/60 pt-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-[10px] uppercase text-ink-faint">
          Proof evidence
        </h3>
        <StatusBadge tone={latest.status === "passed" ? "emerald" : "amber"}>
          {latest.status}
        </StatusBadge>
      </div>
      <dl className="mt-3 grid gap-x-4 gap-y-3 text-[11px] sm:grid-cols-2">
        <CompactFact label="tool" value={latest.skill_name} mono size="compact" />
        <CompactFact
          label="coverage"
          value={declaredSkill ? "declared tool" : "external tool"}
          tone={declaredSkill ? "emerald" : "amber"}
          mono
          size="compact"
        />
        <CompactFact label="proofs" value={`${passedRuns}/${totalRuns} passed`} mono size="compact" />
        <CompactFact label="files" value={String(latest.file_ops_count)} mono size="compact" />
        <CompactFact label="runtime" value={fmtMs(latest.elapsed_ms)} mono size="compact" />
        <CompactFact label="evidence" value={evidenceId ? evidenceId.slice(0, 12) : "-"} mono size="compact" />
        <CompactFact label="created" value={fmtDate(latest.created_at)} mono size="compact" />
      </dl>
      {latest.summary && (
        <p className="mt-3 line-clamp-2 text-xs leading-relaxed text-ink-muted">
          {latest.summary}
        </p>
      )}
    </section>
  );
}

function ProvisioningBadge({ provisioning }: { provisioning: RuntimeProvisioning }) {
  return (
    <div className="flex flex-col items-end gap-1 text-right">
      <span className="text-[10px] text-ink-muted">
        LLM: <span className="text-ink-dim">{formatProvisioning(provisioning)}</span>
      </span>
    </div>
  );
}

function AccountTrialBadge({
  access,
}: {
  access?: AccountAccessPolicy;
}) {
  if (!access?.required) return null;
  const calls = Math.max(0, access.platform_skill_calls || 0);
  return (
    <StatusBadge
      tone="emerald"
      title={`${calls} platform-funded skill calls per account, then bring your own model key`}
    >
      {calls > 0 ? `${calls} funded calls → BYOK` : "Account + BYOK"}
    </StatusBadge>
  );
}

function TagList({
  values,
  compact = false,
}: {
  values: string[];
  compact?: boolean;
}) {
  if (values.length === 0) return null;
  return (
    <div className={`flex flex-wrap gap-1 ${compact ? "justify-end" : "mt-2"}`}>
      {values.map((value) => (
        <span
          key={value}
          className="rounded-full border border-runtime-line-soft/60 px-2 py-0.5 text-[10px] text-ink-muted"
        >
          {value}
        </span>
      ))}
    </div>
  );
}

function DisabledAction({ label, reason }: { label: string; reason: string }) {
  return (
    <ToolbarButton
      type="button"
      disabled
      aria-label={`${label} unavailable: ${reason}`}
      title={reason}
    >
      {label}
    </ToolbarButton>
  );
}

type MarketplaceNextAction =
  | {
      kind: "link";
      label: string;
      detail: string;
      href: string;
      action: string;
      variant: "primary" | "secondary";
    }
  | {
      kind: "open-agent";
      label: string;
      detail: string;
      agentName: string;
      action: string;
      variant: "primary" | "secondary";
    }
  | {
      kind: "clear";
      label: string;
      detail: string;
      action: string;
    };

function marketplaceNextAction({
  agents,
  stats,
  filteredCount,
  activeView,
  hasActiveFilters,
  firstAgent,
}: {
  agents: AgentListing[] | null;
  stats: ReturnType<typeof summarizeAgents>;
  filteredCount: number;
  activeView: MarketplaceViewId;
  hasActiveFilters: boolean;
  firstAgent: AgentListing | null;
}): MarketplaceNextAction {
  if (!agents) {
    return {
      kind: "link",
      label: "Load marketplace catalog",
      detail: "Fetching agents, public proof receipts, and installed setup state.",
      href: "/marketplace",
      action: "Browse",
      variant: "secondary",
    };
  }
  if (hasActiveFilters && filteredCount === 0) {
    return {
      kind: "clear",
      label: "Broaden the marketplace view",
      detail: "No agents match the active filters. Clear them to return to the full catalog.",
      action: "Clear filters",
    };
  }
  if (activeView !== "proofs" && stats.total > stats.verified) {
    return {
      kind: "link",
      label: "Review proof coverage",
      detail: `${Math.max(stats.total - stats.verified, 0)} public agents still need verified proof evidence.`,
      href: "/marketplace/proofs?proof=needs-proof",
      action: "Open proofs",
      variant: "primary",
    };
  }
  if (firstAgent) {
    return {
      kind: "open-agent",
      label: "Open the highest ranked agent",
      detail: `${firstAgent.name} is ready for a closer card, proof, setup, and trial review.`,
      agentName: firstAgent.name,
      action: "Open agent",
      variant: "secondary",
    };
  }
  return {
    kind: "link",
    label: "No public agents available",
    detail: "Published agents will appear here with proof receipts and install actions.",
    href: "/marketplace",
    action: "Browse",
    variant: "secondary",
  };
}

function marketplacePostureLabel(
  agents: AgentListing[] | null,
  stats: ReturnType<typeof summarizeAgents>,
) {
  if (!agents) return "loading";
  if (stats.total === 0) return "empty";
  if (stats.running > 0 && stats.verified > 0) return "ready catalog";
  if (stats.running > 0) return "needs proof";
  if (stats.verified > 0) return "needs runtime";
  return "review catalog";
}

function marketplacePostureDetail({
  agents,
  filteredCount,
  activeView,
  hasActiveFilters,
  searchLoading,
  searchMatches,
}: {
  agents: AgentListing[] | null;
  filteredCount: number;
  activeView: MarketplaceViewId;
  hasActiveFilters: boolean;
  searchLoading: boolean;
  searchMatches: AgentSearchResult[] | null;
}) {
  if (!agents) return "loading agents and receipts";
  if (searchLoading) return "searching remote index";
  const view = MARKETPLACE_VIEWS.find((item) => item.id === activeView)?.label.toLowerCase() || "browse";
  const matchSource =
    searchMatches && searchMatches[0]?.match_source
      ? ` via ${searchMatches[0].match_source}`
      : "";
  if (hasActiveFilters) {
    return `${filteredCount} matching ${view} result${filteredCount === 1 ? "" : "s"}${matchSource}`;
  }
  return `${agents.length} public agent${agents.length === 1 ? "" : "s"} in ${view}`;
}

type MarketplaceAgentNextAction =
  | {
      kind: "install" | "trial" | "proof";
      label: string;
      detail: string;
      action: string;
      variant: "primary" | "secondary";
    }
  | {
      kind: "activity";
      label: string;
      detail: string;
      href: string;
      action: string;
      variant: "primary" | "secondary";
    };

function marketplaceAgentNextAction({
  agent,
  proof,
  installed,
  primarySkill,
  setupFieldCount,
}: {
  agent: AgentListing;
  proof: PublicAgentProofSummary | null;
  installed: boolean;
  primarySkill: AgentSkill | null;
  setupFieldCount: number;
}): MarketplaceAgentNextAction {
  if (!installed && setupFieldCount > 0) {
    return {
      kind: "install",
      label: "Configure required setup",
      detail: `${setupFieldCount} consumer setup field${setupFieldCount === 1 ? "" : "s"} must be saved before this agent can run for you.`,
      action: "Configure setup",
      variant: "primary",
    };
  }
  if (!installed) {
    return {
      kind: "install",
      label: "Install before repeated use",
      detail: "Install this agent to make setup and future runs easier to manage from your account.",
      action: "Open install",
      variant: "primary",
    };
  }
  if ((agent.status === "running" || agent.status === "ready") && primarySkill) {
    return {
      kind: "trial",
      label: "Run a trial with the primary tool",
      detail: `${primarySkill.name} is available for a focused marketplace trial.`,
      action: "Run trial",
      variant: "primary",
    };
  }
  if (!proof?.latest) {
    return {
      kind: "proof",
      label: "Inspect proof gaps",
      detail: "No public proof receipt is attached to this marketplace card yet.",
      action: "Open proof",
      variant: "secondary",
    };
  }
  return {
    kind: "activity",
    label: "Review operational history",
    detail: "Open filtered activity to inspect proof receipts, installs, and recent runtime events.",
    href: buildRoute("/activity", {
      agent: agent.name,
      source: proof.latest ? "proof" : undefined,
    }),
    action: "Open activity",
    variant: "secondary",
  };
}

function filterAgents(
  agents: AgentListing[],
  proofsByAgent: Map<string, PublicAgentProofSummary>,
  query: MarketplaceQueryState,
) {
  const q = query.q.trim().toLowerCase();
  const focusedAgent = query.agent.trim().toLowerCase();
  const focusedSkill = query.skill.trim().toLowerCase();

  return agents
    .filter((agent) => {
      if (focusedAgent && agent.name.toLowerCase() !== focusedAgent) return false;
      if (
        focusedSkill &&
        !(agent.card?.skills || []).some(
          (skill) => skill.name.toLowerCase() === focusedSkill,
        )
      ) {
        return false;
      }
      if (!q) return true;
      return agentSearchText(agent, proofsByAgent.get(agent.name) || null).includes(q);
    })
    .filter((agent) => {
      const proof = proofsByAgent.get(agent.name) || null;
      if (query.proof === "verified") return proof?.badge === "verified";
      if (query.proof === "needs-proof") return proof?.badge !== "verified";
      return true;
    });
}

function agentSearchText(agent: AgentListing, proof: PublicAgentProofSummary | null) {
  return [
    agent.name,
    agent.description,
    agent.status,
    agent.card?.description ?? "",
    agent.card?.version ?? "",
    proof?.badge || "",
    proof?.latest?.skill_name || "",
    proof?.latest?.summary || "",
    ...(agent.card?.runtime?.tools_used || []),
    ...(agent.card?.skills || []).flatMap((skill) => [
      skill.name,
      skill.description,
      ...(skill.tags || []),
    ]),
  ]
    .join(" ")
    .toLowerCase();
}

function requiredSetupFields(agent: AgentListing | null): ConsumerSetupField[] {
  return (agent?.card?.consumer_setup?.fields || []).filter((field) => field.required);
}

function initialSetupValues(status: ConsumerSetupStatus): Record<string, unknown> {
  const configured = new Set(
    status.values.filter((value) => value.configured).map((value) => value.name),
  );
  const out: Record<string, unknown> = {};
  for (const field of status.declaration.fields) {
    if (!configured.has(field.name)) out[field.name] = "";
  }
  return out;
}

function payloadSetupValues(
  fields: ConsumerSetupField[],
  values: Record<string, unknown>,
): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const field of fields) {
    const value = values[field.name];
    if (!fieldValuePresent(value)) continue;
    out[field.name] = value;
  }
  return out;
}

function fieldValuePresent(value: unknown) {
  return value !== undefined && value !== null && value !== "";
}

function summarizeAgents(
  agents: AgentListing[],
  proofsByAgent: Map<string, PublicAgentProofSummary>,
) {
  return agents.reduce(
    (acc, agent) => {
      const proof = proofsByAgent.get(agent.name) || null;
      acc.total += 1;
      if (agent.status === "running" || agent.status === "ready") acc.running += 1;
      if (proof?.badge === "verified") acc.verified += 1;
      if (requiredSetupFields(agent).length > 0) acc.setupRequired += 1;
      return acc;
    },
    { total: 0, running: 0, verified: 0, setupRequired: 0 },
  );
}

function agentRank(agent: AgentListing, proof: PublicAgentProofSummary | null) {
  let score = 0;
  if (agent.status === "running" || agent.status === "ready") score += 40;
  if (proof?.badge === "verified") score += 30;
  if (proof?.latest?.status === "passed") score += 15;
  if ((agent.card?.skills || []).length > 0) score += 10;
  return score;
}

function formatProvisioning(provisioning: RuntimeProvisioning) {
  if (provisioning === "platform_or_caller_provided") return "your key or platform";
  if (provisioning === "caller_provided") return "your key";
  if (provisioning === "agent_byok") return "author";
  return "platform";
}

function uniqueStrings(values: string[]) {
  return Array.from(new Set(values.filter(Boolean)));
}

function readMarketplaceQueryState(search: string): MarketplaceQueryState {
  const params = new URLSearchParams(search);
  return {
    q: params.get("q") || "",
    agent: params.get("agent") || "",
    skill: params.get("skill") || "",
    proof: parseProofFilter(params.get("proof")),
  };
}

function marketplaceQueryPath(state: MarketplaceQueryState, basePath = "/marketplace") {
  const params = new URLSearchParams();
  if (state.q.trim()) params.set("q", state.q.trim());
  if (state.agent.trim()) params.set("agent", state.agent.trim());
  if (state.skill.trim()) params.set("skill", state.skill.trim());
  if (state.proof !== "all") params.set("proof", state.proof);
  const query = params.toString();
  return query ? `${basePath}?${query}` : basePath;
}

function parseProofFilter(value: string | null): ProofFilter {
  return PROOF_FILTERS.some((filter) => filter.id === value)
    ? (value as ProofFilter)
    : "all";
}

function buildRoute(path: string, params: Record<string, string | undefined>) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value) search.set(key, value);
  });
  const qs = search.toString();
  return qs ? `${path}?${qs}` : path;
}

function proofEvidenceId(latest: PublicAgentProofSummary["latest"]) {
  const evidenceId = latest?.card_hash || latest?.head_sha || "";
  return evidenceId ? evidenceId.slice(0, 12) : "-";
}

function buildPublicAgentHref(agentName: string) {
  return publicAgentUrl(agentName);
}

function buildAgentOpenApiHref(agentName: string) {
  const path = `/v1/agents/${encodeURIComponent(agentName)}/api/openapi.json`;
  return new URL(path, window.location.origin).toString();
}

function messageFromError(ex: unknown) {
  return ex instanceof Error ? ex.message : String(ex);
}

function fmtMs(ms: number | null) {
  if (ms == null) return "-";
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
}

function fmtDate(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return date.toLocaleDateString();
}
