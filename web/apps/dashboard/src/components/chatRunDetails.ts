import type { DagRun, LLMUsage, SubagentRun } from "../api";
import type { RunDetails } from "./RunDetailsPanel";

type BackendErrorCarrier = { error?: string | null };

function backendError(value: unknown): string | null {
  const err = (value as BackendErrorCarrier | null)?.error;
  return typeof err === "string" && err.trim() ? err : null;
}

function terminalError(status: string, fallback?: string | null): string | null {
  if (["error", "failed", "canceled", "denied"].includes(status)) {
    return fallback || status;
  }
  return null;
}

export function parseDagArgs(argsJson: string): Record<string, unknown> {
  try {
    const parsed = JSON.parse(argsJson || "{}");
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : {};
  } catch {
    return {};
  }
}

export function subagentRunDetails(run: SubagentRun): RunDetails {
  const result = latestSubagentResult(run);
  const handoffs = run.handoffs || [];
  const grants = run.grants || [];
  const receipts = run.receipts || [];
  return {
    kind: "subagent",
    id: run.grant_id,
    title: `${run.agent_name}.${run.skill_name}`,
    subtitle: run.rerun_of_grant_id
      ? `rerun of ${run.rerun_of_grant_id.slice(0, 12)}`
      : undefined,
    status: run.status,
    summary: run.summary,
    error: backendError(run) || terminalError(run.status, run.summary),
    stats: [
      { label: "grant", value: run.grant_id.slice(0, 12) },
      { label: "thread", value: run.thread_id || "-" },
      { label: "handoffs", value: String(handoffs.length) },
      { label: "grants", value: String(grants.length) },
      { label: "receipts", value: String(receipts.length) },
      { label: "updated", value: new Date(run.updated_at).toLocaleString() },
    ],
    args: parseDagArgs(run.args_json),
    result,
    scopes: run.scopes,
    file_ops: run.file_ops,
    original_request: run.original_request || {
      agent_name: run.agent_name,
      skill_name: run.skill_name,
      grant_id: run.grant_id,
      args: parseDagArgs(run.args_json),
      scopes: run.scopes,
    },
    handoffs,
    grants,
    receipts,
    events: run.events.map((event) => ({
      id: event.id,
      type: event.event_type,
      payload: event.payload,
      created_at: event.created_at,
    })),
  };
}

export function dagRunDetails(run: DagRun): RunDetails {
  return {
    kind: "dag",
    id: run.dag_run_id,
    title: run.dag_run_id,
    subtitle: run.goal,
    status: run.status,
    summary: run.summary || run.goal,
    error: backendError(run) || terminalError(run.status, run.summary),
    stats: [
      { label: "nodes", value: String(run.nodes.length) },
      { label: "thread", value: run.thread_id || "-" },
      { label: "updated", value: new Date(run.updated_at).toLocaleString() },
    ],
    result: run.summary ? { summary: run.summary } : undefined,
    nodes: run.nodes.map((node) => ({
      id: node.node_id,
      agent: node.agent_name,
      skill: node.skill_name,
      status: node.status,
      summary: node.summary,
      error: backendError(node) || terminalError(node.status, node.summary),
      grant_id: node.grant_id,
      elapsed_ms: node.elapsed_ms,
      file_ops: node.file_ops,
      args: parseDagArgs(node.args_json),
      result: node.result,
    })),
  };
}

export function llmUsageDetails(usage: LLMUsage): RunDetails {
  return {
    kind: "llm",
    id: String(usage.id),
    title: usage.model || "LLM call",
    subtitle: usage.agent_name && usage.skill_name
      ? `${usage.agent_name}.${usage.skill_name}`
      : usage.source,
    status: usage.status,
    summary: [
      `${usage.total_tokens} tokens`,
      usage.cost_usd ? `$${usage.cost_usd.toFixed(4)}` : null,
      usage.provider,
    ].filter(Boolean).join(" · "),
    stats: [
      { label: "source", value: usage.source || "-" },
      { label: "provider", value: usage.provider || "-" },
      { label: "prompt", value: String(usage.prompt_tokens) },
      { label: "completion", value: String(usage.completion_tokens) },
      { label: "total", value: String(usage.total_tokens) },
      { label: "cost", value: usage.cost_usd ? `$${usage.cost_usd.toFixed(6)}` : "$0" },
      { label: "thread", value: usage.thread_id || "-" },
      { label: "grant", value: usage.grant_id ? usage.grant_id.slice(0, 12) : "-" },
      { label: "created", value: new Date(usage.created_at).toLocaleString() },
    ],
    result: {
      provider: usage.provider,
      model: usage.model,
      prompt_tokens: usage.prompt_tokens,
      completion_tokens: usage.completion_tokens,
      total_tokens: usage.total_tokens,
      cost_usd: usage.cost_usd,
      dag_run_id: usage.dag_run_id,
      grant_id: usage.grant_id,
      agent_name: usage.agent_name,
      skill_name: usage.skill_name,
    },
    receipt: usage.metadata,
  };
}

function latestSubagentResult(run: SubagentRun): unknown {
  for (let index = run.events.length - 1; index >= 0; index -= 1) {
    const payload = run.events[index]?.payload || {};
    if ("result" in payload) return payload.result;
    const nestedPayload = payload.payload;
    if (
      nestedPayload &&
      typeof nestedPayload === "object" &&
      !Array.isArray(nestedPayload) &&
      "result" in nestedPayload
    ) {
      return (nestedPayload as Record<string, unknown>).result;
    }
  }
  return undefined;
}
