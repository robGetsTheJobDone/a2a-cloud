"""Signed kernel policy decision envelopes.

The graph kernel uses these envelopes as evidence, not as live mutation
authority. Simulation callers pass deterministic logical times so replayed
traces produce byte-stable signatures.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..config import settings

POLICY_DECISION_SCHEMA = "kernel_policy_decision@v1"
POLICY_DECISION_SIGNATURE_ALG = "hmac-sha256"
DEFAULT_POLICY_DECISION_TTL = 900

_SENSITIVE_MARKERS = ("token", "secret", "jwt", "credential", "key", "password")


class PolicyDecisionVerificationError(ValueError):
    """Raised when a signed kernel policy decision cannot be trusted."""


@dataclass(frozen=True)
class PolicyRuleSnapshot:
    policy_id: str
    level: str
    effect: str
    actions: tuple[str, ...] = ()
    resources: tuple[str, ...] = ()
    precedence: int = 0

    @classmethod
    def from_payload(cls, value: dict[str, Any]) -> "PolicyRuleSnapshot":
        return cls(
            policy_id=_required_text(value, "policy_id"),
            level=_required_text(value, "level"),
            effect=_required_text(value, "effect"),
            actions=_text_tuple(value.get("actions")),
            resources=_text_tuple(value.get("resources")),
            precedence=int(value.get("precedence") or 0),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "level": self.level,
            "effect": self.effect,
            "actions": list(self.actions),
            "resources": list(self.resources),
            "precedence": self.precedence,
        }


@dataclass(frozen=True)
class PolicyDecisionEnvelope:
    schema: str
    decision_id: str
    subject: str
    action: str
    resource: str
    decision: str
    effect: str | None
    reason: str
    policy_refs: tuple[str, ...]
    matched_rules: tuple[PolicyRuleSnapshot, ...]
    evidence_refs: tuple[str, ...]
    issued_at: int | str
    expires_at: int | str
    signature_algorithm: str
    signed_by: str
    signature: str | None = None

    @classmethod
    def create(
        cls,
        *,
        decision_id: str,
        subject: str,
        action: str,
        resource: str,
        decision: str,
        effect: str | None,
        reason: str,
        policy_refs: tuple[str, ...],
        matched_rules: tuple[PolicyRuleSnapshot, ...],
        evidence_refs: tuple[str, ...] = (),
        issued_at: int | str | None = None,
        expires_at: int | str | None = None,
        signed_by: str = "control-plane:kernel-policy",
    ) -> "PolicyDecisionEnvelope":
        issued = issued_at if issued_at is not None else _utc_now()
        expires = (
            expires_at
            if expires_at is not None
            else _expiry_for(issued, DEFAULT_POLICY_DECISION_TTL)
        )
        unsigned = cls(
            schema=POLICY_DECISION_SCHEMA,
            decision_id=decision_id,
            subject=subject,
            action=action,
            resource=resource,
            decision=decision,
            effect=effect,
            reason=reason,
            policy_refs=policy_refs,
            matched_rules=matched_rules,
            evidence_refs=evidence_refs,
            issued_at=issued,
            expires_at=expires,
            signature_algorithm=POLICY_DECISION_SIGNATURE_ALG,
            signed_by=signed_by,
        )
        return unsigned.with_signature(_signature(unsigned.to_unsigned_payload()))

    @classmethod
    def from_payload(cls, value: dict[str, Any]) -> "PolicyDecisionEnvelope":
        return cls(
            schema=_required_text(value, "schema"),
            decision_id=_required_text(value, "decision_id"),
            subject=_required_text(value, "subject"),
            action=_required_text(value, "action"),
            resource=_required_text(value, "resource"),
            decision=_required_text(value, "decision"),
            effect=_optional_text(value.get("effect")),
            reason=_required_text(value, "reason"),
            policy_refs=_text_tuple(value.get("policy_refs")),
            matched_rules=tuple(
                PolicyRuleSnapshot.from_payload(item)
                for item in _dict_tuple(value.get("matched_rules"))
            ),
            evidence_refs=_text_tuple(value.get("evidence_refs")),
            issued_at=_required_time(value, "issued_at"),
            expires_at=_required_time(value, "expires_at"),
            signature_algorithm=_required_text(value, "signature_algorithm"),
            signed_by=_required_text(value, "signed_by"),
            signature=_optional_text(value.get("signature")),
        )

    def with_signature(self, signature: str) -> "PolicyDecisionEnvelope":
        return PolicyDecisionEnvelope(
            schema=self.schema,
            decision_id=self.decision_id,
            subject=self.subject,
            action=self.action,
            resource=self.resource,
            decision=self.decision,
            effect=self.effect,
            reason=self.reason,
            policy_refs=self.policy_refs,
            matched_rules=self.matched_rules,
            evidence_refs=self.evidence_refs,
            issued_at=self.issued_at,
            expires_at=self.expires_at,
            signature_algorithm=self.signature_algorithm,
            signed_by=self.signed_by,
            signature=signature,
        )

    def to_unsigned_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "decision_id": self.decision_id,
            "subject": self.subject,
            "action": self.action,
            "resource": self.resource,
            "decision": self.decision,
            "effect": self.effect,
            "reason": self.reason,
            "policy_refs": list(self.policy_refs),
            "matched_rules": [rule.to_payload() for rule in self.matched_rules],
            "evidence_refs": list(self.evidence_refs),
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "signature_algorithm": self.signature_algorithm,
            "signed_by": self.signed_by,
        }

    def to_payload(self) -> dict[str, Any]:
        payload = self.to_unsigned_payload()
        payload["signature"] = self.signature
        return payload


def build_policy_decision_envelope(
    *,
    decision_id: str,
    subject: str,
    action: str,
    resource: str,
    decision: str,
    effect: str | None,
    reason: str,
    policy_refs: tuple[str, ...],
    matched_rules: tuple[dict[str, Any] | PolicyRuleSnapshot, ...],
    evidence_refs: tuple[str, ...] = (),
    issued_at: int | str | None = None,
    expires_at: int | str | None = None,
    signed_by: str = "control-plane:kernel-policy",
) -> PolicyDecisionEnvelope:
    rule_snapshots = tuple(
        item if isinstance(item, PolicyRuleSnapshot) else PolicyRuleSnapshot.from_payload(item)
        for item in matched_rules
    )
    return PolicyDecisionEnvelope.create(
        decision_id=decision_id,
        subject=subject,
        action=action,
        resource=resource,
        decision=decision,
        effect=effect,
        reason=reason,
        policy_refs=policy_refs,
        matched_rules=rule_snapshots,
        evidence_refs=evidence_refs,
        issued_at=issued_at,
        expires_at=expires_at,
        signed_by=signed_by,
    )


def sign_policy_decision(
    *,
    decision_id: str,
    subject: str,
    action: str,
    resource: str,
    decision: str,
    effect: str | None,
    reason: str,
    policy_refs: tuple[str, ...],
    matched_rules: tuple[dict[str, Any], ...],
    evidence_refs: tuple[str, ...] = (),
    issued_at: int | str | None = None,
    expires_at: int | str | None = None,
    signed_by: str = "control-plane:kernel-policy",
) -> dict[str, Any]:
    return build_policy_decision_envelope(
        decision_id=decision_id,
        subject=subject,
        action=action,
        resource=resource,
        decision=decision,
        effect=effect,
        reason=reason,
        policy_refs=policy_refs,
        matched_rules=matched_rules,
        evidence_refs=evidence_refs,
        issued_at=issued_at,
        expires_at=expires_at,
        signed_by=signed_by,
    ).to_payload()


def verify_policy_decision(
    envelope: dict[str, Any],
    *,
    now: int | str | datetime | None = None,
) -> bool:
    if not isinstance(envelope, dict):
        raise PolicyDecisionVerificationError("policy decision envelope must be an object")
    parsed = PolicyDecisionEnvelope.from_payload(envelope)
    if parsed.schema != POLICY_DECISION_SCHEMA:
        raise PolicyDecisionVerificationError("unsupported policy decision schema")
    if parsed.signature_algorithm != POLICY_DECISION_SIGNATURE_ALG:
        raise PolicyDecisionVerificationError("unsupported policy decision signature algorithm")
    signature = parsed.signature
    if not isinstance(signature, str) or not signature:
        raise PolicyDecisionVerificationError("policy decision signature is missing")
    expected = _signature(parsed.to_unsigned_payload())
    if not hmac.compare_digest(signature, expected):
        raise PolicyDecisionVerificationError("policy decision signature mismatch")
    current = _coerce_time(now if now is not None else _utc_now())
    expires = _coerce_time(parsed.expires_at)
    if current >= expires:
        raise PolicyDecisionVerificationError("policy decision has expired")
    return True


def redacted_policy_decision(
    envelope: dict[str, Any] | PolicyDecisionEnvelope,
) -> dict[str, Any]:
    """Return a dashboard-safe view that keeps provenance and hides secrets."""
    payload = envelope.to_payload() if isinstance(envelope, PolicyDecisionEnvelope) else envelope
    redacted = _redact(payload)
    signature = payload.get("signature")
    if isinstance(signature, str) and signature:
        redacted["signature"] = f"{signature[:12]}..."
        redacted["signature_present"] = True
    else:
        redacted["signature"] = None
        redacted["signature_present"] = False
    return redacted


def _signature(envelope: dict[str, Any]) -> str:
    return hmac.new(
        _signing_secret().encode("utf-8"),
        _canonical_payload(envelope).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _canonical_payload(envelope: dict[str, Any]) -> str:
    body = {key: value for key, value in envelope.items() if key != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)


def _signing_secret() -> str:
    return settings.protocol_registry_signing_secret or settings.jwt_secret


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _expiry_for(issued_at: int | str, ttl_seconds: int) -> int | str:
    if isinstance(issued_at, int):
        return issued_at + ttl_seconds
    issued = _coerce_time(issued_at)
    return datetime.fromtimestamp(issued + ttl_seconds, tz=timezone.utc).replace(microsecond=0).isoformat()


def _coerce_time(value: int | str | datetime) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, datetime):
        return int(value.timestamp())
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except ValueError as exc:
        raise PolicyDecisionVerificationError("policy decision time is invalid") from exc


def _required_text(value: dict[str, Any], key: str) -> str:
    text = _optional_text(value.get(key))
    if text is None:
        raise PolicyDecisionVerificationError(f"policy decision {key} is missing")
    return text


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str | int):
        raise PolicyDecisionVerificationError("policy decision text field is invalid")
    text = str(value).strip()
    return text or None


def _required_time(value: dict[str, Any], key: str) -> int | str:
    item = value.get(key)
    if not isinstance(item, int | str):
        raise PolicyDecisionVerificationError(f"policy decision {key} is missing")
    return item


def _text_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list | tuple):
        raise PolicyDecisionVerificationError("policy decision list field is invalid")
    out: list[str] = []
    for item in value:
        text = _optional_text(item)
        if text is None:
            raise PolicyDecisionVerificationError("policy decision list item is invalid")
        out.append(text)
    return tuple(out)


def _dict_tuple(value: Any) -> tuple[dict[str, Any], ...]:
    if value is None:
        return ()
    if not isinstance(value, list | tuple):
        raise PolicyDecisionVerificationError("policy decision object list is invalid")
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise PolicyDecisionVerificationError("policy decision object list item is invalid")
        out.append(item)
    return tuple(out)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if key == "signature":
                continue
            if any(marker in lowered for marker in _SENSITIVE_MARKERS):
                out[str(key)] = "[redacted]"
            else:
                out[str(key)] = _redact(item)
        return out
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        lowered = value.lower()
        if value.startswith(("sk-", "gitea_", "eyJ", "eyj")) or any(
            marker in lowered for marker in _SENSITIVE_MARKERS
        ):
            return "[redacted]"
    return value
