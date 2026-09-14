from __future__ import annotations

from pathlib import Path

from control_plane.dynamic_graph_sim import (
    Capability,
    DeletionPreviewState,
    EdgeState,
    GraphSimState,
    OutcomeState,
    PolicyDecisionState,
    PolicyRule,
    ProcessState,
    PortState,
    RouteState,
    RewriteState,
    ScoreState,
    SignalState,
    WinnerState,
)


def cap(
    *actions: str,
    resources: tuple[str, ...] = ("repo/*",),
    deny: tuple[str, ...] = (),
    budget: int = 10,
    ttl: int | None = 100,
    delegation_depth: int = 2,
) -> Capability:
    return Capability(
        actions=frozenset(actions),
        resources=resources,
        deny=deny,
        expires_at=ttl,
        budget=budget,
        delegation_depth=delegation_depth,
    )


def activate_process_edge(
    state: GraphSimState,
    *,
    process_id: str = "process-1",
    process_budget: int = 10,
    process_ttl: int = 10,
    max_calls: int = 10,
    heartbeat_timeout: int = 0,
) -> None:
    state.create_node("router")
    state.create_node("worker")
    state.create_port("router", "route:work", direction="output")
    state.create_port("worker", "invoke:work", direction="input")
    state.mint_capability("process-cap", "router", cap("call", resources=("skill:*",), budget=20))
    state.mint_capability("edge-cap", "router", cap("call", resources=("worker:*",), budget=20))
    assert (
        state.start_process(
            process_id,
            owner="router",
            cap_id="process-cap",
            ttl=process_ttl,
            budget=process_budget,
            max_calls=max_calls,
            heartbeat_timeout=heartbeat_timeout,
        )
        == "allow"
    )
    assert (
        state.propose_edge(
            "edge-process-worker",
            from_node="router",
            from_port="route:work",
            to_node="worker",
            to_port="invoke:work",
            edge_type="call",
            capability_id="edge-cap",
            process_id=process_id,
            provenance_ref=f"process:{process_id}",
        )
        == "allow"
    )
    assert (
        state.activate_edge(
            "edge-process-worker",
            policies=(
                PolicyRule(
                    policy_id="owner-process-call",
                    level="owner",
                    effect="allow",
                    actions=frozenset({"call"}),
                    resources=("worker:*",),
                ),
            ),
            decision_id="pd-process-edge",
        )
        == "allow"
    )


def test_child_scope_narrowing_and_implicit_union_denial() -> None:
    state = GraphSimState()
    state.create_node("parent")
    state.create_node("child")
    state.mint_capability("parent-read", "parent", cap("read"))

    decision = state.delegate(
        "parent-read",
        "child-write",
        "child",
        cap("write", resources=("repo/private.py",)),
    )

    assert decision == "deny"
    assert "child-write" not in state.capabilities
    assert state.request_union(["read-a", "write-b"], "unsafe-union") == "deny"
    assert state.ledger[-1].payload["reason"] == "implicit_union_forbidden"


def test_deny_freeze_revoke_ttl_and_budget_stop_authority_use() -> None:
    state = GraphSimState(now=1)
    state.mint_capability(
        "call-cap",
        "agent-a",
        cap("call", resources=("skill:*",), deny=("skill:delete",), budget=2, ttl=3),
    )

    assert state.use_capability("call-cap", "call", "skill:run", cost=1) == "allow"
    assert state.use_capability("call-cap", "call", "skill:delete") == "deny"
    assert state.use_capability("call-cap", "call", "skill:run", cost=5) == "deny"
    state.freeze_node("agent-a", "unsafe reviewer signal")
    assert state.use_capability("call-cap", "call", "skill:run") == "deny"
    state.revoke_capability("call-cap")
    assert state.use_capability("call-cap", "call", "skill:run") == "deny"

    expired = GraphSimState(now=3)
    expired.mint_capability("expired", "agent-a", cap("call", resources=("skill:*",), ttl=3))
    assert expired.use_capability("expired", "call", "skill:run") == "deny"


