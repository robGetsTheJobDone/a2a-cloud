import type {
  AgentListing,
  KernelEvolutionRun,
  ProtocolSimulation,
  UserKernelSimulationTemplate,
} from "../../api";
import { type SimulationViewId } from "../../navigation";

export function simulationKindLabel(value: string) {
  return value.replace(/_/g, " ");
}

export const DEFAULT_EVOLUTION_PARTICIPANTS = [
  { id: "allocator", role: "capital-router", skill: "allocate", fitness: 72, cost: 2 },
  { id: "reviewer", role: "risk-reviewer", skill: "review", fitness: 81, cost: 1 },
  { id: "forecaster", role: "network-forecaster", skill: "forecast", fitness: 76, cost: 2 },
];

export const SIMULATION_RUN_SECTIONS = [
  { id: "overview", label: "Overview" },
  { id: "live", label: "Live calls" },
  { id: "trace", label: "Trace" },
] as const;
const SIMULATION_ACTIVE_STATUSES = new Set([
  "active",
  "created",
  "pending",
  "queued",
  "running",
  "started",
  "waiting",
  "in_progress",
]);
const SIMULATION_DONE_STATUSES = new Set([
  "complete",
  "completed",
  "done",
  "passed",
  "ready",
  "success",
  "succeeded",
]);
const SIMULATION_FAILED_STATUSES = new Set([
  "canceled",
  "cancelled",
  "error",
  "errored",
  "failed",
  "killed",
  "timeout",
  "timed_out",
]);

export type SimulationRunSectionId = (typeof SIMULATION_RUN_SECTIONS)[number]["id"];
export type KernelSimulationsResource = {
  templates: UserKernelSimulationTemplate[];
  runs: ProtocolSimulation[];
  evolutionRuns: KernelEvolutionRun[];
  agents: AgentListing[];
};
type SimulationStatusFamily = "active" | "done" | "failed" | "other";
export type SimulationRunLike = ProtocolSimulation | KernelEvolutionRun;
export type SimulationRunSummary = {
  total: number;
  active: number;
  done: number;
  failed: number;
  other: number;
  firstActive: SimulationRunLike | null;
  firstFailed: SimulationRunLike | null;
  latest: SimulationRunLike | null;
};

export function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

export function runResult(run: KernelEvolutionRun | null): Record<string, unknown> | null {
  return objectValue(run?.job.result) || objectValue(run?.job.output_payload);
}

export function resultVariants(result: Record<string, unknown> | null): Record<string, unknown>[] {
  const variants = result?.variants;
  return Array.isArray(variants)
    ? variants.filter((item): item is Record<string, unknown> => Boolean(objectValue(item)))
    : [];
}

export function evolutionMutation(variant: Record<string, unknown>) {
  return objectValue(variant.mutation) || {};
}

export function evolutionProposal(result: Record<string, unknown> | null) {
  return objectValue(result?.proposal);
}

function simulationRunRoute(
  jobId: string | number,
  section: SimulationRunSectionId = "overview",
) {
  const base = `/simulations/runs/${encodeURIComponent(String(jobId))}`;
  return section === "overview" ? base : `${base}/${section}`;
}

function simulationEvolutionRunRoute(jobId: string | number) {
  return `/simulations/evolution-results/${encodeURIComponent(String(jobId))}`;
}

export function textValue(value: unknown, fallback = "") {
  return typeof value === "string" && value.trim() ? value : fallback;
}

