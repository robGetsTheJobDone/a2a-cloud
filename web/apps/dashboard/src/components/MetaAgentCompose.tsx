import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  composeAgent,
  getAgentDeployment,
  listAgents,
  listMetaAgentRuns,
  listMyAgents,
  streamAgentDeployment,
  type AgentComposeResult,
  type AgentDeployment,
  type AgentListing,
  type AgentSkill,
  type MetaAgentRun,
  type MyAgentListing,
} from "../api";
import { AgentDeploymentTimeline } from "./AgentDeploymentTimeline";
import {
  CodeBlock,
  EmptyState,
  FormField,
  InlineAlert,
  LoadingState,
  SectionPanel,
  SegmentedControl,
  SelectableSurfaceButton,
  SummaryMetric,
  SummaryStrip,
  SurfacePanel,
  TextArea,
  TextInput,
  ToggleField,
  ToolbarButton,
  ToolbarLink,
} from "./DashboardChrome";
import { StateBadge, StatusBadge } from "./StatusPillAdapters";
import {
  DASHBOARD_SECTION_CACHE_KEYS,
  useDashboardSectionResource,
  useDashboardSectionState,
} from "./DashboardSectionCache";
import {
  COMPOSE_STEPS,
  decodeRouteSegment,
  normalizeComposeStepId,
  type ComposeStep,
  type ComposeStepId,
} from "../navigation";
import { RoutePageShell } from "./RoutePageShell";

type CandidateAgent = AgentListing & {
  owned: boolean;
  source: "owned" | "registry" | "public";
};

type ComposeDraft = {
  name: string;
  description: string;
  version: string;
  public: boolean;
  refreshExisting: boolean;
  goal: string;
  successCriteria: string;
  memoryNamespace: string;
  memoryTiers: Record<MemoryTier, boolean>;
  maxNodes: number;
  maxParallel: number;
  maxReplans: number;
};

type MemoryTier = "files" | "kv" | "vector";
type ComposeRunDetailSection = "overview" | "progress" | "plan" | "result";
type ComposeStepStatus = ComposeStep & { detail: string; complete: boolean };

const COMPOSE_RUN_DETAIL_SECTIONS: Array<{
  id: ComposeRunDetailSection;
  label: string;
  description: string;
}> = [
  {
    id: "overview",
    label: "Overview",
    description: "Run status, goal, success criteria, timestamps, and summary.",
  },
  {
    id: "progress",
    label: "Progress",
    description: "Latest progress entries emitted by the meta-agent while pursuing the goal.",
  },
  {
    id: "plan",
    label: "Plan",
    description: "Current planning graph, rounds, nodes, and execution limits.",
  },
  {
    id: "result",
    label: "Result",
    description: "Final summary, error state, and persisted run state.",
  },
];

function normalizeComposeRunDetailSection(
  value: string | null | undefined,
): ComposeRunDetailSection {
  return COMPOSE_RUN_DETAIL_SECTIONS.some((section) => section.id === value)
    ? (value as ComposeRunDetailSection)
    : "overview";
}

function composeRunRoute(
  agentName: string,
  runId: string,
  section: ComposeRunDetailSection = "overview",
) {
  const suffix = section === "overview" ? "" : `/${section}`;
  return `/compose/runs/${encodeURIComponent(agentName)}/${encodeURIComponent(runId)}${suffix}`;
}

function composeStepIdFromPath(path: string): ComposeStepId | null {
  return COMPOSE_STEPS.find((step) => step.path === path)?.id ?? null;
}

const EMPTY_SELECTED: Record<string, string[]> = {};
const AGENT_NAME_RE = /^[a-z][a-z0-9-]{1,62}$/;
const MEMORY_TIERS: { id: MemoryTier; label: string }[] = [
  { id: "files", label: "Files" },
  { id: "kv", label: "KV" },
  { id: "vector", label: "Vector" },
];

const INITIAL_DRAFT: ComposeDraft = {
  name: "",
  description: "Manifest-backed meta-agent composed in the dashboard.",
  version: "0.1.0",
  public: true,
  refreshExisting: false,
  goal: "",
  successCriteria: "",
  memoryNamespace: "meta",
  memoryTiers: { files: true, kv: true, vector: false },
  maxNodes: 8,
  maxParallel: 3,
  maxReplans: 1,
};

