import {
  type AgentListing,
  type TrialRoom,
  type TrialRun,
} from "../../api";
import { type RunDetails } from "../RunDetailsPanel";
import { type StatusBadgeTone } from "../DashboardChrome";
import {
  changedFileCount,
  fmtMs,
  isTrialRunnableAgent,
  summarizeTrialAgentReadiness,
  type TrialComparison,
  type TrialAgentReadiness,
} from "../trialRoomUtils";
import { type TrialRoomViewId } from "../../navigation";

export type RoomPatch = (room: TrialRoom) => void;

export type TrialRoomsResource = {
  rooms: TrialRoom[];
  agents: AgentListing[];
};

export const TRIAL_RUN_DETAIL_SECTIONS = [
  { id: "overview", label: "Overview" },
  { id: "evidence", label: "Evidence" },
  { id: "review", label: "Review" },
  { id: "receipt", label: "Receipt" },
] as const;

export type TrialRunDetailSectionId =
  (typeof TRIAL_RUN_DETAIL_SECTIONS)[number]["id"];

function trialRoomRoute(
  slug: string,
  view: TrialRoomViewId = "overview",
) {
  const base = `/trials/${encodeURIComponent(slug)}`;
  return view === "overview" ? base : `${base}/${view}`;
}

function trialRunRoute(
  slug: string,
  runId: number | string,
  section: TrialRunDetailSectionId = "overview",
) {
  const base = `/trials/${encodeURIComponent(slug)}/runs/${encodeURIComponent(String(runId))}`;
  return section === "overview" ? base : `${base}/${section}`;
}

export function normalizeTrialRunDetailSection(
  section: string | null | undefined,
): TrialRunDetailSectionId {
  return TRIAL_RUN_DETAIL_SECTIONS.some((item) => item.id === section)
    ? (section as TrialRunDetailSectionId)
    : "overview";
}

export type TrialRoomNextAction = {
  label: string;
  detail: string;
  href: string;
  action: string;
};

export function trialRunDetails(room: TrialRoom, run: TrialRun): RunDetails {
  return {
    kind: "trial",
    id: String(run.id),
    title: `${run.agent_name}.${run.skill_name}`,
    subtitle: room.title,
    status: run.status,
    summary: run.summary || run.evaluator_notes,
    error: run.error,
    stats: [
      { label: "score", value: String(run.score) },
      { label: "grant", value: run.grant_id ? run.grant_id.slice(0, 12) : "-" },
      { label: "elapsed", value: fmtMs(run.elapsed_ms) },
      { label: "created", value: new Date(run.created_at).toLocaleString() },
    ],
    args: run.args_preview,
    result: run.result,
    file_ops: run.file_ops,
    events: run.events.map((payload, idx) => ({
      id: idx,
      type:
        typeof payload.event_type === "string"
          ? payload.event_type
          : typeof payload.type === "string"
          ? payload.type
          : `event-${idx + 1}`,
      payload,
    })),
    receipt: run.receipt_json,
  };
}

export function trialRoomNextAction({
  room,
  runs,
  comparison,
  winner,
  liveRuns,
  failedRuns,
  topRun,
}: {
  room: TrialRoom;
  runs: TrialRun[];
  comparison: TrialComparison;
  winner: TrialRun | null;
  liveRuns: number;
  failedRuns: number;
  topRun: TrialRun | null;
}): TrialRoomNextAction {
  if (liveRuns > 0) {
    return {
      label: "Watch running candidates",
      detail: `${liveRuns} candidate ${liveRuns === 1 ? "is" : "are"} still producing evidence for this room.`,
      href: trialRoomRoute(room.slug, "receipts"),
      action: "Open receipts",
    };
  }
  if (runs.length === 0) {
    return {
      label: "Run the first candidate",
      detail: "No agent has produced a receipt yet. Start with one runnable public agent and compare from there.",
      href: trialRoomRoute(room.slug, "run"),
      action: "Run candidate",
    };
  }
  if (!winner && comparison.passedRuns > 0 && topRun) {
    return {
      label: "Pick a winner from passed evidence",
      detail: `${comparison.passedRuns} candidate ${comparison.passedRuns === 1 ? "has" : "have"} passed review. Open the strongest run before selecting it.`,
      href: trialRunRoute(room.slug, topRun.id, "review"),
      action: "Review top run",
    };
  }
  if (!winner && topRun) {
    return {
      label: "Review the strongest candidate",
      detail:
        failedRuns === runs.length
          ? "Every candidate failed. Inspect the top receipt, then adjust the room or run another agent."
          : "No winner is selected yet. Score the leading receipt and record evaluator notes.",
      href: trialRunRoute(room.slug, topRun.id, "review"),
      action: "Review run",
    };
  }
  if (winner) {
    return {
      label: "Audit the selected receipt",
      detail: `${winner.agent_name}.${winner.skill_name} is selected. Keep its receipt, artifacts, and review notes easy to verify.`,
      href: trialRunRoute(room.slug, winner.id, "receipt"),
      action: "Open receipt",
    };
  }
  return {
    label: "Compare candidate receipts",
    detail: "Use the comparison view to inspect scores, schema coverage, changed files, and receipts.",
    href: trialRoomRoute(room.slug, "comparison"),
    action: "Compare runs",
  };
}

