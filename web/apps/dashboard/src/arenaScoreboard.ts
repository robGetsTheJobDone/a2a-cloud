type BackendArenaScoreboardParticipant = {
  participant_id: string;
  episodes: string[];
  outcome_count: number;
  score_count: number;
  total_score: number;
  average_score: number;
  max_score: number;
  wins: number;
  losses: number;
  exclusions: number;
  exclusion_reasons: Record<string, number>;
  total_cost: number;
  budget_efficiency: number;
  outcome_refs: string[];
  score_refs: string[];
  winner_refs: string[];
  exclusion_refs: string[];
  evidence_refs: string[];
};

type BackendArenaWinnerEvent = {
  episode_id: string;
  event_id: string;
  arena_id?: string;
  winner_id?: string;
  score_id?: string;
  score?: number;
  eligible_candidates?: Array<Record<string, unknown>>;
  excluded_candidates?: Array<Record<string, unknown>>;
  [key: string]: unknown;
};

type BackendArenaInvariantFailure = {
  episode_id: string;
  invariant_id: string;
  passed: false;
  details?: Record<string, unknown>;
  active_apply_enabled: false;
  [key: string]: unknown;
};

type BackendArenaScoreboardContract = {
  participants: BackendArenaScoreboardParticipant[];
  winner_events: BackendArenaWinnerEvent[];
  rejected_winner_events: BackendArenaWinnerEvent[];
  episode_count: number;
  passed_episode_count: number;
  failed_episode_count: number;
  invariant_failures: BackendArenaInvariantFailure[];
  simulation_only: true;
  proposal_only: true;
  active_apply_enabled: false;
};

export type BackendArenaSuiteRecordedPayload = {
  target_agent?: string;
  protocol_ref?: Record<string, unknown>;
  template_ref?: string;
  template_id?: string | null;
  suite_id: string;
  title: string;
  passed: boolean;
  episode_count: number;
  trace_summary?: Record<string, unknown>;
  runtime_readiness?: Record<string, unknown>;
  episodes?: Array<Record<string, unknown>>;
  scoreboard: BackendArenaScoreboardContract;
  alerts?: string[];
  violations?: string[];
  cost_cents?: number;
  simulation_only: true;
  proposal_only: true;
  active_apply_enabled: false;
};

export type ArenaScoreboardParticipant = {
  participantId: string;
  episodes: string[];
  outcomeCount: number;
  scoreCount: number;
  wins: number;
  losses: number;
  exclusions: number;
  exclusionReasons: Record<string, number>;
  averageScore: number;
  maxScore: number;
  totalScore: number;
  totalCost: number;
  budgetEfficiency: number;
  evidenceRefs: string[];
};

export type ArenaScoreboardView = {
  suiteId: string | null;
  title: string;
  passed: boolean;
  episodeCount: number;
  passedEpisodeCount: number;
  failedEpisodeCount: number;
  invariantFailureCount: number;
  participants: ArenaScoreboardParticipant[];
  winnerEventCount: number;
  rejectedWinnerEventCount: number;
  activeApplyEnabled: boolean;
};

