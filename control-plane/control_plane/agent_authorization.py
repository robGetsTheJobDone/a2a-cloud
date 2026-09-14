"""Central authorization decisions for discovering and reusing agents.

``Agent.public`` makes a card discoverable and the deployed interface usable;
it never grants access to the managed source repository.  Source-bearing
actions are controlled independently through organization membership and the
owner-selected fork policy.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from fastapi import HTTPException
from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from .models import Agent, AgentSession, OrganizationMember, User


AgentAction = Literal[
    "discover",
    "invoke",
    "use_existing",
    "compose",
    "fork",
    "edit_existing",
]

_SOURCE_WRITER_ROLES = frozenset({"owner", "admin", "maintainer"})
_VALID_FORK_POLICIES = frozenset({"private", "organization", "public"})


async def may_read_agent_evidence(
    session: AsyncSession,
    *,
    user: User,
    agent: Agent,
) -> bool:
    """Whether ``user`` may read receipts / replay sessions of ``agent``.

    This is the single definition of "evidence reader" for the platform.  Every
    surface that serves receipt payloads or replay events — ``agent_receipts``,
    ``agent_sessions``, and the org compliance decision-record detail route —
    resolves through it, because the last time these three encoded the rule
    independently they silently disagreed.

    The rule: **whoever may edit the agent may read its evidence**, plus
    platform admins.  ``decide_agent_access(action="edit_existing")`` is the
    authority for the first half, so an organization maintainer who can push new
    source to an agent is not mysteriously blocked from the receipts of the code
    they ship.  Everything weaker is excluded on purpose: ``discover``/``invoke``
    is granted to the whole world by ``agent.public`` and to every active org
    member for a private agent, while a receipt carries the **caller's** prompt,
    input hash, grant ids and result preview.  A plain org member can therefore
    call the agent and read its card, and still not read who called it or with
    what.
    """
    if getattr(user, "is_admin", False):
        return True
    decision = await decide_agent_access(
        session, user=user, agent=agent, action="edit_existing"
    )
    return decision.allowed


def evidence_caller_ref(user: User) -> str:
    """The ``caller`` value that identifies ``user`` on an evidence row.

    ``user:<id>`` is the only caller string the *platform* mints for an
    identifiable human: ``routes/agents._persist_agent_api_receipt`` writes it
    for every call made through the agent API, and ``routes/subagent_runs``
    already scopes a run's receipts with exactly this predicate.  Reusing it —
    rather than a looser parser that would also accept a bare ``"7"`` — keeps
    the grant to values the platform itself produced, and keeps it a SQL
    equality so the list queries can filter in the database.

    The gateway (``agent_ingress._external_caller``) deliberately records
    ``"anonymous"`` / ``"credential-present"`` instead, because it cannot assert
    who authenticated to the agent.  Those rows therefore match nobody here,
    which is the correct outcome: the platform does not know whose they are.
    """
    return f"user:{int(user.id)}"


def is_own_evidence(caller: str | None, user: User | None) -> bool:
    """Whether an evidence row records a call *made by* ``user``.

    The caller of a run is entitled to the receipt for it, so refusing them
    that evidence is not a policy anyone chose.  It grants nothing about
    anyone else's calls:
    the predicate is an equality against one row's caller.
    """
    if user is None:
        return False
    return (caller or "") == evidence_caller_ref(user)


@dataclass(frozen=True)
class EvidenceRead:
    """An authorized read of an agent's evidence collections.

    ``own_caller is None`` means every row of the agent; otherwise only rows
    whose ``caller`` equals that string — the reader's own calls.
    """

    agent: Agent
    own_caller: str | None

    @property
    def scoped_to_caller(self) -> bool:
        return self.own_caller is not None


async def agent_is_discoverable(
    session: AsyncSession,
    *,
    user: User,
    agent: Agent,
) -> bool:
    """Whether ``user`` may see that ``agent`` exists at all."""
    if getattr(user, "is_admin", False):
        return True
    decision = await decide_agent_access(
        session, user=user, agent=agent, action="discover"
    )
    return decision.allowed


async def resolve_agent_for_evidence_read(
    session: AsyncSession,
    *,
    name: str,
    user: User | None,
) -> EvidenceRead:
    """Resolve ``name`` for an evidence read, or raise the right denial.

    Shared by ``GET /v1/agents/{name}/receipts[/{id}]`` and
    ``GET /v1/agents/{name}/sessions`` so the two cannot drift.  Three outcomes:

    * an **evidence reader** (:func:`may_read_agent_evidence`) reads every row;
    * anyone else who can *discover* the agent reads **only their own calls** —
      the rows whose ``caller`` is :func:`evidence_caller_ref`.  A buyer billed
      for a call must be able to fetch the receipt for it, and serving that from
      the same collection avoids a second, parallel "my receipts" surface that
      would drift from this one;
    * everyone else gets 404 carrying the body of a genuinely missing agent —
      including a logged-in caller who cannot discover the agent, so this is not
      an oracle for private agent names.  Anonymous callers have no identity to
      scope to and always land here.
    """
    agent = (
        await session.execute(select(Agent).where(Agent.name == name))
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(404, "agent not found")
    if user is not None:
        if await may_read_agent_evidence(session, user=user, agent=agent):
            return EvidenceRead(agent, None)
        if await agent_is_discoverable(session, user=user, agent=agent):
            return EvidenceRead(agent, evidence_caller_ref(user))
    raise HTTPException(404, "agent not found")


async def authorize_session_read(
    session: AsyncSession,
    *,
    row: AgentSession,
    user: User | None,
) -> None:
    """Gate for ``GET /v1/sessions/{session_id}``.

    A replay session's event stream carries ``skill_start`` with the caller's
    **complete** validated arguments (``a2a_pack.agent``), so holding the id is
    not a grant however unguessable the id is, and ``agent.public`` — which
    means anyone may *call* the agent — is not one either.

    Exactly two principals read a session:

    * an **evidence reader** of its agent (:func:`may_read_agent_evidence`), or
      a platform admin when the agent row is gone (``agents.py`` nulls
      ``agent_id`` on delete, leaving no owner to consult);
    * the **caller who produced it** (:func:`is_own_evidence`) — their own
      inputs, replayed back to them.

    There is deliberately **no anonymous branch**.  An earlier version granted
    one to sessions "published" by a passing public proof run, joined through
    ``AgentProofRun.events[*].payload.session_id``.  That blob is stored
    verbatim from whatever the agent process returned, and the agent process is
    owner-supplied code, so the grant let an owner republish a *third party's*
    session by naming its id — a publication decision the control plane never
    made.  It was also unreachable in production (a proof run invokes the pod
    directly and never writes an ``agent_sessions`` row) and made every
    unauthenticated request scan proof-run event blobs.

    Nothing anonymous reaches a sealed session by another door either.  The
    marketing ``/replay`` page used to decode the sealed token out of the
    ``events`` blob republished by ``/v1/public/agent-proofs``; that feed now
    serves ``PublicAgentProofOut``, which carries no ``events``, so ``/replay``
    without a handle shows the run's public standing and says the timeline is
    served only to the principals below.  Adding an anonymous branch here would
    reopen that path, not merely this route.

    Anonymous callers get 404 so the URL never confirms a session exists.
    """
    if user is not None:
        if is_own_evidence(row.caller, user):
            return
        agent = (
            await session.get(Agent, row.agent_id)
            if row.agent_id is not None
            else None
        )
        if agent is not None and await may_read_agent_evidence(
            session, user=user, agent=agent
        ):
            return
        if agent is None and getattr(user, "is_admin", False):
            return
        raise HTTPException(403, "not authorized to view this session")
    raise HTTPException(404, "session not found")


@dataclass(frozen=True)
class AgentAccessDecision:
    action: AgentAction
    allowed: bool
    reason: str
    basis: str
    organization_role: str | None = None

    def snapshot(self) -> dict[str, object]:
        return asdict(self)


def visible_agents_clause(user_id: int) -> ColumnElement[bool]:
    """SQL predicate matching cards the user may discover."""
    organization_member = exists(
        select(OrganizationMember.id).where(
            OrganizationMember.organization_id == Agent.organization_id,
            OrganizationMember.user_id == user_id,
            OrganizationMember.active.is_(True),
        )
    )
    return or_(
        Agent.owner_id == user_id,
        Agent.public.is_(True),
        organization_member,
    )


async def active_organization_ids(
    session: AsyncSession,
    user_id: int,
) -> tuple[int, ...]:
    values = (
        await session.execute(
            select(OrganizationMember.organization_id).where(
                OrganizationMember.user_id == user_id,
                OrganizationMember.active.is_(True),
            )
        )
    ).scalars().all()
    return tuple(sorted({int(value) for value in values}))


async def decide_agent_access(
    session: AsyncSession,
    *,
    user: User,
    agent: Agent,
    action: AgentAction,
) -> AgentAccessDecision:
    if int(agent.owner_id) == int(user.id):
        return AgentAccessDecision(action, True, "agent owner", "owner")

    membership = None
    if agent.organization_id is not None:
        membership = (
            await session.execute(
                select(OrganizationMember).where(
                    OrganizationMember.organization_id == agent.organization_id,
                    OrganizationMember.user_id == user.id,
                    OrganizationMember.active.is_(True),
                )
            )
        ).scalar_one_or_none()

    if action == "edit_existing":
        if membership is not None and membership.role in _SOURCE_WRITER_ROLES:
            return AgentAccessDecision(
                action,
                True,
                "organization source maintainer",
                "organization",
                membership.role,
            )
        return AgentAccessDecision(
            action,
            False,
            "editing requires ownership or an organization maintainer role",
            "denied",
            membership.role if membership is not None else None,
        )

    if action == "fork":
        policy = str(
            getattr(agent, "fork_policy", "organization") or "organization"
        ).lower()
        if policy not in _VALID_FORK_POLICIES:
            policy = "private"
        if membership is not None and policy in {"organization", "public"}:
            return AgentAccessDecision(
                action,
                True,
                "organization fork policy permits source reuse",
                "organization",
                membership.role,
            )
        if agent.public and policy == "public":
            return AgentAccessDecision(
                action,
                True,
                "owner explicitly permits public forks",
                "public_fork_policy",
            )
        return AgentAccessDecision(
            action,
            False,
            "source is not licensed for this caller to fork",
            "denied",
            membership.role if membership is not None else None,
        )

    if membership is not None:
        return AgentAccessDecision(
            action,
            True,
            "active organization member",
            "organization",
            membership.role,
        )

    if agent.public:
        return AgentAccessDecision(
            action,
            True,
            "public deployed interface",
            "public_interface",
        )

    return AgentAccessDecision(
        action,
        False,
        "agent is private to another owner or organization",
        "denied",
    )


async def require_agent_access(
    session: AsyncSession,
    *,
    user: User,
    agent: Agent,
    action: AgentAction,
) -> AgentAccessDecision:
    decision = await decide_agent_access(
        session,
        user=user,
        agent=agent,
        action=action,
    )
    if not decision.allowed:
        raise HTTPException(
            403,
            {
                "error": "agent_action_denied",
                "action": action,
                "agent": agent.name,
                "reason": decision.reason,
            },
        )
    return decision