export function trialRoomPostureTone({
  runs,
  comparison,
  winner,
  liveRuns,
  failedRuns,
}: {
  runs: TrialRun[];
  comparison: TrialComparison;
  winner: TrialRun | null;
  liveRuns: number;
  failedRuns: number;
}): StatusBadgeTone {
  if (winner) return "emerald";
  if (liveRuns > 0 || comparison.passedRuns > 0) return "amber";
  if (runs.length > 0 && failedRuns === runs.length) return "red";
  if (runs.length === 0) return "neutral";
  return "amber";
}

export function trialRoomPostureLabel({
  runs,
  comparison,
  winner,
  liveRuns,
  failedRuns,
}: {
  runs: TrialRun[];
  comparison: TrialComparison;
  winner: TrialRun | null;
  liveRuns: number;
  failedRuns: number;
}) {
  if (winner) return "winner selected";
  if (liveRuns > 0) return "running";
  if (comparison.passedRuns > 0) return "ready to choose";
  if (runs.length > 0 && failedRuns === runs.length) return "needs rerun";
  if (runs.length === 0) return "needs candidates";
  return "needs review";
}

export function trialAgentReadinessContent(readiness: TrialAgentReadiness) {
  if (readiness.state === "empty") {
    return {
      title: "No candidate agents installed",
      detail:
        "Bring an A2A agent into My Agents, make it public, and wait for it to run before starting this trial.",
      href: "/my-agents/import",
      action: "Bring A2A agent",
    };
  }
  if (readiness.state === "missing_skills") {
    return {
      title: "Running public agents need tools",
      detail:
        "At least one public agent is running, but its card has no callable tools for a trial run.",
      href: "/my-agents",
      action: "Manage agents",
    };
  }
  if (readiness.state === "private_running") {
    return {
      title: "Running agents are private",
      detail:
        "Trial rooms run public agents so receipts can be compared by agent and tool. Publish one, then refresh candidates.",
      href: "/my-agents",
      action: "Manage agents",
    };
  }
  if (readiness.state === "public_offline") {
    return {
      title: "Public agents are not running",
      detail:
        "Start or redeploy a public agent, then refresh this list before running the candidate.",
      href: "/my-agents",
      action: "Manage agents",
    };
  }
  return {
    title: "No runnable public candidate",
    detail:
      "A trial run needs a public running agent with at least one callable tool.",
    href: "/my-agents",
    action: "Manage agents",
  };
}

export function formatTrialAgentRefreshMessage(readiness: TrialAgentReadiness) {
  if (readiness.runnable > 0) {
    return `${pluralize(readiness.runnable, "candidate agent")} ready for trial runs.`;
  }
  if (readiness.state === "empty") {
    return "Candidate list refreshed. No agents are installed yet.";
  }
  if (readiness.state === "missing_skills") {
    return "Candidate list refreshed. Public running agents still need callable tools.";
  }
  if (readiness.state === "private_running") {
    return "Candidate list refreshed. Running agents are still private.";
  }
  if (readiness.state === "public_offline") {
    return "Candidate list refreshed. Public agents are still not running.";
  }
  return "Candidate list refreshed. No runnable public agent is available yet.";
}

export function pluralize(count: number, singular: string) {
  return `${count.toLocaleString()} ${singular}${count === 1 ? "" : "s"}`;
}

export function clampScore(value: string) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return 0;
  return Math.max(0, Math.min(100, Math.round(parsed)));
}

export function parseWorkspacePaths(paths: string) {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const path of paths.split(/[\n,]/)) {
    const clean = path.trim();
    if (!clean || seen.has(clean)) continue;
    seen.add(clean);
    out.push(clean);
  }
  return out;
}

// Re-export shared util signatures used across trial-room components so callers
// import a single barrel without changing behavior.
export {
  isTrialRunnableAgent,
  summarizeTrialAgentReadiness,
  changedFileCount,
  fmtMs,
};
