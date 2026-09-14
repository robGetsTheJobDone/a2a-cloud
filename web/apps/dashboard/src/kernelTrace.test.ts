import { describe, expect, it } from "vitest";
import { findKernelTraceView, parseKernelTraceView } from "./kernelTrace";

describe("parseKernelTraceView", () => {
  it("returns null for empty or unrelated payloads", () => {
    expect(parseKernelTraceView(null)).toBeNull();
    expect(parseKernelTraceView({ status: "complete", summary: "done" })).toBeNull();
  });

  it("normalizes a summary plus raw traces", () => {
    const trace = parseKernelTraceView({
      trace_summary: {
        protocol_id: "graph_kernel",
        protocol_version: 1,
        scenario_count: 2,
        invariant_pass_count: 3,
        invariant_fail_count: 0,
        replay_pass_count: 2,
        passed: true,
        alerts: ["budget_near_limit"],
      },
      traces: [
        {
          scenario_id: "S1",
          title: "cooperation",
          passed: true,
          replay_passed: true,
          events: [{ type: "edge_added" }],
          invariant_results: [{ invariant_id: "no_widen", passed: true }],
        },
      ],
    });

    expect(trace).not.toBeNull();
    expect(trace?.protocolId).toBe("graph_kernel");
    expect(trace?.scenarioCount).toBe(2);
    expect(trace?.invariantPassCount).toBe(3);
    expect(trace?.replayPassCount).toBe(2);
    expect(trace?.passed).toBe(true);
    expect(trace?.traces[0]?.scenarioId).toBe("S1");
    expect(trace?.alerts).toEqual(["budget_near_limit"]);
  });

  it("surfaces failed invariants and blocked attempts", () => {
    const trace = parseKernelTraceView({
      traces: [
        {
          scenario_id: "arena-1",
          passed: false,
          invariant_results: [
            { invariant_id: "bounded_authority", passed: false },
            { invariant_id: "audit_written", passed: true },
          ],
          violations: ["capability_widening"],
        },
      ],
    });

    expect(trace?.passed).toBe(false);
    expect(trace?.invariantFailCount).toBe(1);
    expect(trace?.violationCount).toBe(1);
    expect(trace?.violations).toEqual(["capability_widening"]);
    expect(trace?.traces[0]?.invariantFailCount).toBe(1);
  });

  it("collects traces from scenario_trace_recorded event envelopes", () => {
    const trace = parseKernelTraceView({
      metadata: {
        trace_summary: {
          display_name: "Market allocation",
          scenario_count: 1,
          invariant_pass_count: 1,
        },
      },
      events: [
        {
          type: "scenario_trace_recorded",
          payload: {
            traces: [
              {
                scenario_id: "market-clear",
                passed: true,
                replay_consistent: true,
                invariant_results: [{ passed: true }],
              },
            ],
          },
        },
      ],
    });

    expect(trace?.title).toBe("Market allocation proof packet");
    expect(trace?.traces).toHaveLength(1);
    expect(trace?.traces[0]?.replayPassed).toBe(true);
  });

  it("normalizes scenario summaries from evidence event payloads", () => {
    const trace = parseKernelTraceView({
      payload: {
        scenario_trace_summary: {
          protocol_id: "payload_kernel",
          scenario_count: 2,
          invariant_pass_count: 4,
          invariant_fail_count: 0,
          replay_pass_count: 2,
        },
      },
    });

    expect(trace?.protocolId).toBe("payload_kernel");
    expect(trace?.scenarioCount).toBe(2);
    expect(trace?.replayPassCount).toBe(2);
  });

  it("caps high-volume trace lanes and reports omitted scenarios", () => {
    const trace = parseKernelTraceView(
      {
        traces: Array.from({ length: 12 }, (_, index) => ({
          scenario_id: `S${index + 1}`,
          passed: true,
        })),
      },
      { traceLimit: 5 },
    );

    expect(trace?.totalTraceCount).toBe(12);
    expect(trace?.traces).toHaveLength(5);
    expect(trace?.omittedTraceCount).toBe(7);
  });

  it("ignores malformed trace arrays without throwing", () => {
    const trace = parseKernelTraceView({
      trace_summary: {
        scenario_count: "bad-number",
        alerts: "not-an-array",
      },
      traces: [
        null,
        "broken",
        { scenario_id: "" },
        { scenario_id: "valid", invariant_results: "broken" },
      ],
    });

    expect(trace?.scenarioCount).toBe(1);
    expect(trace?.alerts).toEqual([]);
    expect(trace?.traces).toHaveLength(1);
  });

  it("keeps detailed kernel trace evidence for chat cards", () => {
    const trace = parseKernelTraceView({
      trace_summary: {
        scenario_count: 1,
        invariant_pass_count: 2,
        replay_pass_count: 1,
        event_count: 6,
        passed: true,
      },
      runtime_readiness: {
        allowed: false,
        active_apply_enabled: false,
        reason: "active runtime blocked by safety gates",
        missing_gates: ["owner_approved"],
        satisfied_gates: ["simulation_passed"],
      },
      policy_decisions: [
        {
          decision_id: "pd-bid",
          decision: "allow",
          effect: "allow",
          resource: "skill:bid",
          reason: "all matching policies allow",
          redacted_decision: {
            signature_present: true,
            policy_refs: ["owner-allow-call"],
          },
        },
      ],
      traces: [
        {
          scenario_id: "competitive-allocation-drill",
          title: "Competitive allocation drill",
          passed: true,
          replay_passed: true,
          invariant_results: [
            { invariant_id: "no_violations", passed: true },
            { invariant_id: "process_absent", passed: true },
          ],
          policy_decisions: [
            {
              decision_id: "pd-edge-bidder-a",
              decision: "allow",
              resource: "bidder-a:invoke:bid",
              signature_present: true,
              policy_refs: ["owner-allow-call"],
            },
          ],
          events: [
            {
              seq: 12,
              event_type: "policy.checked",
              actor_node_id: "policy",
              payload: {
                decision_id: "pd-edge-bidder-a",
                decision: { decision: "allow" },
                resource: "bidder-a:invoke:bid",
              },
            },
            {
              seq: 23,
              event_type: "signal.emitted",
              actor_node_id: "bidder-a",
              payload: { signal_type: "success", score: 1 },
            },
            {
              seq: 27,
              event_type: "route.selected",
              actor_node_id: "market",
              payload: { node_id: "bidder-a", skill: "bid", score: 10 },
            },
          ],
        },
      ],
    });

    expect(trace?.runtimeGate?.reason).toBe("active runtime blocked by safety gates");
    expect(trace?.runtimeGate?.missingGates).toEqual(["owner_approved"]);
    expect(trace?.policies[0]).toMatchObject({
      id: "pd-bid",
      decision: "allow",
      resource: "skill:bid",
      signed: true,
    });
    expect(trace?.traces[0]?.invariants.map((item) => item.id)).toEqual([
      "no_violations",
      "process_absent",
    ]);
    expect(trace?.traces[0]?.policies[0]).toMatchObject({
      id: "pd-edge-bidder-a",
      resource: "bidder-a:invoke:bid",
    });
    expect(trace?.traces[0]?.timeline.map((event) => event.summary)).toEqual([
      "allow bidder-a:invoke:bid",
      "success / score 1",
      "bidder-a / bid / score 10",
    ]);
  });
});

describe("findKernelTraceView", () => {
  it("returns the first parseable kernel payload", () => {
    const trace = findKernelTraceView([
      { status: "complete" },
      {
        trace_summary: { scenario_count: 1, invariant_pass_count: 1 },
      },
      {
        traces: [{ scenario_id: "later" }],
      },
    ]);

    expect(trace?.scenarioCount).toBe(1);
    expect(trace?.totalTraceCount).toBe(0);
  });
});