def test_route_weight_is_explained_and_never_grants_authority() -> None:
    state = GraphSimState()
    state.mint_capability("cheap-call", "cheap-agent", cap("call", resources=("skill:run",)))
    state.mint_capability("expensive-call", "expensive-agent", cap("call", resources=("skill:run",)))
    state.emit_signal("cheap-agent", "success", {"skill": "run"})
    state.emit_signal("cheap-agent", "cost", {"usd": 0.01})
    state.emit_signal("expensive-agent", "success", {"skill": "run"})
    state.emit_signal("expensive-agent", "cost", {"usd": 1.0})

    selected = state.select_route(
        "run",
        {"cheap-agent": "cheap-call", "expensive-agent": "expensive-call"},
    )

    assert selected == "cheap-agent"
    assert state.routes["run"]["explanation_refs"]

    denied = GraphSimState()
    denied.emit_signal("popular-agent", "taste", {"rank": 1})
    assert denied.select_route("run", {"popular-agent": "missing-cap"}) is None
    assert denied.ledger[-1].event_type == "route.rejected"


def test_typed_ports_edges_require_policy_and_replay_deterministically() -> None:
    state = GraphSimState()
    state.create_node("router")
    state.create_node("worker")
    state.create_port("router", "route:work", direction="output", schema_ref="route.v1")
    state.create_port("worker", "invoke:work", direction="input", schema_ref="skill.v1")
    state.mint_capability(
        "edge-cap",
        "router",
        cap("call", resources=("worker:*",), budget=2),
    )

    assert (
        state.propose_edge(
            "edge-router-worker",
            from_node="router",
            from_port="route:work",
            to_node="worker",
            to_port="invoke:work",
            edge_type="call",
            capability_id="edge-cap",
            provenance_ref="template:competitive_allocation@v1",
        )
        == "allow"
    )
    assert (
        state.activate_edge(
            "edge-router-worker",
            policies=(
                PolicyRule(
                    policy_id="owner-allows-call",
                    level="owner",
                    effect="allow",
                    actions=frozenset({"call"}),
                    resources=("worker:*",),
                ),
            ),
            decision_id="pd-edge-router-worker",
        )
        == "allow"
    )

    assert state.edges["edge-router-worker"]["state"] == "active"
    assert state.policy_decisions["pd-edge-router-worker"]["decision"] == "allow"
    assert state.capabilities["edge-cap"].budget == 2
    replayed = GraphSimState.replay([event.dump() for event in state.ledger])
    assert replayed.summary() == state.summary()


def test_graph_sim_state_uses_typed_snapshots_with_stable_json_summary() -> None:
    state = GraphSimState()
    state.create_node("router")
    state.create_node("worker")
    state.create_port("router", "route:work", direction="output", schema_ref="route.v1")
    state.create_port("worker", "invoke:work", direction="input", schema_ref="skill.v1")
    state.mint_capability("edge-cap", "router", cap("call", resources=("worker:*",), budget=2))
    state.mint_capability("route-cap", "router", cap("call", resources=("skill:*",), budget=2))
    assert (
        state.propose_edge(
            "edge-router-worker",
            from_node="router",
            from_port="route:work",
            to_node="worker",
            to_port="invoke:work",
            edge_type="call",
            capability_id="edge-cap",
        )
        == "allow"
    )
    assert (
        state.activate_edge(
            "edge-router-worker",
            policies=(
                PolicyRule(
                    policy_id="owner-allows-call",
                    level="owner",
                    effect="allow",
                    actions=frozenset({"call"}),
                    resources=("worker:*",),
                ),
            ),
            decision_id="pd-edge-router-worker",
        )
        == "allow"
    )
    state.emit_signal("router", "success", {"skill": "work"})
    assert state.select_route("work", {"router": "route-cap"}) == "router"

    assert isinstance(state.ports["router"]["route:work"], PortState)
    assert isinstance(state.edges["edge-router-worker"], EdgeState)
    assert isinstance(state.policy_decisions["pd-edge-router-worker"], PolicyDecisionState)
    assert isinstance(state.signals[-1], SignalState)
    assert isinstance(state.routes["work"], RouteState)

    summary = state.summary()
    assert isinstance(summary["edges"]["edge-router-worker"], dict)
    assert summary["edges"]["edge-router-worker"]["state"] == "active"
    assert summary["policy_decisions"]["pd-edge-router-worker"]["decision"] == "allow"
    assert GraphSimState.replay([event.dump() for event in state.ledger]).summary() == summary