export function MetaAgentCompose() {
  const navigate = useNavigate();
  const {
    step: routeStep,
    agentName: encodedRouteAgentName,
    runId: encodedRouteRunId,
    runSection: encodedRouteRunSection,
  } = useParams<{
    step?: string;
    agentName?: string;
    runId?: string;
    runSection?: string;
  }>();
  const routeRunAgentName = decodeRouteSegment(encodedRouteAgentName);
  const routeStepFromUrl: ComposeStepId = routeRunAgentName
    ? "runs"
    : normalizeComposeStepId(routeStep);

  // The 7-step wizard is an inline stepper (mandates A/C): nav and form state
  // stay mounted, no per-step route remount. The route still hydrates the
  // active step + run selection on mount and stays shareable via next/prev.
  const [activeStep, setActiveStep] = useState<ComposeStepId>(routeStepFromUrl);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(() =>
    decodeRouteSegment(encodedRouteRunId),
  );
  const [activeRunSection, setActiveRunSection] = useState<ComposeRunDetailSection>(
    () => normalizeComposeRunDetailSection(decodeRouteSegment(encodedRouteRunSection)),
  );

  // Keep state aligned with deep links / browser nav without remounting.
  useEffect(() => {
    setActiveStep(routeStepFromUrl);
  }, [routeStepFromUrl]);
  useEffect(() => {
    setSelectedRunId(decodeRouteSegment(encodedRouteRunId));
  }, [encodedRouteRunId]);
  useEffect(() => {
    setActiveRunSection(
      normalizeComposeRunDetailSection(decodeRouteSegment(encodedRouteRunSection)),
    );
  }, [encodedRouteRunSection]);

  const loadCandidates = useCallback(async () => {
    const [registry, mine] = await Promise.all([
      listAgents(),
      listMyAgents(),
    ]);
    return mergeCandidates(registry, mine);
  }, []);
  const {
    data: agents,
    error: loadErr,
    refresh: refreshCandidates,
  } = useDashboardSectionResource(
    DASHBOARD_SECTION_CACHE_KEYS.agents.composeCandidates,
    loadCandidates,
  );
  const [actionErr, setActionErr] = useState<string | null>(null);
  const err = actionErr ?? loadErr;
  const [query, setQuery] = useDashboardSectionState(
    DASHBOARD_SECTION_CACHE_KEYS.agents.composeQuery,
    "",
  );
  const [selected, setSelected] = useDashboardSectionState<Record<string, string[]>>(
    DASHBOARD_SECTION_CACHE_KEYS.agents.composeSelected,
    EMPTY_SELECTED,
  );
  const [draft, setDraft] = useDashboardSectionState<ComposeDraft>(
    DASHBOARD_SECTION_CACHE_KEYS.agents.composeDraft,
    INITIAL_DRAFT,
  );
  const [submitting, setSubmitting] = useState(false);
  const [composeResult, setComposeResult] = useState<AgentComposeResult | null>(null);
  const [deployment, setDeployment] = useState<AgentDeployment | null>(null);
  const [runs, setRuns] = useState<MetaAgentRun[] | null>(null);

  const refreshAgents = useCallback(async () => {
    setActionErr(null);
    try {
      await refreshCandidates();
    } catch {
      // The section cache stores and exposes the load error.
    }
  }, [refreshCandidates]);

  const filteredAgents = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const list = agents || [];
    if (!needle) return list;
    return list.filter((agent) => agentSearchText(agent).includes(needle));
  }, [agents, query]);

  const selectedAgents = useMemo(() => {
    const byName = new Map((agents || []).map((agent) => [agent.name, agent]));
    return Object.entries(selected)
      .map(([name, skills]) => ({ agent: byName.get(name) || null, skills }))
      .filter((item): item is { agent: CandidateAgent; skills: string[] } =>
        Boolean(item.agent && item.skills.length > 0),
      )
      .sort((left, right) => left.agent.name.localeCompare(right.agent.name));
  }, [agents, selected]);

  const manifest = useMemo(
    () => buildManifest(draft, selectedAgents),
    [draft, selectedAgents],
  );
  const selectedSkillCount = selectedAgents.reduce(
    (total, item) => total + item.skills.length,
    0,
  );
  const validation = validateDraft(draft, selectedAgents);
  const canSubmit = validation.length === 0 && !submitting;
  const selectionReady = selectedSkillCount > 0;
  const identityReady = identityIsReady(draft);
  const controlsReady = controlsAreReady(draft);
  const composeSteps = COMPOSE_STEPS.map((step) => {
    if (step.id === "select") {
      return {
        ...step,
        detail: selectionReady
          ? `${filteredAgents.length} matching agents`
          : "Browse available tools",
        complete: selectionReady,
      };
    }
    if (step.id === "selection") {
      return {
        ...step,
        detail: selectionReady
          ? `${selectedAgents.length} agents, ${selectedSkillCount} tools`
          : "No tools selected",
        complete: selectionReady,
      };
    }
    if (step.id === "identity") {
      return {
        ...step,
        detail: identityReady ? draft.name.trim() : "Name and publish settings",
        complete: identityReady,
      };
    }
    if (step.id === "controls") {
      return {
        ...step,
        detail: controlsReady
          ? `${enabledMemoryTiers(draft).length} memory tiers, ${draft.maxNodes} nodes`
          : "Goal, memory, and limits",
        complete: controlsReady,
      };
    }
    if (step.id === "manifest") {
      return {
        ...step,
        detail: canSubmit ? "Ready to deploy" : "Resolve validation",
        complete: Boolean(composeResult),
      };
    }
    if (step.id === "deployment") {
      return {
        ...step,
        detail: deployment?.status || (composeResult ? "Deployment queued" : "Deploy manifest first"),
        complete: Boolean(deployment && ["ready", "deployed", "complete"].includes(deployment.status)),
      };
    }
    return {
      ...step,
      detail: composeResult
        ? `${runs?.length ?? 0} recent runs`
        : "Deploy manifest first",
      complete: Boolean(runs && runs.length > 0),
    };
  });

  function updateDraft(patch: Partial<ComposeDraft>) {
    setDraft((prev) => ({ ...prev, ...patch }));
  }

  function toggleMemoryTier(tier: MemoryTier) {
    setDraft((prev) => ({
      ...prev,
      memoryTiers: {
        ...prev.memoryTiers,
        [tier]: !prev.memoryTiers[tier],
      },
    }));
  }

  function toggleAgent(agent: CandidateAgent) {
    const skills = skillList(agent).map((skill) => skill.name);
    if (skills.length === 0) return;
    setSelected((prev) => {
      const next = { ...prev };
      if (next[agent.name]?.length) {
        delete next[agent.name];
      } else {
        next[agent.name] = skills;
      }
      return next;
    });
  }

  function toggleSkill(agent: CandidateAgent, skillName: string) {
    setSelected((prev) => {
      const current = new Set(prev[agent.name] || []);
      if (current.has(skillName)) {
        current.delete(skillName);
      } else {
        current.add(skillName);
      }
      const next = { ...prev };
      const values = Array.from(current).sort();
      if (values.length) {
        next[agent.name] = values;
      } else {
        delete next[agent.name];
      }
      return next;
    });
  }

  async function submitCompose() {
    if (!canSubmit) return;
    setSubmitting(true);
    setActionErr(null);
    setComposeResult(null);
    setDeployment(null);
    setRuns(null);
    try {
      const result = await composeAgent({
        name: draft.name.trim(),
        description: draft.description.trim(),
        version: draft.version.trim() || "0.1.0",
        public: draft.public,
        manifest,
        refresh_existing: draft.refreshExisting,
      });
      setComposeResult(result);
    } catch (ex) {
      setActionErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setSubmitting(false);
    }
  }

  useEffect(() => {
    if (!composeResult?.deployment_id) return undefined;
    const controller = new AbortController();
    const { name, deployment_id: deployId } = composeResult;

    void (async () => {
      try {
        const current = await getAgentDeployment(name, deployId);
        if (!controller.signal.aborted) setDeployment(current);
      } catch {
        // The stream or periodic My agents refresh remains the fallback.
      }
      try {
        for await (const event of streamAgentDeployment(
          name,
          deployId,
          controller.signal,
        )) {
          if (controller.signal.aborted) break;
          if ((event.type === "snapshot" || event.type === "done") && event.deployment) {
            setDeployment(event.deployment);
          } else if (event.type === "event" && event.event) {
            setDeployment((prev) =>
              prev
                ? {
                    ...prev,
                    events: [...prev.events, event.event],
                    updated_at: event.event.created_at,
                  }
                : prev,
            );
          }
        }
      } catch {
        // Active deployments are still visible through My agents and a refresh.
      }
    })();

    return () => controller.abort();
  }, [composeResult]);

  const runsAgentName = composeResult?.name || routeRunAgentName;

  // Stepper navigation: update local state immediately (no remount) and mirror
  // the active step into the URL so the wizard stays shareable.
  const goToStep = useCallback(
    (stepId: ComposeStepId) => {
      setActiveStep(stepId);
      const step = COMPOSE_STEPS.find((item) => item.id === stepId);
      navigate(step?.path ?? "/compose/select");
    },
    [navigate],
  );

  const activeStepIndex = COMPOSE_STEPS.findIndex((step) => step.id === activeStep);
  const prevStep = activeStepIndex > 0 ? COMPOSE_STEPS[activeStepIndex - 1] : null;
  const nextStep =
    activeStepIndex >= 0 && activeStepIndex < COMPOSE_STEPS.length - 1
      ? COMPOSE_STEPS[activeStepIndex + 1]
      : null;

  const selectRun = useCallback(
    (agentName: string, runId: string, section: ComposeRunDetailSection = "overview") => {
      setSelectedRunId(runId);
      setActiveRunSection(section);
      navigate(composeRunRoute(agentName, runId, section));
    },
    [navigate],
  );

  useEffect(() => {
    if (!runsAgentName) {
      setRuns(null);
      return undefined;
    }
    const agentName = runsAgentName;
    let cancelled = false;
    setRuns(null);
    async function loadRuns() {
      try {
        const next = await listMetaAgentRuns(agentName, { limit: 20 });
        if (!cancelled) setRuns(next);
      } catch {
        if (!cancelled) setRuns([]);
      }
    }
    void loadRuns();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "hidden") return;
      void loadRuns();
    }, 8000);
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") void loadRuns();
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [runsAgentName]);

  return (
    <RoutePageShell
      routeId="compose"
      actions={
        <ToolbarButton
          onClick={refreshAgents}
          disabled={agents === null}
          variant="secondary"
          size="md"
        >
          Refresh
        </ToolbarButton>
      }
    >
      {err && <InlineAlert tone="red">{err}</InlineAlert>}
      {composeResult && (
        <InlineAlert tone="emerald">
          {composeResult.name} queued from manifest at {shortSha(composeResult.head_sha)}.
        </InlineAlert>
      )}

      <ComposeStepper
        activeStep={activeStep}
        steps={composeSteps}
        onSelectStep={goToStep}
      />

      <ComposeReadinessPanel
        activeStep={activeStep}
        steps={composeSteps}
        agents={agents}
        selectedAgentCount={selectedAgents.length}
        selectedSkillCount={selectedSkillCount}
        enabledMemoryCount={enabledMemoryTiers(draft).length}
        maxNodes={draft.maxNodes}
        validation={validation}
        submitting={submitting}
        composeResult={composeResult}
        deployment={deployment}
        runs={runs}
        onGoToStep={goToStep}
      />

      <div className="grid gap-4">
        {activeStep === "select" && (
          <div data-onboarding-target="compose-picker">
            <SectionPanel
              title="Agent catalog"
              description="Owned agents and public marketplace agents with declared tools."
              actions={
                <FormField label={<span className="sr-only">Filter agents</span>} className="w-full sm:w-72">
                  <TextInput
                    type="search"
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                    placeholder="Filter by agent, tool, or tag"
                  />
                </FormField>
              }
            >
              {agents === null ? (
                <LoadingState label="Loading agents..." />
              ) : filteredAgents.length === 0 ? (
                <EmptyState
                  title="No agents match"
                  description="Try a broader agent name, tool, or tag."
                />
              ) : (
                <div className="grid gap-3 lg:grid-cols-2">
                  {filteredAgents.map((agent) => (
                    <PickerAgentCard
                      key={agent.name}
                      agent={agent}
                      selectedSkills={selected[agent.name] || []}
                      onToggleAgent={() => toggleAgent(agent)}
                      onToggleSkill={(skill) => toggleSkill(agent, skill)}
                    />
                  ))}
                </div>
              )}
            </SectionPanel>
          </div>
        )}

        {activeStep === "selection" && (
          <SectionPanel
            title="Selected tools"
            description="Review the exact sub-agent tools that will be exposed to the planner."
            actions={
              <ToolbarButton type="button" onClick={() => goToStep("select")}>
                Edit catalog
              </ToolbarButton>
            }
          >
            <SelectedAgentList items={selectedAgents} />
          </SectionPanel>
        )}

        {activeStep === "identity" && (
          <SectionPanel
            title="Agent identity"
            description="These values become the composed agent card and deployment target."
          >
            <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(260px,360px)]">
              <div className="space-y-4">
                <FormField label="Name">
                  <TextInput
                    value={draft.name}
                    onChange={(event) => updateDraft({ name: event.target.value })}
                    placeholder="launch-report-meta"
                  />
                </FormField>
                <FormField label="Description">
                  <TextInput
                    value={draft.description}
                    onChange={(event) => updateDraft({ description: event.target.value })}
                  />
                </FormField>
                <FormField label="Version">
                  <TextInput
                    value={draft.version}
                    onChange={(event) => updateDraft({ version: event.target.value })}
                  />
                </FormField>
              </div>

              <SurfacePanel as="div" className="space-y-3 bg-runtime-bg p-4">
                <div>
                  <div className="text-xs uppercase text-ink-muted">
                    Publishing
                  </div>
                  <p className="mt-1 text-sm leading-relaxed text-ink-muted">
                    Visibility and refresh behavior apply when the manifest is deployed.
                  </p>
                </div>
                <ToggleField
                  label={draft.public ? "Public" : "Private"}
                  checked={draft.public}
                  onCheckedChange={(value) => updateDraft({ public: value })}
                  className="bg-runtime-panel/70 px-3 py-2 text-xs"
                />
                <ToggleField
                  label="Refresh existing"
                  checked={draft.refreshExisting}
                  onCheckedChange={(value) => updateDraft({ refreshExisting: value })}
                  className="bg-runtime-panel/70 px-3 py-2 text-xs"
                />
              </SurfacePanel>
            </div>
          </SectionPanel>
        )}

        {activeStep === "controls" && (
          <div data-onboarding-target="compose-goal">
            <SectionPanel
              title="Goal and controls"
              description="The generated meta-agent uses these as its planning and memory contract."
            >
              <div className="grid gap-4 lg:grid-cols-2">
                <FormField label="Goal">
                  <TextArea
                    value={draft.goal}
                    onChange={(event) => updateDraft({ goal: event.target.value })}
                    rows={5}
                    placeholder="Produce a launch report with draft, chart, and final review."
                  />
                </FormField>
                <FormField label="Success criteria">
                  <TextArea
                    value={draft.successCriteria}
                    onChange={(event) =>
                      updateDraft({ successCriteria: event.target.value })
                    }
                    rows={5}
                    placeholder={"Draft complete\nChart rendered\nReview passed"}
                  />
                </FormField>
              </div>

              <div className="mt-4 grid gap-4 border-t border-runtime-line-soft/60 pt-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
                <SurfacePanel as="fieldset" className="bg-runtime-bg p-4">
                  <legend className="text-[10px] uppercase text-ink-faint">
                    Memory
                  </legend>
                  <div className="mt-2 flex flex-wrap gap-2">
                    {MEMORY_TIERS.map((tier) => (
                      <ToggleField
                        key={tier.id}
                        label={tier.label}
                        checked={draft.memoryTiers[tier.id]}
                        onCheckedChange={() => toggleMemoryTier(tier.id)}
                        className="bg-runtime-panel/70 px-3 py-2 text-xs"
                      />
                    ))}
                  </div>
                  <FormField label="Memory namespace" className="mt-4">
                    <TextInput
                      value={draft.memoryNamespace}
                      onChange={(event) =>
                        updateDraft({ memoryNamespace: event.target.value })
                      }
                    />
                  </FormField>
                </SurfacePanel>

                <SurfacePanel as="div" className="bg-runtime-bg p-4">
                  <div className="text-[10px] uppercase text-ink-faint">
                    Execution limits
                  </div>
                  <div className="mt-3 grid gap-3 sm:grid-cols-3">
                    <NumberField
                      label="Max nodes"
                      value={draft.maxNodes}
                      min={1}
                      max={32}
                      onChange={(value) => updateDraft({ maxNodes: value })}
                    />
                    <NumberField
                      label="Parallel"
                      value={draft.maxParallel}
                      min={1}
                      max={16}
                      onChange={(value) => updateDraft({ maxParallel: value })}
                    />
                    <NumberField
                      label="Replans"
                      value={draft.maxReplans}
                      min={0}
                      max={8}
                      onChange={(value) => updateDraft({ maxReplans: value })}
                    />
                  </div>
                </SurfacePanel>
              </div>
            </SectionPanel>
          </div>
        )}

        {activeStep === "manifest" && (
          <div data-onboarding-target="compose-manifest">
            <SectionPanel
              title="Manifest"
              description="Review the generated contract before deploying the composed agent."
              actions={
                <ToolbarButton
                  onClick={submitCompose}
                  disabled={!canSubmit}
                  variant="primary"
                  size="md"
                >
                  {submitting ? "Composing..." : "Deploy"}
                </ToolbarButton>
              }
            >
              {validation.length > 0 && (
                <div className="mb-3">
                  <InlineAlert tone="amber">
                    <ul className="space-y-1 text-xs">
                      {validation.map((item) => (
                        <li key={item}>{item}</li>
                      ))}
                    </ul>
                  </InlineAlert>
                </div>
              )}
              <SelectedAgentList items={selectedAgents} />
              <CodeBlock className="mt-4 max-h-[420px]">
                {JSON.stringify(manifest, null, 2)}
              </CodeBlock>
            </SectionPanel>
          </div>
        )}

        {activeStep === "deployment" && (
          composeResult ? (
            <AgentDeploymentTimeline
              deployment={deployment}
              agent={{
                name: composeResult.name,
                repo_url: composeResult.repo_url,
                url: composeResult.expected_url,
              }}
              title="Compose deployment"
              emptyMessage={
                composeResult.deployment_id
                  ? "Waiting for the deployment stream."
                  : "No compose deployment has been started."
              }
            />
          ) : (
            <SectionPanel title="Deployment">
              <EmptyState
                title="No deployment yet"
                description="Review and deploy the manifest before watching deployment progress."
                action={
                  <ToolbarButton type="button" onClick={() => goToStep("manifest")}>
                    Open manifest
                  </ToolbarButton>
                }
              />
            </SectionPanel>
          )
        )}

        {activeStep === "runs" && (
          <MetaRunsPanel
            runs={runs}
            agentName={runsAgentName ?? null}
            selectedRunId={selectedRunId}
            activeSection={activeRunSection}
            onSelectRun={selectRun}
            onGoToStep={goToStep}
          />
        )}
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-runtime-line-soft/70 pt-4">
        <ToolbarButton
          type="button"
          onClick={() => prevStep && goToStep(prevStep.id)}
          disabled={!prevStep}
        >
          {prevStep ? `Back: ${prevStep.label}` : "Back"}
        </ToolbarButton>
        <span className="font-mono text-[11px] uppercase tracking-wide text-ink-faint">
          Step {Math.max(activeStepIndex, 0) + 1} of {COMPOSE_STEPS.length}
        </span>
        <ToolbarButton
          type="button"
          variant="primary"
          onClick={() => nextStep && goToStep(nextStep.id)}
          disabled={!nextStep}
        >
          {nextStep ? `Next: ${nextStep.label}` : "Done"}
        </ToolbarButton>
      </div>
    </RoutePageShell>
  );
}

