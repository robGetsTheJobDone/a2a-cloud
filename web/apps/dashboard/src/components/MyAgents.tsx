import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import {
  getMyAgent,
  listAgentProofs,
  listMyAgentSummaries,
  listSubagentRuns,
  streamAgentDeployment,
  upgradeAgentRuntime,
  type AgentDeployment,
  type MyAgentListing,
} from "../api";
import {
  decodeRouteSegment,
  normalizeAgentDetailSection,
  normalizePathname,
  type AgentDetailSection,
} from "../navigation";
import { summarizeAgentLifecycle } from "./AgentLifecycleDashboard";
import {
  AgentLifecycleLoadingState,
  FilteredAgentLifecycleEmptyState,
  NoAgentsYetState,
} from "./AgentLifecycleEmptyStates";
import {
  AgentLifecycleFilters,
  DEFAULT_AGENT_LIFECYCLE_FILTERS,
  type AgentLifecycleFilterValue,
} from "./AgentLifecycleFilters";
import { AgentFleetPosture } from "./AgentFleetPosture";
import {
  agentFiltersActive,
  agentStatusFromDeployment,
  canBulkUpgradeRuntime,
  filterMyAgents,
  groupProofsByAgent,
  groupRunsByAgent,
  isActiveDeploymentStatus,
  isTransientAgentStatus,
  latestProofsByAgent,
} from "./myAgentsFleet";
import {
  agentSearchWithoutRequestedAgent,
  requestedAgentNameFromSearch,
} from "./myAgentRouteState";
import { InlineAlert, ToolbarButton, ToolbarLink } from "./DashboardChrome";
import { DualPaneResourceBrowser } from "./ListDetailLayout";
import {
  DASHBOARD_SECTION_CACHE_KEYS,
  useDashboardSectionResource,
  useDashboardSectionState,
} from "./DashboardSectionCache";
import { RoutePageShell } from "./RoutePageShell";
import {
  agentSummaryFromListing,
  myAgentsImportRoute,
  myAgentsRequestedImportRoute,
  myAgentsRoute,
  normalizeAgentEvidenceView,
  normalizeAgentListing,
  normalizeAgentRunDetailView,
  normalizeAgentSummary,
  shouldApplyDeploymentUpdate,
  type AgentIndexAgent,
  type MyAgentsResource,
} from "./agents/agentTypes";
import { MyAgentCard } from "./agents/AgentDetail";
import {
  AgentDetailNav,
  AgentDetailRouteState,
  AgentIndexRow,
} from "./agents/AgentFleet";
import { ImportAgentPage } from "./agents/AgentImport";

export { AgentDetailRouteState };

