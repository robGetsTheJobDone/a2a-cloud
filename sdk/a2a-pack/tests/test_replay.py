from __future__ import annotations

import base64

import pytest

from a2a_pack.replay import (
    EVENT_KINDS,
    EventRecorder,
    ReplayInvalid,
    filter_events,
    iter_events,
    seal_replay_session,
    sign_replay_session,
    verify_replay_session,
)


def _fake_clock() -> "_FakeClock":
    return _FakeClock()


class _FakeClock:
    def __init__(self) -> None:
        self.t = 1_700_000_000_000  # ms

    def __call__(self) -> int:
        self.t += 100
        return self.t


def test_recorder_appends_in_order() -> None:
    rec = EventRecorder(
        agent_name="finance-recon",
        skill_name="reconcile",
        random_seed="seed-abc",
        input_hash="hash-xyz",
        clock=_fake_clock(),
    )
    rec.record("skill_start", {"args": {"period": "2026-Q1"}})
    rec.record("llm_call", {"model": "claude", "prompt_hash": "h1"})
    rec.record("llm_response", {"tokens": 42})
    rec.record("workspace_read", {"path": "invoices/", "count": 1247})
    rec.record("artifact_write", {"path": "reports/2026-Q1.xlsx"})
    rec.record("skill_end", {"status": "ok"})

    events = rec.events
    assert [e.kind for e in events] == [
        "skill_start",
        "llm_call",
        "llm_response",
        "workspace_read",
        "artifact_write",
        "skill_end",
    ]
    assert [e.idx for e in events] == [0, 1, 2, 3, 4, 5]
    # ts_ms monotonically advances via the fake clock
    assert events[1].ts_ms > events[0].ts_ms


def test_recorder_rejects_unknown_kind() -> None:
    rec = EventRecorder(agent_name="x", skill_name="y")
    with pytest.raises(ValueError):
        rec.record("definitely_not_a_real_kind")


def test_seal_and_verify_roundtrip_under_ed25519() -> None:
    rec = EventRecorder(
        agent_name="finance-recon",
        agent_version="1.4.2",
        skill_name="reconcile",
        caller="controller@acme",
        task_id="task-abc",
        random_seed="seed-1",
        input_hash="hash-1",
        receipt_id="receipt-99",
    )
    rec.record("skill_start", {"args": {"period": "2026-Q1"}})
    rec.record("llm_call", {"prompt_hash": "h1"})
    rec.record("llm_response", {"output_hash": "h2"})
    rec.record("skill_end", {"status": "ok"})

    session = rec.build_session()
    _, token = seal_replay_session(session)

    verified = verify_replay_session(token)
    assert verified.session_id == session.session_id
    assert verified.receipt_id == "receipt-99"
    assert verified.random_seed == "seed-1"
    assert len(verified.events) == 4
    assert verified.events[0].kind == "skill_start"
    assert verified.events[-1].payload["status"] == "ok"


def test_verify_detects_tampering() -> None:
    rec = EventRecorder(agent_name="x", skill_name="y")
    rec.record("skill_start")
    rec.record("skill_end")
    _, token = seal_replay_session(rec.build_session())

    payload, sig = token.rsplit(".", 1)
    tampered = sig[:-1] + ("A" if sig[-1] != "A" else "B")
    with pytest.raises(ReplayInvalid):
        verify_replay_session(f"{payload}.{tampered}")


def test_verify_rejects_malformed() -> None:
    with pytest.raises(ReplayInvalid):
        verify_replay_session("garbage")
    with pytest.raises(ReplayInvalid):
        verify_replay_session("")


def test_verify_rejects_non_monotonic_events() -> None:
    """A forger that re-orders events must be caught."""
    # Build a session with a hand-crafted out-of-order events tuple, then
    # sign it so the signature is valid but the structural check fails.
    from a2a_pack.replay import ReplayEvent, ReplaySession

    bogus = ReplaySession(
        session_id="forged",
        agent_name="x",
        skill_name="y",
        events=(
            ReplayEvent(idx=0, kind="skill_start", ts_ms=1),
            ReplayEvent(idx=2, kind="skill_end", ts_ms=2),  # idx skip
        ),
    )
    token = sign_replay_session(bogus)
    with pytest.raises(ReplayInvalid, match="non-monotonic"):
        verify_replay_session(token)


def test_ed25519_roundtrip(monkeypatch) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    priv_bytes = key.private_bytes_raw()
    pub_bytes = key.public_key().public_bytes_raw()

    monkeypatch.setenv(
        "A2A_REPLAY_SIGNING_KEY",
        base64.b64encode(priv_bytes).decode(),
    )
    monkeypatch.setenv(
        "A2A_REPLAY_VERIFYING_KEY",
        base64.b64encode(pub_bytes).decode(),
    )

    rec = EventRecorder(agent_name="x", skill_name="y")
    rec.record("skill_start")
    rec.record("skill_end")
    _, token = seal_replay_session(rec.build_session())
    verified = verify_replay_session(token)
    assert verified.agent_name == "x"


def test_iter_and_filter() -> None:
    rec = EventRecorder(agent_name="x", skill_name="y")
    rec.record("skill_start")
    rec.record("llm_call")
    rec.record("llm_response")
    rec.record("tool_call")
    rec.record("tool_response")
    rec.record("skill_end")

    session = rec.build_session()
    assert [e.idx for e in iter_events(session)] == [0, 1, 2, 3, 4, 5]
    llm_only = list(filter_events(session, kinds=("llm_call", "llm_response")))
    assert [e.kind for e in llm_only] == ["llm_call", "llm_response"]


def test_event_kinds_are_closed_set() -> None:
    # If you add a new EVENT_KIND, update tests + the EVENT_KINDS tuple — and
    # remember that older sessions on the wire must still validate. Keeping
    # this assertion makes that contract explicit.
    assert "skill_start" in EVENT_KINDS
    assert "skill_end" in EVENT_KINDS
    assert "handoff_start" in EVENT_KINDS
    assert "handoff_end" in EVENT_KINDS
