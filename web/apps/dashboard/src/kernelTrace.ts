type KernelTraceLane = {
  scenarioId: string;
  title: string | null;
  passed: boolean;
  eventCount: number;
  invariantCount: number;
  invariantPassCount: number;
  invariantFailCount: number;
  replayPassed: boolean;
  alerts: string[];
  violations: string[];
  invariants: KernelTraceInvariant[];
  policies: KernelTracePolicyDecision[];
  timeline: KernelTraceTimelineEvent[];
  omittedTimelineCount: number;
};

type KernelTraceInvariant = {
  id: string;
  passed: boolean;
  message: string | null;
};

type KernelTracePolicyDecision = {
  id: string;
  decision: string;
  effect: string | null;
  resource: string | null;
  reason: string | null;
  signed: boolean;
  policyRefs: string[];
};

type KernelTraceTimelineEvent = {
  seq: number | null;
  eventType: string;
  actor: string | null;
  summary: string;
  tone: "cyan" | "emerald" | "amber" | "red" | "neutral";
};

type KernelTraceRuntimeGate = {
  allowed: boolean | null;
  activeApplyEnabled: boolean | null;
  reason: string | null;
  missingGates: string[];
  satisfiedGates: string[];
};

export type KernelTraceView = {
  title: string;
  protocolId: string | null;
  protocolVersion: string | null;
  riskClass: string | null;
  passed: boolean;
  scenarioCount: number;
  invariantPassCount: number;
  invariantFailCount: number;
  replayPassCount: number;
  eventCount: number;
  alertCount: number;
  violationCount: number;
  alerts: string[];
  violations: string[];
  traces: KernelTraceLane[];
  totalTraceCount: number;
  omittedTraceCount: number;
  policies: KernelTracePolicyDecision[];
  runtimeGate: KernelTraceRuntimeGate | null;
};

const DEFAULT_TRACE_LIMIT = 8;

export function parseKernelTraceView(
  value: unknown,
  options: { traceLimit?: number } = {},
): KernelTraceView | null {
  const traceLimit = Math.max(1, options.traceLimit ?? DEFAULT_TRACE_LIMIT);
  const root = asRecord(value);
  if (Object.keys(root).length === 0) return null;

  const summary = firstRecord([
    root.trace_summary,
    root.scenario_trace_summary,
    asRecord(root.metadata).trace_summary,
    asRecord(root.metadata).scenario_trace_summary,
    asRecord(root.payload).trace_summary,
    asRecord(root.payload).scenario_trace_summary,
    asRecord(root.result).trace_summary,
    asRecord(root.result).scenario_trace_summary,
  ]);
  const protocolClass = firstRecord([
    root.protocol_class,
    summary.protocol_class,
    asRecord(root.metadata).protocol_class,
    asRecord(root.payload).protocol_class,
  ]);
  const protocolRef = firstRecord([
    root.protocol_ref,
    summary.protocol_ref,
    protocolClass.protocol_ref,
    asRecord(root.metadata).protocol_ref,
  ]);
  const rawTraces = collectTraceRecords(root);
  const lanes = rawTraces
    .map(toTraceLane)
    .filter((trace): trace is KernelTraceLane => trace !== null);
  const visibleTraces = lanes.slice(0, traceLimit);
  const policies = uniquePolicies([
    ...collectPolicyDecisions(root),
    ...rawTraces.flatMap(collectPolicyDecisions),
  ]);

  const scenarioCount = numberFrom(summary.scenario_count) || lanes.length;
  const invariantPassCount =
    numberFrom(summary.invariant_pass_count) ||
    lanes.reduce((total, lane) => total + lane.invariantPassCount, 0);
  const invariantFailCount =
    numberFrom(summary.invariant_fail_count) ||
    lanes.reduce((total, lane) => total + lane.invariantFailCount, 0);
  const replayPassCount =
    numberFrom(summary.replay_pass_count) ||
    lanes.filter((lane) => lane.replayPassed).length;
  const alerts = uniqueText([
    ...textArray(summary.alerts),
    ...lanes.flatMap((lane) => lane.alerts),
  ]);
  const violations = uniqueText([
    ...textArray(summary.violations),
    ...textArray(summary.blocked_attempts),
    ...lanes.flatMap((lane) => lane.violations),
  ]);
  const eventCount =
    numberFrom(summary.event_count) ||
    numberFrom(summary.scenario_event_count) ||
    lanes.reduce((total, lane) => total + lane.eventCount, 0);
  const hasKernelEvidence =
    scenarioCount > 0 ||
    lanes.length > 0 ||
    invariantPassCount > 0 ||
    invariantFailCount > 0 ||
    alerts.length > 0 ||
    violations.length > 0;
  if (!hasKernelEvidence) return null;

  const explicitPassed = booleanFrom(summary.passed);
  const passed =
    explicitPassed ??
    (scenarioCount > 0 &&
      invariantFailCount === 0 &&
      lanes.every((lane) => lane.passed));
  const protocolId =
    stringFrom(summary.protocol_id) ||
    stringFrom(protocolRef.protocol_id) ||
    stringFrom(protocolClass.protocol_id);
  const protocolVersion =
    stringFrom(summary.protocol_version) ||
    stringFrom(protocolRef.version) ||
    stringFrom(protocolClass.version);
  const displayName =
    stringFrom(summary.display_name) ||
    stringFrom(protocolClass.display_name) ||
    stringFrom(protocolId);

  return {
    title: displayName ? `${displayName} proof packet` : "Kernel proof packet",
    protocolId,
    protocolVersion,
    riskClass: stringFrom(summary.risk_class) || stringFrom(protocolClass.risk_class),
    passed,
    scenarioCount,
    invariantPassCount,
    invariantFailCount,
    replayPassCount,
    eventCount,
    alertCount: numberFrom(summary.alert_count) || alerts.length,
    violationCount:
      numberFrom(summary.violation_count) ||
      numberFrom(summary.blocked_attempt_count) ||
      violations.length,
    alerts,
    violations,
    traces: visibleTraces,
    totalTraceCount: lanes.length,
    omittedTraceCount: Math.max(0, lanes.length - visibleTraces.length),
    policies,
    runtimeGate: parseRuntimeGate(root),
  };
}