function ComposeStepper({
  steps,
  activeStep,
  onSelectStep,
}: {
  steps: ComposeStepStatus[];
  activeStep: ComposeStep["id"];
  onSelectStep: (stepId: ComposeStepId) => void;
}) {
  return (
    <nav aria-label="Compose progress" className="grid gap-2 md:grid-cols-3">
      {steps.map((step, index) => {
        const current = step.id === activeStep;
        const done = step.complete;
        return (
          <SelectableSurfaceButton
            key={step.id}
            selected={current}
            onClick={() => onSelectStep(step.id)}
            aria-current={current ? "step" : undefined}
            className={
              "px-3 py-3 " +
              (current
                ? "border-signal-authority/45 bg-signal-authority/12"
                : done
                  ? "border-signal-live/45 bg-signal-live/12"
                  : "bg-runtime-bg/70")
            }
          >
            <div className="flex items-center gap-2">
              <span
                className={
                  "flex h-6 w-6 shrink-0 items-center justify-center rounded-full border text-xs font-semibold " +
                  (done
                    ? "border-signal-live/45 bg-signal-live/12 text-signal-live"
                    : current
                      ? "border-signal-authority/45 bg-signal-authority/12 text-signal-authority"
                      : "border-runtime-line-soft bg-runtime-panel text-ink-muted")
                }
              >
                {done ? "ok" : index + 1}
              </span>
              <div className="min-w-0">
                <div className="truncate text-sm font-semibold text-ink">
                  {step.label}
                </div>
                <div className="mt-0.5 truncate text-xs text-ink-muted">
                  {step.detail}
                </div>
              </div>
            </div>
          </SelectableSurfaceButton>
        );
      })}
    </nav>
  );
}

