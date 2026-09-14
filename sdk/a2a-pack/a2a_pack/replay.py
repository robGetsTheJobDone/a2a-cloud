"""Deterministic replay sessions.

A :class:`ReplaySession` is the ordered event log of a single skill run —
enough material to re-render the timeline (so a UI can scrub through it) and,
combined with double-injected side effects, to re-execute deterministically.

Determinism contract
--------------------

The runtime injects a per-run ``random_seed`` into the :class:`RunContext`;
skill code that wants its random draws to survive a replay must use
:meth:`RunContext.random` (which returns a :class:`random.Random` seeded from
that value). Direct ``random.random()`` / ``secrets.token_hex()`` calls bypass
the seed and will diverge under replay.

Wire format mirrors :mod:`a2a_pack.receipts` and :mod:`a2a_pack.grants`::

    "<base64url(json(payload))>.<base64url(signature)>"

Signing uses Ed25519 only. Sealers use ``A2A_REPLAY_SIGNING_KEY``; verifiers
use ``A2A_REPLAY_VERIFYING_KEY``.

What this module ships:
    * :class:`ReplayEvent` schema (per-step record).
    * :class:`ReplaySession` schema (the full ordered log + run metadata).
    * :class:`EventRecorder` — append-only in-memory recorder the runtime
      drives during a live run.
    * ``seal_replay_session`` / ``sign_replay_session`` / ``verify_replay_session``
      — the wire primitives.
    * ``iter_events`` — synchronous iterator over a verified session, used to
      drive UI scrub timelines.

What this module deliberately does *not* ship:
    * Re-execution of the agent. That requires the runtime to swap real LLM /
      tool calls for replay-doubles; the session log is the *input* to that
      machine, but the machine lives in :mod:`a2a_pack.runtime` / the server
      adapter.
    * Storage. Sessions are signed inline; the control plane picks where they
      land (object store, Postgres, filesystem).
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import secrets
import time
from typing import Any, Iterable, Iterator

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt


class ReplayInvalid(ValueError):
    """Raised by :func:`verify_replay_session` on any failure."""


# Canonical event kinds. The set is a closed enum on the wire (validated by
# pydantic), so adding a new kind is an explicit, reviewable change.
EVENT_KINDS: tuple[str, ...] = (
    "skill_start",
    "llm_call",
    "llm_response",
    "tool_call",
    "tool_response",
    "workspace_read",
    "workspace_write",
    "scope_request",
    "scope_approve",
    "handoff_start",
    "handoff_end",
    "artifact_write",
    "eval",
    "error",
    "skill_end",
)


class ReplayEvent(BaseModel):
    """One event in a replay session.

    Ordering is by ``idx`` (monotonic per session). ``ts_ms`` is the unix
    millisecond stamp of when the event was recorded; useful for the scrub UI
    but not authoritative for ordering (a re-run with cached LLM responses
    will produce earlier stamps).

    ``payload`` stays free-form so the schema can evolve without breaking
    older sessions; UIs render by ``kind`` and inspect ``payload`` defensively.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    idx: NonNegativeInt
    kind: str
    ts_ms: NonNegativeInt
    payload: dict[str, Any] = Field(default_factory=dict)


