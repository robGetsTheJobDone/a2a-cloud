import { describe, expect, it } from "vitest";
import { findChatEvidenceCards, parseChatEvidenceCards } from "./chatEvidence";

describe("parseChatEvidenceCards", () => {
  it("normalizes combined arena suite and kernel trace payloads", () => {
    const cards = parseChatEvidenceCards({
      payload: {
        arena_suite_summary: {
          suite_id: "arena-suite",
          title: "Arena drill",
          passed: false,
          episode_count: 3,
          failed_episode_count: 1,
          winner_count: 2,
          invariant_failure_count: 1,
        },
        scenario_trace_summary: {
          protocol_id: "market_kernel",
          scenario_count: 2,
          invariant_pass_count: 3,
          invariant_fail_count: 1,
          replay_pass_count: 1,
          violations: ["winner_without_bid"],
        },
      },
    });

    expect(cards).toHaveLength(2);
    expect(cards[0]?.kind).toBe("arena_suite");
    expect(cards[1]?.kind).toBe("kernel_trace");
  });

  it("finds one arena card and one trace card across event history", () => {
    const cards = findChatEvidenceCards([
      { event_type: "noop", payload: { status: "ok" } },
      {
        event_type: "arena_suite_recorded",
        payload: {
          arena_suite_summary: {
            suite_id: "summary-only",
            passed: true,
            episode_count: 2,
            winner_count: 2,
          },
        },
      },
      {
        event_type: "scenario_trace_recorded",
        payload: {
          traces: [{ scenario_id: "S1", passed: true }],
        },
      },
    ]);

    expect(cards.map((card) => card.kind)).toEqual(["arena_suite", "kernel_trace"]);
  });

  it("returns no cards for unrelated or malformed payloads", () => {
    expect(parseChatEvidenceCards(null)).toEqual([]);
    expect(parseChatEvidenceCards({ payload: { score: "ok" } })).toEqual([]);
    expect(findChatEvidenceCards([{ events: ["bad"], scoreboard: { participants: [] } }])).toEqual([]);
  });

  it("normalizes live arena evidence event envelopes", () => {
    const cards = parseChatEvidenceCards({
      type: "evidence_event",
      evidence_key: "arena-key",
      evidence_kind: "arena_suite",
      event_type: "arena_suite_recorded",
      payload: {
        suite_id: "arena-live",
        passed: true,
        scoreboard: {
          participants: [{ participant_id: "alpha", wins: 1 }],
          winner_events: [{ event_id: "winner-1" }],
        },
      },
    });

    expect(cards).toHaveLength(1);
    expect(cards[0]?.kind).toBe("arena_suite");
    expect(cards[0]?.evidenceKey).toBe("arena-key");
  });

  it("normalizes live invariant failure evidence events as signal cards", () => {
    const cards = parseChatEvidenceCards({
      type: "evidence_event",
      evidence_key: "invariant-key",
      evidence_kind: "invariant_failure",
      event_type: "invariant_checked",
      title: "Invariant failed: budget",
      source: { kind: "review_loop", agent: "kernel-agent" },
      payload: {
        invariant_id: "budget",
        passed: false,
      },
    });

    expect(cards).toHaveLength(1);
    expect(cards[0]?.kind).toBe("kernel_signal");
    if (cards[0]?.kind !== "kernel_signal") throw new Error("expected signal card");
    expect(cards[0].evidenceKey).toBe("invariant-key");
    expect(cards[0].signal.title).toBe("Invariant failed: budget");
    expect(cards[0].signal.sourceLabel).toBe("kernel-agent");
    expect(cards[0].signal.stats[0]).toEqual({
      label: "invariant",
      value: "budget",
      tone: "red",
    });
  });

  it("normalizes protocol limit evidence events and ignores malformed live rows", () => {
    const cards = parseChatEvidenceCards({
      type: "evidence_event",
      evidence_key: "limit-key",
      evidence_kind: "protocol_limit",
      event_type: "simulation_episode_limit_exceeded",
      payload: {
        attempted: 4,
        episode_limit: 3,
      },
    });

    expect(cards).toHaveLength(1);
    expect(cards[0]?.kind).toBe("kernel_signal");
    if (cards[0]?.kind !== "kernel_signal") throw new Error("expected signal card");
    expect(cards[0].signal.stats.map((stat) => stat.value)).toEqual([
      "episode limit exceeded",
      4,
      3,
    ]);
    expect(parseChatEvidenceCards({ type: "evidence_event", payload: {} })).toEqual([]);
  });

  it("normalizes signed policy decision evidence events", () => {
    const cards = parseChatEvidenceCards({
      type: "evidence_event",
      evidence_key: "policy-key",
      evidence_kind: "policy_decision",
      event_type: "policy_decision_recorded",
      payload: {
        decision_id: "pd-deny",
        decision: "deny",
        effect: "deny",
        policy_refs: ["platform-deny", "owner-allow"],
        signature_present: true,
      },
    });

    expect(cards).toHaveLength(1);
    expect(cards[0]?.kind).toBe("kernel_signal");
    if (cards[0]?.kind !== "kernel_signal") throw new Error("expected signal card");
    expect(cards[0].signal.title).toBe("Policy decision: deny");
    expect(cards[0].signal.stats).toEqual([
      { label: "decision", value: "deny", tone: "red" },
      { label: "effect", value: "deny", tone: "red" },
      { label: "policies", value: 2, tone: "cyan" },
      { label: "signed", value: "yes", tone: "emerald" },
    ]);
  });

  it("normalizes kernel evolution evidence events", () => {
    const experimentCards = parseChatEvidenceCards({
      type: "evidence_event",
      evidence_key: "evolution-key",
      evidence_kind: "kernel_evolution",
      event_type: "evolution_experiment_recorded",
      source: { kind: "kernel_evolution", experiment_id: "evo-1" },
      payload: {
        experiment_id: "evo-1",
        variant_count: 3,
        winner_variant_id: "evo-1-v3",
        passed: true,
        active_apply_enabled: false,
      },
    });
    expect(experimentCards).toHaveLength(1);
    expect(experimentCards[0]?.kind).toBe("kernel_signal");
    if (experimentCards[0]?.kind !== "kernel_signal") throw new Error("expected signal card");
    expect(experimentCards[0].signal.title).toBe("Kernel evolution experiment");
    expect(experimentCards[0].signal.sourceLabel).toBe("evo-1");
    expect(experimentCards[0].signal.stats).toEqual([
      { label: "variants", value: 3, tone: "cyan" },
      { label: "winner", value: "evo-1-v3", tone: "emerald" },
      { label: "status", value: "proposal", tone: "emerald" },
    ]);

    const proposalCards = parseChatEvidenceCards({
      type: "evidence_event",
      evidence_kind: "kernel_evolution_proposal",
      event_type: "evolution_proposal_recorded",
      payload: {
        winner_variant_id: "evo-1-v3",
        score: 1042,
        draft: { status: "disabled" },
        active_apply_enabled: false,
      },
    });
    expect(proposalCards[0]?.kind).toBe("kernel_signal");
    if (proposalCards[0]?.kind !== "kernel_signal") throw new Error("expected signal card");
    expect(proposalCards[0].signal.stats).toEqual([
      { label: "draft", value: "disabled", tone: "amber" },
      { label: "variant", value: "evo-1-v3", tone: "cyan" },
      { label: "score", value: 1042, tone: "emerald" },
    ]);

    const replayCards = parseChatEvidenceCards({
      type: "evidence_event",
      evidence_kind: "kernel_evolution_replay",
      event_type: "evolution_replayed",
      payload: { replay_passed: false, active_apply_enabled: false },
    });
    expect(replayCards[0]?.kind).toBe("kernel_signal");
    if (replayCards[0]?.kind !== "kernel_signal") throw new Error("expected signal card");
    expect(replayCards[0].signal.stats).toEqual([
      { label: "replay", value: "mismatch", tone: "amber" },
      { label: "apply", value: "disabled", tone: "emerald" },
    ]);
  });
});