function ComposeReadinessPanel({
  activeStep,
  steps,
  agents,
  selectedAgentCount,
  selectedSkillCount,
  enabledMemoryCount,
  maxNodes,
  validation,
  submitting,
  composeResult,
  deployment,
  runs,
  onGoToStep,
}: {
  activeStep: ComposeStep["id"];
  steps: ComposeStepStatus[];
  agents: CandidateAgent[] | null;
  selectedAgentCount: number;
  selectedSkillCount: number;
  enabledMemoryCount: number;
  maxNodes: number;
  validation: string[];
  submitting: boolean;
  composeResult: AgentComposeResult | null;
  deployment: AgentDeployment | null;
  runs: MetaAgentRun[] | null;
  onGoToStep: (stepId: ComposeStepId) => void;
}) {
  const completedSteps = steps.filter((step) => step.complete).length;
  const currentStep = steps.find((step) => step.id === activeStep) || steps[0];
  const nextAction = composeNextAction({
    steps,
    validation,
    submitting,
    composeResult,
    deployment,
  });
  const nextActionStep = composeStepIdFromPath(nextAction.href);
  const tone = composeReadinessTone(validation, submitting, composeResult, deployment);
  const label = composeReadinessLabel(validation, submitting, composeResult, deployment);
  const detail = composeReadinessDetail(validation, submitting, composeResult, deployment);
  const activeRuns = runs ? summarizeMetaRuns(runs).active : 0;

  return (
    <SurfacePanel
      as="section"
      data-onboarding-target="compose-readiness"
      className="overflow-hidden bg-runtime-bg/80"
    >
      <div className="flex flex-col gap-4 border-b border-runtime-line-soft/70 p-4 sm:p-5 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge tone={tone} dot={tone === "emerald" || tone === "amber"}>
              {label}
            </StatusBadge>
            <span className="text-xs text-ink-muted">{detail}</span>
          </div>
          <h2 className="mt-3 text-lg font-semibold text-ink">
            Composition readiness
          </h2>
          <p className="mt-1 max-w-2xl text-sm leading-relaxed text-ink-muted">
            Selected skills, identity, controls, manifest readiness, deployment state, and recent goal runs.
          </p>
        </div>
        {nextActionStep ? (
          <ToolbarButton
            type="button"
            onClick={() => onGoToStep(nextActionStep)}
            variant={nextAction.variant}
            size="md"
          >
            {nextAction.action}
          </ToolbarButton>
        ) : (
          <ToolbarLink href={nextAction.href} variant={nextAction.variant} size="md">
            {nextAction.action}
          </ToolbarLink>
        )}
      </div>

      <div className="grid gap-4 p-4 sm:p-5">
        <SummaryStrip className="lg:grid-cols-5" aria-label="Compose readiness summary">
          <SummaryMetric
            label="steps"
            value={`${completedSteps}/${steps.length}`}
            detail={`${currentStep.label} active`}
          />
          <SummaryMetric
            label="catalog"
            value={agents ? agents.length.toLocaleString() : "loading"}
            detail="candidate agents"
          />
          <SummaryMetric
            label="tools"
            value={selectedSkillCount.toLocaleString()}
            detail={`${selectedAgentCount} selected agents`}
            tone={selectedSkillCount > 0 ? "emerald" : "amber"}
          />
          <SummaryMetric
            label="memory"
            value={enabledMemoryCount.toLocaleString()}
            detail={`${maxNodes} max nodes`}
            tone={enabledMemoryCount > 0 ? "emerald" : "amber"}
          />
          <SummaryMetric
            label="runs"
            value={runs ? runs.length.toLocaleString() : composeResult ? "loading" : "pending"}
            detail={activeRuns > 0 ? `${activeRuns} active` : "recent goals"}
          />
        </SummaryStrip>

        <div className="grid gap-4 border-t border-runtime-line-soft/70 pt-4 lg:grid-cols-[minmax(0,1fr)_minmax(260px,360px)]">
          <div className="min-w-0">
            <div className="text-[10px] font-medium uppercase tracking-wide text-ink-faint">
              Next best action
            </div>
            <div className="mt-1 text-sm font-medium text-ink">
              {nextAction.label}
            </div>
            <p className="mt-1 max-w-2xl text-xs leading-relaxed text-ink-muted">
              {nextAction.detail}
            </p>
          </div>
          <ol className="grid gap-2">
            {steps.map((step) => (
              <li
                key={step.id}
                className="grid grid-cols-[auto_minmax(0,1fr)] items-start gap-2 text-xs"
              >
                <StatusBadge
                  tone={
                    step.complete
                      ? "emerald"
                      : step.id === activeStep
                        ? "amber"
                        : "neutral"
                  }
                >
                  {step.complete ? "ok" : step.id === activeStep ? "now" : "todo"}
                </StatusBadge>
                <div className="min-w-0">
                  <div className="font-medium text-ink-soft">{step.label}</div>
                  <div className="mt-0.5 truncate text-ink-faint">{step.detail}</div>
                </div>
              </li>
            ))}
          </ol>
        </div>
      </div>
    </SurfacePanel>
  );
}

