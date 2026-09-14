from __future__ import annotations

import pytest

from control_plane.dynamic_graph_sim import Capability, GraphSimState, PolicyRule


def _cap(
    actions: set[str],
    resources: tuple[str, ...],
    *,
    budget: int = 10,
    ttl: int | None = 100,
    delegation_depth: int = 2,
) -> Capability:
    return Capability(
        actions=frozenset(actions),
        resources=resources,
        budget=budget,
        expires_at=ttl,
        delegation_depth=delegation_depth,
    )


def test_generated_child_capabilities_never_exceed_parent() -> None:
    actions = ["read", "write", "call", "review"]
    resources = ["repo/a.py", "repo/private/b.py", "skill:run", "agent:subject"]

    for index, resource in enumerate(resources):
        state = GraphSimState()
        parent_actions = set(actions[: max(1, index + 1)])
        parent_resource = "repo/*" if resource.startswith("repo/") else "*"
        parent = _cap(parent_actions, (parent_resource,), budget=10 + index, delegation_depth=2)
        requested = _cap(set(actions), (resource,), budget=20, delegation_depth=2)
        state.mint_capability(f"parent-{index}", "parent", parent)

        decision = state.delegate(f"parent-{index}", f"child-{index}", "child", requested)

        if decision == "deny":
            assert f"child-{index}" not in state.capabilities
        else:
            child = state.capabilities[f"child-{index}"]
            assert child.is_subset_of(parent)
            assert child.budget <= parent.budget
            assert child.delegation_depth <= parent.delegation_depth - 1


def test_generated_revoke_use_interleavings_stop_later_use() -> None:
    for revoke_first in (False, True):
        state = GraphSimState()
        state.mint_capability("cap", "agent", _cap({"call"}, ("skill:*",)))
        if revoke_first:
            state.revoke_capability("cap")
            assert state.use_capability("cap", "call", "skill:run") == "deny"
        else:
            assert state.use_capability("cap", "call", "skill:run") == "allow"
            state.revoke_capability("cap")
            assert state.use_capability("cap", "call", "skill:run") == "deny"
        replayed = GraphSimState.replay([event.dump() for event in state.ledger])
        assert replayed.summary() == state.summary()


def test_generated_recursive_process_bounds() -> None:
    for max_depth in range(0, 6):
        state = GraphSimState()
        state.mint_capability("process-cap", "agent", _cap({"call"}, ("skill:*",)))
        state.recurse_process("root", owner="agent", cap_id="process-cap", max_depth=max_depth)

        assert "max_depth_exceeded" in state.alerts
        assert len(state.processes) == max_depth + 1
        replayed = GraphSimState.replay([event.dump() for event in state.ledger])
        assert replayed.summary() == state.summary()


def test_generated_route_signals_never_authorize_missing_capability() -> None:
    for rank in range(1, 6):
        state = GraphSimState()
        state.emit_signal("agent", "taste", {"rank": rank})
        state.emit_signal("agent", "success", {"rank": rank})

        assert state.select_route("run", {"agent": "missing-cap"}) is None
        assert state.ledger[-1].event_type == "route.rejected"


def test_generated_deletion_previews_cover_dependents() -> None:
    for count in range(1, 5):
        state = GraphSimState()
        state.create_node("agent")
        state.mint_capability("root", "agent", _cap({"call"}, ("skill:*",), delegation_depth=2))
        for index in range(count):
            state.delegate("root", f"child-{index}", "agent", _cap({"call"}, (f"skill:{index}",)))
            state.start_process(f"process-{index}", owner="agent", cap_id=f"child-{index}")
            state.add_credential(f"credential-{index}")

        preview = state.preview_delete("agent")
        assert "root" in preview["child_grants"]
        assert all(f"child-{index}" in preview["child_grants"] for index in range(count))
        assert len(preview["active_processes"]) == count
        assert len(preview["credentials"]) == count
        assert state.apply_delete("agent") == "allow"
        assert state.processes == {}
        assert state.credentials == set()


def test_generated_redaction_exports_hide_secret_values() -> None:
    secret_values = ["sk-test", "gitea_secret", "eyJ.jwt"]
    for index, secret in enumerate(secret_values):
        state = GraphSimState()
        state.emit_signal(
            "agent",
            "success",
            {
                "summary": f"case-{index}",
                "api_token": secret,
                "object_key": f"private/object-{index}",
                "private_file": f"repo/secret-{index}.py",
            },
        )

        public = str(state.export_view("public"))
        operator = str(state.export_view("operator"))
        owner = str(state.export_view("owner"))
        assert secret not in public
        assert secret not in operator
        assert secret not in owner
        assert f"private/object-{index}" not in public
        assert f"private/object-{index}" not in operator
        assert f"repo/secret-{index}.py" not in public