export function MyAgents() {
  const location = useLocation();
  const navigate = useNavigate();
  const importRoute = normalizePathname(location.pathname) === myAgentsImportRoute();
  const {
    agentName: encodedAgentName,
    section: routeSection,
    grantId: encodedGrantId,
    runView: encodedRunView,
    callId: encodedCallId,
    proofId: encodedProofId,
    domainHostname: encodedDomainHostname,
    evidenceView: encodedEvidenceView,
  } = useParams<{
    agentName?: string;
    section?: string;
    grantId?: string;
    runView?: string;
    callId?: string;
    proofId?: string;
    domainHostname?: string;
    evidenceView?: string;
  }>();
  const routeAgentName = importRoute ? null : decodeRouteSegment(encodedAgentName);
  const requestedImportAgentName = importRoute
    ? requestedAgentNameFromSearch(location.search)
    : null;
  const routeDetailSection: AgentDetailSection =
    encodedGrantId
      ? "runs"
      : encodedCallId
        ? "insights"
        : encodedProofId
          ? "proofs"
          : encodedDomainHostname
            ? "domains"
            : encodedEvidenceView
              ? "evidence"
              : routeSection === "calls"
                ? "insights"
                : normalizeAgentDetailSection(routeSection);
  const routeGrantId = decodeRouteSegment(encodedGrantId);
  const routeRunView = normalizeAgentRunDetailView(
    decodeRouteSegment(encodedRunView),
  );
  const routeCallId = decodeRouteSegment(encodedCallId);
  const routeProofId = decodeRouteSegment(encodedProofId);
  const routeDomainHostname = decodeRouteSegment(encodedDomainHostname);
  const routeEvidenceView = normalizeAgentEvidenceView(
    decodeRouteSegment(encodedEvidenceView),
  );
  const loadMyAgentsResource = useCallback(async (): Promise<MyAgentsResource> => {
    const [nextAgents, nextRuns, nextProofs] = await Promise.all([
      listMyAgentSummaries(),
      listSubagentRuns({ limit: 100 }),
      listAgentProofs({ limit: 200 }),
    ]);
    return {
      agents: nextAgents.map(normalizeAgentSummary),
      runs: nextRuns,
      proofs: nextProofs,
    };
  }, []);
  const {
    data: myAgentsData,
    error: loadErr,
    refresh: refreshMyAgents,
    setData: setMyAgentsData,
  } = useDashboardSectionResource(
    DASHBOARD_SECTION_CACHE_KEYS.agents.myAgents,
    loadMyAgentsResource,
  );
  const agents = myAgentsData?.agents ?? null;
  const runs = myAgentsData?.runs ?? [];
  const proofs = myAgentsData?.proofs ?? [];
  const [selectedAgent, setSelectedAgent] = useState<MyAgentListing | null>(null);
  const [selectedAgentLoading, setSelectedAgentLoading] = useState(false);
  const [selectedAgentErr, setSelectedAgentErr] = useState<string | null>(null);
  const [selectedAgentReloadToken, setSelectedAgentReloadToken] = useState(0);
  const [actionErr, setActionErr] = useState<string | null>(null);
  const err = actionErr ?? loadErr;
  const [bulkUpgradeBusy, setBulkUpgradeBusy] = useState(false);
  const [bulkUpgradeResult, setBulkUpgradeResult] = useState<string | null>(null);
  const [bulkUpgradeErr, setBulkUpgradeErr] = useState<string | null>(null);
  const [filters, setFilters] = useDashboardSectionState<AgentLifecycleFilterValue>(
    DASHBOARD_SECTION_CACHE_KEYS.agents.myAgentsFilters,
    DEFAULT_AGENT_LIFECYCLE_FILTERS,
  );
  const [selectedAgentName, setSelectedAgentName] = useDashboardSectionState<string | null>(
    DASHBOARD_SECTION_CACHE_KEYS.agents.myAgentsSelectedName,
    () => routeAgentName,
  );
  const [detailSection, setDetailSection] =
    useState<AgentDetailSection>(() => routeDetailSection);
  const updateAgent = useCallback((next: MyAgentListing) => {
    const normalized = normalizeAgentListing(next);
    setSelectedAgent((current) => (current?.id === normalized.id ? normalized : current));
    setMyAgentsData((current) =>
      current
        ? {
            ...current,
            agents: current.agents.map((agent) =>
              agent.id === normalized.id ? agentSummaryFromListing(normalized) : agent,
            ),
          }
        : current,
    );
  }, [setMyAgentsData]);
  const removeAgent = useCallback((name: string) => {
    setMyAgentsData((current) =>
      current
        ? {
            agents: current.agents.filter((agent) => agent.name !== name),
            runs: current.runs.filter((run) => run.agent_name !== name),
            proofs: current.proofs.filter((proof) => proof.agent_name !== name),
          }
        : current,
    );
    setSelectedAgent((current) => (current?.name === name ? null : current));
  }, [setMyAgentsData]);
  const mergeDeployment = useCallback((agentName: string, deployment: AgentDeployment) => {
    setSelectedAgent((current) =>
      current?.name === agentName &&
      shouldApplyDeploymentUpdate(current.latest_deployment, deployment)
        ? {
            ...current,
            status: agentStatusFromDeployment(current.status, deployment.status),
            latest_deployment: deployment,
          }
        : current,
    );
    setMyAgentsData((current) =>
      current
        ? {
            ...current,
            agents: current.agents.map((agent) =>
              agent.name === agentName &&
              shouldApplyDeploymentUpdate(agent.latest_deployment, deployment)
                ? {
                    ...agent,
                    status: agentStatusFromDeployment(agent.status, deployment.status),
                    latest_deployment: deployment,
                  }
                : agent,
            ),
          }
        : current,
    );
  }, [setMyAgentsData]);

  const refresh = useCallback(async () => {
    setActionErr(null);
    try {
      const next = await refreshMyAgents();
      const normalizedAgents = next?.agents ?? [];
      setSelectedAgent((current) => {
        if (!current) return current;
        const summary = normalizedAgents.find((agent) => agent.id === current.id);
        if (!summary) return current;
        return {
          ...current,
          description: summary.description,
          version: summary.version,
          public: summary.public,
          status: summary.status,
          url: summary.url,
          runtime_upgrade: summary.runtime_upgrade,
          latest_deployment: summary.latest_deployment,
        };
      });
    } catch {
      // The section cache stores and exposes the load error.
    }
  }, [refreshMyAgents]);

  // Mandate B/E: hydrate selection from the deep-link ONCE on mount so existing
  // /my-agents/:agentName(/:section) URLs still resolve, then keep selection in
  // local state. In-place selection never rewrites the route, so this effect
  // must not clobber later state — hence the mount guard.
  const hydratedRef = useRef(false);
  useEffect(() => {
    if (hydratedRef.current) return;
    hydratedRef.current = true;
    if (routeAgentName) {
      setSelectedAgentName(routeAgentName);
      setDetailSection(routeDetailSection);
    }
  }, [routeAgentName, routeDetailSection, setSelectedAgentName]);

  useEffect(() => {
    if (!selectedAgentName) {
      setSelectedAgent(null);
      setSelectedAgentErr(null);
      setSelectedAgentLoading(false);
      return;
    }
    let cancelled = false;
    setSelectedAgentLoading(true);
    setSelectedAgentErr(null);
    void (async () => {
      try {
        const next = await getMyAgent(selectedAgentName);
        if (cancelled) return;
        const normalized = normalizeAgentListing(next);
        setSelectedAgent(normalized);
        setMyAgentsData((current) =>
          current
            ? {
                ...current,
                agents: current.agents.some((agent) => agent.id === normalized.id)
                  ? current.agents.map((agent) =>
                      agent.id === normalized.id
                        ? agentSummaryFromListing(normalized)
                        : agent,
                    )
                  : [agentSummaryFromListing(normalized), ...current.agents],
              }
            : current,
        );
      } catch (ex) {
        if (cancelled) return;
        setSelectedAgent(null);
        setSelectedAgentErr(ex instanceof Error ? ex.message : String(ex));
      } finally {
        if (!cancelled) setSelectedAgentLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selectedAgentName, selectedAgentReloadToken, setMyAgentsData]);

  const hasActiveDeploy = (agents || []).some(
    (agent) =>
      isActiveDeploymentStatus(agent.latest_deployment?.status) ||
      isTransientAgentStatus(agent.status),
  );

  useEffect(() => {
    if (!hasActiveDeploy) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "hidden") return;
      refresh();
    }, 5000);
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") refresh();
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [hasActiveDeploy, refresh]);

  const activeDeploymentKeys = useMemo(
    () =>
      JSON.stringify(
        (agents || [])
          .map((agent) => {
            const deployment = agent.latest_deployment;
            return deployment && isActiveDeploymentStatus(deployment.status)
              ? { name: agent.name, deployId: deployment.deploy_id }
              : null;
          })
          .filter(Boolean),
      ),
    [agents],
  );

  useEffect(() => {
    const active = JSON.parse(activeDeploymentKeys) as Array<{
      name: string;
      deployId: string;
    }>;
    if (active.length === 0) return;

    const controllers = active.map((deployment) => {
      const controller = new AbortController();
      void (async () => {
        try {
          for await (const event of streamAgentDeployment(
            deployment.name,
            deployment.deployId,
            controller.signal,
          )) {
            if (controller.signal.aborted) break;
            if ((event.type === "snapshot" || event.type === "done") && event.deployment) {
              mergeDeployment(deployment.name, event.deployment);
            }
          }
        } catch {
          // The 5s list polling above remains the fallback when streaming fails.
        }
      })();
      return controller;
    });

    return () => controllers.forEach((controller) => controller.abort());
  }, [activeDeploymentKeys, mergeDeployment]);

  const filtered = useMemo(
    () => filterMyAgents(agents || [], filters, proofs, runs),
    [agents, filters, proofs, runs],
  );
  const lifecycleSummary = useMemo(
    () => summarizeAgentLifecycle(agents || [], runs, proofs),
    [agents, runs, proofs],
  );
  const hasActiveFilters = useMemo(() => agentFiltersActive(filters), [filters]);
  const bulkUpgradeCandidates = useMemo(
    () => (agents || []).filter(canBulkUpgradeRuntime),
    [agents],
  );
  const latestProofs = useMemo(() => latestProofsByAgent(proofs), [proofs]);
  const runsByAgent = useMemo(() => groupRunsByAgent(runs), [runs]);
  const proofsByAgent = useMemo(() => groupProofsByAgent(proofs), [proofs]);
  const selectedAgentSummary =
    selectedAgentName && agents
      ? agents.find((agent) => agent.name === selectedAgentName) || null
      : null;
  const detailHeaderAgent = selectedAgent
    ? agentSummaryFromListing(selectedAgent)
    : selectedAgentSummary;

  const selectAgent = useCallback(
    (name: string) => {
      setSelectedAgentName(name);
      setSelectedAgentErr(null);
      setDetailSection("overview");
    },
    [setSelectedAgentName],
  );

  const closeAgent = useCallback(() => {
    setSelectedAgentName(null);
    setSelectedAgent(null);
    setSelectedAgentErr(null);
    setDetailSection("overview");
  }, [setSelectedAgentName]);

  const clearFleetFilters = useCallback(() => {
    setFilters({ ...DEFAULT_AGENT_LIFECYCLE_FILTERS });
  }, [setFilters]);

  const showFailedAgents = useCallback(() => {
    closeAgent();
    setFilters({
      ...DEFAULT_AGENT_LIFECYCLE_FILTERS,
      status: "failed",
      sort: "status",
    });
  }, [closeAgent, setFilters]);

  const showDeployingAgents = useCallback(() => {
    closeAgent();
    setFilters({
      ...DEFAULT_AGENT_LIFECYCLE_FILTERS,
      status: "deploying",
      sort: "status",
    });
  }, [closeAgent, setFilters]);

  const showProofGaps = useCallback(() => {
    closeAgent();
    setFilters({
      ...DEFAULT_AGENT_LIFECYCLE_FILTERS,
      proof:
        lifecycleSummary.proofCoverage.degraded > 0
          ? "degraded"
          : "unverified",
      sort: "proof",
    });
  }, [closeAgent, lifecycleSummary.proofCoverage.degraded, setFilters]);

  const showRuntimeUpdates = useCallback(() => {
    closeAgent();
    setFilters({
      ...DEFAULT_AGENT_LIFECYCLE_FILTERS,
      runtimeUpdate: "update-available",
      sort: "runtime-update",
    });
  }, [closeAgent, setFilters]);

  const changeDetailSection = useCallback(
    (section: AgentDetailSection) => {
      if (!selectedAgentName) return;
      setDetailSection(section);
    },
    [selectedAgentName],
  );

  async function upgradeAllAgentRuntimes() {
    if (bulkUpgradeBusy || !agents || bulkUpgradeCandidates.length === 0) return;
    setBulkUpgradeBusy(true);
    setBulkUpgradeErr(null);
    setBulkUpgradeResult(null);
    const queued: string[] = [];
    const failures: string[] = [];
    try {
      for (const agent of bulkUpgradeCandidates) {
        try {
          const next = await upgradeAgentRuntime(agent.name);
          queued.push(next.name);
          updateAgent(next);
        } catch (ex) {
          failures.push(
            `${agent.name}: ${ex instanceof Error ? ex.message : String(ex)}`,
          );
        }
      }
      await refresh();
      const skipped = Math.max(0, agents.length - bulkUpgradeCandidates.length);
      setBulkUpgradeResult(
        `Queued ${queued.length} a2a-pack redeploy${queued.length === 1 ? "" : "s"}. ` +
          `Skipped ${skipped} already current, external, or active agent${skipped === 1 ? "" : "s"}.`,
      );
      setBulkUpgradeErr(failures.length > 0 ? failures.join(" | ") : null);
    } finally {
      setBulkUpgradeBusy(false);
    }
  }

  const fleetDetail = selectedAgentName ? (
    selectedAgent ? (
      <div className="flex flex-col gap-4 p-4">
        {detailHeaderAgent && (
          <AgentDetailNav
            agent={detailHeaderAgent}
            latestProof={latestProofs.get(selectedAgentName) || null}
            activeSection={detailSection}
            onBack={closeAgent}
            onSectionChange={changeDetailSection}
          />
        )}
        <MyAgentCard
          agent={selectedAgent}
          activeSection={detailSection}
          selectedRunId={routeGrantId}
          selectedRunView={routeRunView}
          selectedCallId={routeCallId}
          selectedProofId={routeProofId}
          selectedDomainHostname={routeDomainHostname}
          evidenceView={routeEvidenceView}
          onSectionChange={changeDetailSection}
          runs={runsByAgent.get(selectedAgent.name) || []}
          proofs={proofsByAgent.get(selectedAgent.name) || []}
          onProofCreated={(proof) =>
            setMyAgentsData((current) =>
              current ? { ...current, proofs: [proof, ...current.proofs] } : current,
            )
          }
          onAgentUpdated={updateAgent}
          onAgentDeleted={(name) => {
            removeAgent(name);
            closeAgent();
          }}
          onAgentRefreshed={refresh}
        />
      </div>
    ) : (
      <div className="p-4">
        <AgentDetailRouteState
          agentName={selectedAgentName}
          loading={selectedAgentLoading}
          error={selectedAgentErr}
          onBack={closeAgent}
          onRetry={() => setSelectedAgentReloadToken((token) => token + 1)}
          importHref={myAgentsRequestedImportRoute(
            location.search,
            selectedAgentName,
          )}
          agents={agents}
          search={location.search}
        />
      </div>
    )
  ) : null;

  // Keep the selected agent visible (and its detail rendered) even when active
  // filters would exclude it, so in-place selection and deep-link hydration
  // always resolve to a detail pane (mandate B/C).
  const browserItems = useMemo<AgentIndexAgent[]>(() => {
    if (!selectedAgentName) return filtered;
    if (filtered.some((agent) => agent.name === selectedAgentName)) return filtered;
    const pinned: AgentIndexAgent =
      selectedAgentSummary ||
      (selectedAgent ? agentSummaryFromListing(selectedAgent) : null) ||
      // Deep-link to an agent absent from summaries: a placeholder row keeps the
      // detail pane mounted so AgentDetailRouteState can recover the route.
      {
        id: -1,
        name: selectedAgentName,
        description: "",
        version: "",
        public: false,
        status: selectedAgentErr ? "missing" : "loading",
        url: null,
        created_at: new Date(0).toISOString(),
        skill_count: 0,
        runtime_upgrade: {
          package: "a2a-pack",
          current_version: null,
          latest_version: "",
          update_available: false,
          can_redeploy: false,
          message: "",
        },
        latest_deployment: null,
      };
    return [pinned, ...filtered];
  }, [
    filtered,
    selectedAgent,
    selectedAgentErr,
    selectedAgentName,
    selectedAgentSummary,
  ]);

  const fleetEmpty =
    agents === null ? (
      <AgentLifecycleLoadingState />
    ) : agents.length === 0 ? (
      <NoAgentsYetState size="comfortable" />
    ) : (
      <FilteredAgentLifecycleEmptyState size="comfortable" />
    );

  return (
    <RoutePageShell
      routeId="my-agents"
      data-onboarding-target="my-agents-page"
      actions={
        importRoute ? (
          <ToolbarLink href={myAgentsRoute(null, "overview", location.search)}>
            All agents
          </ToolbarLink>
        ) : (
          <div className="flex w-full flex-col gap-2 sm:w-auto sm:flex-row sm:items-center">
            <ToolbarLink
              data-onboarding-target="my-agents-import"
              href={myAgentsImportRoute(location.search)}
              variant="primary"
              className="w-full sm:w-auto"
            >
              bring A2A agent
            </ToolbarLink>
            <ToolbarButton
              onClick={upgradeAllAgentRuntimes}
              disabled={
                bulkUpgradeBusy ||
                agents === null ||
                bulkUpgradeCandidates.length === 0
              }
              size="xs"
              className="w-full border-signal-authority/45 bg-transparent text-signal-authority hover:border-signal-authority/45 hover:bg-signal-authority/12 hover:text-signal-authority sm:w-auto"
            >
              {bulkUpgradeBusy
                ? "queueing..."
                : bulkUpgradeCandidates.length > 0
                  ? `update ${bulkUpgradeCandidates.length} runtime${bulkUpgradeCandidates.length === 1 ? "" : "s"}`
                  : "a2a-pack up to date"}
            </ToolbarButton>
          </div>
        )
      }
    >
      {bulkUpgradeResult && (
        <InlineAlert tone="amber">{bulkUpgradeResult}</InlineAlert>
      )}

      {bulkUpgradeErr && (
        <InlineAlert tone="red">{bulkUpgradeErr}</InlineAlert>
      )}

      {importRoute ? (
        <ImportAgentPage
          agents={agents || []}
          requestedAgentName={requestedImportAgentName}
          onCancel={() =>
            navigate(
              myAgentsRoute(
                null,
                "overview",
                agentSearchWithoutRequestedAgent(location.search),
              ),
            )
          }
          onImported={async () => {
            await refresh();
            navigate(
              myAgentsRoute(
                null,
                "overview",
                agentSearchWithoutRequestedAgent(location.search),
              ),
            );
          }}
        />
      ) : (
        <>
          <AgentFleetPosture
            search={location.search}
            agents={agents}
            filtered={filtered}
            summary={lifecycleSummary}
            filters={filters}
            hasActiveFilters={hasActiveFilters}
            selectedAgentName={selectedAgentName}
            selectedAgent={detailHeaderAgent}
            bulkUpgradeCount={bulkUpgradeCandidates.length}
            bulkUpgradeBusy={bulkUpgradeBusy}
            onClearFilters={clearFleetFilters}
            onShowFailed={showFailedAgents}
            onShowDeploying={showDeployingAgents}
            onShowProofGaps={showProofGaps}
            onShowRuntimeUpdates={showRuntimeUpdates}
            onUpgradeAll={upgradeAllAgentRuntimes}
            onSelectTopResult={(agent) => selectAgent(agent.name)}
          />

          {err && <InlineAlert tone="red">{err}</InlineAlert>}

          <div
            data-onboarding-target="my-agents-list"
            className="flex min-h-[60vh] flex-col overflow-hidden rounded-xl border border-runtime-line-soft/70"
          >
            {selectedAgentName ? (
              // An agent is selected: collapse into the 320px rail + detail view.
              // Deep-link hydration also lands here (selectedAgentName is set on
              // mount), so /my-agents/:agentName resolves straight to the detail
              // pane (mandate B/E).
              <DualPaneResourceBrowser<AgentIndexAgent>
                listWidth="320px"
                listLabel="Agent fleet"
                items={browserItems}
                selectedId={selectedAgentName}
                getId={(agent) => agent.name}
                onSelect={(id) => selectAgent(id)}
                toolbar={
                  <AgentLifecycleFilters
                    value={filters}
                    resultCount={filtered.length}
                    totalCount={agents?.length || 0}
                    disabled={agents === null}
                    onChange={setFilters}
                  />
                }
                renderRow={(agent, { selected, onSelect }) => (
                  <AgentIndexRow
                    agent={agent}
                    selected={selected}
                    onSelect={onSelect}
                    runCount={runsByAgent.get(agent.name)?.length || 0}
                    latestProof={latestProofs.get(agent.name) || null}
                  />
                )}
                renderDetail={() => fleetDetail}
                emptyState={fleetEmpty}
              />
            ) : (
              // Nothing selected: let the fleet fill the page as a responsive
              // multi-column grid. Filter state is lifted in MyAgents, so it
              // survives the swap into/out of the dual-pane layout.
              <section
                aria-label="Agent fleet"
                className="flex min-h-0 min-w-0 flex-1 flex-col"
              >
                <div className="sticky top-0 z-10 border-b border-runtime-line-soft/70 bg-runtime-bg/95 px-3 py-2 backdrop-blur">
                  <AgentLifecycleFilters
                    value={filters}
                    resultCount={filtered.length}
                    totalCount={agents?.length || 0}
                    disabled={agents === null}
                    onChange={setFilters}
                  />
                </div>
                {filtered.length === 0 ? (
                  <div className="p-4">{fleetEmpty}</div>
                ) : (
                  <ul
                    role="list"
                    className="grid grid-cols-1 gap-2 p-3 sm:grid-cols-2 xl:grid-cols-3"
                  >
                    {filtered.map((agent) => (
                      <li key={agent.name} className="min-w-0">
                        <AgentIndexRow
                          agent={agent}
                          selected={false}
                          onSelect={() => selectAgent(agent.name)}
                          runCount={runsByAgent.get(agent.name)?.length || 0}
                          latestProof={latestProofs.get(agent.name) || null}
                        />
                      </li>
                    ))}
                  </ul>
                )}
              </section>
            )}
          </div>
        </>
      )}
    </RoutePageShell>
  );
}
