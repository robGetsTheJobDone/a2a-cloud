import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useParams } from "react-router-dom";
import {
  getKernelEvolutionRun,
  getUserKernelSimulationRun,
  listKernelEvolutionRuns,
  listMyAgents,
  listUserKernelSimulationRuns,
  listUserKernelSimulationTemplates,
  replayKernelEvolution,
  replayUserKernelSimulation,
  runKernelEvolution,
  runUserKernelSimulation,
  type ProtocolSimulation,
} from "../../api";
import { buildAgentKernelSimulationSpec } from "../../kernelSimulationBuilder";
import { formatElapsed, parseLiveInvocations } from "../../liveKernelInvocations";
import { parseKernelTraceView } from "../../kernelTrace";
import {
  DASHBOARD_SECTION_CACHE_KEYS,
  useDashboardSectionResource,
  useDashboardSectionState,
} from "../DashboardSectionCache";
import {
  simulationViewForPath,
  type SimulationViewId,
} from "../../navigation";
import {
  analyzeSimulationSpecReadiness,
  mergeRunByJobId,
  DEFAULT_EVOLUTION_PARTICIPANTS,
  formatActionError,
  normalizeSimulationRunSection,
  summarizeSimulationRuns,
  type KernelSimulationsResource,
  type SimulationRunSectionId,
} from "./simulationModel";

export function latestTrace(run: ProtocolSimulation | null) {
  return run ? parseKernelTraceView(run, { traceLimit: 5 }) : null;
}

/**
 * useSimulationsData — owns ALL data-fetching, route-derived selection, builder
 * state, and lifecycle effects for the kernel simulation lab. Behavior-preserving
 * extraction of the original KernelSimulations() body; the returned shape exposes
 * exactly the values and callbacks the view tree consumed inline.
 */
// Section-cache keys for hoisted simulation-lab selection (mandate B). Holding
// the active view + selected run/section in the section cache means switching
// tabs or opening a run is pure state — the lab never remounts or refetches,
// and builder/evolution form fields (already cached) survive list swaps.
const SIM_SELECTION_KEYS = {
  view: "runtime.simulations.active-view",
  runId: "runtime.simulations.selected-run-id",
  evolutionRunId: "runtime.simulations.selected-evolution-run-id",
  runSection: "runtime.simulations.active-run-section",
} as const;