export function findKernelTraceView(
  values: unknown[],
  options: { traceLimit?: number } = {},
): KernelTraceView | null {
  for (const value of values) {
    const trace = parseKernelTraceView(value, options);
    if (trace) return trace;
  }
  return null;
}

function collectTraceRecords(root: Record<string, unknown>): Record<string, unknown>[] {
  const candidates: unknown[] = [
    root.traces,
    root.scenario_traces,
    root.scenario_trace,
    asRecord(root.trace).traces,
    asRecord(root.payload).traces,
    asRecord(root.payload).scenario_traces,
    asRecord(root.result).traces,
    asRecord(root.result).scenario_traces,
  ];

  for (const event of asArray(root.events)) {
    const record = asRecord(event);
    if (record.type === "scenario_trace_recorded" || record.event_type === "scenario_trace_recorded") {
      candidates.push(asRecord(record.payload).traces);
      candidates.push(asRecord(record.payload).scenario_traces);
    }
  }
  if (root.type === "scenario_trace_recorded" || root.event_type === "scenario_trace_recorded") {
    candidates.push(asRecord(root.payload).traces);
    candidates.push(asRecord(root.payload).scenario_traces);
  }

  return candidates
    .flatMap((candidate) => {
      if (Array.isArray(candidate)) return candidate;
      if (asRecord(candidate).scenario_id) return [candidate];
      return [];
    })
    .map(asRecord)
    .filter((trace) => Boolean(stringFrom(trace.scenario_id)));
}

