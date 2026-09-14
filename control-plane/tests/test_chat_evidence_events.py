from __future__ import annotations

from control_plane.routes.chat import _evidence_events_from_chat_event


def test_review_loop_arena_suite_emits_live_evidence_event() -> None:
    events = _evidence_events_from_chat_event(
        {
            "type": "review_loop_event",
            "job_id": "job-1",
            "agent": "kernel-agent",
            "event_type": "arena_suite_recorded",
            "status": "complete",
            "payload": {
                "suite_id": "arena-suite",
                "title": "Arena suite drill",
                "passed": True,
                "scoreboard": {
                    "participants": [{"participant_id": "alpha", "wins": 1}],
                    "winner_events": [{"event_id": "winner-1"}],
                },
            },
        }
    )

    assert len(events) == 1
    assert events[0]["type"] == "evidence_event"
    assert events[0]["evidence_kind"] == "arena_suite"
    assert events[0]["event_type"] == "arena_suite_recorded"
    assert events[0]["payload"]["suite_id"] == "arena-suite"
    assert "job_id:job-1" in events[0]["evidence_key"]
    assert events[0]["source"] == {
        "kind": "review_loop",
        "job_id": "job-1",
        "agent": "kernel-agent",
    }


def test_invariant_events_only_emit_failures() -> None:
    passing = _evidence_events_from_chat_event(
        {
            "type": "review_loop_event",
            "job_id": "job-1",
            "agent": "kernel-agent",
            "event_type": "invariant_checked",
            "payload": {"invariant_id": "bounded_authority", "passed": True},
        }
    )
    failing = _evidence_events_from_chat_event(
        {
            "type": "review_loop_event",
            "job_id": "job-1",
            "agent": "kernel-agent",
            "event_type": "invariant_checked",
            "payload": {"invariant_id": "bounded_authority", "passed": False},
        }
    )

    assert passing == []
    assert len(failing) == 1
    assert failing[0]["evidence_kind"] == "invariant_failure"
    assert failing[0]["title"] == "Invariant failed: bounded_authority"


def test_dag_node_result_nested_events_emit_live_evidence_events() -> None:
    events = _evidence_events_from_chat_event(
        {
            "type": "dag_node_complete",
            "dag_run_id": "dag-1",
            "node_id": "simulate",
            "agent": "kernel-agent",
            "skill": "run",
            "result": {
                "events": [
                    {
                        "event_id": "evt-trace",
                        "event_type": "scenario_trace_recorded",
                        "payload": {
                            "scenario_id": "scenario-1",
                            "trace_summary": {
                                "scenario_count": 1,
                                "invariant_pass_count": 2,
                            },
                        },
                    },
                    {
                        "event_id": "evt-limit",
                        "event_type": "simulation_episode_limit_exceeded",
                        "payload": {"episode_limit": 3, "attempted": 4},
                    },
                    {
                        "event_id": "evt-pass",
                        "event_type": "invariant_checked",
                        "payload": {"invariant_id": "ok", "passed": True},
                    },
                ]
            },
        }
    )

    assert [event["evidence_kind"] for event in events] == [
        "scenario_trace",
        "protocol_limit",
    ]
    assert "dag_run_id:dag-1" in events[0]["evidence_key"]
    assert "event:evt-trace" in events[0]["evidence_key"]
    assert events[1]["title"] == "simulation episode limit exceeded"


def test_policy_decision_work_events_emit_live_evidence() -> None:
    events = _evidence_events_from_chat_event(
        {
            "type": "review_loop_event",
            "job_id": "job-policy",
            "agent": "kernel-agent",
            "event_type": "policy_decision_recorded",
            "status": "complete",
            "severity": "critical",
            "payload": {
                "decision_id": "pd-deny",
                "decision": "deny",
                "action": "write",
                "resource": "[redacted]",
                "signature_present": True,
            },
        }
    )

    assert len(events) == 1
    assert events[0]["evidence_kind"] == "policy_decision"
    assert events[0]["title"] == "Policy decision: deny"
    assert events[0]["severity"] == "critical"
    assert "decision_id:pd-deny" in events[0]["evidence_key"]


def test_malformed_and_unrelated_chat_events_emit_no_evidence() -> None:
    assert _evidence_events_from_chat_event({"type": "tool_call", "tool": "x"}) == []
    assert _evidence_events_from_chat_event(
        {"type": "review_loop_event", "event_type": "noop", "payload": "bad"}
    ) == []
    assert _evidence_events_from_chat_event(
        {"type": "dag_node_complete", "result": {"events": [None, "broken", {}]}}
    ) == []
