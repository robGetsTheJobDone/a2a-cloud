"""Replay re-execution doubles.

Given a :class:`ReplaySession` (the live event log of a previous run), the
doubles in this module stand in for the real LLM, tool, and workspace
side-effect surfaces during a re-execution. Each double consumes events from
the session in their recorded order; any deviation from the recorded shape
raises :class:`ReplayDivergence` so the caller knows the session no longer
reproduces.

These doubles only re-execute deterministically when the original recording
populated the relevant events. For example, ``ReplayLLM.complete()`` replays
``llm_response`` events; if the original run never produced one, the first
call diverges.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

from .context import LLMCreds, LocalRunContext
from .replay import ReplayEvent, ReplaySession, filter_events


class ReplayDivergence(Exception):
    """Raised when a replay observation does not match the recorded log.

    Carries ``idx`` (position in the session where divergence was detected),
    ``recorded`` (the recorded payload, or ``None`` if the recording ran out
    of events), and ``observed`` (the call signature the agent attempted).
    """

    def __init__(
        self,
        idx: int,
        recorded: Any,
        observed: Any,
        *,
        message: str | None = None,
    ) -> None:
        self.idx = idx
        self.recorded = recorded
        self.observed = observed
        super().__init__(
            message
            or f"replay divergence at idx={idx}: recorded={recorded!r}, observed={observed!r}"
        )


def _next_event(
    iterator: Iterator[ReplayEvent], *, observed: Any
) -> ReplayEvent:
    try:
        return next(iterator)
    except StopIteration as exc:
        raise ReplayDivergence(-1, None, observed) from exc


@dataclass
class ReplayLLM:
    """Stand-in for the real LLM endpoint during replay.

    Each :meth:`complete` returns the ``response`` payload of the next
    ``llm_response`` event. If the recorded ``llm_call`` (paired by index)
    used a different model / prompt signature, divergence is raised.
    """

    session: ReplaySession
    creds: LLMCreds | None = None

    def __post_init__(self) -> None:
        # We pair llm_call + llm_response by their relative position so a
        # recording can store the *intent* (call) and the *outcome*
        # (response) independently. The simplest stable contract: the
        # consumer asks "what's the next response?" and the call signature
        # is checked against the most-recent llm_call event we've seen.
        self._responses = filter_events(self.session, kinds=("llm_response",))
        self._calls = filter_events(self.session, kinds=("llm_call",))

    def complete(self, **call_signature: Any) -> Any:
        call_event = _next_event(self._calls, observed=call_signature)
        response_event = _next_event(self._responses, observed=call_signature)
        recorded_call = call_event.payload
        # Mismatch on call signature is divergence. We only compare on the
        # keys the recording captured; extra observed keys are ignored so
        # callers can attach trace metadata without breaking replay.
        for key, value in recorded_call.items():
            if key not in call_signature:
                continue
            if call_signature[key] != value:
                raise ReplayDivergence(
                    call_event.idx,
                    recorded=recorded_call,
                    observed=call_signature,
                )
        return response_event.payload.get("response")


@dataclass
class ReplayToolCaller:
    """Stand-in for arbitrary tool / MCP calls during replay."""

    session: ReplaySession

    def __post_init__(self) -> None:
        self._responses = filter_events(self.session, kinds=("tool_response",))
        self._calls = filter_events(self.session, kinds=("tool_call",))

    def call(self, name: str, **call_signature: Any) -> Any:
        call_event = _next_event(self._calls, observed={"name": name, **call_signature})
        response_event = _next_event(
            self._responses, observed={"name": name, **call_signature}
        )
        recorded = call_event.payload
        if recorded.get("name") != name:
            raise ReplayDivergence(
                call_event.idx,
                recorded=recorded,
                observed={"name": name, **call_signature},
            )
        for key, value in recorded.items():
            if key == "name" or key not in call_signature:
                continue
            if call_signature[key] != value:
                raise ReplayDivergence(
                    call_event.idx,
                    recorded=recorded,
                    observed={"name": name, **call_signature},
                )
        return response_event.payload.get("response")


@dataclass
class ReplayWorkspaceClient:
    """Workspace stand-in that serves bytes from ``workspace_read`` events and
    refuses writes whose target path does not match the next recorded
    ``workspace_write`` event.

    The double tracks two cursors (read / write) independently so a skill that
    reads then writes can do so without having to interleave the recorded
    events in the same exact order across kinds.
    """

    session: ReplaySession

    def __post_init__(self) -> None:
        self._reads = filter_events(self.session, kinds=("workspace_read",))
        self._writes = filter_events(self.session, kinds=("workspace_write",))

    def read(self, path: str) -> bytes:
        event = _next_event(self._reads, observed={"path": path})
        recorded_path = event.payload.get("path")
        if recorded_path != path:
            raise ReplayDivergence(
                event.idx,
                recorded={"path": recorded_path},
                observed={"path": path},
            )
        data = event.payload.get("data", b"")
        if isinstance(data, str):
            return data.encode("utf-8")
        return bytes(data)

    def write(self, path: str, data: bytes | str) -> None:
        event = _next_event(self._writes, observed={"path": path})
        recorded_path = event.payload.get("path")
        if recorded_path != path:
            raise ReplayDivergence(
                event.idx,
                recorded={"path": recorded_path},
                observed={"path": path},
            )


async def replay_session(agent: Any, session: ReplaySession) -> Any:
    """Re-execute ``session`` against ``agent`` and return the handler result.

    Builds a doubled :class:`LocalRunContext` whose identity fields
    (``caller``, ``task_id``, ``grant_ids``, ``random_seed``) match the
    recording, wires :class:`ReplayLLM` / :class:`ReplayToolCaller` /
    :class:`ReplayWorkspaceClient` onto the context (private slots —
    advanced skills opt into them), and dispatches the recorded skill with
    the recorded ``args``.

    Returns the same value the live run returned. Raises
    :class:`ReplayDivergence` if the live re-execution observes a side effect
    that does not match the recording.
    """
    args = _extract_recorded_args(session)
    ctx: LocalRunContext[Any] = LocalRunContext(
        auth=_AnyAuth(),
        task_id=session.task_id or "replay",
        caller=session.caller,
        grant_ids=(),
        random_seed=session.random_seed,
        workspace=ReplayWorkspaceClient(session),  # type: ignore[arg-type]
    )
    # Doubles are attached via private slots so existing skill code that
    # uses them through helpers (e.g. ``ctx.llm``, ``ctx.tools``) can pick
    # them up; bare skills that only use ``ctx.random()`` reproduce
    # without needing any of these.
    object.__setattr__(ctx, "_replay_llm", ReplayLLM(session))
    object.__setattr__(ctx, "_replay_tools", ReplayToolCaller(session))
    return await agent.invoke(session.skill_name, ctx, **args)


def _extract_recorded_args(session: ReplaySession) -> dict[str, Any]:
    for event in session.events:
        if event.kind == "skill_start":
            args = event.payload.get("args", {})
            if isinstance(args, dict):
                return dict(args)
            return {}
    return {}


class _AnyAuth:
    """Placeholder auth principal for replay-built contexts.

    Skill code authored against typed auth models will still see a value on
    ``ctx.auth``; we don't try to reconstruct the original principal because
    replays don't re-execute auth-gated decisions.
    """

    scopes: tuple[str, ...] = ()


__all__ = [
    "ReplayDivergence",
    "ReplayLLM",
    "ReplayToolCaller",
    "ReplayWorkspaceClient",
    "replay_session",
]