function PickerAgentCard({
  agent,
  selectedSkills,
  onToggleAgent,
  onToggleSkill,
}: {
  agent: CandidateAgent;
  selectedSkills: string[];
  onToggleAgent: () => void;
  onToggleSkill: (skill: string) => void;
}) {
  const skills = skillList(agent);
  const allSelected = skills.length > 0 && selectedSkills.length === skills.length;
  const someSelected = selectedSkills.length > 0;
  return (
    <SurfacePanel
      as="article"
      className={
        "bg-runtime-bg p-3 transition " +
        (someSelected
          ? "border-signal-authority/45 ring-1 ring-signal-authority/20"
          : "hover:border-runtime-line-mid")
      }
    >
      <header className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="truncate font-mono text-sm font-semibold text-ink">
            {agent.name}
          </h3>
          <div className="mt-1 flex flex-wrap items-center gap-2">
            <StatusBadge tone="neutral">
              {agent.owned ? "owned" : agent.source}
            </StatusBadge>
            <StatusBadge tone={agent.public ? "emerald" : "neutral"}>
              {agent.public ? "public" : "private"}
            </StatusBadge>
            <span className="text-[11px] text-ink-faint">
              {skills.length} skills
            </span>
          </div>
        </div>
        <ToolbarButton
          onClick={onToggleAgent}
          disabled={skills.length === 0}
          active={allSelected}
          size="xs"
          variant={someSelected ? "primary" : "secondary"}
        >
          {someSelected ? "Selected" : "Select"}
        </ToolbarButton>
      </header>
      <p className="mt-2 line-clamp-2 text-xs leading-relaxed text-ink-muted">
        {agent.card?.description || agent.description || "No description."}
      </p>
      {skills.length === 0 ? (
        <p className="mt-3 text-xs text-ink-faint">No declared tools.</p>
      ) : (
        <ul className="mt-3 space-y-2">
          {skills.map((skill) => {
            const checked = selectedSkills.includes(skill.name);
            return (
              <li key={skill.name}>
                <ToggleField
                  label={<span className="font-mono font-medium">{skill.name}</span>}
                  description={skill.description}
                  checked={checked}
                  onCheckedChange={() => onToggleSkill(skill.name)}
                  className={
                    "items-start px-2.5 py-2 text-xs " +
                    (checked
                      ? "border-signal-authority/45 bg-signal-authority/12 text-signal-authority"
                      : "bg-runtime-panel/30 text-ink-soft hover:border-runtime-line-mid")
                  }
                />
              </li>
            );
          })}
        </ul>
      )}
    </SurfacePanel>
  );
}

function SelectedAgentList({
  items,
}: {
  items: Array<{ agent: CandidateAgent; skills: string[] }>;
}) {
  if (items.length === 0) {
    return (
      <EmptyState title="No sub-agents selected" size="compact" />
    );
  }
  return (
    <ul className="space-y-2">
      {items.map(({ agent, skills }) => (
        <li
          key={agent.name}
          className="rounded-lg border border-runtime-line-soft/70 bg-runtime-bg px-3 py-2"
        >
          <div className="flex items-center justify-between gap-3">
            <span className="min-w-0 truncate font-mono text-xs font-semibold text-ink-soft">
              {agent.name}
            </span>
            <span className="shrink-0 text-[11px] text-ink-faint">
              {skills.length} skills
            </span>
          </div>
          <div className="mt-2 flex flex-wrap gap-1">
            {skills.map((skill) => (
              <span
                key={skill}
                className="rounded-md border border-runtime-line-soft/60 px-1.5 py-0.5 font-mono text-[10px] text-ink-dim"
              >
                {skill}
              </span>
            ))}
          </div>
        </li>
      ))}
    </ul>
  );
}

