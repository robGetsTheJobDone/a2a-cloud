import {
  findArenaScoreboardView,
  parseArenaScoreboardView,
  type ArenaScoreboardView,
} from "./arenaScoreboard";
import {
  findKernelTraceView,
  parseKernelTraceView,
  type KernelTraceView,
} from "./kernelTrace";

export type ChatEvidenceCard =
  | { kind: "arena_suite"; evidenceKey?: string | null; scoreboard: ArenaScoreboardView }
  | { kind: "kernel_trace"; evidenceKey?: string | null; trace: KernelTraceView }
  | { kind: "kernel_signal"; evidenceKey?: string | null; signal: ChatEvidenceSignalView };

type ChatEvidenceSignalView = {
  evidenceKind: string;
  eventType: string;
  title: string;
  severity: string | null;
  status: string | null;
  message: string | null;
  sourceLabel: string | null;
  stats: ChatEvidenceSignalStat[];
};

type ChatEvidenceSignalStat = {
  label: string;
  value: string | number;
  tone: "cyan" | "emerald" | "amber" | "red" | "neutral";
};

export function parseChatEvidenceCards(value: unknown): ChatEvidenceCard[] {
  const liveEvent = asRecord(value);
  if (liveEvent.type === "evidence_event") {
    return parseLiveEvidenceEvent(liveEvent);
  }

  const cards: ChatEvidenceCard[] = [];
  const arena = parseArenaScoreboardView(value);
  if (arena) cards.push({ kind: "arena_suite", scoreboard: arena });
  const trace = parseKernelTraceView(value, { traceLimit: 4 });
  if (trace) cards.push({ kind: "kernel_trace", trace });
  return cards;
}

export function findChatEvidenceCards(values: unknown[]): ChatEvidenceCard[] {
  const cards: ChatEvidenceCard[] = [];
  const arena = findArenaScoreboardView(values);
  if (arena) cards.push({ kind: "arena_suite", scoreboard: arena });
  const trace = findKernelTraceView(values, { traceLimit: 4 });
  if (trace) cards.push({ kind: "kernel_trace", trace });
  return cards;
}

function parseLiveEvidenceEvent(root: Record<string, unknown>): ChatEvidenceCard[] {
  const evidenceKey = stringFrom(root.evidence_key);
  const payload = asRecord(root.payload);
  const envelope = {
    event_type: root.event_type,
    payload,
  };
  const cards: ChatEvidenceCard[] = [];
  const arena = parseArenaScoreboardView(envelope);
  if (arena) cards.push({ kind: "arena_suite", evidenceKey, scoreboard: arena });
  const trace = parseKernelTraceView(envelope, { traceLimit: 4 });
  if (trace) cards.push({ kind: "kernel_trace", evidenceKey, trace });
  if (cards.length > 0) return cards;

  const signal = parseSignalView(root, payload);
  return signal ? [{ kind: "kernel_signal", evidenceKey, signal }] : [];
}

function parseSignalView(
  root: Record<string, unknown>,
  payload: Record<string, unknown>,
): ChatEvidenceSignalView | null {
  const eventType = stringFrom(root.event_type);
  const evidenceKind = stringFrom(root.evidence_kind);
  if (!eventType || !evidenceKind) return null;
  return {
    evidenceKind,
    eventType,
    title: stringFrom(root.title) || defaultSignalTitle(eventType, evidenceKind, payload),
    severity: stringFrom(root.severity),
    status: stringFrom(root.status),
    message: stringFrom(root.message),
    sourceLabel: sourceLabel(asRecord(root.source)),
    stats: signalStats(eventType, evidenceKind, payload),
  };
}