function toTraceLane(trace: Record<string, unknown>): KernelTraceLane | null {
  const scenarioId = stringFrom(trace.scenario_id);
  if (!scenarioId) return null;
  const invariantResults = asArray(trace.invariant_results).map(asRecord);
  const invariants = invariantResults
    .map(toInvariant)
    .filter((item): item is KernelTraceInvariant => item !== null);
  const invariantPassCount = invariantResults.filter((item) => booleanFrom(item.passed) === true).length;
  const invariantFailCount = invariantResults.filter((item) => booleanFrom(item.passed) === false).length;
  const timeline = asArray(trace.events)
    .map(asRecord)
    .map(toTimelineEvent)
    .filter((item): item is KernelTraceTimelineEvent => item !== null);
  const visibleTimeline = timeline.slice(0, 10);
  const replayPassed =
    booleanFrom(trace.replay_passed) ??
    booleanFrom(trace.replay_consistent) ??
    booleanFrom(asRecord(trace.replay).passed) ??
    false;
  return {
    scenarioId,
    title: stringFrom(trace.title),
    passed: booleanFrom(trace.passed) ?? invariantFailCount === 0,
    eventCount: asArray(trace.events).length || numberFrom(trace.event_count),
    invariantCount: invariantResults.length || numberFrom(trace.invariant_count),
    invariantPassCount,
    invariantFailCount,
    replayPassed,
    alerts: textArray(trace.alerts),
    violations: uniqueText([
      ...textArray(trace.violations),
      ...textArray(trace.blocked_attempts),
    ]),
    invariants,
    policies: collectPolicyDecisions(trace),
    timeline: visibleTimeline,
    omittedTimelineCount: Math.max(0, timeline.length - visibleTimeline.length),
  };
}

function toInvariant(record: Record<string, unknown>): KernelTraceInvariant | null {
  const id =
    stringFrom(record.invariant_id) ||
    stringFrom(record.id) ||
    stringFrom(record.name);
  if (!id) return null;
  return {
    id,
    passed: booleanFrom(record.passed) ?? stringFrom(record.status) !== "failed",
    message:
      stringFrom(record.message) ||
      stringFrom(record.reason) ||
      stringFrom(record.detail),
  };
}

function collectPolicyDecisions(root: Record<string, unknown>): KernelTracePolicyDecision[] {
  const direct = asArray(root.policy_decisions);
  const fromEvents = asArray(root.events)
    .map(asRecord)
    .filter((event) => stringFrom(event.event_type) === "policy.checked")
    .map((event) => asRecord(event.payload));
  return uniquePolicies(
    [...direct, ...fromEvents]
      .map(asRecord)
      .map(toPolicyDecision)
      .filter((item): item is KernelTracePolicyDecision => item !== null),
  );
}

function toPolicyDecision(record: Record<string, unknown>): KernelTracePolicyDecision | null {
  const decisionRecord = firstRecord([
    record.redacted_decision,
    record.signed_decision,
    asRecord(record.decision).redacted_decision,
    asRecord(record.decision).signed_decision,
    record.decision,
    record,
  ]);
  const id =
    stringFrom(record.decision_id) ||
    stringFrom(decisionRecord.decision_id);
  const decision =
    stringFrom(decisionRecord.decision) ||
    stringFrom(record.decision) ||
    stringFrom(decisionRecord.effect) ||
    stringFrom(record.effect);
  if (!id && !decision) return null;
  return {
    id: id || decision || "policy",
    decision: decision || "unknown",
    effect: stringFrom(decisionRecord.effect) || stringFrom(record.effect),
    resource: stringFrom(decisionRecord.resource) || stringFrom(record.resource),
    reason: stringFrom(decisionRecord.reason) || stringFrom(record.reason),
    signed:
      booleanFrom(decisionRecord.signature_present) ??
      Boolean(stringFrom(decisionRecord.signature)),
    policyRefs: textArray(decisionRecord.policy_refs).length
      ? textArray(decisionRecord.policy_refs)
      : textArray(record.policy_refs),
  };
}

function toTimelineEvent(event: Record<string, unknown>): KernelTraceTimelineEvent | null {
  const eventType = stringFrom(event.event_type);
  if (!eventType) return null;
  const payload = asRecord(event.payload);
  const seq = numberOrNull(event.seq);
  const actor = stringFrom(event.actor_node_id);
  const decision = stringFrom(payload.decision) || stringFrom(asRecord(payload.decision).decision);
  const resource = stringFrom(payload.resource);
  const summary =
    summaryForEvent(eventType, payload) ||
    resource ||
    textArray(event.target_refs).slice(0, 2).join(" -> ") ||
    actor ||
    eventType;
  return {
    seq,
    eventType,
    actor,
    summary,
    tone: toneForEvent(eventType, decision),
  };
}