def test_generated_replay_tampering_changes_final_state() -> None:
    for tampered_budget in (0, 1, 9):
        state = GraphSimState(now=1)
        state.create_node("router")
        state.create_node("worker")
        state.create_port("router", "route:work", direction="output")
        state.create_port("worker", "invoke:work", direction="input")
        state.mint_capability("edge-cap", "router", _cap({"call"}, ("worker:*",), budget=5))
        assert state.propose_edge(
            "edge-router-worker",
            from_node="router",
            from_port="route:work",
            to_node="worker",
            to_port="invoke:work",
            edge_type="call",
            capability_id="edge-cap",
        ) == "allow"
        assert state.activate_edge(
            "edge-router-worker",
            policies=(PolicyRule("owner-call", "owner", "allow", frozenset({"call"}), ("worker:*",)),),
            decision_id="pd-edge",
        ) == "allow"

        events = [event.dump() for event in state.ledger]
        minted = next(event for event in events if event["event_type"] == "capability.minted")
        minted["payload"]["capability"]["budget"] = tampered_budget

        replayed = GraphSimState.replay(events)
        assert replayed.summary() != state.summary()
        assert replayed.capabilities["edge-cap"].budget == tampered_budget


@pytest.mark.parametrize(
    ("process_budget", "max_calls", "advance_seconds", "cost", "expected_reason"),
    [
        (1, 5, 0, 2, "budget_exhausted"),
        (5, 1, 0, 1, "max_calls_exceeded"),
        (5, 5, 2, 1, "ttl_expired"),
    ],
)
def test_generated_process_edge_resource_exhaustion_matrix(
    process_budget: int,
    max_calls: int,
    advance_seconds: int,
    cost: int,
    expected_reason: str,
) -> None:
    state = GraphSimState()
    state.create_node("router")
    state.create_node("worker")
    state.create_port("router", "route:work", direction="output")
    state.create_port("worker", "invoke:work", direction="input")
    state.mint_capability("process-cap", "router", _cap({"call"}, ("skill:*",), budget=20))
    state.mint_capability("edge-cap", "router", _cap({"call"}, ("worker:*",), budget=20))
    assert state.start_process(
        "process-1",
        owner="router",
        cap_id="process-cap",
        ttl=2,
        budget=process_budget,
        max_calls=max_calls,
    ) == "allow"
    assert state.propose_edge(
        "edge-process-worker",
        from_node="router",
        from_port="route:work",
        to_node="worker",
        to_port="invoke:work",
        edge_type="call",
        capability_id="edge-cap",
        process_id="process-1",
    ) == "allow"
    assert state.activate_edge(
        "edge-process-worker",
        policies=(PolicyRule("owner-call", "owner", "allow", frozenset({"call"}), ("worker:*",)),),
        decision_id="pd-edge",
    ) == "allow"
    if expected_reason == "max_calls_exceeded":
        assert state.use_edge("edge-process-worker", cost=cost) == "allow"
    if advance_seconds:
        state.advance_time(advance_seconds)

    assert state.use_edge("edge-process-worker", cost=cost) == "blocked"
    assert state.edges["edge-process-worker"]["state"] == "expired"
    assert state.edges["edge-process-worker"]["expired_reason"] == expected_reason
    assert "process-1" not in state.processes
    assert GraphSimState.replay([event.dump() for event in state.ledger]).summary() == state.summary()


def test_generated_delete_does_not_leak_child_grants_processes_or_credentials() -> None:
    for direct_children in range(1, 4):
        state = GraphSimState()
        state.create_node("agent")
        state.mint_capability(
            "root",
            "agent",
            _cap({"call", "read"}, ("skill:*", "repo/*"), budget=20, delegation_depth=3),
        )
        for index in range(direct_children):
            cap_id = f"child-{index}"
            state.delegate("root", cap_id, "agent", _cap({"call"}, (f"skill:{index}",)))
            state.start_process(f"process-{index}", owner="agent", cap_id=cap_id)
            state.add_credential(f"credential-{index}")

        preview = state.preview_delete("agent")
        assert sorted(preview["child_grants"]) == sorted(
            ["root", *[f"child-{index}" for index in range(direct_children)]]
        )
        assert state.apply_delete("agent") == "allow"
        assert state.credentials == set()
        assert state.processes == {}
        assert all(cap.revoked for cap_id, cap in state.capabilities.items() if cap_id.startswith("child-"))
