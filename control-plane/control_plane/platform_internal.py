"""One authoritative answer to "does a2a run this agent for itself?".

The marketing site has to keep the platform's own scaffolds, build specialists
and test fixtures out of anything a visitor reads as social proof. It used to
decide that from a hand-maintained list of agent names in the Next.js app,
which is the wrong place for it: the platform, not the marketing site, is what
knows which accounts and which names belong to a2a.

Nothing new is recorded to answer the question. Both halves already exist:

* **Name** — :data:`control_plane.auth.PLATFORM_TOOLCHAIN_AGENTS` is the set of
  build specialists shipped from this repo. ``auth.py`` already grants them a
  privilege no marketplace listing gets (driving the source/deploy surface on a
  caller's behalf), and its own comment says they "are not marketplace
  listings". Membership is fixed in code, so a name is enough.
* **Owner** — the two service principals that exist only to run the platform's
  own acceptance checks: the Agent Studio cleanup harness and the
  kernel-simulation E2E user. Neither ever produces something a visitor is
  meant to browse.

Deliberately *not* a signal, and each for a reason that was checked against the
live registry rather than reasoned about:

* **The shape of an agent's name.** The platform mints ``<slug>-<4 hex>-<n>``
  for studio idea-factory batches and hands those agents to real users
  without renaming them. A regex over that shape would permanently unlist
  customers' agents.
"""
from __future__ import annotations

from .auth import PLATFORM_TOOLCHAIN_AGENTS
from .config import settings
from .e2e_users import DEFAULT_E2E_EMAIL

def platform_internal_owner_emails() -> frozenset[str]:
    """Accounts the platform deploys agents under, lowercased.

    Only test/acceptance principals belong here: an account is in this set when
    *everything* it will ever own is a2a's own machinery.

    Read at call time rather than frozen at import so the harness address stays
    whatever ``A2A_CP_*`` configured it to be.
    """
    candidates = (
        settings.agent_studio_harness_cleanup_owner_email,
        DEFAULT_E2E_EMAIL,
    )
    return frozenset(
        email.strip().lower() for email in candidates if email and email.strip()
    )


def is_platform_internal_agent(name: str | None, owner_email: str | None) -> bool:
    """True when this agent is a2a's own rather than a seller's listing.

    False is the answer for everything else, including "we have no signal
    either way" — the flag only ever makes a positive claim.
    """
    if (name or "").strip().lower() in PLATFORM_TOOLCHAIN_AGENTS:
        return True
    return (owner_email or "").strip().lower() in platform_internal_owner_emails()