export function useSimulationsData() {
  const location = useLocation();
  const {
    jobId: routeJobId,
    section: routeRunSection,
  } = useParams<{ jobId?: string; section?: string }>();

  // --- Hoisted selection state (mandate B/C: no reload/redirect on nav) ------
  const [activeView, setActiveView] = useDashboardSectionState<SimulationViewId>(
    SIM_SELECTION_KEYS.view,
    "builder",
  );
  const [selectedRunId, setSelectedRunId] = useDashboardSectionState<string>(
    SIM_SELECTION_KEYS.runId,
    "",
  );
  const [selectedEvolutionRunId, setSelectedEvolutionRunId] =
    useDashboardSectionState<string>(SIM_SELECTION_KEYS.evolutionRunId, "");
  const [selectedRunSection, setSelectedRunSection] =
    useDashboardSectionState<SimulationRunSectionId>(
      SIM_SELECTION_KEYS.runSection,
      "overview",
    );

  // --- Deep-link hydration (mandate C: map URL -> state on mount, no redirect)
  // Runs once. Existing /simulations[/spec|/runs/:jobId[/:section]|/evolution|
  // /evolution-results/:jobId] URLs seed the hoisted state rather than acting as
  // the live source of truth, so the lab never unmounts on later navigation.
  const hydratedRef = useRef(false);
  if (!hydratedRef.current) {
    hydratedRef.current = true;
    const initialView = simulationViewForPath(location.pathname).id;
    setActiveView(initialView);
    if (initialView === "runs" && routeJobId) setSelectedRunId(routeJobId);
    if (initialView === "evolution-results" && routeJobId) {
      setSelectedEvolutionRunId(routeJobId);
    }
    if (routeRunSection) {
      setSelectedRunSection(normalizeSimulationRunSection(routeRunSection));
    }
  }

  const loadKernelSimulationsResource = useCallback(async (): Promise<KernelSimulationsResource> => {
    const [templates, runs, evolutionRuns, agents] = await Promise.all([
      listUserKernelSimulationTemplates(),
      listUserKernelSimulationRuns(12),
      listKernelEvolutionRuns(12),
      listMyAgents(),
    ]);
    return { templates, runs, evolutionRuns, agents };
  }, []);
  const {
    data,
    error: loadErr,
    loading,
    refreshing,
    refresh,
    setData: setSimulationData,
  } = useDashboardSectionResource(
    DASHBOARD_SECTION_CACHE_KEYS.runtime.simulations,
    loadKernelSimulationsResource,
  );
  const templates = data?.templates ?? [];
  const runs = data?.runs ?? [];
  const evolutionRuns = data?.evolutionRuns ?? [];
  const agents = data?.agents ?? [];
  const [selectedId, setSelectedId] = useDashboardSectionState<string>(
    DASHBOARD_SECTION_CACHE_KEYS.runtime.simulationSelectedTemplate,
    "",
  );
  const [builderKind, setBuilderKind] = useDashboardSectionState(
    DASHBOARD_SECTION_CACHE_KEYS.runtime.simulationBuilderKind,
    "market",
  );
  const [builderTitle, setBuilderTitle] = useDashboardSectionState(
    DASHBOARD_SECTION_CACHE_KEYS.runtime.simulationBuilderTitle,
    "Agent market simulation",
  );
  const [builderGoal, setBuilderGoal] = useDashboardSectionState(
    DASHBOARD_SECTION_CACHE_KEYS.runtime.simulationBuilderGoal,
    "Compare selected agents in a bounded simulation using signed policy decisions, outcome scoring, and deterministic replay.",
  );
  const [executionMode, setExecutionMode] = useDashboardSectionState<"bounded" | "hybrid">(
    DASHBOARD_SECTION_CACHE_KEYS.runtime.simulationExecutionMode,
    "bounded",
  );
  const [selectedAgentNames, setSelectedAgentNames] = useDashboardSectionState<string[]>(
    DASHBOARD_SECTION_CACHE_KEYS.runtime.simulationSelectedAgents,
    [],
  );
  const [specText, setSpecText] = useDashboardSectionState(
    DASHBOARD_SECTION_CACHE_KEYS.runtime.simulationSpecText,
    "",
  );
  const [evolutionTitle, setEvolutionTitle] = useDashboardSectionState(
    DASHBOARD_SECTION_CACHE_KEYS.runtime.simulationEvolutionTitle,
    "Self evolving money network",
  );
  const [evolutionSeed, setEvolutionSeed] = useDashboardSectionState(
    DASHBOARD_SECTION_CACHE_KEYS.runtime.simulationEvolutionSeed,
    "population",
  );
  const [evolutionVariantCount, setEvolutionVariantCount] = useDashboardSectionState(
    DASHBOARD_SECTION_CACHE_KEYS.runtime.simulationEvolutionVariantCount,
    5,
  );
  const [evolutionMaxDelta, setEvolutionMaxDelta] = useDashboardSectionState(
    DASHBOARD_SECTION_CACHE_KEYS.runtime.simulationEvolutionMaxDelta,
    4,
  );
  const [evolutionParticipantsText, setEvolutionParticipantsText] = useDashboardSectionState(
    DASHBOARD_SECTION_CACHE_KEYS.runtime.simulationEvolutionParticipants,
    JSON.stringify(DEFAULT_EVOLUTION_PARTICIPANTS, null, 2),
  );
  const [busy, setBusy] = useState(false);
  const [evolutionBusy, setEvolutionBusy] = useState(false);
  const [runStartedAt, setRunStartedAt] = useState<number | null>(null);
  const [clockMs, setClockMs] = useState(Date.now());
  const [replayBusy, setReplayBusy] = useState(false);
  const [evolutionReplayBusy, setEvolutionReplayBusy] = useState(false);
  const [directRunLoadingId, setDirectRunLoadingId] = useState("");
  const [directEvolutionRunLoadingId, setDirectEvolutionRunLoadingId] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);
  const [replayResult, setReplayResult] = useState<string | null>(null);
  const [evolutionReplayResult, setEvolutionReplayResult] = useState<string | null>(null);
  const error = actionError ?? loadErr;

  const selected = useMemo(
    () => templates.find((template) => template.template_id === selectedId) || null,
    [selectedId, templates],
  );
  const availableAgents = useMemo(() => agents, [agents]);
  const selectedAgents = useMemo(
    () => availableAgents.filter((agent) => selectedAgentNames.includes(agent.name)),
    [availableAgents, selectedAgentNames],
  );
  const selectedRun = useMemo(
    () => selectedRunId
      ? runs.find((run) => String(run.job.job_id) === selectedRunId) || null
      : null,
    [runs, selectedRunId],
  );
  const selectedEvolutionRun = useMemo(
    () => selectedEvolutionRunId
      ? evolutionRuns.find((run) => String(run.job.job_id) === selectedEvolutionRunId) || null
      : null,
    [evolutionRuns, selectedEvolutionRunId],
  );
  const trace = latestTrace(selectedRun);
  const activeLiveInvocations = parseLiveInvocations(selectedRun);
  const runSummary = useMemo(() => summarizeSimulationRuns(runs), [runs]);
  const evolutionSummary = useMemo(
    () => summarizeSimulationRuns(evolutionRuns),
    [evolutionRuns],
  );
  const specReadiness = useMemo(
    () => analyzeSimulationSpecReadiness(specText, executionMode),
    [executionMode, specText],
  );
  const activeRunElapsed =
    busy && runStartedAt ? formatElapsed(Math.max(0, clockMs - runStartedAt)) : null;
  const resourceLoading = loading && !data;

  const refreshSimulationData = useCallback(() => {
    setActionError(null);
    void refresh().catch(() => undefined);
  }, [refresh]);

  useEffect(() => {
    if (!data) return;
    const first = data.templates[0] || null;
    if (!selectedId && first) setSelectedId(first.template_id);
    if (!specText && first) setSpecText(JSON.stringify(first.spec, null, 2));
    if (selectedAgentNames.length === 0) {
      setSelectedAgentNames(data.agents.slice(0, 3).map((agent) => agent.name));
    }
  }, [
    data,
    selectedAgentNames.length,
    selectedId,
    setSelectedAgentNames,
    setSelectedId,
    setSpecText,
    specText,
  ]);

  // Reset the spec editor only when the user actually picks a DIFFERENT
  // template. Keying off `selectedId` (not the `selected` object identity)
  // means a background list refetch — which mints new template object
  // references with the same id — no longer clobbers in-progress spec/builder
  // edits (mandate B: form state survives list swaps). `appliedTemplateIdRef`
  // tracks which template's spec is currently loaded so the first hydration of
  // an existing selection still seeds the editor exactly once.
  const appliedTemplateIdRef = useRef<string | null>(null);
  useEffect(() => {
    if (!selected) return;
    if (appliedTemplateIdRef.current === selected.template_id) return;
    appliedTemplateIdRef.current = selected.template_id;
    setSpecText(JSON.stringify(selected.spec, null, 2));
    setExecutionMode("bounded");
    setReplayResult(null);
  }, [selected, setExecutionMode, setSpecText]);

  useEffect(() => {
    setReplayResult(null);
  }, [selectedRunId]);

  useEffect(() => {
    if (!selectedRunId || selectedRun || !data) {
      if (!selectedRunId || selectedRun) setDirectRunLoadingId("");
      return;
    }

    let cancelled = false;
    setDirectRunLoadingId(selectedRunId);
    setActionError(null);
    void getUserKernelSimulationRun(selectedRunId)
      .then((run) => {
        if (cancelled) return;
        setSimulationData((current) =>
          current
            ? {
                ...current,
                runs: mergeRunByJobId(current.runs, run),
              }
            : current,
        );
      })
      .catch((ex) => {
        if (cancelled) return;
        setActionError(
          `Could not load simulation run ${selectedRunId}: ${formatActionError(ex)}`,
        );
      })
      .finally(() => {
        if (!cancelled) {
          setDirectRunLoadingId((current) =>
            current === selectedRunId ? "" : current,
          );
        }
      });

    return () => {
      cancelled = true;
    };
  }, [data, selectedRun, selectedRunId, setSimulationData]);

  useEffect(() => {
    setEvolutionReplayResult(null);
  }, [selectedEvolutionRunId]);

  useEffect(() => {
    if (!selectedEvolutionRunId || selectedEvolutionRun || !data) {
      if (!selectedEvolutionRunId || selectedEvolutionRun) {
        setDirectEvolutionRunLoadingId("");
      }
      return;
    }

    let cancelled = false;
    setDirectEvolutionRunLoadingId(selectedEvolutionRunId);
    setActionError(null);
    void getKernelEvolutionRun(selectedEvolutionRunId)
      .then((run) => {
        if (cancelled) return;
        setSimulationData((current) =>
          current
            ? {
                ...current,
                evolutionRuns: mergeRunByJobId(current.evolutionRuns, run),
              }
            : current,
        );
      })
      .catch((ex) => {
        if (cancelled) return;
        setActionError(
          `Could not load evolution run ${selectedEvolutionRunId}: ${formatActionError(ex)}`,
        );
      })
      .finally(() => {
        if (!cancelled) {
          setDirectEvolutionRunLoadingId((current) =>
            current === selectedEvolutionRunId ? "" : current,
          );
        }
      });

    return () => {
      cancelled = true;
    };
  }, [data, selectedEvolutionRun, selectedEvolutionRunId, setSimulationData]);

  useEffect(() => {
    if (!busy || !runStartedAt) return;
    setClockMs(Date.now());
    const id = window.setInterval(() => setClockMs(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [busy, runStartedAt]);

  function toggleAgent(name: string) {
    setSelectedAgentNames((current) => {
      if (current.includes(name)) return current.filter((item) => item !== name);
      return [...current, name].slice(0, 8);
    });
  }

  function buildFromAgents() {
    setActionError(null);
    setReplayResult(null);
    try {
      const spec = buildAgentKernelSimulationSpec({
        simulationType: builderKind,
        title: builderTitle.trim() || "Agent simulation",
        goal: builderGoal.trim() || "Run a bounded agent simulation.",
        agents: selectedAgents,
      });
      setSelectedId("");
      setSpecText(JSON.stringify(spec, null, 2));
      setExecutionMode("hybrid");
    } catch (ex) {
      setActionError(ex instanceof Error ? ex.message : String(ex));
    }
  }

  function restoreSelectedTemplateSpec() {
    if (!selected) return;
    setSpecText(JSON.stringify(selected.spec, null, 2));
    setExecutionMode("bounded");
    setActionError(null);
    setReplayResult(null);
  }

  async function runSelected() {
    if (busy) return;
    setBusy(true);
    const startedAt = Date.now();
    setRunStartedAt(startedAt);
    setClockMs(startedAt);
    setActionError(null);
    setReplayResult(null);
    try {
      const readiness = analyzeSimulationSpecReadiness(specText, executionMode);
      if (!readiness.canRun || !readiness.spec) {
        throw new Error(readiness.detail);
      }
      const specObject = readiness.spec;
      const next = await runUserKernelSimulation({
        spec: specObject,
        execution_mode: executionMode,
        max_live_calls: Math.max(1, Math.min(8, selectedAgents.length || 3)),
      });
      setReplayResult(null);
      setSimulationData((current) =>
        current
          ? {
              ...current,
              runs: [
                next,
                ...current.runs.filter((run) => run.job.job_id !== next.job.job_id),
              ].slice(0, 12),
            }
          : current,
      );
      // Open the new run in place (mandate C): flip to the runs view and select
      // it via state instead of a route redirect that unmounts the lab.
      setActiveView("runs");
      setSelectedRunSection("overview");
      setSelectedRunId(String(next.job.job_id));
    } catch (ex) {
      setActionError(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setBusy(false);
      setRunStartedAt(null);
    }
  }

  async function replayActive() {
    if (!selectedRun || replayBusy) return;
    setReplayBusy(true);
    setReplayResult(null);
    setActionError(null);
    try {
      const replay = await replayUserKernelSimulation(String(selectedRun.job.job_id));
      setReplayResult(replay.replay_passed ? "replay passed" : "replay mismatch");
    } catch (ex) {
      setActionError(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setReplayBusy(false);
    }
  }

  async function runEvolutionExperiment() {
    if (evolutionBusy) return;
    setEvolutionBusy(true);
    setActionError(null);
    setEvolutionReplayResult(null);
    try {
      const parsed = JSON.parse(evolutionParticipantsText);
      if (!Array.isArray(parsed)) throw new Error("Participants must be a JSON array.");
      const next = await runKernelEvolution({
        title: evolutionTitle.trim() || "Self evolving simulation",
        seed: evolutionSeed.trim() || null,
        variant_count: Math.max(1, Math.min(8, evolutionVariantCount)),
        max_score_delta: Math.max(0, Math.min(25, evolutionMaxDelta)),
        participants: parsed,
        strategy: "multi_agent_topology",
        simulation_type: "money_network_evolution",
      });
      setEvolutionReplayResult(null);
      setSimulationData((current) =>
        current
          ? {
              ...current,
              evolutionRuns: [
                next,
                ...current.evolutionRuns.filter(
                  (run) => run.job.job_id !== next.job.job_id,
                ),
              ].slice(0, 12),
            }
          : current,
      );
      // Open the new evolution result in place (mandate C) via state.
      setActiveView("evolution-results");
      setSelectedEvolutionRunId(String(next.job.job_id));
    } catch (ex) {
      setActionError(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setEvolutionBusy(false);
    }
  }

  async function replayActiveEvolution() {
    if (!selectedEvolutionRun || evolutionReplayBusy) return;
    setEvolutionReplayBusy(true);
    setActionError(null);
    setEvolutionReplayResult(null);
    try {
      const replay = await replayKernelEvolution(String(selectedEvolutionRun.job.job_id));
      setEvolutionReplayResult(replay.replay_passed ? "replay passed" : "replay mismatch");
    } catch (ex) {
      setActionError(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setEvolutionReplayBusy(false);
    }
  }

  return {
    activeView,
    setActiveView,
    setSelectedRunId,
    setSelectedEvolutionRunId,
    setSelectedRunSection,
    templates,
    runs,
    evolutionRuns,
    availableAgents,
    selectedAgents,
    selectedId,
    setSelectedId,
    builderKind,
    setBuilderKind,
    builderTitle,
    setBuilderTitle,
    builderGoal,
    setBuilderGoal,
    executionMode,
    setExecutionMode,
    selectedAgentNames,
    specText,
    setSpecText,
    evolutionTitle,
    setEvolutionTitle,
    evolutionSeed,
    setEvolutionSeed,
    evolutionVariantCount,
    setEvolutionVariantCount,
    evolutionMaxDelta,
    setEvolutionMaxDelta,
    evolutionParticipantsText,
    setEvolutionParticipantsText,
    busy,
    evolutionBusy,
    replayBusy,
    evolutionReplayBusy,
    directRunLoadingId,
    directEvolutionRunLoadingId,
    replayResult,
    evolutionReplayResult,
    error,
    selected,
    selectedRunId,
    selectedEvolutionRunId,
    selectedRunSection,
    selectedRun,
    selectedEvolutionRun,
    trace,
    activeLiveInvocations,
    runSummary,
    evolutionSummary,
    specReadiness,
    activeRunElapsed,
    resourceLoading,
    refreshing,
    refreshSimulationData,
    toggleAgent,
    buildFromAgents,
    restoreSelectedTemplateSpec,
    runSelected,
    replayActive,
    runEvolutionExperiment,
    replayActiveEvolution,
  };
}

export type { ProtocolSimulation };