def test_typed_edges_reject_missing_endpoint_ports() -> None:
    state = GraphSimState()
    state.create_node("router")
    state.create_node("worker")
    state.create_port("router", "route:work", direction="output")

    assert (
        state.propose_edge(
            "edge-missing-port",
            from_node="router",
            from_port="route:work",
            to_node="worker",
            to_port="invoke:work",
            edge_type="call",
        )
        == "deny"
    )
    assert "edge-missing-port" not in state.edges
    assert "missing_endpoint_port" in state.violations


def test_process_local_edges_expire_when_process_stops() -> None:
    state = GraphSimState()
    state.create_node("router")
    state.create_node("worker")
    state.create_port("router", "route:work", direction="output")
    state.create_port("worker", "invoke:work", direction="input")
    state.mint_capability("process-cap", "router", cap("call", resources=("skill:*",), budget=5))
    state.mint_capability("edge-cap", "router", cap("call", resources=("worker:*",), budget=5))
    assert state.start_process("process-1", owner="router", cap_id="process-cap") == "allow"
    assert (
        state.propose_edge(
            "edge-process-worker",
            from_node="router",
            from_port="route:work",
            to_node="worker",
            to_port="invoke:work",
            edge_type="call",
            capability_id="edge-cap",
            process_id="process-1",
            provenance_ref="process:process-1",
        )
        == "allow"
    )
    assert (
        state.activate_edge(
            "edge-process-worker",
            policies=(
                PolicyRule(
                    policy_id="owner-process-call",
                    level="owner",
                    effect="allow",
                    actions=frozenset({"call"}),
                    resources=("worker:*",),
                ),
            ),
            decision_id="pd-process-edge",
        )
        == "allow"
    )
    assert state.edges["edge-process-worker"]["state"] == "active"

    assert state.stop_process("process-1", reason="budget_exhausted") == "allow"

    assert "process-1" not in state.processes
    assert state.edges["edge-process-worker"]["state"] == "expired"
    assert state.edges["edge-process-worker"]["expired_reason"] == "budget_exhausted"
    replayed = GraphSimState.replay([event.dump() for event in state.ledger])
    assert replayed.summary() == state.summary()


def test_process_edge_use_debits_budget_and_replays_deterministically() -> None:
    state = GraphSimState()
    activate_process_edge(state, process_budget=4, max_calls=3)

    assert state.use_edge("edge-process-worker", cost=2) == "allow"
    assert state.use_edge("edge-process-worker", cost=1) == "allow"

    assert state.processes["process-1"]["call_count"] == 2
    assert state.processes["process-1"]["budget_remaining"] == 1
    assert isinstance(state.processes["process-1"], ProcessState)
    assert state.edges["edge-process-worker"]["last_used_at"] == state.ledger[-1].created_at
    assert state.capabilities["edge-cap"].budget == 20
    replayed = GraphSimState.replay([event.dump() for event in state.ledger])
    assert replayed.summary() == state.summary()


def test_process_edge_use_blocks_when_budget_is_exhausted() -> None:
    state = GraphSimState()
    activate_process_edge(state, process_budget=1)

    assert state.use_edge("edge-process-worker", cost=2) == "blocked"

    assert "process-1" not in state.processes
    assert state.edges["edge-process-worker"]["state"] == "expired"
    assert state.edges["edge-process-worker"]["expired_reason"] == "budget_exhausted"
    assert "process_budget_exhausted" in state.alerts
    replayed = GraphSimState.replay([event.dump() for event in state.ledger])
    assert replayed.summary() == state.summary()