function signalStats(
  eventType: string,
  evidenceKind: string,
  payload: Record<string, unknown>,
): ChatEvidenceSignalStat[] {
  if (evidenceKind === "invariant_failure") {
    return [
      {
        label: "invariant",
        value: stringFrom(payload.invariant_id) || stringFrom(payload.id) || "failed",
        tone: "red",
      },
      {
        label: "status",
        value: stringFrom(payload.status) || "failed",
        tone: "red",
      },
    ];
  }
  if (evidenceKind === "protocol_limit") {
    return [
      { label: "limit", value: eventType.replace(/^simulation_/, "").replace(/_/g, " "), tone: "amber" },
      { label: "attempted", value: numberOrDash(payload.attempted), tone: "neutral" },
      { label: "cap", value: firstNumberLabel([payload.episode_limit, payload.limit, payload.max]), tone: "neutral" },
    ];
  }
  if (evidenceKind === "policy_decision") {
    const decision = stringFrom(payload.decision) || "-";
    const policyRefs = Array.isArray(payload.policy_refs) ? payload.policy_refs.length : 0;
    return [
      { label: "decision", value: decision, tone: policyDecisionTone(decision) },
      { label: "effect", value: stringFrom(payload.effect) || "-", tone: policyDecisionTone(decision) },
      { label: "policies", value: policyRefs, tone: policyRefs > 0 ? "cyan" : "neutral" },
      {
        label: "signed",
        value: payload.signature_present === true ? "yes" : "no",
        tone: payload.signature_present === true ? "emerald" : "red",
      },
    ];
  }
  if (evidenceKind === "attempt_scored") {
    return [
      { label: "attempt", value: stringFrom(payload.attempt_id) || stringFrom(payload.id) || "-", tone: "cyan" },
      { label: "score", value: firstNumberLabel([payload.score, payload.total_score]), tone: "emerald" },
      { label: "cost", value: firstNumberLabel([payload.cost_cents, payload.cost]), tone: "neutral" },
    ];
  }
  if (evidenceKind === "kernel_evolution") {
    const passed = payload.passed === true;
    return [
      { label: "variants", value: firstNumberLabel([payload.variant_count]), tone: "cyan" },
      { label: "winner", value: stringFrom(payload.winner_variant_id) || "-", tone: passed ? "emerald" : "neutral" },
      { label: "status", value: passed ? "proposal" : "no proposal", tone: passed ? "emerald" : "amber" },
    ];
  }
  if (evidenceKind === "kernel_evolution_proposal") {
    const draft = asRecord(payload.draft);
    const lineage = asRecord(payload.lineage);
    return [
      { label: "draft", value: stringFrom(draft.status) || "disabled", tone: "amber" },
      { label: "variant", value: stringFrom(payload.winner_variant_id) || stringFrom(lineage.variant_id) || "-", tone: "cyan" },
      { label: "score", value: firstNumberLabel([payload.score]), tone: "emerald" },
    ];
  }
  if (evidenceKind === "kernel_evolution_replay") {
    const passed = payload.replay_passed === true;
    return [
      { label: "replay", value: passed ? "matched" : "mismatch", tone: passed ? "emerald" : "amber" },
      { label: "apply", value: payload.active_apply_enabled === true ? "enabled" : "disabled", tone: payload.active_apply_enabled === true ? "red" : "emerald" },
    ];
  }
  return [
    { label: "event", value: eventType, tone: "cyan" },
    { label: "status", value: stringFrom(payload.status) || "-", tone: "neutral" },
  ];
}

function defaultSignalTitle(
  eventType: string,
  evidenceKind: string,
  payload: Record<string, unknown>,
): string {
  if (evidenceKind === "invariant_failure") {
    const invariant = stringFrom(payload.invariant_id) || stringFrom(payload.id);
    return invariant ? `Invariant failed: ${invariant}` : "Invariant failed";
  }
  if (evidenceKind === "policy_decision") {
    const decision = stringFrom(payload.decision);
    return decision ? `Policy decision: ${decision}` : "Policy decision";
  }
  if (evidenceKind === "kernel_evolution") return "Kernel evolution experiment";
  if (evidenceKind === "kernel_evolution_proposal") return "Kernel evolution proposal";
  if (evidenceKind === "kernel_evolution_replay") return "Kernel evolution replay";
  return eventType.replace(/_/g, " ");
}

function policyDecisionTone(decision: string): ChatEvidenceSignalStat["tone"] {
  if (decision === "allow") return "emerald";
  if (decision === "narrow" || decision === "blocked") return "amber";
  if (decision === "deny") return "red";
  return "neutral";
}

function sourceLabel(source: Record<string, unknown>): string | null {
  const kind = stringFrom(source.kind);
  if (!kind) return null;
  if (kind === "dag_node") {
    const node = stringFrom(source.node_id);
    const agent = stringFrom(source.agent);
    return [agent, node].filter(Boolean).join(" · ") || "DAG node";
  }
  if (kind === "review_loop") {
    return stringFrom(source.agent) || stringFrom(source.job_id) || "review loop";
  }
  if (kind === "kernel_evolution") {
    return stringFrom(source.experiment_id) || stringFrom(source.job_id) || "kernel evolution";
  }
  return kind;
}

function firstNumberLabel(values: unknown[]): string | number {
  for (const value of values) {
    const number = numberOrNull(value);
    if (number !== null) return formatNumber(number);
  }
  return "-";
}

function numberOrDash(value: unknown): string | number {
  const number = numberOrNull(value);
  return number === null ? "-" : formatNumber(number);
}

function formatNumber(value: number): string | number {
  if (!Number.isFinite(value)) return "-";
  if (Math.abs(value) >= 100) return Math.round(value);
  if (Number.isInteger(value)) return value;
  return value.toFixed(2);
}

function numberOrNull(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function stringFrom(value: unknown): string | null {
  if (typeof value !== "string" && typeof value !== "number") return null;
  const text = String(value).trim();
  return text ? text : null;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}