export function parseArenaScoreboardView(value: unknown): ArenaScoreboardView | null {
  const root = asRecord(value);
  if (Object.keys(root).length === 0) return null;
  const payload = asRecord(root.payload);
  const scoreboard = firstRecord([
    root.scoreboard,
    payload.scoreboard,
    asRecord(root.result).scoreboard,
  ]);
  const summary = firstRecord([
    root.arena_suite_summary,
    payload.arena_suite_summary,
    asRecord(root.result).arena_suite_summary,
  ]);
  if (Object.keys(scoreboard).length === 0 && Object.keys(summary).length === 0) return null;

  const rawParticipants = asArray(scoreboard.participants).map(asRecord);
  const participants = rawParticipants
    .map(toParticipant)
    .filter((participant): participant is ArenaScoreboardParticipant => participant !== null);
  if (participants.length === 0 && Object.keys(summary).length === 0) return null;

  return {
    suiteId: stringFrom(root.suite_id) || stringFrom(payload.suite_id) || stringFrom(summary.suite_id),
    title: stringFrom(root.title) || stringFrom(payload.title) || stringFrom(summary.title) || "Arena suite scoreboard",
    passed:
      booleanFrom(root.passed) ??
      booleanFrom(payload.passed) ??
      booleanFrom(summary.passed) ??
      firstNumber([scoreboard.failed_episode_count, summary.failed_episode_count]) === 0,
    episodeCount: firstNumber([
      root.episode_count,
      payload.episode_count,
      scoreboard.episode_count,
      summary.episode_count,
    ]),
    passedEpisodeCount: firstNumber([scoreboard.passed_episode_count, summary.passed_episode_count]),
    failedEpisodeCount: firstNumber([scoreboard.failed_episode_count, summary.failed_episode_count]),
    invariantFailureCount: firstNumber([
      arrayCountOrNull(scoreboard.invariant_failures),
      summary.invariant_failure_count,
    ]),
    participants,
    winnerEventCount: firstNumber([arrayCountOrNull(scoreboard.winner_events), summary.winner_count]),
    rejectedWinnerEventCount: firstNumber([
      arrayCountOrNull(scoreboard.rejected_winner_events),
      summary.rejected_winner_count,
    ]),
    activeApplyEnabled: booleanFrom(scoreboard.active_apply_enabled) ?? booleanFrom(summary.active_apply_enabled) ?? false,
  };
}

export function findArenaScoreboardView(values: unknown[]): ArenaScoreboardView | null {
  for (const value of values) {
    const view = parseArenaScoreboardView(value);
    if (view) return view;
    for (const event of asArray(asRecord(value).events)) {
      const record = asRecord(event);
      if (record.event_type === "arena_suite_recorded" || record.type === "arena_suite_recorded") {
        const eventView = parseArenaScoreboardView(record);
        if (eventView) return eventView;
      }
    }
  }
  return null;
}

function toParticipant(row: Record<string, unknown>): ArenaScoreboardParticipant | null {
  const participantId = stringFrom(row.participant_id);
  if (!participantId) return null;
  return {
    participantId,
    episodes: textArray(row.episodes),
    outcomeCount: numberFrom(row.outcome_count),
    scoreCount: numberFrom(row.score_count),
    wins: numberFrom(row.wins),
    losses: numberFrom(row.losses),
    exclusions: numberFrom(row.exclusions),
    exclusionReasons: numberRecord(row.exclusion_reasons),
    averageScore: numberFrom(row.average_score),
    maxScore: numberFrom(row.max_score),
    totalScore: numberFrom(row.total_score),
    totalCost: numberFrom(row.total_cost),
    budgetEfficiency: numberFrom(row.budget_efficiency),
    evidenceRefs: textArray(row.evidence_refs),
  };
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
    ? (value as Record<string, unknown>)
    : {};
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function stringFrom(value: unknown): string | null {
  if (typeof value === "string" && value.trim()) return value.trim();
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return null;
}

function numberFrom(value: unknown): number {
  return numberOrNull(value) ?? 0;
}

function numberOrNull(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function firstNumber(values: unknown[]): number {
  for (const value of values) {
    const parsed = numberOrNull(value);
    if (parsed !== null) return parsed;
  }
  return 0;
}

function arrayCountOrNull(value: unknown): number | null {
  return Array.isArray(value) ? value.length : null;
}

function booleanFrom(value: unknown): boolean | null {
  if (typeof value === "boolean") return value;
  if (value === "true") return true;
  if (value === "false") return false;
  return null;
}

function textArray(value: unknown): string[] {
  return asArray(value)
    .map(stringFrom)
    .filter((item): item is string => Boolean(item));
}

function numberRecord(value: unknown): Record<string, number> {
  const record = asRecord(value);
  return Object.fromEntries(
    Object.entries(record)
      .map(([key, item]) => [key, numberFrom(item)] as const)
      .filter(([, item]) => item > 0),
  );
}
