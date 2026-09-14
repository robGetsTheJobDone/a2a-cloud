"""Arena-suite runner over custom graph-kernel simulations.

Suites are simulation-only aggregations. They run bounded custom kernel specs,
then derive a scoreboard from replayable outcome, score, and winner events.
The scoreboard is evidence, not authority.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .simulation import (
    CustomSimulationError,
    CustomSimulationResult,
    run_custom_kernel_simulation,
)
from .templates import render_custom_kernel_template
from .protocols import summarize_scenario_trace_payload

MAX_SUITE_EPISODES = 25
MAX_SUITE_PARTICIPANTS = 50
JsonObject = dict[str, Any]


@dataclass(frozen=True)
class CustomSuiteEpisodeSpec:
    episode_id: str
    title: str
    template_id: str | None
    spec: JsonObject


@dataclass(frozen=True)
class CustomSuiteEpisodeResult:
    episode_id: str
    title: str
    passed: bool
    trace: JsonObject
    trace_summary: JsonObject
    state_summary: JsonObject
    step_results: list[JsonObject]
    template_id: str | None = None

    def to_payload(self) -> JsonObject:
        return {
            "episode_id": self.episode_id,
            "title": self.title,
            "template_id": self.template_id,
            "passed": self.passed,
            "trace": self.trace,
            "trace_summary": self.trace_summary,
            "state_summary": self.state_summary,
            "step_results": self.step_results,
            "simulation_only": True,
            "proposal_only": True,
            "active_apply_enabled": False,
        }


@dataclass
class ArenaScoreboardParticipant:
    participant_id: str
    episodes: set[str]
    outcome_count: int = 0
    score_count: int = 0
    total_score: float = 0.0
    max_score: float = 0.0
    wins: int = 0
    losses: int = 0
    exclusions: int = 0
    exclusion_reasons: dict[str, int] | None = None
    total_cost: float = 0.0
    outcome_refs: set[str] | None = None
    score_refs: set[str] | None = None
    winner_refs: set[str] | None = None
    exclusion_refs: set[str] | None = None
    evidence_refs: set[str] | None = None

    def __post_init__(self) -> None:
        self.exclusion_reasons = self.exclusion_reasons or {}
        self.outcome_refs = self.outcome_refs or set()
        self.score_refs = self.score_refs or set()
        self.winner_refs = self.winner_refs or set()
        self.exclusion_refs = self.exclusion_refs or set()
        self.evidence_refs = self.evidence_refs or set()

    def record_outcome(self, *, episode_id: str, event_id: str, cost: float) -> None:
        self.episodes.add(episode_id)
        self.outcome_count += 1
        self.total_cost += cost
        self.outcome_refs.add(event_id)
        self.evidence_refs.add(event_id)

    def record_score(self, *, episode_id: str, event_id: str, score: float) -> None:
        self.episodes.add(episode_id)
        self.score_count += 1
        self.total_score += score
        self.max_score = max(self.max_score, score)
        self.score_refs.add(event_id)
        self.evidence_refs.add(event_id)

    def record_win(self, *, episode_id: str, event_id: str) -> None:
        self.episodes.add(episode_id)
        self.wins += 1
        self.winner_refs.add(event_id)
        self.evidence_refs.add(event_id)

    def record_loss(self, *, episode_id: str, event_id: str) -> None:
        self.episodes.add(episode_id)
        self.losses += 1
        self.evidence_refs.add(event_id)

    def record_exclusion(self, *, episode_id: str, event_id: str, reason: str) -> None:
        self.episodes.add(episode_id)
        self.exclusions += 1
        self.exclusion_reasons[reason] = int(self.exclusion_reasons.get(reason) or 0) + 1
        self.exclusion_refs.add(event_id)
        self.evidence_refs.add(event_id)

    def to_payload(self) -> JsonObject:
        average_score = self.total_score / self.score_count if self.score_count else 0.0
        budget_efficiency = self.wins / self.total_cost if self.total_cost else float(self.wins)
        return {
            "participant_id": self.participant_id,
            "episodes": sorted(self.episodes),
            "outcome_count": self.outcome_count,
            "score_count": self.score_count,
            "total_score": self.total_score,
            "average_score": average_score,
            "max_score": self.max_score,
            "wins": self.wins,
            "losses": self.losses,
            "exclusions": self.exclusions,
            "exclusion_reasons": dict(sorted(self.exclusion_reasons.items())),
            "total_cost": self.total_cost,
            "budget_efficiency": budget_efficiency,
            "outcome_refs": sorted(self.outcome_refs),
            "score_refs": sorted(self.score_refs),
            "winner_refs": sorted(self.winner_refs),
            "exclusion_refs": sorted(self.exclusion_refs),
            "evidence_refs": sorted(self.evidence_refs),
        }


@dataclass(frozen=True)
class ArenaWinnerEvent:
    episode_id: str
    event_id: str
    payload: JsonObject

    def to_payload(self) -> JsonObject:
        return {"episode_id": self.episode_id, "event_id": self.event_id, **self.payload}


@dataclass(frozen=True)
class ArenaInvariantFailure:
    episode_id: str
    payload: JsonObject

    def to_payload(self) -> JsonObject:
        return {"episode_id": self.episode_id, **self.payload}


@dataclass(frozen=True)
class ArenaScoreboard:
    participants: tuple[ArenaScoreboardParticipant, ...]
    winner_events: tuple[ArenaWinnerEvent, ...]
    rejected_winner_events: tuple[ArenaWinnerEvent, ...]
    episode_count: int
    passed_episode_count: int
    failed_episode_count: int
    invariant_failures: tuple[ArenaInvariantFailure, ...]

    def to_payload(self) -> JsonObject:
        participant_payloads = [participant.to_payload() for participant in self.participants]
        ordered = sorted(
            participant_payloads,
            key=lambda row: (int(row["wins"]), float(row["average_score"]), str(row["participant_id"])),
            reverse=True,
        )
        return {
            "participants": ordered,
            "winner_events": [event.to_payload() for event in self.winner_events],
            "rejected_winner_events": [event.to_payload() for event in self.rejected_winner_events],
            "episode_count": self.episode_count,
            "passed_episode_count": self.passed_episode_count,
            "failed_episode_count": self.failed_episode_count,
            "invariant_failures": [failure.to_payload() for failure in self.invariant_failures],
            "simulation_only": True,
            "proposal_only": True,
            "active_apply_enabled": False,
        }


@dataclass(frozen=True)
class CustomSuiteResult:
    suite_id: str
    title: str
    passed: bool
    episode_contracts: tuple[CustomSuiteEpisodeResult, ...]
    scoreboard_contract: ArenaScoreboard
    trace_summary: JsonObject
    alerts: list[str]
    violations: list[str]

    @property
    def episodes(self) -> list[JsonObject]:
        return [episode.to_payload() for episode in self.episode_contracts]

    @property
    def scoreboard(self) -> JsonObject:
        return self.scoreboard_contract.to_payload()


def run_custom_kernel_suite(suite: dict[str, Any]) -> CustomSuiteResult:
    if not isinstance(suite, dict):
        raise CustomSimulationError("suite spec must be an object")
    title = _text(suite.get("title") or "Arena suite", field="title")
    suite_id = _slug(suite.get("suite_id") or title)
    raw_episodes = suite.get("episodes") or []
    if not isinstance(raw_episodes, list):
        raise CustomSimulationError("suite episodes must be a list")
    if not raw_episodes:
        raise CustomSimulationError("suite requires at least one episode")
    if len(raw_episodes) > MAX_SUITE_EPISODES:
        raise CustomSimulationError(f"suite episodes exceeds limit {MAX_SUITE_EPISODES}")

    episodes: list[CustomSuiteEpisodeResult] = []
    traces: list[JsonObject] = []
    for index, raw_episode in enumerate(raw_episodes, start=1):
        episode = _episode_spec(raw_episode, index=index)
        result = run_custom_kernel_simulation(episode.spec)
        episode_payload = _episode_payload(episode, result)
        episodes.append(episode_payload)
        traces.append(episode_payload.trace)

    scoreboard = _scoreboard(episodes)
    trace_summary = summarize_scenario_trace_payload({"passed": all(item.passed for item in episodes), "traces": traces})
    passed = all(item.passed for item in episodes) and not scoreboard.invariant_failures
    return CustomSuiteResult(
        suite_id=suite_id,
        title=title,
        passed=passed,
        episode_contracts=tuple(episodes),
        scoreboard_contract=scoreboard,
        trace_summary=trace_summary,
        alerts=sorted({alert for trace in traces for alert in trace.get("alerts", [])}),
        violations=sorted({violation for trace in traces for violation in trace.get("violations", [])}),
    )


def list_custom_kernel_suite_templates() -> list[dict[str, Any]]:
    return [_suite_template_payload(_ARENA_SUITE_TEMPLATE)]


def render_custom_kernel_suite_template(template_id: str) -> dict[str, Any]:
    normalized = str(template_id or "").strip()
    if normalized != _ARENA_SUITE_TEMPLATE["template_id"]:
        raise CustomSimulationError(f"unknown custom kernel suite template: {template_id!r}")
    suite = deepcopy(_ARENA_SUITE_TEMPLATE["suite"])
    run_custom_kernel_suite(suite)
    suite["template_ref"] = normalized
    suite["template_kind"] = _ARENA_SUITE_TEMPLATE["kind"]
    return suite


def _episode_spec(raw_episode: Any, *, index: int) -> CustomSuiteEpisodeSpec:
    if not isinstance(raw_episode, dict):
        raise CustomSimulationError("suite episode must be an object")
    episode_id = _slug(raw_episode.get("episode_id") or raw_episode.get("id") or f"episode-{index}")
    if raw_episode.get("template_id"):
        if raw_episode.get("spec"):
            raise CustomSimulationError("suite episode must provide either template_id or spec, not both")
        spec = render_custom_kernel_template(_text(raw_episode["template_id"], field="template_id"))
    else:
        spec = raw_episode.get("spec")
        if not isinstance(spec, dict):
            raise CustomSimulationError("suite episode requires a spec object")
        spec = dict(spec)
    spec.setdefault("scenario_id", episode_id)
    template_id = raw_episode.get("template_id")
    return CustomSuiteEpisodeSpec(
        episode_id=episode_id,
        title=_text(raw_episode.get("title") or spec.get("title") or episode_id, field="episode title"),
        template_id=str(template_id) if template_id is not None else None,
        spec=spec,
    )


def _episode_payload(episode: CustomSuiteEpisodeSpec, result: CustomSimulationResult) -> CustomSuiteEpisodeResult:
    return CustomSuiteEpisodeResult(
        episode_id=episode.episode_id,
        title=episode.title,
        template_id=episode.template_id,
        passed=result.passed,
        trace=result.trace,
        trace_summary=result.trace_summary,
        state_summary=result.state_summary,
        step_results=result.step_results,
    )


def _scoreboard(episodes: list[CustomSuiteEpisodeResult]) -> ArenaScoreboard:
    participants: dict[str, ArenaScoreboardParticipant] = {}
    winner_events: list[ArenaWinnerEvent] = []
    rejected_winner_events: list[ArenaWinnerEvent] = []
    invariant_failures: list[ArenaInvariantFailure] = []

    for episode in episodes:
        episode_id = episode.episode_id
        trace = episode.trace
        for row in trace.get("invariant_results", []):
            if not row.get("passed"):
                invariant_failures.append(ArenaInvariantFailure(episode_id=episode_id, payload=dict(row)))
        for event in trace.get("events", []):
            event_type = str(event.get("event_type") or "")
            payload = event.get("payload") or {}
            event_id = str(event.get("event_id") or "")
            if event_type == "outcome.recorded":
                participant = _participant(participants, str(payload.get("participant_id") or ""))
                metrics = payload.get("metrics") or {}
                cost = 0.0
                if isinstance(metrics, dict):
                    cost = float(metrics.get("cost") or metrics.get("budget") or 0)
                participant.record_outcome(episode_id=episode_id, event_id=event_id, cost=cost)
            elif event_type == "score.assigned":
                participant = _participant(participants, str(payload.get("participant_id") or ""))
                score = float(payload.get("score") or 0)
                participant.record_score(episode_id=episode_id, event_id=event_id, score=score)
            elif event_type == "winner.selected":
                winner_events.append(ArenaWinnerEvent(episode_id=episode_id, event_id=event_id, payload=dict(payload)))
                winner_id = str(payload.get("winner_id") or "")
                winner = _participant(participants, winner_id)
                winner.record_win(episode_id=episode_id, event_id=event_id)
                for candidate in payload.get("eligible_candidates") or []:
                    if not isinstance(candidate, dict):
                        continue
                    participant_id = str(candidate.get("participant_id") or "")
                    if participant_id and participant_id != winner_id:
                        participant = _participant(participants, participant_id)
                        participant.record_loss(episode_id=episode_id, event_id=event_id)
                for candidate in payload.get("excluded_candidates") or []:
                    _record_exclusion(participants, candidate, episode_id=episode_id, event_id=event_id)
            elif event_type == "winner.rejected":
                rejected_winner_events.append(ArenaWinnerEvent(episode_id=episode_id, event_id=event_id, payload=dict(payload)))
                for candidate in payload.get("excluded_candidates") or []:
                    _record_exclusion(participants, candidate, episode_id=episode_id, event_id=event_id)

    if len(participants) > MAX_SUITE_PARTICIPANTS:
        raise CustomSimulationError(f"suite participants exceeds limit {MAX_SUITE_PARTICIPANTS}")

    return ArenaScoreboard(
        participants=tuple(participants.values()),
        winner_events=tuple(winner_events),
        rejected_winner_events=tuple(rejected_winner_events),
        episode_count=len(episodes),
        passed_episode_count=sum(1 for episode in episodes if episode.passed),
        failed_episode_count=sum(1 for episode in episodes if not episode.passed),
        invariant_failures=tuple(invariant_failures),
    )


def _participant(rows: dict[str, ArenaScoreboardParticipant], participant_id: str) -> ArenaScoreboardParticipant:
    if not participant_id:
        raise CustomSimulationError("participant id is required")
    participant = rows.get(participant_id)
    if participant is None:
        participant = ArenaScoreboardParticipant(participant_id=participant_id, episodes=set())
        rows[participant_id] = participant
    return participant


def _record_exclusion(
    participants: dict[str, ArenaScoreboardParticipant],
    candidate: Any,
    *,
    episode_id: str,
    event_id: str,
) -> None:
    if not isinstance(candidate, dict):
        return
    participant_id = str(candidate.get("participant_id") or "")
    reason = str(candidate.get("reason") or "unknown")
    participant = _participant(participants, participant_id)
    participant.record_exclusion(episode_id=episode_id, event_id=event_id, reason=reason)


def _suite_template_payload(template: dict[str, Any]) -> dict[str, Any]:
    payload = {key: deepcopy(value) for key, value in template.items() if key != "suite"}
    payload.update(
        {
            "suite": deepcopy(template["suite"]),
            "simulation_only": True,
            "proposal_only": True,
            "active_apply_enabled": False,
        }
    )
    return payload


def _text(value: Any, *, field: str) -> str:
    if not isinstance(value, (str, int)):
        raise CustomSimulationError(f"{field} must be text")
    text = str(value).strip()
    if not text:
        raise CustomSimulationError(f"{field} is required")
    if len(text) > 256:
        raise CustomSimulationError(f"{field} exceeds 256 characters")
    return text


def _slug(value: Any) -> str:
    text = _text(value, field="id").lower()
    out = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in text).strip("-")
    return out[:80] or "suite"


_ARENA_SUITE_TEMPLATE: dict[str, Any] = {
    "template_id": "arena_suite@v1",
    "name": "Arena suite scoreboard",
    "kind": "arena_suite",
    "risk_class": "simulation",
    "description": "Runs multiple arena outcome episodes and aggregates an evidence-backed scoreboard.",
    "ledger_requirements": ["outcome.recorded", "score.assigned", "winner.selected"],
    "invariants": ["replay_deterministic", "winner_selected", "candidate_excluded"],
    "gates": ["scores cannot grant authority", "excluded participants remain excluded per episode"],
    "suite": {
        "title": "Arena suite scoreboard drill",
        "suite_id": "arena-suite-drill",
        "episodes": [
            {"episode_id": "round-1", "template_id": "arena_outcome@v1"},
            {
                "episode_id": "round-2",
                "spec": {
                    "title": "Arena outcome round 2",
                    "actors": [{"id": "arena"}, {"id": "alpha"}, {"id": "beta"}],
                    "steps": [
                        {"type": "record_outcome", "outcome_id": "outcome-alpha-r2", "participant_id": "alpha", "metrics": {"success": True, "cost": 2}},
                        {"type": "record_outcome", "outcome_id": "outcome-beta-r2", "participant_id": "beta", "metrics": {"success": True, "cost": 1}},
                        {"type": "score_participant", "score_id": "score-alpha-r2", "participant_id": "alpha", "outcome_id": "outcome-alpha-r2", "score": 18},
                        {"type": "score_participant", "score_id": "score-beta-r2", "participant_id": "beta", "outcome_id": "outcome-beta-r2", "score": 12},
                        {
                            "type": "select_winner",
                            "arena_id": "arena-2",
                            "candidates": {
                                "alpha": {"score_id": "score-alpha-r2"},
                                "beta": {"score_id": "score-beta-r2"},
                            },
                        },
                    ],
                    "invariants": [
                        "replay_deterministic",
                        "no_active_apply",
                        "no_violations",
                        {"id": "winner_selected", "arena_id": "arena-2", "winner_id": "alpha"},
                    ],
                },
            },
        ],
    },
}
