from __future__ import annotations

import json
from pathlib import Path

import pytest

from control_plane.custom_kernel_simulations import CustomSimulationError
from control_plane.custom_kernel_suites import (
    ArenaScoreboard,
    ArenaScoreboardParticipant,
    CustomSuiteEpisodeResult,
    list_custom_kernel_suite_templates,
    render_custom_kernel_suite_template,
    run_custom_kernel_suite,
)


def _episode(episode_id: str, *, alpha_score: float, beta_score: float, freeze_beta: bool = False) -> dict:
    steps = [
        {
            "type": "record_outcome",
            "outcome_id": f"outcome-alpha-{episode_id}",
            "participant_id": "alpha",
            "metrics": {"success": True, "cost": 2},
        },
        {
            "type": "record_outcome",
            "outcome_id": f"outcome-beta-{episode_id}",
            "participant_id": "beta",
            "metrics": {"success": True, "cost": 1},
        },
        {
            "type": "score_participant",
            "score_id": f"score-alpha-{episode_id}",
            "participant_id": "alpha",
            "outcome_id": f"outcome-alpha-{episode_id}",
            "score": alpha_score,
        },
        {
            "type": "score_participant",
            "score_id": f"score-beta-{episode_id}",
            "participant_id": "beta",
            "outcome_id": f"outcome-beta-{episode_id}",
            "score": beta_score,
        },
    ]
    if freeze_beta:
        steps.append({"type": "freeze_node", "node_id": "beta", "reason": "unsafe finalist"})
    steps.append(
        {
            "type": "select_winner",
            "arena_id": f"arena-{episode_id}",
            "candidates": {
                "alpha": {"score_id": f"score-alpha-{episode_id}"},
                "beta": {"score_id": f"score-beta-{episode_id}"},
            },
        }
    )
    return {
        "episode_id": episode_id,
        "spec": {
            "title": f"Arena {episode_id}",
            "actors": [{"id": "arena"}, {"id": "alpha"}, {"id": "beta"}],
            "steps": steps,
            "invariants": [
                "replay_deterministic",
                "no_active_apply",
                "no_violations",
                {"id": "winner_selected", "arena_id": f"arena-{episode_id}", "winner_id": "alpha" if freeze_beta or alpha_score >= beta_score else "beta"},
            ],
        },
    }


def test_custom_kernel_suite_aggregates_scoreboard_and_evidence_refs() -> None:
    result = run_custom_kernel_suite(
        {
            "title": "Two round arena",
            "episodes": [
                _episode("round-1", alpha_score=10, beta_score=20, freeze_beta=True),
                _episode("round-2", alpha_score=18, beta_score=12),
            ],
        }
    )

    assert result.passed is True
    assert isinstance(result.scoreboard_contract, ArenaScoreboard)
    assert isinstance(result.episode_contracts[0], CustomSuiteEpisodeResult)
    assert all(
        isinstance(participant, ArenaScoreboardParticipant)
        for participant in result.scoreboard_contract.participants
    )
    json.dumps({"episodes": result.episodes, "scoreboard": result.scoreboard})
    assert result.scoreboard["episode_count"] == 2
    alpha = result.scoreboard["participants"][0]
    beta = result.scoreboard["participants"][1]
    assert alpha["participant_id"] == "alpha"
    assert alpha["wins"] == 2
    assert alpha["losses"] == 0
    assert alpha["average_score"] == 14
    assert alpha["evidence_refs"]
    assert beta["participant_id"] == "beta"
    assert beta["exclusions"] == 1
    assert beta["losses"] == 1
    assert beta["exclusion_reasons"] == {"participant_frozen": 1}
    assert result.trace_summary["scenario_count"] == 2


def test_custom_kernel_suite_records_failed_episode_without_authority() -> None:
    suite = {
        "title": "Failed invariant suite",
        "episodes": [
            {
                "episode_id": "bad-round",
                "spec": {
                    "title": "Bad round",
                    "actors": [{"id": "alpha"}],
                    "steps": [
                        {"type": "record_outcome", "outcome_id": "outcome-alpha", "participant_id": "alpha"},
                        {"type": "score_participant", "score_id": "score-alpha", "participant_id": "alpha", "outcome_id": "outcome-alpha", "score": 1},
                    ],
                    "invariants": [{"id": "winner_selected", "arena_id": "missing-arena", "winner_id": "alpha"}],
                },
            }
        ],
    }

    result = run_custom_kernel_suite(suite)

    assert result.passed is False
    assert result.scoreboard["failed_episode_count"] == 1
    assert result.scoreboard["invariant_failures"]
    assert result.scoreboard["active_apply_enabled"] is False


def test_custom_kernel_suite_template_renders_and_runs() -> None:
    templates = list_custom_kernel_suite_templates()
    assert [template["template_id"] for template in templates] == ["arena_suite@v1"]

    suite = render_custom_kernel_suite_template("arena_suite@v1")
    result = run_custom_kernel_suite(suite)

    assert result.passed is True
    assert result.scoreboard["participants"][0]["participant_id"] == "alpha"
    assert result.scoreboard["participants"][0]["wins"] == 2


def test_custom_kernel_suite_rejects_invalid_bounds() -> None:
    with pytest.raises(CustomSimulationError, match="at least one episode"):
        run_custom_kernel_suite({"title": "empty", "episodes": []})

    with pytest.raises(CustomSimulationError, match="exceeds limit"):
        run_custom_kernel_suite(
            {
                "title": "too many",
                "episodes": [{"episode_id": f"round-{index}", "template_id": "arena_outcome@v1"} for index in range(26)],
            }
        )


def test_custom_kernel_suite_has_no_free_dict_scoreboard_internals() -> None:
    source = Path(__file__).resolve().parents[1] / "control_plane" / "custom_kernel_suites.py"
    text = source.read_text()

    forbidden = (
        "episodes: list[dict[str, Any]]",
        "scoreboard: dict[str, Any]",
        "def _scoreboard(episodes: list[dict[str, Any]]) -> dict[str, Any]",
        "participants: dict[str, dict[str, Any]]",
        "winner_events: list[dict[str, Any]]",
        "invariant_failures: list[dict[str, Any]]",
    )
    for snippet in forbidden:
        assert snippet not in text