def test_process_edge_use_blocks_after_max_calls() -> None:
    state = GraphSimState()
    activate_process_edge(state, process_budget=5, max_calls=1)

    assert state.use_edge("edge-process-worker") == "allow"
    assert state.use_edge("edge-process-worker") == "blocked"

    assert "process-1" not in state.processes
    assert state.edges["edge-process-worker"]["state"] == "expired"
    assert state.edges["edge-process-worker"]["expired_reason"] == "max_calls_exceeded"
    assert "process_max_calls_exceeded" in state.alerts


def test_process_edge_use_blocks_on_ttl_expiry() -> None:
    state = GraphSimState()
    activate_process_edge(state, process_ttl=2)

    state.advance_time(2)

    assert state.use_edge("edge-process-worker") == "blocked"
    assert "process-1" not in state.processes
    assert state.edges["edge-process-worker"]["expired_reason"] == "ttl_expired"
    assert "process_ttl_expired" in state.alerts


def test_process_edge_use_blocks_on_stale_heartbeat() -> None:
    state = GraphSimState()
    activate_process_edge(state, heartbeat_timeout=2)

    state.advance_time(1)
    assert state.heartbeat_process("process-1") == "allow"
    state.advance_time(2)

    assert state.use_edge("edge-process-worker") == "blocked"
    assert "process-1" not in state.processes
    assert state.edges["edge-process-worker"]["expired_reason"] == "heartbeat_stale"
    assert "process_heartbeat_stale" in state.alerts


def test_process_edge_use_rejects_revoked_capability_and_frozen_endpoint() -> None:
    revoked = GraphSimState()
    activate_process_edge(revoked)
    revoked.revoke_capability("edge-cap")

    assert revoked.use_edge("edge-process-worker") == "deny"
    assert "capability_denied" in revoked.violations
    assert revoked.edges["edge-process-worker"]["state"] == "active"

    frozen = GraphSimState()
    activate_process_edge(frozen)
    frozen.freeze_node("worker", "unsafe endpoint")

    assert frozen.use_edge("edge-process-worker") == "deny"
    assert "endpoint_frozen" in frozen.violations
    assert frozen.edges["edge-process-worker"]["state"] == "active"


def test_outcome_scoring_selects_eligible_winner_and_replays() -> None:
    state = GraphSimState()
    state.create_node("bidder-a")
    state.create_node("bidder-b")

    assert state.record_outcome("outcome-a", participant_id="bidder-a", metrics={"success": True}) == "allow"
    assert state.record_outcome("outcome-b", participant_id="bidder-b", metrics={"success": True}) == "allow"
    assert state.score_participant("score-a", participant_id="bidder-a", outcome_id="outcome-a", score=7.0) == "allow"
    assert state.score_participant("score-b", participant_id="bidder-b", outcome_id="outcome-b", score=9.0) == "allow"

    winner = state.select_winner(
        "arena-1",
        {
            "bidder-a": {"score_id": "score-a"},
            "bidder-b": {"score_id": "score-b"},
        },
    )

    assert winner == "bidder-b"
    assert isinstance(state.outcomes["outcome-a"], OutcomeState)
    assert isinstance(state.scores["score-a"], ScoreState)
    assert isinstance(state.winners["arena-1"], WinnerState)
    assert state.winners["arena-1"]["winner_id"] == "bidder-b"
    assert state.winners["arena-1"]["score"] == 9.0
    assert state.winners["arena-1"]["active_apply_enabled"] is False
    replayed = GraphSimState.replay([event.dump() for event in state.ledger])
    assert replayed.summary() == state.summary()


