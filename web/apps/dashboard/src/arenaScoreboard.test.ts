import { describe, expect, it } from "vitest";
import {
  findArenaScoreboardView,
  parseArenaScoreboardView,
  type BackendArenaSuiteRecordedPayload,
} from "./arenaScoreboard";

const scoreboard = {
  participants: [
    {
      participant_id: "alpha",
      episodes: ["round-1", "round-2"],
      wins: 2,
      losses: 0,
      exclusions: 0,
      average_score: 14,
      max_score: 18,
      total_score: 28,
      total_cost: 4,
      budget_efficiency: 0.5,
      evidence_refs: ["evt-1", "evt-2"],
    },
    {
      participant_id: "beta",
      episodes: ["round-1", "round-2"],
      wins: 0,
      losses: 1,
      exclusions: 1,
      exclusion_reasons: { participant_frozen: 1 },
      average_score: 16,
      max_score: 20,
      total_score: 32,
      total_cost: 3,
      budget_efficiency: 0,
      evidence_refs: ["evt-3"],
    },
  ],
  winner_events: [{ event_id: "evt-w1" }, { event_id: "evt-w2" }],
  rejected_winner_events: [],
  episode_count: 2,
  passed_episode_count: 2,
  failed_episode_count: 0,
  invariant_failures: [],
  active_apply_enabled: false,
};

const backendArenaSuitePayload = {
  target_agent: "suite-agent",
  protocol_ref: { id: "custom_kernel_suite", version: 1 },
  template_ref: "custom_kernel_suite@v1",
  template_id: "arena_suite@v1",
  suite_id: "arena-suite-drill",
  title: "Arena suite scoreboard drill",
  passed: true,
  episode_count: 2,
  trace_summary: { scenario_count: 2, invariant_fail_count: 0 },
  runtime_readiness: { allowed: false, active_apply_enabled: false },
  episodes: [
    {
      episode_id: "round-1",
      title: "Arena round 1",
      template_id: "arena_outcome@v1",
      passed: true,
      simulation_only: true,
      proposal_only: true,
      active_apply_enabled: false,
    },
  ],
  scoreboard: {
    participants: [
      {
        participant_id: "alpha",
        episodes: ["round-1", "round-2"],
        outcome_count: 2,
        score_count: 2,
        total_score: 28,
        average_score: 14,
        max_score: 18,
        wins: 2,
        losses: 0,
        exclusions: 0,
        exclusion_reasons: {},
        total_cost: 4,
        budget_efficiency: 0.5,
        outcome_refs: ["evt-1"],
        score_refs: ["evt-2"],
        winner_refs: ["evt-3"],
        exclusion_refs: [],
        evidence_refs: ["evt-1", "evt-2", "evt-3"],
      },
    ],
    winner_events: [
      {
        episode_id: "round-1",
        event_id: "evt-3",
        arena_id: "arena-1",
        winner_id: "alpha",
        score_id: "score-alpha",
        score: 18,
        eligible_candidates: [{ participant_id: "alpha", score_id: "score-alpha", score: 18 }],
        excluded_candidates: [],
      },
    ],
    rejected_winner_events: [],
    episode_count: 2,
    passed_episode_count: 2,
    failed_episode_count: 0,
    invariant_failures: [],
    simulation_only: true,
    proposal_only: true,
    active_apply_enabled: false,
  },
  alerts: [],
  violations: [],
  cost_cents: 21,
  simulation_only: true,
  proposal_only: true,
  active_apply_enabled: false,
} satisfies BackendArenaSuiteRecordedPayload;

describe("parseArenaScoreboardView", () => {
  it("normalizes arena suite scoreboard payloads", () => {
    const view = parseArenaScoreboardView({
      suite_id: "arena-suite",
      title: "Arena Suite",
      passed: true,
      episode_count: 2,
      scoreboard,
    });

    expect(view?.suiteId).toBe("arena-suite");
    expect(view?.passed).toBe(true);
    expect(view?.episodeCount).toBe(2);
    expect(view?.winnerEventCount).toBe(2);
    expect(view?.activeApplyEnabled).toBe(false);
    expect(view?.participants[0]?.participantId).toBe("alpha");
    expect(view?.participants[0]?.wins).toBe(2);
    expect(view?.participants[1]?.exclusionReasons).toEqual({ participant_frozen: 1 });
  });

  it("normalizes the backend typed arena suite recorded payload contract", () => {
    const view = parseArenaScoreboardView(backendArenaSuitePayload);

    expect(view?.suiteId).toBe("arena-suite-drill");
    expect(view?.episodeCount).toBe(2);
    expect(view?.passedEpisodeCount).toBe(2);
    expect(view?.participants[0]?.participantId).toBe("alpha");
    expect(view?.participants[0]?.outcomeCount).toBe(2);
    expect(view?.winnerEventCount).toBe(1);
    expect(view?.activeApplyEnabled).toBe(false);
  });

  it("collects scoreboard from arena suite event envelopes", () => {
    const view = findArenaScoreboardView([
      { job: { status: "complete" } },
      {
        events: [
          {
            event_type: "arena_suite_recorded",
            payload: {
              suite_id: "suite-from-event",
              title: "Event suite",
              scoreboard,
            },
          },
        ],
      },
    ]);

    expect(view?.suiteId).toBe("suite-from-event");
    expect(view?.participants).toHaveLength(2);
  });

  it("normalizes summary-only evidence timeline rows", () => {
    const view = parseArenaScoreboardView({
      label: "arena_suite_recorded",
      payload: {
        arena_suite_summary: {
          suite_id: "suite-summary",
          title: "Historical suite",
          passed: true,
          episode_count: 3,
          winner_count: 3,
          exclusion_count: 2,
          invariant_failure_count: 0,
          active_apply_enabled: false,
        },
      },
    });

    expect(view?.suiteId).toBe("suite-summary");
    expect(view?.title).toBe("Historical suite");
    expect(view?.episodeCount).toBe(3);
    expect(view?.winnerEventCount).toBe(3);
    expect(view?.participants).toEqual([]);
  });

  it("keeps explicit zero counts from detailed scoreboards before summary fallbacks", () => {
    const view = parseArenaScoreboardView({
      payload: {
        arena_suite_summary: {
          failed_episode_count: 4,
          invariant_failure_count: 3,
          rejected_winner_count: 2,
          winner_count: 5,
        },
        scoreboard: {
          participants: [{ participant_id: "alpha" }],
          failed_episode_count: 0,
          invariant_failures: [],
          rejected_winner_events: [],
          winner_events: [],
        },
      },
    });

    expect(view?.failedEpisodeCount).toBe(0);
    expect(view?.invariantFailureCount).toBe(0);
    expect(view?.rejectedWinnerEventCount).toBe(0);
    expect(view?.winnerEventCount).toBe(0);
  });

  it("surfaces failed suite counts", () => {
    const view = parseArenaScoreboardView({
      payload: {
        passed: false,
        scoreboard: {
          ...scoreboard,
          failed_episode_count: 1,
          invariant_failures: [{ invariant_id: "winner_selected" }],
        },
      },
    });

    expect(view?.passed).toBe(false);
    expect(view?.failedEpisodeCount).toBe(1);
    expect(view?.invariantFailureCount).toBe(1);
  });

  it("returns null for unrelated or malformed payloads", () => {
    expect(parseArenaScoreboardView(null)).toBeNull();
    expect(parseArenaScoreboardView({ scoreboard: { participants: [] } })).toBeNull();
    expect(findArenaScoreboardView([{ events: [{ event_type: "scenario_trace_recorded" }] }])).toBeNull();
  });
});
