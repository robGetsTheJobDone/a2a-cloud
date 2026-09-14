from __future__ import annotations

import pytest

from control_plane.kernel_policy import (
    PolicyDecisionVerificationError,
    redacted_policy_decision,
    sign_policy_decision,
    verify_policy_decision,
)


def test_policy_decision_signature_verifies_and_redacts_sensitive_values() -> None:
    envelope = sign_policy_decision(
        decision_id="pd-secret",
        subject="policy",
        action="call",
        resource="secret/token/sk-live",
        decision="deny",
        effect="deny",
        reason="deny overrides allow",
        policy_refs=("platform-deny",),
        matched_rules=(
            {
                "policy_id": "platform-deny",
                "level": "platform",
                "effect": "deny",
                "actions": ["call"],
                "resources": ["secret/token/sk-live"],
            },
        ),
        issued_at=10,
        expires_at=20,
    )

    assert verify_policy_decision(envelope, now=19) is True
    redacted = redacted_policy_decision(envelope)
    assert redacted["resource"] == "[redacted]"
    assert redacted["matched_rules"][0]["resources"] == ["[redacted]"]
    assert redacted["signature"].endswith("...")
    assert redacted["signature_present"] is True


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda envelope: envelope.pop("signature"), "missing"),
        (lambda envelope: envelope.update({"resource": "repo/other.py"}), "mismatch"),
        (lambda envelope: envelope.update({"schema": "unknown"}), "schema"),
    ],
)
def test_policy_decision_verification_rejects_bad_envelopes(mutate: object, message: str) -> None:
    envelope = sign_policy_decision(
        decision_id="pd-1",
        subject="policy",
        action="write",
        resource="repo/private.py",
        decision="allow",
        effect="allow",
        reason="all matching policies allow",
        policy_refs=("owner-allow",),
        matched_rules=(),
        issued_at=10,
        expires_at=20,
    )
    mutate(envelope)  # type: ignore[operator]

    with pytest.raises(PolicyDecisionVerificationError, match=message):
        verify_policy_decision(envelope, now=15)


def test_policy_decision_verification_rejects_expired_envelope() -> None:
    envelope = sign_policy_decision(
        decision_id="pd-expired",
        subject="policy",
        action="write",
        resource="repo/private.py",
        decision="allow",
        effect="allow",
        reason="all matching policies allow",
        policy_refs=("owner-allow",),
        matched_rules=(),
        issued_at=10,
        expires_at=20,
    )

    with pytest.raises(PolicyDecisionVerificationError, match="expired"):
        verify_policy_decision(envelope, now=20)