function MetaRunsPanel({
  runs,
  agentName,
  selectedRunId,
  activeSection,
  onSelectRun,
  onGoToStep,
}: {
  runs: MetaAgentRun[] | null;
  agentName: string | null;
  selectedRunId: string | null;
  activeSection: ComposeRunDetailSection;
  onSelectRun: (
    agentName: string,
    runId: string,
    section?: ComposeRunDetailSection,
  ) => void;
  onGoToStep: (stepId: ComposeStepId) => void;
}) {
  const selectedRun =
    selectedRunId && runs
      ? runs.find((run) => run.run_id === selectedRunId) || null
      : null;
  const summary = runs ? summarizeMetaRuns(runs) : null;
  return (
    <SectionPanel title="Goal runs">
      {!agentName ? (
        <EmptyState
          title="No deployed meta-agent"
          description="Deploy a manifest before inspecting persisted goal runs."
          size="compact"
          action={
            <ToolbarButton type="button" onClick={() => onGoToStep("manifest")}>
              Open manifest
            </ToolbarButton>
          }
        />
      ) : runs === null ? (
        <LoadingState label="Loading meta-agent runs..." />
      ) : runs.length === 0 ? (
        <EmptyState
          title="No goal runs yet"
          description="Runs appear here when the meta-agent pursues a goal."
        />
      ) : (
        <div className="space-y-4">
          <SummaryStrip className="lg:grid-cols-5" aria-label="Meta-agent run summary">
            <SummaryMetric label="runs" value={summary?.total ?? 0} detail="recent goals" />
            <SummaryMetric label="active" value={summary?.active ?? 0} detail="running or queued" tone="amber" />
            <SummaryMetric label="finished" value={summary?.done ?? 0} detail="completed goals" tone="emerald" />
            <SummaryMetric label="failed" value={summary?.failed ?? 0} detail="needs review" tone="red" />
            <SummaryMetric label="latest" value={summary?.latest ? fmtDate(summary.latest) : "-"} detail={agentName} />
          </SummaryStrip>

          <div
            className={
              selectedRunId
                ? "grid gap-4 xl:grid-cols-[minmax(280px,380px)_minmax(0,1fr)] xl:items-start"
                : "grid gap-3"
            }
          >
            <ul className="space-y-3">
              {runs.slice(0, 12).map((run) => (
                <MetaRunRow
                  key={run.run_id}
                  run={run}
                  selected={run.run_id === selectedRunId}
                  compact={Boolean(selectedRunId)}
                  onSelect={() => onSelectRun(run.agent_name, run.run_id)}
                />
              ))}
            </ul>

            {selectedRunId && (
              <MetaRunDetail
                agentName={agentName}
                run={selectedRun}
                requestedRunId={selectedRunId}
                activeSection={activeSection}
                onSelectSection={(section) =>
                  onSelectRun(agentName, selectedRunId, section)
                }
                onClearSelection={() => onGoToStep("runs")}
              />
            )}
          </div>
        </div>
      )}
    </SectionPanel>
  );
}

function MetaRunRow({
  run,
  selected,
  compact,
  onSelect,
}: {
  run: MetaAgentRun;
  selected: boolean;
  compact: boolean;
  onSelect: () => void;
}) {
  const family = metaRunStatusFamily(run.status);
  return (
    <li>
      <SelectableSurfaceButton
        selected={selected}
        onClick={onSelect}
        className={
          "bg-runtime-bg p-3 " +
          (family === "failed"
            ? "border-signal-danger/50 ring-signal-danger/10 "
            : family === "active"
              ? "border-signal-authority/45 ring-signal-authority/10 "
              : "") +
          (compact ? "" : "hover:border-runtime-line-mid hover:bg-runtime-panel/70")
        }
      >
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-xs font-semibold text-ink-soft [overflow-wrap:anywhere]">
                {run.run_id}
              </span>
              <StateBadge status={run.status} />
            </div>
            <p className="mt-2 line-clamp-2 text-xs leading-relaxed text-ink-dim">
              {run.goal}
            </p>
          </div>
          <time className="text-[11px] text-ink-faint" dateTime={run.updated_at}>
            {fmtDate(run.updated_at)}
          </time>
        </div>
        <div className="mt-3 grid grid-cols-3 gap-2 border-t border-runtime-line-soft/60 pt-3 text-[11px]">
          <SummaryMetric
            label="criteria"
            value={run.success_criteria.length}
            size="compact"
          />
          <SummaryMetric
            label="plan nodes"
            value={planNodeCount(run.current_plan)}
            size="compact"
          />
          <SummaryMetric
            label="progress"
            value={run.progress.length}
            size="compact"
          />
        </div>
      </SelectableSurfaceButton>
    </li>
  );
}

function MetaRunDetail({
  agentName,
  run,
  requestedRunId,
  activeSection,
  onSelectSection,
  onClearSelection,
}: {
  agentName: string;
  run: MetaAgentRun | null;
  requestedRunId: string;
  activeSection: ComposeRunDetailSection;
  onSelectSection: (section: ComposeRunDetailSection) => void;
  onClearSelection: () => void;
}) {
  if (!run) {
    return (
      <SurfacePanel as="section" className="bg-runtime-bg p-4">
        <EmptyState
          title="Run not found"
          description={`${requestedRunId} is not in the recent meta-agent runs for ${agentName}.`}
          action={
            <ToolbarButton type="button" onClick={onClearSelection}>
              All runs
            </ToolbarButton>
          }
        />
      </SurfacePanel>
    );
  }

  const current =
    COMPOSE_RUN_DETAIL_SECTIONS.find((section) => section.id === activeSection) ||
    COMPOSE_RUN_DETAIL_SECTIONS[0];

  return (
    <SurfacePanel as="section" className="min-w-0 bg-runtime-bg">
      <div className="flex flex-col gap-3 border-b border-runtime-line-soft/60 p-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="text-[10px] uppercase text-ink-faint">
            Meta-agent run
          </div>
          <h3 className="mt-1 font-mono text-sm text-ink [overflow-wrap:anywhere]">
            {run.run_id}
          </h3>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-ink-muted">
            <StateBadge status={run.status} />
            <span>{run.agent_name}</span>
          </div>
        </div>
        <ToolbarButton type="button" onClick={onClearSelection}>
          All runs
        </ToolbarButton>
      </div>

      <div className="border-b border-runtime-line-soft/60 px-4 py-3">
        <SegmentedControl
          role="tablist"
          aria-label={`${run.run_id} meta-agent run sections`}
          className="flex flex-wrap gap-1 bg-runtime-panel/60"
        >
          {COMPOSE_RUN_DETAIL_SECTIONS.map((section) => (
            <ToolbarButton
              key={section.id}
              type="button"
              role="tab"
              aria-selected={activeSection === section.id}
              active={activeSection === section.id}
              onClick={() => onSelectSection(section.id)}
              size="xs"
            >
              {section.label}
            </ToolbarButton>
          ))}
        </SegmentedControl>
        <div className="mt-2 text-xs leading-relaxed text-ink-muted">
          {current.description}
        </div>
      </div>

      <div className="p-4">
        {activeSection === "overview" && <MetaRunOverview run={run} />}
        {activeSection === "progress" && <MetaRunProgress run={run} />}
        {activeSection === "plan" && <MetaRunPlan run={run} />}
        {activeSection === "result" && <MetaRunResult run={run} />}
      </div>
    </SurfacePanel>
  );
}

