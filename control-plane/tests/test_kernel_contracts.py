from __future__ import annotations

from control_plane.kernel_contracts import KernelEvidenceEvent, evidence_key
from control_plane.kernel_policy import (
    PolicyDecisionEnvelope,
    PolicyRuleSnapshot,
    verify_policy_decision,
)


def test_policy_decision_envelope_owns_stable_payload_and_load_round_trip() -> None:
    envelope = PolicyDecisionEnvelope.create(
        decision_id="pd-typed",
        subject="policy",
        action="write",
        resource="repo/private.py",
        decision="deny",
        effect="deny",
        reason="deny overrides allow",
        policy_refs=("platform-deny", "owner-allow"),
        matched_rules=(
            PolicyRuleSnapshot(
                policy_id="platform-deny",
                level="platform",
                effect="deny",
                actions=("write",),
                resources=("repo/private.py",),
            ),
        ),
        evidence_refs=("pd-typed",),
        issued_at=10,
        expires_at=20,
    )

    payload = envelope.to_payload()
    assert payload == PolicyDecisionEnvelope.from_payload(payload).to_payload()
    assert payload["schema"] == "kernel_policy_decision@v1"
    assert payload["matched_rules"] == [
        {
            "policy_id": "platform-deny",
            "level": "platform",
            "effect": "deny",
            "actions": ["write"],
            "resources": ["repo/private.py"],
            "precedence": 0,
        }
    ]
    assert verify_policy_decision(payload, now=19)


def test_kernel_evidence_event_preserves_dashboard_payload_shape_and_key() -> None:
    payload = {"decision_id": "pd-1", "decision": "deny"}
    event = KernelEvidenceEvent(
        evidence_key=evidence_key(
            "policy_decision_recorded",
            payload,
            source={"kind": "review_loop", "job_id": "job-1"},
            source_event_id="evt-1",
        ),
        evidence_kind="policy_decision",
        event_type="policy_decision_recorded",
        title="Policy decision: deny",
        status="complete",
        severity="critical",
        message="deny overrides allow",
        source={"kind": "review_loop", "job_id": "job-1"},
        payload=payload,
    ).to_payload()

    assert event == {
        "type": "evidence_event",
        "evidence_key": (
            "policy_decision_recorded|kind:review_loop|job_id:job-1|"
            "event:evt-1|decision_id:pd-1"
        ),
        "evidence_kind": "policy_decision",
        "event_type": "policy_decision_recorded",
        "title": "Policy decision: deny",
        "status": "complete",
        "severity": "critical",
        "message": "deny overrides allow",
        "source": {"kind": "review_loop", "job_id": "job-1"},
        "payload": payload,
    }