def test_winner_selection_excludes_frozen_revoked_and_missing_evidence() -> None:
    state = GraphSimState()
    for node_id in ("eligible", "frozen", "revoked", "missing-score"):
        state.create_node(node_id)
    state.mint_capability("revoked-cap", "revoked", cap("call", resources=("arena:*",)))
    state.revoke_capability("revoked-cap")
    for node_id, score in (("eligible", 3.0), ("frozen", 100.0), ("revoked", 99.0)):
        assert state.record_outcome(f"outcome-{node_id}", participant_id=node_id) == "allow"
        assert (
            state.score_participant(
                f"score-{node_id}",
                participant_id=node_id,
                outcome_id=f"outcome-{node_id}",
                score=score,
            )
            == "allow"
        )
    state.freeze_node("frozen", "unsafe arena participant")

    winner = state.select_winner(
        "arena-1",
        {
            "eligible": {"score_id": "score-eligible"},
            "frozen": {"score_id": "score-frozen"},
            "revoked": {"score_id": "score-revoked", "capability_id": "revoked-cap"},
            "missing-score": {"score_id": "score-missing"},
        },
    )

    assert winner == "eligible"
    exclusions = {
        row["participant_id"]: row["reason"]
        for row in state.winners["arena-1"]["excluded_candidates"]
    }
    assert exclusions == {
        "frozen": "participant_frozen",
        "revoked": "capability_not_active",
        "missing-score": "missing_score",
    }
    assert state.capabilities["revoked-cap"].revoked is True


def test_outcome_requires_active_process_and_edge() -> None:
    state = GraphSimState()
    activate_process_edge(state, process_budget=1)

    assert (
        state.record_outcome(
            "outcome-worker",
            participant_id="worker",
            process_id="process-1",
            edge_id="edge-process-worker",
        )
        == "allow"
    )
    assert state.score_participant("score-worker", participant_id="worker", outcome_id="outcome-worker", score=5.0) == "allow"
    assert state.use_edge("edge-process-worker", cost=2) == "blocked"

    assert (
        state.record_outcome(
            "outcome-after-stop",
            participant_id="worker",
            process_id="process-1",
            edge_id="edge-process-worker",
        )
        == "deny"
    )
    assert state.ledger[-1].payload["reason"] == "process_not_active"
    assert (
        state.select_winner(
            "arena-1",
            {"worker": {"score_id": "score-worker", "process_id": "process-1", "edge_id": "edge-process-worker"}},
        )
        is None
    )
    assert state.ledger[-1].event_type == "winner.rejected"
    assert state.ledger[-1].payload["excluded_candidates"][0]["reason"] == "process_not_active"
    assert "arena-1" not in state.winners


def test_rewrite_requires_approval_review_and_canary() -> None:
    state = GraphSimState()
    state.propose_rewrite(
        "rewrite-1",
        "agent-a",
        {"new_child": "cheaper-agent", "rollback_plan": "restore old edge"},
    )

    assert state.apply_rewrite("rewrite-1") == "deny"
    state.approve_rewrite("rewrite-1", review="critical", canary=True)
    assert state.apply_rewrite("rewrite-1") == "deny"
    state.approve_rewrite("rewrite-1", review="passed", canary=False)
    assert state.apply_rewrite("rewrite-1") == "deny"
    state.approve_rewrite("rewrite-1", review="passed", canary=True)
    assert state.apply_rewrite("rewrite-1") == "allow"
    assert state.rewrites["rewrite-1"]["applied"] is True
    assert isinstance(state.rewrites["rewrite-1"], RewriteState)
    assert state.rewrites["rewrite-1"]["rollback_plan"] == "restore old edge"


def test_adversarial_review_loop_is_findings_only_and_freezes_critical() -> None:
    state = GraphSimState()
    state.mint_capability(
        "review-cap",
        "agent-reviewer",
        cap("review", resources=("agent:*",), budget=5),
    )

    assert (
        state.start_review_loop(
            "arl-1",
            reviewer="agent-reviewer",
            target="agent-a",
            cap_id="review-cap",
            ttl=10,
            budget=3,
            max_iterations=3,
        )
        == "allow"
    )
    assert (
        state.emit_review_finding(
            "arl-1",
            reviewer="agent-reviewer",
            target="agent-a",
            finding_id="finding-warning",
            severity="warning",
            cost=1,
        )
        == "allow"
    )
    assert state.processes["arl-1"]["iterations"] == 1
    assert state.processes["arl-1"]["budget_remaining"] == 2

    assert (
        state.emit_review_finding(
            "arl-1",
            reviewer="agent-reviewer",
            target="agent-a",
            finding_id="finding-critical",
            severity="critical",
            cost=1,
        )
        == "allow"
    )
    assert "agent-a" in state.frozen_nodes

    assert (
        state.emit_review_finding(
            "arl-1",
            reviewer="agent-reviewer",
            target="agent-a",
            finding_id="finding-mutating",
            severity="critical",
            can_mutate=True,
        )
        == "deny"
    )
    assert "reviewer_mutation_authority_forbidden" in state.violations

    assert (
        state.propose_fix_from_finding(
            "arl-1",
            finding_id="finding-critical",
            proposal_id="sip-loop-fix",
            direct_apply=True,
        )
        == "deny"
    )
    assert "review_loop_direct_apply_forbidden" in state.violations

    assert (
        state.propose_fix_from_finding(
            "arl-1",
            finding_id="finding-critical",
            proposal_id="sip-loop-fix",
        )
        == "allow"
    )
    assert state.rewrites["sip-loop-fix"]["applied"] is False
    assert state.rewrites["sip-loop-fix"]["proposal_only"] is True
    assert state.apply_rewrite("sip-loop-fix") == "deny"