function summaryForEvent(eventType: string, payload: Record<string, unknown>): string | null {
  if (eventType === "policy.checked") {
    const decision = stringFrom(payload.decision) || stringFrom(asRecord(payload.decision).decision) || "checked";
    const resource = stringFrom(payload.resource);
    return resource ? `${decision} ${resource}` : decision;
  }
  if (eventType === "route.selected") {
    const node = stringFrom(payload.node_id);
    const skill = stringFrom(payload.skill);
    const score = numberOrNull(payload.score);
    return [node, skill, score === null ? null : `score ${formatNumber(score)}`]
      .filter(Boolean)
      .join(" / ") || null;
  }
  if (eventType === "signal.emitted") {
    const signal = stringFrom(payload.signal_type);
    const score = numberOrNull(payload.score);
    return [signal, score === null ? null : `score ${formatNumber(score)}`].filter(Boolean).join(" / ") || null;
  }
  if (eventType === "edge.proposed" || eventType === "edge.created") {
    const from = stringFrom(asRecord(payload.from).node_id);
    const to = stringFrom(asRecord(payload.to).node_id);
    return from && to ? `${from} -> ${to}` : stringFrom(payload.edge_id);
  }
  if (eventType === "edge.used") return stringFrom(payload.edge_id);
  if (eventType === "capability.minted" || eventType === "capability.delegated") {
    const owner = stringFrom(payload.owner);
    const cap = stringFrom(payload.cap_id);
    const budget = numberOrNull(asRecord(payload.capability).budget);
    return [owner, cap, budget === null ? null : `budget ${formatNumber(budget)}`].filter(Boolean).join(" / ") || null;
  }
  if (eventType === "capability.used") {
    const resource = stringFrom(payload.resource);
    const action = stringFrom(payload.action);
    return [action, resource].filter(Boolean).join(" ") || null;
  }
  if (eventType === "process.started" || eventType === "process.stopped") {
    return stringFrom(payload.process_id) || stringFrom(payload.reason);
  }
  if (eventType === "node.created") return stringFrom(payload.node_id);
  if (eventType === "port.created") {
    const node = stringFrom(payload.node_id);
    const port = stringFrom(payload.port_id);
    return node && port ? `${node}:${port}` : node || port;
  }
  return null;
}

function toneForEvent(
  eventType: string,
  decision: string | null,
): KernelTraceTimelineEvent["tone"] {
  if (decision === "deny" || decision === "blocked") return "red";
  if (eventType === "policy.checked" || eventType === "route.selected") return "emerald";
  if (eventType.startsWith("capability.")) return "cyan";
  if (eventType === "signal.emitted") return "amber";
  if (eventType === "process.stopped") return "neutral";
  return "cyan";
}

function parseRuntimeGate(root: Record<string, unknown>): KernelTraceRuntimeGate | null {
  const gate = firstRecord([
    root.runtime_readiness,
    asRecord(root.payload).runtime_readiness,
    asRecord(root.result).runtime_readiness,
  ]);
  if (Object.keys(gate).length === 0) return null;
  return {
    allowed: booleanFrom(gate.allowed),
    activeApplyEnabled: booleanFrom(gate.active_apply_enabled),
    reason: stringFrom(gate.reason),
    missingGates: textArray(gate.missing_gates),
    satisfiedGates: textArray(gate.satisfied_gates),
  };
}

function uniquePolicies(values: KernelTracePolicyDecision[]): KernelTracePolicyDecision[] {
  const seen = new Set<string>();
  return values.filter((value) => {
    const key = `${value.id}:${value.resource || ""}:${value.decision}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function firstRecord(values: unknown[]): Record<string, unknown> {
  for (const value of values) {
    const record = asRecord(value);
    if (Object.keys(record).length > 0) return record;
  }
  return {};
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function textArray(value: unknown): string[] {
  return asArray(value).map((item) => String(item)).filter(Boolean);
}

function uniqueText(values: string[]): string[] {
  return Array.from(new Set(values.map((value) => value.trim()).filter(Boolean)));
}

function numberFrom(value: unknown): number {
  const parsed = Number(value || 0);
  return Number.isFinite(parsed) ? parsed : 0;
}

function numberOrNull(value: unknown): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function formatNumber(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(2);
}

function booleanFrom(value: unknown): boolean | null {
  if (typeof value === "boolean") return value;
  if (value === "true") return true;
  if (value === "false") return false;
  return null;
}

function stringFrom(value: unknown): string | null {
  if (typeof value !== "string" && typeof value !== "number") return null;
  const text = String(value).trim();
  return text ? text : null;
}