export function numberValue(value: unknown, fallback = 0) {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

export function formatActionError(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}

export function normalizeSimulationRunSection(
  section: string | null | undefined,
): SimulationRunSectionId {
  return SIMULATION_RUN_SECTIONS.some((item) => item.id === section)
    ? (section as SimulationRunSectionId)
    : "overview";
}

export type SimulationNextAction = {
  label: string;
  detail: string;
  href: string;
  action: string;
  variant: "primary" | "secondary";
};

export function simulationNextAction({
  activeView,
  templates,
  selectedAgentCount,
  runSummary,
  evolutionSummary,
  selectedRunId,
  selectedEvolutionRunId,
  busy,
  evolutionBusy,
  activeRunElapsed,
}: {
  activeView: SimulationViewId;
  templates: UserKernelSimulationTemplate[];
  selectedAgentCount: number;
  runSummary: SimulationRunSummary;
  evolutionSummary: SimulationRunSummary;
  selectedRunId: string;
  selectedEvolutionRunId: string;
  busy: boolean;
  evolutionBusy: boolean;
  activeRunElapsed: string | null;
}): SimulationNextAction {
  if (busy) {
    return {
      label: "Watch the running simulation",
      detail: activeRunElapsed
        ? `Live request has been running for ${activeRunElapsed}. Results will appear in recent runs when it completes.`
        : "Simulation execution is in progress.",
      href: "/simulations/runs",
      action: "Open runs",
      variant: "primary",
    };
  }
  if (evolutionBusy) {
    return {
      label: "Watch evolution results",
      detail: "Topology evolution is running; variants and winner metadata will appear after completion.",
      href: "/simulations/evolution-results",
      action: "Open results",
      variant: "primary",
    };
  }
  if (runSummary.firstFailed) {
    return {
      label: "Review failed simulation",
      detail: `${simulationRunTitle(runSummary.firstFailed)} reported ${runSummary.firstFailed.job.status}.`,
      href: simulationRunRoute(runSummary.firstFailed.job.job_id),
      action: "Open failure",
      variant: "primary",
    };
  }
  if (evolutionSummary.firstFailed) {
    return {
      label: "Review failed evolution",
      detail: `${simulationRunTitle(evolutionSummary.firstFailed)} reported ${evolutionSummary.firstFailed.job.status}.`,
      href: simulationEvolutionRunRoute(evolutionSummary.firstFailed.job.job_id),
      action: "Open failure",
      variant: "primary",
    };
  }
  if (runSummary.firstActive) {
    return {
      label: "Monitor active simulation",
      detail: `${simulationRunTitle(runSummary.firstActive)} is currently ${runSummary.firstActive.job.status}.`,
      href: simulationRunRoute(runSummary.firstActive.job.job_id),
      action: "Open active",
      variant: "secondary",
    };
  }
  if (evolutionSummary.firstActive) {
    return {
      label: "Monitor active evolution",
      detail: `${simulationRunTitle(evolutionSummary.firstActive)} is currently ${evolutionSummary.firstActive.job.status}.`,
      href: simulationEvolutionRunRoute(evolutionSummary.firstActive.job.job_id),
      action: "Open active",
      variant: "secondary",
    };
  }
  if (activeView === "builder" && selectedAgentCount === 0) {
    return {
      label: "Select agents for a live spec",
      detail: "Choose one or more owned agents before building a live-agent simulation spec.",
      href: "/simulations",
      action: "Open builder",
      variant: "primary",
    };
  }
  if (activeView === "builder" && selectedAgentCount > 0) {
    return {
      label: "Build and review the selected-agent spec",
      detail: `${selectedAgentCount} agents are selected for the simulation builder.`,
      href: "/simulations/spec",
      action: "Review spec",
      variant: "primary",
    };
  }
  if (selectedRunId) {
    return {
      label: "Inspect run evidence",
      detail: "Use overview, live calls, and trace tabs to review replay and execution evidence.",
      href: simulationRunRoute(selectedRunId, "trace"),
      action: "Open trace",
      variant: "secondary",
    };
  }
  if (selectedEvolutionRunId) {
    return {
      label: "Inspect evolution variants",
      detail: "Review variant scores, winner metadata, replay status, and disabled proposal details.",
      href: simulationEvolutionRunRoute(selectedEvolutionRunId),
      action: "Open result",
      variant: "secondary",
    };
  }
  if (templates.length > 0) {
    return {
      label: "Run a starter template",
      detail: `${templates.length} templates are available for proof-only or live-agent simulation.`,
      href: "/simulations/spec",
      action: "Open spec",
      variant: "primary",
    };
  }
  return {
    label: "Open the simulation builder",
    detail: "Templates and owned agents will populate this lab when they are available.",
    href: "/simulations",
    action: "Open builder",
    variant: "secondary",
  };
}

export function simulationPostureTone(
  runSummary: SimulationRunSummary,
  evolutionSummary: SimulationRunSummary,
  busy: boolean,
  evolutionBusy: boolean,
) {
  if (runSummary.failed > 0 || evolutionSummary.failed > 0) return "red" as const;
  if (busy || evolutionBusy || runSummary.active > 0 || evolutionSummary.active > 0) {
    return "amber" as const;
  }
  if (runSummary.done > 0 || evolutionSummary.done > 0) return "emerald" as const;
  return "neutral" as const;
}

export function simulationPostureLabel(
  runSummary: SimulationRunSummary,
  evolutionSummary: SimulationRunSummary,
  busy: boolean,
  evolutionBusy: boolean,
) {
  if (runSummary.failed > 0 || evolutionSummary.failed > 0) return "needs review";
  if (busy || evolutionBusy) return "running";
  if (runSummary.active > 0 || evolutionSummary.active > 0) return "active";
  if (runSummary.done > 0 || evolutionSummary.done > 0) return "ready";
  return "not started";
}

export function summarizeSimulationRuns(items: SimulationRunLike[]): SimulationRunSummary {
  let active = 0;
  let done = 0;
  let failed = 0;
  let other = 0;
  let firstActive: SimulationRunLike | null = null;
  let firstFailed: SimulationRunLike | null = null;
  let latest: SimulationRunLike | null = null;
  for (const item of items) {
    const family = simulationStatusFamily(item.job.status);
    if (family === "active") {
      active += 1;
      firstActive ??= item;
    } else if (family === "done") {
      done += 1;
    } else if (family === "failed") {
      failed += 1;
      firstFailed ??= item;
    } else {
      other += 1;
    }
    if (!latest || simulationRunTimestamp(item) > simulationRunTimestamp(latest)) {
      latest = item;
    }
  }
  return {
    total: items.length,
    active,
    done,
    failed,
    other,
    firstActive,
    firstFailed,
    latest,
  };
}

function simulationStatusFamily(status: string): SimulationStatusFamily {
  const normalized = status.toLowerCase();
  if (SIMULATION_ACTIVE_STATUSES.has(normalized)) return "active";
  if (SIMULATION_DONE_STATUSES.has(normalized)) return "done";
  if (SIMULATION_FAILED_STATUSES.has(normalized)) return "failed";
  return "other";
}

function simulationRunTimestamp(run: SimulationRunLike) {
  const value = run.job.completed_at || run.job.started_at || run.job.created_at;
  const parsed = value ? Date.parse(value) : Number.NaN;
  return Number.isFinite(parsed) ? parsed : 0;
}

function simulationRunTitle(run: SimulationRunLike) {
  return String(run.job.title || run.job.job_id);
}

export function formatSimulationDate(value: string | null | undefined) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

// --- Spec readiness + run merge (consolidated from the former kernelSimulationView.ts) ---

export function hasSelectedLiveAgents(spec: Record<string, unknown>) {
  const metadata = spec.metadata;
  if (!metadata || typeof metadata !== "object" || Array.isArray(metadata)) return false;
  const sourceAgentNames = (metadata as Record<string, unknown>).source_agent_names;
  return Array.isArray(sourceAgentNames) && sourceAgentNames.some((item) => String(item || "").trim());
}

export type SimulationExecutionMode = "bounded" | "hybrid";

type SimulationSpecReadinessState = "empty" | "invalid" | "blocked" | "ready";

export type SimulationSpecReadiness = {
  state: SimulationSpecReadinessState;
  tone: "neutral" | "emerald" | "amber" | "red";
  label: string;
  detail: string;
  canRun: boolean;
  spec: Record<string, unknown> | null;
  actorCount: number;
  stepCount: number;
  invariantCount: number;
  liveSourceCount: number;
};

function arrayCount(value: unknown) {
  return Array.isArray(value) ? value.length : 0;
}

function liveSourceCount(spec: Record<string, unknown>) {
  const metadata = objectValue(spec.metadata);
  const sourceAgentNames = metadata?.source_agent_names;
  return Array.isArray(sourceAgentNames)
    ? sourceAgentNames.filter((item) => String(item || "").trim()).length
    : 0;
}

function specParseError(error: unknown) {
  const message = error instanceof Error ? error.message : String(error);
  return message.length > 140 ? `${message.slice(0, 137)}...` : message;
}

export function analyzeSimulationSpecReadiness(
  specText: string,
  executionMode: SimulationExecutionMode,
): SimulationSpecReadiness {
  if (!specText.trim()) {
    return {
      state: "empty",
      tone: "neutral",
      label: "No spec loaded",
      detail: "Choose a starter template or build a live-agent spec before running.",
      canRun: false,
      spec: null,
      actorCount: 0,
      stepCount: 0,
      invariantCount: 0,
      liveSourceCount: 0,
    };
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(specText);
  } catch (error) {
    return {
      state: "invalid",
      tone: "red",
      label: "Invalid JSON",
      detail: `Fix the simulation JSON before running. ${specParseError(error)}`,
      canRun: false,
      spec: null,
      actorCount: 0,
      stepCount: 0,
      invariantCount: 0,
      liveSourceCount: 0,
    };
  }

  const spec = objectValue(parsed);
  if (!spec) {
    return {
      state: "invalid",
      tone: "red",
      label: "Spec must be an object",
      detail: "Simulation specs must be JSON objects with actors, steps, and invariants.",
      canRun: false,
      spec: null,
      actorCount: 0,
      stepCount: 0,
      invariantCount: 0,
      liveSourceCount: 0,
    };
  }

  const actorCount = arrayCount(spec.actors);
  const stepCount = arrayCount(spec.steps);
  const invariantCount = arrayCount(spec.invariants);
  const sourceCount = liveSourceCount(spec);

  if (executionMode === "hybrid" && !hasSelectedLiveAgents(spec)) {
    return {
      state: "blocked",
      tone: "amber",
      label: "Live-agent source missing",
      detail: "Live-agent mode requires a spec built from selected agents.",
      canRun: false,
      spec,
      actorCount,
      stepCount,
      invariantCount,
      liveSourceCount: sourceCount,
    };
  }

  return {
    state: "ready",
    tone: "emerald",
    label: executionMode === "hybrid" ? "Ready for live run" : "Ready for proof-only run",
    detail:
      executionMode === "hybrid"
        ? `${sourceCount} selected agent source${sourceCount === 1 ? "" : "s"} will be called with live execution.`
        : "The spec can run in deterministic proof-only mode.",
    canRun: true,
    spec,
    actorCount,
    stepCount,
    invariantCount,
    liveSourceCount: sourceCount,
  };
}

type RunWithJobId = {
  job: {
    job_id: string | number;
  };
};

export function mergeRunByJobId<T extends RunWithJobId>(items: T[], next: T): T[] {
  const nextJobId = String(next.job.job_id);
  return [next, ...items.filter((item) => String(item.job.job_id) !== nextJobId)];
}