def test_adversarial_review_loop_enforces_ttl_budget_iterations_and_kill_switch() -> None:
    state = GraphSimState()
    state.mint_capability(
        "review-cap",
        "agent-reviewer",
        cap("review", resources=("agent:*",), budget=5),
    )
    assert (
        state.start_review_loop(
            "arl-iterations",
            reviewer="agent-reviewer",
            target="agent-a",
            cap_id="review-cap",
            ttl=10,
            budget=2,
            max_iterations=1,
        )
        == "allow"
    )
    assert (
        state.emit_review_finding(
            "arl-iterations",
            reviewer="agent-reviewer",
            target="agent-a",
            finding_id="finding-1",
        )
        == "allow"
    )
    assert (
        state.emit_review_finding(
            "arl-iterations",
            reviewer="agent-reviewer",
            target="agent-a",
            finding_id="finding-2",
        )
        == "blocked"
    )
    assert "review_loop_max_iterations_exceeded" in state.alerts
    assert "arl-iterations" not in state.processes

    assert (
        state.start_review_loop(
            "arl-budget",
            reviewer="agent-reviewer",
            target="agent-a",
            cap_id="review-cap",
            ttl=10,
            budget=1,
            max_iterations=2,
        )
        == "allow"
    )
    assert (
        state.emit_review_finding(
            "arl-budget",
            reviewer="agent-reviewer",
            target="agent-a",
            finding_id="finding-costly",
            cost=2,
        )
        == "blocked"
    )
    assert "review_loop_budget_exhausted" in state.alerts

    assert (
        state.start_review_loop(
            "arl-ttl",
            reviewer="agent-reviewer",
            target="agent-a",
            cap_id="review-cap",
            ttl=1,
            budget=2,
            max_iterations=2,
        )
        == "allow"
    )
    state.now = 2
    assert (
        state.emit_review_finding(
            "arl-ttl",
            reviewer="agent-reviewer",
            target="agent-a",
            finding_id="finding-expired",
        )
        == "blocked"
    )
    assert "review_loop_ttl_expired" in state.alerts

    assert (
        state.start_review_loop(
            "arl-stop",
            reviewer="agent-reviewer",
            target="agent-a",
            cap_id="review-cap",
        )
        == "allow"
    )
    assert state.stop_review_loop("arl-stop", "operator_disable") == "allow"
    assert "arl-stop" not in state.processes


def test_protocol_simulation_is_bounded_and_simulation_only() -> None:
    state = GraphSimState()
    assert (
        state.start_protocol_simulation(
            "sim-1",
            target="agent:writer",
            protocol_id="market",
            budget=10,
            ttl=5,
            max_episodes=1,
        )
        == "allow"
    )
    assert state.record_protocol_episode("sim-1", episode=1, cost=4) == "allow"
    assert state.processes["sim-1"]["episodes"] == 1
    assert state.processes["sim-1"]["budget_remaining"] == 6

    assert state.record_protocol_episode("sim-1", episode=2, cost=1) == "blocked"
    assert "protocol_simulation_episode_limit_exceeded" in state.alerts
    assert "sim-1" not in state.processes

    state.start_protocol_simulation(
        "sim-2",
        target="agent:writer",
        protocol_id="red_team",
        budget=10,
        ttl=5,
        max_episodes=2,
    )
    assert state.record_protocol_episode("sim-2", episode=1, direct_apply=True) == "deny"
    assert "protocol_simulation_direct_apply_forbidden" in state.violations