class ReplaySession(BaseModel):
    """A signed, ordered event log for a single skill run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    schema_version: int = 1

    # Identity (matches ExecutionReceipt fields so a session and its receipt
    # can be cross-linked by the control plane without a join table).
    agent_name: str
    agent_version: str = ""
    caller: str = ""
    task_id: str = ""
    skill_name: str

    # Determinism inputs. ``random_seed`` is what the runtime injected at
    # start; recording it lets a re-run reproduce the same RNG stream.
    random_seed: str = ""
    input_hash: str = ""

    # Timing
    started_at: NonNegativeInt = 0
    ended_at: NonNegativeInt = 0

    # The log itself
    events: tuple[ReplayEvent, ...] = ()

    # Cross-link to the run's signed receipt, if one was sealed.
    receipt_id: str = ""

    nonce: str = Field(default_factory=lambda: secrets.token_hex(8))


def _b64encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64decode(s: str) -> bytes:
    clean = s.strip()
    pad = "=" * (-len(clean) % 4)
    decoded = base64.urlsafe_b64decode(clean + pad)
    if _b64encode(decoded) != clean.rstrip("="):
        raise ValueError("non-canonical base64url encoding")
    return decoded


def _replay_signing_key_env() -> str:
    return os.environ.get("A2A_REPLAY_SIGNING_KEY", "").strip()


def _replay_verifying_key_env() -> str:
    return os.environ.get("A2A_REPLAY_VERIFYING_KEY", "").strip()


def _decode_key_material(value: str) -> bytes:
    clean = value.strip()
    if clean.startswith("base64:"):
        clean = clean[len("base64:") :].strip()
    try:
        return base64.b64decode(clean, validate=True)
    except binascii.Error:
        pad = "=" * (-len(clean) % 4)
        return base64.urlsafe_b64decode(clean + pad)


def _ed25519_private_key(value: str) -> Any:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if "BEGIN" in value:
        return serialization.load_pem_private_key(value.encode("utf-8"), password=None)
    return Ed25519PrivateKey.from_private_bytes(_decode_key_material(value))


def _ed25519_public_key(value: str) -> Any:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if "BEGIN" in value:
        return serialization.load_pem_public_key(value.encode("utf-8"))
    return Ed25519PublicKey.from_public_bytes(_decode_key_material(value))


def _ed25519_verifying_key() -> Any | None:
    verifying_key = _replay_verifying_key_env()
    if verifying_key:
        return _ed25519_public_key(verifying_key)
    signing_key = _replay_signing_key_env()
    if signing_key:
        return _ed25519_private_key(signing_key).public_key()
    return None


def _required_signing_key() -> Any:
    signing_key = _replay_signing_key_env()
    if not signing_key:
        raise RuntimeError("A2A_REPLAY_SIGNING_KEY is required")
    return _ed25519_private_key(signing_key)


def _required_verifying_key() -> Any:
    verifying_key = _ed25519_verifying_key()
    if verifying_key is None:
        raise ReplayInvalid("A2A_REPLAY_VERIFYING_KEY is required")
    return verifying_key


class EventRecorder:
    """Append-only event recorder. The runtime drives this during a run.

    Thread-safety: not guaranteed. A skill invocation is single-task by
    design; concurrent handoffs spawn their own recorder.
    """

    def __init__(
        self,
        *,
        agent_name: str,
        skill_name: str,
        agent_version: str = "",
        caller: str = "",
        task_id: str = "",
        random_seed: str = "",
        input_hash: str = "",
        receipt_id: str = "",
        clock: Any = None,
    ) -> None:
        self._agent_name = agent_name
        self._agent_version = agent_version
        self._skill_name = skill_name
        self._caller = caller
        self._task_id = task_id
        self._random_seed = random_seed
        self._input_hash = input_hash
        self._receipt_id = receipt_id
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._started_at = self._clock() // 1000
        self._events: list[ReplayEvent] = []
        self._idx = 0

    def record(self, kind: str, payload: dict[str, Any] | None = None) -> ReplayEvent:
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown event kind: {kind!r}")
        event = ReplayEvent(
            idx=self._idx,
            kind=kind,
            ts_ms=self._clock(),
            payload=dict(payload or {}),
        )
        self._events.append(event)
        self._idx += 1
        return event

    @property
    def events(self) -> tuple[ReplayEvent, ...]:
        return tuple(self._events)

    def build_session(
        self,
        *,
        session_id: str | None = None,
        ended_at: int | None = None,
        receipt_id: str | None = None,
    ) -> ReplaySession:
        end = ended_at if ended_at is not None else self._clock() // 1000
        return ReplaySession(
            # 128 bits: a session id is a bearer capability — the control plane
            # serves a public agent's replay stream to anyone holding the exact
            # id — so it is sized as a secret, not as a correlation id.
            session_id=session_id or secrets.token_hex(16),
            agent_name=self._agent_name,
            agent_version=self._agent_version,
            caller=self._caller,
            task_id=self._task_id,
            skill_name=self._skill_name,
            random_seed=self._random_seed,
            input_hash=self._input_hash,
            started_at=self._started_at,
            ended_at=end,
            events=tuple(self._events),
            receipt_id=receipt_id if receipt_id is not None else self._receipt_id,
        )


def seal_replay_session(
    session: ReplaySession,
) -> tuple[ReplaySession, str]:
    """Return ``(session, signed_token)``."""
    return session, sign_replay_session(session)


def sign_replay_session(
    session: ReplaySession,
) -> str:
    payload = session.model_dump_json(exclude_none=False).encode("utf-8")
    sig = _required_signing_key().sign(payload)
    return f"{_b64encode(payload)}.{_b64encode(sig)}"


def verify_replay_session(
    token: str,
) -> ReplaySession:
    """Parse + verify ``token``. Raises :class:`ReplayInvalid` on any failure.

    Checks signature and structural shape. Also validates that ``events`` is
    monotonically indexed and uses only known event kinds — replay sessions
    are forever; an unindexed log is a forged log.
    """
    if not token or "." not in token:
        raise ReplayInvalid("malformed replay token")
    payload_b64, sig_b64 = token.rsplit(".", 1)
    try:
        payload = _b64decode(payload_b64)
        sig = _b64decode(sig_b64)
    except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        raise ReplayInvalid(f"replay decode failed: {exc}") from exc

    verifying_key = _required_verifying_key()
    try:
        verifying_key.verify(sig, payload)
    except Exception as exc:  # noqa: BLE001
        raise ReplayInvalid("replay signature mismatch") from exc

    try:
        data = json.loads(payload)
        session = ReplaySession.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        raise ReplayInvalid(f"replay payload invalid: {exc}") from exc

    for i, event in enumerate(session.events):
        if event.idx != i:
            raise ReplayInvalid(
                f"replay event {i} has non-monotonic idx {event.idx}"
            )
        if event.kind not in EVENT_KINDS:
            raise ReplayInvalid(f"replay event {i} has unknown kind {event.kind!r}")

    return session


def iter_events(session: ReplaySession) -> Iterator[ReplayEvent]:
    """Yield events in their recorded order. Drives the UI scrub timeline."""
    yield from session.events


def filter_events(
    session: ReplaySession, *, kinds: Iterable[str] | None = None
) -> Iterator[ReplayEvent]:
    """Yield events in order, optionally filtered to ``kinds``."""
    allowed = set(kinds) if kinds else None
    for event in session.events:
        if allowed is None or event.kind in allowed:
            yield event


__all__ = [
    "EVENT_KINDS",
    "EventRecorder",
    "ReplayEvent",
    "ReplayInvalid",
    "ReplaySession",
    "filter_events",
    "iter_events",
    "seal_replay_session",
    "sign_replay_session",
    "verify_replay_session",
]
