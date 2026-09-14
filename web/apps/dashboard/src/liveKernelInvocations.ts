import type { ProtocolSimulation } from "./api";

export type LiveInvocationRecordView = {
  agentName: string;
  skillName: string;
  nodeId: string;
  role: string;
  status: string;
  elapsedMs: number | null;
  elapsedLabel: string;
  error: string;
  resultError: string;
  timeoutLikely: boolean;
  argsPreview: string;
  resultPreview: string;
};

export type LiveInvocationsView = {
  passed: boolean;
  count: number;
  failCount: number;
  digest: string;
  records: LiveInvocationRecordView[];
};

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function asNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

export function formatElapsed(ms: number | null): string {
  if (ms === null) return "-";
  if (ms < 1000) return `${ms}ms`;
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

export function previewValue(value: unknown, maxChars = 520): string {
  if (value === null || value === undefined) return "";
  const raw =
    typeof value === "string"
      ? value
      : JSON.stringify(value, null, 2) || String(value);
  const normalized = raw.replace(/\s+\n/g, "\n").trim();
  if (normalized.length <= maxChars) return normalized;
  return `${normalized.slice(0, Math.max(0, maxChars - 1))}...`;
}

function resultPreview(result: unknown): string {
  const record = asRecord(result);
  if (!record) return previewValue(result);
  for (const key of ["final", "observation", "summary", "message", "value", "status"]) {
    if (record[key] !== undefined && record[key] !== null) {
      return previewValue(record[key]);
    }
  }
  return previewValue(record);
}

function parseRecord(value: unknown): LiveInvocationRecordView | null {
  const record = asRecord(value);
  if (!record) return null;
  const result = asRecord(record.result);
  const error = asString(record.error);
  const resultError = result ? asString(result.error) : "";
  const elapsedMs = asNumber(record.elapsed_ms);
  return {
    agentName: asString(record.agent_name) || "unknown-agent",
    skillName: asString(record.skill_name) || "unknown-skill",
    nodeId: asString(record.node_id),
    role: asString(record.role),
    status: asString(record.status) || "unknown",
    elapsedMs,
    elapsedLabel: formatElapsed(elapsedMs),
    error,
    resultError,
    timeoutLikely: /timeout/i.test(`${error} ${resultError}`) || Boolean(elapsedMs && elapsedMs >= 120000),
    argsPreview: previewValue(record.arguments, 420),
    resultPreview: resultPreview(record.result),
  };
}

export function parseLiveInvocations(run: ProtocolSimulation | null): LiveInvocationsView | null {
  const result = asRecord(run?.job["result"]);
  const live = asRecord(result?.live_invocations);
  if (!live) return null;
  const records = Array.isArray(live.records)
    ? live.records.map(parseRecord).filter((item): item is LiveInvocationRecordView => item !== null)
    : [];
  return {
    passed: Boolean(live.passed),
    count: asNumber(live.count) ?? records.length,
    failCount: asNumber(live.fail_count) ?? records.filter((record) => record.status !== "passed").length,
    digest: asString(live.digest),
    records,
  };
}