function MetaRunOverview({ run }: { run: MetaAgentRun }) {
  return (
    <div className="space-y-4">
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        <SummaryMetric label="status" value={run.status} size="compact" />
        <SummaryMetric label="criteria" value={run.success_criteria.length} size="compact" />
        <SummaryMetric label="plan nodes" value={planNodeCount(run.current_plan)} size="compact" />
        <SummaryMetric label="progress" value={run.progress.length} size="compact" />
        <SummaryMetric label="created" value={fmtDate(run.created_at)} size="compact" />
        <SummaryMetric label="updated" value={fmtDate(run.updated_at)} size="compact" />
        <SummaryMetric
          label="completed"
          value={run.completed_at ? fmtDate(run.completed_at) : "-"}
          size="compact"
        />
        <SummaryMetric label="thread" value={run.thread_id || "-"} size="compact" />
      </div>
      <SurfacePanel as="div" className="bg-runtime-panel/40 p-3">
        <div className="text-[10px] uppercase text-ink-faint">Goal</div>
        <p className="mt-2 text-sm leading-relaxed text-ink-soft">{run.goal}</p>
      </SurfacePanel>
      {run.success_criteria.length > 0 && (
        <SurfacePanel as="section" className="bg-runtime-panel/40 p-3">
          <div className="text-[10px] uppercase text-ink-faint">
            Success criteria
          </div>
          <ul className="mt-2 space-y-1 text-xs leading-relaxed text-ink-dim">
            {run.success_criteria.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </SurfacePanel>
      )}
    </div>
  );
}

function MetaRunProgress({ run }: { run: MetaAgentRun }) {
  if (run.progress.length === 0) {
    return <EmptyState title="No progress entries" size="compact" />;
  }
  return (
    <ol className="space-y-2">
      {run.progress.map((item, index) => (
        <li key={index}>
          <SurfacePanel as="div" className="bg-runtime-panel/40 p-3">
            <div className="text-xs leading-relaxed text-ink-soft">
              {progressText(item)}
            </div>
            <CodeBlock className="mt-2 max-h-44 text-[11px]">
              {JSON.stringify(item, null, 2)}
            </CodeBlock>
          </SurfacePanel>
        </li>
      ))}
    </ol>
  );
}

function MetaRunPlan({ run }: { run: MetaAgentRun }) {
  return (
    <div className="space-y-4">
      <div className="grid gap-2 sm:grid-cols-3">
        <SummaryMetric label="rounds" value={run.current_plan.rounds.length} size="compact" />
        <SummaryMetric label="nodes" value={planNodeCount(run.current_plan)} size="compact" />
        <SummaryMetric
          label="max nodes"
          value={String(run.current_plan.max_nodes ?? "-")}
          size="compact"
        />
      </div>
      <CodeBlock className="max-h-[520px] text-xs">
        {JSON.stringify(run.current_plan, null, 2)}
      </CodeBlock>
    </div>
  );
}

function MetaRunResult({ run }: { run: MetaAgentRun }) {
  return (
    <div className="space-y-4">
      {run.error && (
        <InlineAlert tone="red" className="text-xs [overflow-wrap:anywhere]">
          {run.error}
        </InlineAlert>
      )}
      {run.summary ? (
        <SurfacePanel
          as="div"
          className="bg-runtime-panel/40 px-3 py-2 text-sm leading-relaxed text-ink-soft"
        >
          {run.summary}
        </SurfacePanel>
      ) : (
        <EmptyState title="No final summary yet" size="compact" />
      )}
      <CodeBlock className="max-h-[520px] text-xs">
        {JSON.stringify(run.state || {}, null, 2)}
      </CodeBlock>
    </div>
  );
}

type ComposeNextAction = {
  label: string;
  detail: string;
  href: string;
  action: string;
  variant: "primary" | "secondary";
};

function composeNextAction({
  steps,
  validation,
  submitting,
  composeResult,
  deployment,
}: {
  steps: ComposeStepStatus[];
  validation: string[];
  submitting: boolean;
  composeResult: AgentComposeResult | null;
  deployment: AgentDeployment | null;
}): ComposeNextAction {
  const selectError = validation.find((item) => item.includes("sub-agent"));
  if (selectError) {
    return {
      label: "Choose the working set",
      detail: selectError,
      href: "/compose/select",
      action: "Open catalog",
      variant: "primary",
    };
  }
  const identityError = validation.find((item) => item.includes("Name"));
  if (identityError) {
    return {
      label: "Finish agent identity",
      detail: identityError,
      href: "/compose/identity",
      action: "Open identity",
      variant: "primary",
    };
  }
  const controlsError = validation.find((item) =>
    item.includes("Goal") ||
    item.includes("memory") ||
    item.includes("Parallel"),
  );
  if (controlsError) {
    return {
      label: "Set goal and limits",
      detail: controlsError,
      href: "/compose/controls",
      action: "Open controls",
      variant: "primary",
    };
  }
  if (validation.length > 0) {
    const firstOpenStep = steps.find((step) => !step.complete) || steps[0];
    return {
      label: "Resolve draft requirements",
      detail: validation[0],
      href: firstOpenStep.path,
      action: "Open step",
      variant: "primary",
    };
  }
  if (submitting) {
    return {
      label: "Deployment is being queued",
      detail: "The manifest has been submitted and the deployment stream will update next.",
      href: "/compose/deployment",
      action: "Watch deployment",
      variant: "secondary",
    };
  }
  if (!composeResult) {
    return {
      label: "Review and deploy the manifest",
      detail: "The draft has enough selected tools, identity, goal, memory, and execution controls.",
      href: "/compose/manifest",
      action: "Review manifest",
      variant: "primary",
    };
  }
  if (deploymentHasFailed(deployment)) {
    return {
      label: "Review deployment issue",
      detail: `Deployment status is ${deployment?.status || "failed"}.`,
      href: "/compose/deployment",
      action: "Open deployment",
      variant: "primary",
    };
  }
  if (!deploymentIsReady(deployment)) {
    return {
      label: "Watch deployment progress",
      detail: deployment?.status
        ? `Deployment is currently ${deployment.status}.`
        : "Waiting for deployment status from the stream.",
      href: "/compose/deployment",
      action: "Open deployment",
      variant: "secondary",
    };
  }
  return {
    label: "Inspect goal runs",
    detail: "The composed agent is deployed; recent run records are ready for review.",
    href: "/compose/runs",
    action: "Open runs",
    variant: "secondary",
  };
}

function composeReadinessTone(
  validation: string[],
  submitting: boolean,
  composeResult: AgentComposeResult | null,
  deployment: AgentDeployment | null,
) {
  if (deploymentHasFailed(deployment)) return "red" as const;
  if (validation.length > 0 || submitting || (composeResult && !deploymentIsReady(deployment))) {
    return "amber" as const;
  }
  if (composeResult && deploymentIsReady(deployment)) return "emerald" as const;
  return "neutral" as const;
}

function composeReadinessLabel(
  validation: string[],
  submitting: boolean,
  composeResult: AgentComposeResult | null,
  deployment: AgentDeployment | null,
) {
  if (deploymentHasFailed(deployment)) return "needs review";
  if (submitting) return "deploying";
  if (validation.length > 0) return "setup incomplete";
  if (composeResult && deploymentIsReady(deployment)) return "deployed";
  if (composeResult) return "deployment pending";
  return "ready to deploy";
}

function composeReadinessDetail(
  validation: string[],
  submitting: boolean,
  composeResult: AgentComposeResult | null,
  deployment: AgentDeployment | null,
) {
  if (deploymentHasFailed(deployment)) return "deployment reported a failure";
  if (submitting) return "manifest submission in progress";
  if (validation.length > 0) {
    return `${validation.length} requirement${validation.length === 1 ? "" : "s"} remaining`;
  }
  if (composeResult && deploymentIsReady(deployment)) return "agent is ready for goal runs";
  if (composeResult) return deployment?.status || "waiting for deployment";
  return "all manifest inputs are valid";
}

function deploymentIsReady(deployment: AgentDeployment | null) {
  const status = deployment?.status.toLowerCase() || "";
  return ["ready", "deployed", "complete", "completed"].includes(status);
}

function deploymentHasFailed(deployment: AgentDeployment | null) {
  const status = deployment?.status.toLowerCase() || "";
  return ["failed", "error", "errored", "canceled", "cancelled"].includes(status);
}

function summarizeMetaRuns(runs: MetaAgentRun[]): {
  total: number;
  active: number;
  done: number;
  failed: number;
  latest: string | null;
} {
  let active = 0;
  let done = 0;
  let failed = 0;
  let latest: string | null = null;
  for (const run of runs) {
    const family = metaRunStatusFamily(run.status);
    if (family === "active") active += 1;
    if (family === "done") done += 1;
    if (family === "failed") failed += 1;
    if (!latest || Date.parse(run.updated_at) > Date.parse(latest)) {
      latest = run.updated_at;
    }
  }
  return { total: runs.length, active, done, failed, latest };
}

function metaRunStatusFamily(status: string): "active" | "done" | "failed" | "other" {
  const normalized = status.toLowerCase();
  if (["complete", "completed", "success", "succeeded", "ready"].includes(normalized)) {
    return "done";
  }
  if (["failed", "error", "errored", "canceled", "cancelled"].includes(normalized)) {
    return "failed";
  }
  if (["queued", "pending", "running", "active", "in_progress"].includes(normalized)) {
    return "active";
  }
  return "other";
}

function NumberField({
  label,
  value,
  min,
  max,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  onChange: (value: number) => void;
}) {
  return (
    <FormField label={label}>
      <TextInput
        type="number"
        min={min}
        max={max}
        value={value}
        onChange={(event) => onChange(clampNumber(event.target.value, min, max))}
      />
    </FormField>
  );
}

function mergeCandidates(
  registryAgents: AgentListing[],
  myAgents: MyAgentListing[],
  publicAgents: AgentListing[] = [],
): CandidateAgent[] {
  const map = new Map<string, CandidateAgent>();
  const add = (
    agent: AgentListing,
    source: CandidateAgent["source"],
    owned = false,
  ) => {
    const current = map.get(agent.name);
    map.set(agent.name, {
      ...(current || agent),
      ...agent,
      owned: current?.owned || owned,
      source: current?.owned ? "owned" : owned ? "owned" : current?.source || source,
    });
  };
  publicAgents.forEach((agent) => add(agent, "public", false));
  registryAgents.forEach((agent) => add(agent, "registry", false));
  myAgents.forEach((agent) => add(agent, "owned", true));
  return Array.from(map.values()).sort((left, right) => {
    if (left.owned !== right.owned) return left.owned ? -1 : 1;
    if (left.public !== right.public) return left.public ? -1 : 1;
    return left.name.localeCompare(right.name);
  });
}

function buildManifest(
  draft: ComposeDraft,
  selectedAgents: Array<{ agent: CandidateAgent; skills: string[] }>,
): Record<string, unknown> {
  const success = draft.successCriteria
    .split(/\r?\n/)
    .map((item) => item.trim())
    .filter(Boolean);
  return {
    composition: {
      sub_agents: selectedAgents.map(({ agent, skills }) => ({
        name: agent.name,
        skills,
      })),
      max_nodes: draft.maxNodes,
      max_parallel: draft.maxParallel,
      max_replans: draft.maxReplans,
    },
    goal: {
      objective: draft.goal.trim(),
      success_criteria: success,
    },
    memory: {
      tiers: enabledMemoryTiers(draft),
      namespace: draft.memoryNamespace.trim() || "meta",
    },
  };
}

function validateDraft(
  draft: ComposeDraft,
  selectedAgents: Array<{ agent: CandidateAgent; skills: string[] }>,
): string[] {
  const errors: string[] = [];
  const name = draft.name.trim();
  if (!AGENT_NAME_RE.test(name)) {
    errors.push("Name must be lowercase kebab-case and start with a letter.");
  }
  if (selectedAgents.length === 0) {
    errors.push("Select at least one sub-agent tool.");
  }
  if (!draft.goal.trim()) {
    errors.push("Goal is required.");
  }
  if (enabledMemoryTiers(draft).length === 0) {
    errors.push("Enable at least one memory tier.");
  }
  if (draft.maxParallel > draft.maxNodes) {
    errors.push("Parallel limit cannot exceed max nodes.");
  }
  return errors;
}

function controlsAreReady(draft: ComposeDraft) {
  return (
    Boolean(draft.goal.trim()) &&
    enabledMemoryTiers(draft).length > 0 &&
    draft.maxParallel <= draft.maxNodes
  );
}

function identityIsReady(draft: ComposeDraft) {
  return AGENT_NAME_RE.test(draft.name.trim());
}

function enabledMemoryTiers(draft: ComposeDraft): MemoryTier[] {
  return MEMORY_TIERS.map((tier) => tier.id).filter((tier) => draft.memoryTiers[tier]);
}

function skillList(agent: AgentListing): AgentSkill[] {
  return agent.card?.skills || [];
}

function agentSearchText(agent: AgentListing) {
  const skills = skillList(agent);
  return [
    agent.name,
    agent.description,
    agent.card?.description,
    agent.status,
    ...skills.map((skill) => skill.name),
    ...skills.flatMap((skill) => skill.tags || []),
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

function clampNumber(raw: string, min: number, max: number) {
  const parsed = Number.parseInt(raw, 10);
  if (!Number.isFinite(parsed)) return min;
  return Math.max(min, Math.min(max, parsed));
}

function shortSha(value: string | null | undefined) {
  return value ? value.slice(0, 8) : "pending";
}

function fmtDate(value: string) {
  try {
    return new Intl.DateTimeFormat(undefined, {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
    }).format(new Date(value));
  } catch {
    return value;
  }
}

function planNodeCount(plan: Record<string, unknown>) {
  const nodes = plan.nodes;
  if (Array.isArray(nodes)) return nodes.length;
  if (nodes && typeof nodes === "object") return Object.keys(nodes).length;
  return 0;
}

function progressText(item: Record<string, unknown>) {
  for (const key of ["message", "summary", "status", "event", "kind"]) {
    const value = item[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  try {
    return JSON.stringify(item).slice(0, 180);
  } catch {
    return "progress event";
  }
}