def test_delete_revoke_cascade_and_revocation_race() -> None:
    state = GraphSimState()
    state.create_node("agent-a")
    state.mint_capability("root", "agent-a", cap("call", "read", resources=("skill:*", "repo/*")))
    state.delegate("root", "child", "agent-a", cap("call", resources=("skill:run",)))
    state.start_process("proc-1", owner="agent-a", cap_id="child")
    state.add_credential("cred-1")

    preview = state.preview_delete("agent-a")

    assert "child" in preview["child_grants"]
    assert "proc-1" in preview["active_processes"]
    assert "cred-1" in preview["credentials"]
    assert isinstance(state.deletion_previews["agent-a"], DeletionPreviewState)
    assert state.apply_delete("agent-a") == "allow"
    assert state.capabilities["child"].revoked is True
    assert state.processes == {}
    assert state.credentials == set()

    before = GraphSimState()
    before.mint_capability("cap", "agent", cap("call", resources=("skill:run",)))
    assert before.use_capability("cap", "call", "skill:run") == "allow"
    before.revoke_capability("cap")
    assert before.use_capability("cap", "call", "skill:run") == "deny"

    after = GraphSimState()
    after.mint_capability("cap", "agent", cap("call", resources=("skill:run",)))
    after.revoke_capability("cap")
    assert after.use_capability("cap", "call", "skill:run") == "deny"


def test_graph_sim_state_has_no_free_dict_state_table_annotations() -> None:
    source = Path(__file__).resolve().parents[1] / "control_plane" / "dynamic_graph_sim.py"
    text = source.read_text()

    forbidden = (
        "ports: dict[str, dict[str, dict[str, Any]]]",
        "edges: dict[str, dict[str, Any]]",
        "processes: dict[str, dict[str, Any]]",
        "signals: list[dict[str, Any]]",
        "routes: dict[str, dict[str, Any]]",
        "outcomes: dict[str, dict[str, Any]]",
        "scores: dict[str, dict[str, Any]]",
        "winners: dict[str, dict[str, Any]]",
        "rewrites: dict[str, dict[str, Any]]",
        "policy_decisions: dict[str, dict[str, Any]]",
        "deletion_previews: dict[str, dict[str, Any]]",
    )
    for snippet in forbidden:
        assert snippet not in text


def test_recursive_process_stops_at_max_depth() -> None:
    state = GraphSimState()
    state.mint_capability("process-cap", "agent-a", cap("call", resources=("skill:run",)))

    state.recurse_process("recursive", owner="agent-a", cap_id="process-cap", max_depth=3)

    assert "max_depth_exceeded" in state.alerts
    assert any(event.event_type == "process.stopped" for event in state.ledger)
    assert len(state.processes) == 4


def test_replay_duplicate_idempotency_and_redaction() -> None:
    state = GraphSimState()
    state.create_node("agent-a")
    state.mint_capability("cap", "agent-a", cap("call", resources=("skill:run",)))
    state.emit_signal(
        "agent-a",
        "success",
        {
            "summary": "ok",
            "signed_token": "gitea_super_secret",
            "private_file": "agents/agent-a/secret.py",
        },
    )

    replayed = GraphSimState.replay([event.dump() for event in state.ledger])
    replayed.apply_event(state.ledger[-1])

    assert replayed.summary() == state.summary()
    assert len(replayed.ledger) == len(state.ledger)

    public = state.export_view("public")
    owner = state.export_view("owner")
    public_json = str(public)
    owner_json = str(owner)
    assert "gitea_super_secret" not in public_json
    assert "agents/agent-a/secret.py" not in public_json
    assert "gitea_super_secret" not in owner_json
    assert "secret.py" in owner_json
