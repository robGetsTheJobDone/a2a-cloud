import type { SubagentFileOp, TrialRoom, TrialRun } from "../api";

export type TrialComparison = {
  bestScore: number;
  scoreSpread: number;
  passedRuns: number;
  artifacts: number;
};

type TrialAgentReadinessState =
  | "ready"
  | "empty"
  | "missing_skills"
  | "private_running"
  | "public_offline"
  | "needs_public_running";

export type TrialAgentReadinessAgent = {
  public: boolean;
  status: string | null | undefined;
  card?: {
    skills?: unknown[] | null;
  } | null;
};

export type TrialAgentReadiness = {
  state: TrialAgentReadinessState;
  total: number;
  runnable: number;
  publicRunning: number;
  publicRunningWithoutSkills: number;
  publicOffline: number;
  privateRunning: number;
  privateTotal: number;
};

export function summarizeRooms(rooms: TrialRoom[]) {
  return {
    total: rooms.length,
    runs: rooms.reduce((sum, room) => sum + room.runs.length, 0),
    deployed: rooms.filter((room) => room.status === "deployed").length,
  };
}

export function mergeTrialRoomList(rooms: TrialRoom[], room: TrialRoom) {
  const exists = rooms.some((item) => item.slug === room.slug);
  return exists
    ? rooms.map((item) => (item.slug === room.slug ? room : item))
    : [room, ...rooms];
}

export function summarizeRunComparison(runs: TrialRun[]): TrialComparison {
  if (runs.length === 0) {
    return { bestScore: 0, scoreSpread: 0, passedRuns: 0, artifacts: 0 };
  }
  const scores = runs.map((run) => run.score);
  const bestScore = Math.max(...scores);
  const worstScore = Math.min(...scores);
  return {
    bestScore,
    scoreSpread: bestScore - worstScore,
    passedRuns: runs.filter((run) => run.status === "passed").length,
    artifacts: runs.reduce((sum, run) => sum + changedFileCount(run), 0),
  };
}

export function isTrialRunnableAgent(agent: TrialAgentReadinessAgent) {
  return (
    agent.public &&
    String(agent.status || "").toLowerCase() === "running" &&
    (agent.card?.skills?.length || 0) > 0
  );
}

export function summarizeTrialAgentReadiness(
  agents: TrialAgentReadinessAgent[],
): TrialAgentReadiness {
  const publicRunning = agents.filter(
    (agent) => agent.public && String(agent.status || "").toLowerCase() === "running",
  );
  const publicRunningWithoutSkills = publicRunning.filter(
    (agent) => (agent.card?.skills?.length || 0) === 0,
  ).length;
  const publicOffline = agents.filter(
    (agent) => agent.public && String(agent.status || "").toLowerCase() !== "running",
  ).length;
  const privateRunning = agents.filter(
    (agent) => !agent.public && String(agent.status || "").toLowerCase() === "running",
  ).length;
  const privateTotal = agents.filter((agent) => !agent.public).length;
  const runnable = agents.filter(isTrialRunnableAgent).length;
  let state: TrialAgentReadinessState = "needs_public_running";

  if (runnable > 0) {
    state = "ready";
  } else if (agents.length === 0) {
    state = "empty";
  } else if (publicRunningWithoutSkills > 0) {
    state = "missing_skills";
  } else if (privateRunning > 0) {
    state = "private_running";
  } else if (publicOffline > 0) {
    state = "public_offline";
  }

  return {
    state,
    total: agents.length,
    runnable,
    publicRunning: publicRunning.length,
    publicRunningWithoutSkills,
    publicOffline,
    privateRunning,
    privateTotal,
  };
}

export function outputCoverage(room: TrialRoom, run: TrialRun) {
  const required = schemaRequiredKeys(room.output_schema);
  const result = run.result || {};
  const present = required.filter((key) =>
    Object.prototype.hasOwnProperty.call(result, key),
  );
  return {
    required,
    present,
    missing: required.filter((key) => !present.includes(key)),
  };
}

export function schemaRequiredKeys(schema: Record<string, unknown>) {
  const required = schema["required"];
  if (!Array.isArray(required)) return [];
  return required.map((key) => String(key).trim()).filter(Boolean);
}

export function changedFileCount(run: TrialRun) {
  return run.file_ops.filter((op) => op.op === "create" || op.op === "update").length;
}

export function receiptId(run: TrialRun) {
  const value = run.receipt_json["receipt_id"];
  return typeof value === "string" ? value : "";
}

export function stringReceiptValue(run: TrialRun, path: string[]) {
  let value: unknown = run.receipt_json;
  for (const key of path) {
    if (!value || typeof value !== "object") return "";
    value = (value as Record<string, unknown>)[key];
  }
  return typeof value === "string" ? value : "";
}

export function fmtMoney(cents: number) {
  if (!cents) return "$0";
  return `$${(cents / 100).toFixed(0)}`;
}

export function fmtMs(ms: number | null) {
  if (ms == null) return "-";
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
}

export function fmtBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function opTone(op: SubagentFileOp["op"] | string) {
  if (op === "create") return "w-14 text-signal-live";
  if (op === "update") return "w-14 text-signal-authority";
  if (op === "delete") return "w-14 text-signal-danger";
  return "w-14 text-ink-soft";
}
