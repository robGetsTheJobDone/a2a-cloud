"""HTTP rate limiting for the surfaces that actually need one.

Until this module existed none of the ~300 control-plane routes had any
request-rate ceiling: 30 rapid unauthenticated ``GET /v1/public/agents``
answered 200 thirty times, and the one-time-code redemption endpoints under
``/v1/auth`` could be guessed at indefinitely.

Four decisions shape everything here.

**A wrong limiter is worse than none.** Locking a paying user out is a worse
outcome than the scraping it prevents, so every default errs permissive:

* the policy is an *allowlist* of endpoint classes (:data:`_EXACT_RULES` /
  :data:`_PREFIX_RULES`), not a blanket. Authenticated CRUD, the dashboard's
  poll loops, ``/v1/me``, the CLI's deploy-status poll, the SDK runtime's
  writebacks (``/v1/agents/{name}/meta-runs``,
  ``/v1/agents/{name}/protocol-simulations``) and the hosted-agent invoke path
  carry no rule at all;
* first-party in-cluster callers are exempt (:func:`_ip_key`);
* when a key cannot be derived the request is allowed, never bucketed into a
  shared "unknown" counter;
* the backend failing means *allowed*, and a repeatedly failing backend is
  taken out of the request path entirely (:class:`_Breaker`) so a Redis outage
  cannot turn into an API outage - or even into added latency.

**The count has to be shared.** ``deploy/20-deployment.yaml`` runs the API at
Knative ``max-scale: 4`` with ``A2A_CP_UVICORN_WORKERS=4``: up to sixteen
processes. A process-local counter - the shape
:class:`InMemoryRateLimitBackend` keeps for tests and Redis-less development -
would bound one worker and let the other fifteen through. Production uses
:class:`RedisRateLimitBackend` against the Redis this service already runs.

**A per-account bucket must never be fillable by someone else, and must never
be escapable by its owner.** Those are the two halves of the same requirement
and both are load-bearing:

* fillable-by-someone-else makes the limiter the denial of service, so the
  account key is derived only from a *verified* credential the caller actually
  holds - never from an email, username, or account id read out of the
  request;
* escapable-by-its-owner makes the ceiling opt-in for the attacker. An earlier
  revision of :func:`_account_key` fell back to ``sha256(token)`` for anything
  it could not verify, which keyed the bucket on the credential *string*.
  Refresh-token rotation is disabled in realm ``a2acloud``, so one refresh
  token mints an unbounded stream of fresh Keycloak access tokens - and each
  one was a brand-new budget. Identity now comes from a verified subject
  instead: :func:`_platform_subject` for the control plane's own HS256 tokens,
  :func:`_keycloak_subject` for Keycloak's RS256 ones. What does *not* get an
  account bucket is as deliberate as what does - see :func:`_platform_subject`
  on the three typed credentials that live in somebody else's process.

**The client address has to survive a hostile ``X-Forwarded-For``.** The header
is attacker-controlled up to the first proxy that rewrites it, so taking its
first hop - what :func:`analytics.client_ip` does for PostHog geo - gives an
attacker three separate wins: rotate the value for unlimited requests, set a
private value to be exempted, or set a *victim's* value to spend the victim's
budget and lock them out of sign-in. :func:`_client_address` therefore takes
the **rightmost** hop that is not our own infrastructure. Whatever the caller
sends sits to the left of the address the ingress appends, so it can never be
the answer - and that holds whether or not Traefik strips inbound
``X-Forwarded-*``, which is not configured in this repo and so cannot be
relied on.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

import jwt
from fastapi import HTTPException, Request

from .config import settings
from .metrics import observe_rate_limit_client_source, observe_rate_limit_decision

log = logging.getLogger(__name__)

_KEY_PREFIX = "control-plane:rate-limit"

# Key scopes. ``ip`` buckets by client address, ``account`` by the identity the
# caller proved, ``audience`` by the agent a request names in its body,
# ``global`` by nothing - a platform-wide budget.
IP = "ip"
ACCOUNT = "account"
AUDIENCE = "audience"
GLOBAL = "global"

_IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


@dataclass(frozen=True)
class RateLimitRule:
    """One bucket: ``limit`` requests per ``window_seconds`` per key."""

    name: str
    limit: int
    window_seconds: int
    scope: str
    # Printed in the 429 body, so it has to read as an instruction to a
    # developer rather than as an accusation.
    subject: str


# --------------------------------------------------------------------------
# The policy
#
# Numbers are chosen against measured caller behaviour, not from a round-number
# instinct; the reasoning for each sits on the rule. Every legitimate
# high-frequency caller is either exempt (in-cluster, see ``_ip_key``) or has
# at least an order of magnitude of headroom.
# --------------------------------------------------------------------------

# The two endpoints where a *guessable* secret is presented: the 32-byte
# (``secrets.token_urlsafe(32)``) one-time browser exchange code and the
# same-origin confirmation that consumes it. This is the only place in the
# control plane with anything brute-forceable at all - there is no password
# login here, Keycloak owns that at ``auth.a2acloud.io``.
#
# 120/5min is 24/min per address. A CLI login redeems exactly once, so this is
# 120 sign-ins per five minutes from a single address: a NAT'd office, VPN
# concentrator or campus egress does not reach it. It still cuts a code-guessing
# loop from network speed to ~35k/day against a 256-bit space.
AUTH_REDEEM = RateLimitRule(
    name="auth_redeem",
    limit=120,
    window_seconds=300,
    scope=IP,
    subject="one-time sign-in code redemption",
)

# The OIDC dance itself. Nothing here is guessable - ``/oidc/callback`` refuses
# a request whose ``state`` does not match the signed state cookie before it
# spends anything - so this is a *cost* ceiling on the Keycloak token exchange
# an attacker can drive by replaying their own state cookie, not an
# anti-guessing one.
#
# It is deliberately far looser than the redemption bucket, because the failure
# mode is the worst in this module: a 429 on ``/oidc/callback`` lands *after*
# Keycloak has authenticated the user and consumed the code, stranding a real
# person mid-login. 600/5min is 2/s sustained per address - 300 complete
# browser logins per five minutes from one egress IP - so no plausible office
# reaches it, while a single attacker is still bounded.
AUTH_FLOW = RateLimitRule(
    name="auth_flow",
    limit=600,
    window_seconds=300,
    scope=IP,
    subject="sign-in redirect endpoints",
)

# ``POST /v1/auth/agent-session/exchange`` is keyed on the *agent* it names, not
# on the address, and that is not a stylistic choice: ``k8s.py`` injects
# ``A2A_CP_URL=https://api.a2acloud.io`` (``settings.public_cp_url``) into every
# hosted agent pod, and ``a2a_pack/serve/asgi.py`` posts here once per visitor
# sign-in. Those calls hairpin out through the cluster's egress address, so an
# IP key would put *every hosted agent on the platform* in one bucket - a
# fleet-wide sign-in outage the first time any single agent got busy.
#
# 600/5min per agent is 2 visitor sign-ins per second for one agent, sustained.
# The code being redeemed is 256 bits, so the ceiling here is defence in depth,
# not the control that makes the endpoint safe.
AGENT_SESSION_EXCHANGE = RateLimitRule(
    name="agent_session_exchange",
    limit=600,
    window_seconds=300,
    scope=AUDIENCE,
    subject="hosted-agent session exchange",
)

# Minting a browser exchange code requires a bearer token already, so this
# bucket can only ever be spent by its own holder. ``a2a login`` mints one.
# 30/min leaves room for a login script that retries.
AUTH_MINT = RateLimitRule(
    name="auth_mint",
    limit=30,
    window_seconds=60,
    scope=ACCOUNT,
    subject="session-code minting",
)

# ``/v1/public/*`` is anonymous by design and is read by real browsers: the
# dashboard SPA fetches ``/v1/public/agents``, ``/v1/public/agent-proofs`` and
# ``/v1/public/receipt-keys`` from the user's own machine, so a shared office
# NAT is one key for everybody behind it. 600/min (10/s sustained) leaves that
# alone while still capping a single scraper at a rate it will notice.
# First-party in-cluster fetches never reach this rule - they arrive from a
# cluster address and are exempt.
PUBLIC_READ = RateLimitRule(
    name="public_read",
    limit=600,
    window_seconds=60,
    scope=IP,
    subject="public discovery endpoints",
)

# Anything that starts an image build or a source deploy: minutes of CPU, a
# registry push and an Argo sync per call. 60/10min is 6/min sustained - a
# fleet script or CI job deploying dozens of agents in one window passes, and
# 60 concurrent builds is already more than the cluster will absorb.
AGENT_BUILD = RateLimitRule(
    name="agent_build",
    limit=60,
    window_seconds=600,
    scope=ACCOUNT,
    subject="agent create/build/deploy endpoints",
)

# Endpoints whose whole job is to spend LLM tokens on the caller's behalf.
# 60/5min is 12/min: a fast chat exchange is one message every few seconds and
# a burst of ten in a row still passes.
LLM_RUN = RateLimitRule(
    name="llm_run",
    limit=60,
    window_seconds=300,
    scope=ACCOUNT,
    subject="LLM-backed run endpoints",
)

# The other doors to the same cost. Limiting ``/v1/me/chat`` alone bounded one
# door in a corridor of unlocked ones: ``/v1/me/agent-proofs/{name}/run``
# executes a real proof against a hosted agent (waking a scale-to-zero revision
# and running its LLM calls), and the schedule / collective-runtime /
# kernel / trial-room run families all reach agent execution
# the same way.
#
# 120/5min is 24/min - twice the chat ceiling, because these are one-click
# actions a user may fire in bursts while iterating - and every one of them is
# a deliberate user action, not a poll target. The SDK-runtime writebacks that
# *look* like this family (``/v1/agents/{name}/meta-runs``,
# ``/v1/agents/{name}/protocol-simulations``) are excluded: a busy agent writes
# one per invocation, so a ceiling there would throttle the fleet.
AGENT_RUN = RateLimitRule(
    name="agent_run",
    limit=120,
    window_seconds=300,
    scope=ACCOUNT,
    subject="agent and orchestrator run endpoints",
)

# ``(METHOD, route template) -> rule``. Templates are FastAPI path formats, so
# they are matched against ``request.scope["route"].path_format`` - never
# against a raw URL - and the same table drives the published 429 in
# ``openapi.py``.
_EXACT_RULES: dict[tuple[str, str], RateLimitRule] = {
    # --- auth ------------------------------------------------------------
    ("GET", "/v1/auth/cli-session/redeem"): AUTH_REDEEM,
    ("POST", "/v1/auth/cli-session/confirm"): AUTH_REDEEM,
    ("GET", "/v1/auth/oidc/start"): AUTH_FLOW,
    ("GET", "/v1/auth/oidc/callback"): AUTH_FLOW,
    ("GET", "/v1/auth/agent-session/authorize"): AUTH_FLOW,
    ("POST", "/v1/auth/agent-session/exchange"): AGENT_SESSION_EXCHANGE,
    ("POST", "/v1/auth/cli-session"): AUTH_MINT,
    # --- anonymous reads that are not under /v1/public -------------------
    # Same bounty record as ``GET /v1/public/bounties/{slug}``, same anonymous
    # access ("Public-readable bounty detail (no auth required for v1)"), so it
    # gets the same ceiling. Without it a scraper just uses the unprefixed
    # path.
    ("GET", "/v1/bounties/{slug}"): PUBLIC_READ,
    # --- builds ----------------------------------------------------------
    ("POST", "/v1/agents"): AGENT_BUILD,
    ("POST", "/v1/agents/import"): AGENT_BUILD,
    ("POST", "/v1/agents/from-source"): AGENT_BUILD,
    ("POST", "/v1/agents/from-openapi"): AGENT_BUILD,
    ("POST", "/v1/agents/compose"): AGENT_BUILD,
    ("POST", "/v1/agents/from-tarball"): AGENT_BUILD,
    ("POST", "/v1/agents/{name}/source/deploy"): AGENT_BUILD,
    ("POST", "/v1/agents/{name}/runtime-upgrade"): AGENT_BUILD,
    ("POST", "/v1/agents/{name}/template-update"): AGENT_BUILD,
    # --- LLM spend -------------------------------------------------------
    ("POST", "/v1/me/chat"): LLM_RUN,
    ("POST", "/v1/agents/studio/runs"): LLM_RUN,
    ("POST", "/v1/agents/studio/idea-factory"): LLM_RUN,
    ("POST", "/v1/agents/studio/autopilot/run"): LLM_RUN,
    # --- agent execution, the other doors to LLM spend -------------------
    ("POST", "/v1/me/agent-proofs/{name}/run"): AGENT_RUN,
    ("POST", "/v1/me/trial-rooms/{slug}/runs"): AGENT_RUN,
    ("POST", "/v1/me/schedules/{schedule_id}/run"): AGENT_RUN,
    ("POST", "/v1/me/subagent-runs/{grant_id}/rerun"): AGENT_RUN,
    ("POST", "/v1/me/collective-runtime/runs"): AGENT_RUN,
    ("POST", "/v1/me/collective-runtime/runs/{dag_run_id}/advance"): AGENT_RUN,
    ("POST", "/v1/me/collective-runtime/memories/extract"): AGENT_RUN,
    ("POST", "/v1/me/kernel-simulations/runs"): AGENT_RUN,
    ("POST", "/v1/me/kernel-simulations/runs/{job_id}/replay"): AGENT_RUN,
    ("POST", "/v1/me/kernel-evolution/runs"): AGENT_RUN,
    ("POST", "/v1/me/kernel-evolution/runs/{job_id}/replay"): AGENT_RUN,
    # --- anonymous spend -------------------------------------------------
}

# ``(template prefix, methods or None) -> rule``, consulted only when no exact
# rule matched.
_PREFIX_RULES: tuple[tuple[str, frozenset[str] | None, RateLimitRule], ...] = (
    ("/v1/public/", None, PUBLIC_READ),
)


def rule_for(method: str, path_template: str) -> RateLimitRule | None:
    """The rule guarding one operation, or ``None`` if it is unlimited."""
    method = method.upper()
    rule = _EXACT_RULES.get((method, path_template))
    if rule is not None:
        return rule
    for prefix, methods, prefix_rule in _PREFIX_RULES:
        if path_template.startswith(prefix) and (methods is None or method in methods):
            return prefix_rule
    return None


def limited_operations() -> frozenset[tuple[str, str]]:
    """Every ``(METHOD, template)`` the exact table names. Used by tests."""
    return frozenset(_EXACT_RULES)


# --------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------


@lru_cache(maxsize=8)
def _trusted_proxy_networks(raw: str) -> tuple[Any, ...]:
    """Extra CIDRs to treat as our own infrastructure, parsed once.

    Everything non-globally-routable is already treated this way. This exists
    for the one case that property cannot express: a hop of *our* own that has
    a public address - a load balancer that appends its own egress IP, or the
    cluster's egress itself. Left empty by default, because guessing at it
    would be worse than leaving it out.
    """
    networks = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            log.warning("rate limit: ignoring unparseable trusted proxy CIDR %r", entry)
    return tuple(networks)


def _is_our_infrastructure(address: _IpAddress) -> bool:
    """True for anything that cannot be an internet client.

    ``is_global`` is the whole question in one property: RFC1918, CGNAT,
    loopback, link-local, IPv6 ULA and the reserved ranges all answer False.
    Enumerating them by hand is how one gets missed.
    """
    if not address.is_global:
        return True
    for network in _trusted_proxy_networks(settings.rate_limit_trusted_proxy_cidrs):
        if address in network:
            return True
    return False


def _parse(raw: str | None) -> _IpAddress | None:
    if not raw:
        return None
    value = raw.strip()
    if not value:
        return None
    # ``[2001:db8::1]:443`` and ``1.2.3.4:443`` both show up in forwarded
    # chains from proxies that include the port.
    if value.startswith("[") and "]" in value:
        value = value[1 : value.index("]")]
    elif value.count(":") == 1:
        value = value.split(":", 1)[0]
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _forwarded_chain(request: Request) -> list[_IpAddress | None]:
    headers = getattr(request, "headers", None) or {}
    raw = headers.get("x-forwarded-for", "")
    return [_parse(hop) for hop in raw.split(",")] if raw else []


def _peer(request: Request) -> _IpAddress | None:
    client = getattr(request, "client", None)
    return _parse(getattr(client, "host", None)) if client else None


def _client_address(request: Request) -> _IpAddress | None:
    """The rightmost hop that is not our own infrastructure, or ``None``.

    Read the chain right to left. Every hop a caller can write sits to the
    *left* of the address our ingress appends for them, so the first untrusted
    address found scanning from the right is the closest thing to a real client
    that we can prove. That is true whether Traefik strips inbound
    ``X-Forwarded-*`` (chain is just the client) or preserves it (chain is
    ``<whatever they sent>, <client>``), which matters because nothing in this
    repo pins ``forwardedHeaders.trustedIPs``.

    Consequences, all of them deliberate:

    * a rotating ``X-Forwarded-For`` no longer buys a fresh bucket;
    * ``X-Forwarded-For: 10.0.0.1`` no longer buys the first-party exemption;
    * ``X-Forwarded-For: <victim>`` no longer spends the victim's bucket, so
      the limiter cannot be turned into a tool for locking a named user out of
      sign-in.

    ``None`` means every hop we can see is ours: a genuine first-party
    in-cluster caller (cronjobs, agent-to-agent, the SDK runtime),
    which is exempted rather than throttled.
    """
    chain = _forwarded_chain(request)
    untrusted = [
        address
        for address in chain
        if address is not None and not _is_our_infrastructure(address)
    ]
    if untrusted:
        # More than one means either a caller prepended something or a proxy we
        # do not recognise appended one. The rightmost is still the safe
        # answer; the count is exported so an operator can tell the two apart.
        observe_rate_limit_client_source(
            "forwarded" if len(untrusted) == 1 else "forwarded_ambiguous"
        )
        return untrusted[-1]
    peer = _peer(request)
    if peer is not None and not _is_our_infrastructure(peer):
        observe_rate_limit_client_source("peer")
        return peer
    observe_rate_limit_client_source("first_party")
    return None


def _ip_key(request: Request) -> str | None:
    """The caller's address as a bucket key, or ``None`` when it must not be one.

    Deliberately *not* :func:`analytics.client_ip`: that one takes the first
    forwarded hop, which is correct for PostHog geo (a spoofed value only
    mislabels the spoofer's own event) and wrong for a security control (a
    spoofed value picks whose ceiling gets spent). See :func:`_client_address`.
    """
    address = _client_address(request)
    if address is not None:
        return f"ip:{address.compressed}"
    if settings.rate_limit_exempt_private_clients:
        return None
    # Operator switch off: bucket first-party callers too, on the nearest hop
    # we could parse.
    for candidate in reversed(_forwarded_chain(request)):
        if candidate is not None:
            return f"ip:{candidate.compressed}"
    peer = _peer(request)
    return f"ip:{peer.compressed}" if peer is not None else None


def _platform_subject(token: str) -> str | None:
    """``sub`` of a control-plane credential the *account itself* holds.

    Every token the platform mints is HS256 over ``settings.jwt_secret`` with
    ``sub`` = the owning user id, so verifying the signature gives one stable
    bucket per human however many tokens they are carrying. That is the whole
    fix for the rotation bypass, and it has to cover more than the plain
    session: ``decode_token`` deliberately *refuses* the typed variants, which
    is exactly why the earlier revision dropped them into a per-token-string
    bucket that could be rotated.

    But it must not cover all of them. Three typed credentials are handed to
    code the account does not control - the agent invoke and agent runtime
    tokens live in the invoked agent's pod, and the agent session token lives in
    a visitor's browser at the agent's own origin. Bucketing those on ``sub``
    would let a hostile hosted agent replay a caller's token and spend that
    caller's ceiling, which is the lockout this module refuses to build. They
    reach no account-scoped route legitimately either (``current_user`` and
    ``user_from_token`` both refuse them, and
    ``test_no_account_scoped_route_accepts_an_agent_held_credential`` pins it),
    so excluding them costs nothing and closes the vector: they fall back to
    the address key like any other unusable credential.

    Expiry is checked, for the same reason - a stale copy of someone's token
    would otherwise be a way to spend their budget, and it is going to be
    refused by the route anyway.
    """
    from .auth import (
        AGENT_INVOKE_TOKEN_TYPE,
        AGENT_RUNTIME_TOKEN_TYPE,
        AGENT_SESSION_TOKEN_TYPE,
    )

    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_alg],
            options={"verify_aud": False},
        )
    except Exception:  # noqa: BLE001 - not ours, or not valid; try the next shape
        return None
    if payload.get("typ") in {
        AGENT_INVOKE_TOKEN_TYPE,
        AGENT_RUNTIME_TOKEN_TYPE,
        AGENT_SESSION_TOKEN_TYPE,
    }:
        return None
    sub = payload.get("sub")
    return str(sub) if sub not in (None, "") else None


def _keycloak_subject(token: str) -> str | None:
    """``sub`` of a signature-verified Keycloak access token, or ``None``.

    This is the fix for the bypass that made every account-scoped ceiling
    opt-in: refresh-token rotation is disabled in realm ``a2acloud``, so a
    holder can mint a new access token whenever they like. Keying on the token
    string handed each one a fresh budget; keying on the verified ``sub`` does
    not. ``keycloak_auth`` caches the realm JWKS, so the steady-state cost is a
    local RS256 verification - the same one ``current_user`` is about to do on
    this request anyway.
    """
    from .auth import _is_keycloak_token
    from .keycloak_auth import _client, verify_keycloak_token

    try:
        if not _is_keycloak_token(token):
            return None
        # Gate on a ``kid`` the cached JWK set already knows, and never force a
        # refresh from in here. ``PyJWKClient.get_signing_key`` retries an
        # unknown ``kid`` with ``refresh=True``, which is a *blocking* HTTPS
        # round trip on the event loop that an unauthenticated caller could
        # drive on every request just by inventing a header. A key we have
        # never seen is not an account as far as bucketing is concerned: the
        # request falls back to the address key and the route's own auth - which
        # is allowed to go and fetch - refuses it a moment later. On genuine
        # realm key rotation this costs one address-keyed window until that
        # fetch warms the cache, which is the permissive direction.
        kid = jwt.get_unverified_header(token).get("kid")
        if not kid:
            return None
        if jwt.PyJWKClient.match_kid(_client().get_signing_keys(), kid) is None:
            return None
        sub = verify_keycloak_token(token).get("sub")
    except Exception:  # noqa: BLE001 - unverifiable is simply "not an account"
        return None
    return str(sub) if sub not in (None, "") else None


def _account_key(request: Request) -> str | None:
    """The caller's own identity, proven - or ``None``.

    Only a verified signature produces a key here, so no request from a
    stranger can exhaust a real user's budget, and no rotation of the
    credential *string* produces a second budget for its holder.

    ``current_user`` accepts exactly two credential shapes - a control-plane
    HS256 token and a Keycloak RS256 access token - so anything that verifies
    as neither is not an account here at all. Nor is a control-plane token that
    lives in somebody else's process (see :func:`_platform_subject`). Both get
    no account bucket, and :func:`request_key` falls back to the address.
    """
    from .auth import _credential_token

    token = _credential_token(
        request.headers.get("authorization"),
        request.cookies.get(settings.session_cookie_name),
    )
    if not token:
        return None
    subject = _platform_subject(token)
    if subject is not None:
        return f"user:{subject}"
    subject = _keycloak_subject(token)
    if subject is not None:
        return f"kc:{subject}"
    return None


#: Bodies larger than this are not inspected for an audience. The one route
#: keyed this way posts a two-field JSON object.
_MAX_AUDIENCE_BODY_BYTES = 8192


async def _audience_key(request: Request) -> str | None:
    """The agent a request names in its JSON body, or ``None``.

    FastAPI has already read and cached the body by the time a dependency runs
    (``get_request_handler`` reads it, then calls ``solve_dependencies``), so
    this re-reads the cached bytes rather than the stream.
    """
    headers = getattr(request, "headers", None) or {}
    if "json" not in (headers.get("content-type") or "").lower():
        return None
    declared = headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > _MAX_AUDIENCE_BODY_BYTES:
                return None
        except ValueError:
            return None
    try:
        raw = await request.body()
        if not raw or len(raw) > _MAX_AUDIENCE_BODY_BYTES:
            # Length is re-checked after the read rather than trusted from the
            # header, so a chunked request (no ``Content-Length``) still gets
            # its per-agent bucket instead of silently falling back to the
            # cluster-egress address every hosted agent shares.
            return None
        payload = json.loads(raw)
    except Exception:  # noqa: BLE001 - a malformed body is about to 422
        return None
    if not isinstance(payload, dict):
        return None
    audience = payload.get("audience")
    if not isinstance(audience, str):
        return None
    audience = audience.strip()
    if not audience or len(audience) > 128:
        return None
    return f"agent:{audience}"


async def request_key(rule: RateLimitRule, request: Request) -> str | None:
    if rule.scope == GLOBAL:
        return "all"
    if rule.scope == ACCOUNT:
        # An unauthenticated request to an account-keyed route is about to be
        # rejected by the route's own auth dependency anyway; fall back to the
        # address so a flood of them is still bounded.
        return _account_key(request) or _ip_key(request)
    if rule.scope == AUDIENCE:
        return await _audience_key(request) or _ip_key(request)
    return _ip_key(request)


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------


class RateLimitBackend(Protocol):
    async def incr(self, key: str, window_seconds: int) -> tuple[int, int]:
        """``(hits in the current window, seconds until it resets)``.

        Raises on any backend failure; the caller turns that into "allowed".
        """


class InMemoryRateLimitBackend:
    """Process-local counter, for tests and Redis-less development.

    **This bounds one worker, not the service.** The deployed API runs up to
    four Knative replicas of four uvicorn workers, so with this backend the
    effective limit is up to sixteen times what the rule says. It exists so
    the limiter is testable and so a laptop without Redis behaves sanely - it
    is not the production backend.

    Eviction: drop what has expired,
    then drop oldest-first until back under the cap, so a key flood cannot
    grow the map without bound. Evicting a live counter only ever *grants*
    requests, so the cap can be enforced cheaply.
    """

    _MAX_KEYS = 20_000

    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}

    async def incr(self, key: str, window_seconds: int) -> tuple[int, int]:
        now = time.monotonic()
        entry = self._hits.get(key)
        if entry is None or entry[1] <= now:
            self._hits[key] = [1.0, now + window_seconds]
            self._evict(now)
            return 1, window_seconds
        entry[0] += 1
        return int(entry[0]), max(1, int(round(entry[1] - now)))

    def _evict(self, now: float) -> None:
        if len(self._hits) <= self._MAX_KEYS:
            return
        for key in [k for k, v in self._hits.items() if v[1] <= now]:
            self._hits.pop(key, None)
        # Still over: drop insertion-oldest first. Popping from the front of the
        # dict is O(1); scanning for the true minimum expiry on every insert
        # would make a key flood quadratic, which is the wrong thing to do
        # while under one.
        while len(self._hits) > self._MAX_KEYS:
            self._hits.pop(next(iter(self._hits)), None)

    async def aclose(self) -> None:
        self._hits.clear()


class RedisRateLimitBackend:
    """Fixed window shared by every replica and worker.

    One round trip in the steady state (``INCR`` + ``TTL`` pipelined), a
    second only when a window opens. Deliberately *not* a sliding window: the
    extra state costs a round trip on the hot path, and the failure mode of a
    fixed window - a burst straddling the boundary getting up to 2x the limit -
    errs permissive, which is the direction this module always takes.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def incr(self, key: str, window_seconds: int) -> tuple[int, int]:
        pipe = self._client.pipeline(transaction=False)
        pipe.incr(key)
        pipe.ttl(key)
        count, ttl = await pipe.execute()
        if ttl is None or int(ttl) < 0:
            # Either the window just opened, or - the dangerous case - an
            # ``INCR`` landed and its ``EXPIRE`` did not, leaving a counter
            # that would block this key forever. Re-arm the TTL and report a
            # fresh window, so a key with no expiry can never lock anyone out.
            await self._client.expire(key, window_seconds)
            return 1, window_seconds
        return int(count), int(ttl)

    async def aclose(self) -> None:
        close = getattr(self._client, "aclose", None) or getattr(
            self._client, "close", None
        )
        if callable(close):
            result = close()
            if hasattr(result, "__await__"):
                await result


def create_backend(redis_url: str | None) -> RateLimitBackend:
    if redis_url:
        try:
            import redis.asyncio as redis_asyncio  # type: ignore[import-not-found]

            client = redis_asyncio.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=settings.rate_limit_redis_timeout_seconds,
                socket_timeout=settings.rate_limit_redis_timeout_seconds,
            )
            return RedisRateLimitBackend(client)
        except Exception:  # noqa: BLE001
            log.warning(
                "rate limit: Redis backend unavailable; falling back to a "
                "process-local counter that only bounds this worker",
                exc_info=True,
            )
    return InMemoryRateLimitBackend()


# --------------------------------------------------------------------------
# The limiter
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    rule: RateLimitRule
    remaining: int
    retry_after: int


class _Breaker:
    """Take a failing backend out of the request path.

    Failing open on each request still pays the connect timeout on each
    request. Under a Redis outage that is latency added to every call to a
    limited endpoint, which is the "limiter outage becomes API outage" failure
    in slow motion. After ``threshold`` consecutive failures the limiter stops
    calling the backend for ``cooldown`` seconds.
    """

    def __init__(self, threshold: int, cooldown: float) -> None:
        self._threshold = threshold
        self._cooldown = cooldown
        self._failures = 0
        self._opened_at: float | None = None

    def is_open(self, now: float) -> bool:
        if self._opened_at is None:
            return False
        if now - self._opened_at >= self._cooldown:
            self._opened_at = None
            self._failures = 0
            return False
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self, now: float) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._opened_at = now


class RateLimiter:
    def __init__(
        self,
        backend: RateLimitBackend,
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 15.0,
    ) -> None:
        self.backend = backend
        self._breaker = _Breaker(failure_threshold, cooldown_seconds)

    async def check(self, rule: RateLimitRule, key: str) -> RateLimitDecision:
        now = time.monotonic()
        if self._breaker.is_open(now):
            observe_rate_limit_decision(rule.name, "backend_unavailable")
            return RateLimitDecision(True, rule, rule.limit, 0)
        try:
            count, reset = await self.backend.incr(
                f"{_KEY_PREFIX}:{rule.name}:{key}",
                rule.window_seconds,
            )
        except Exception:  # noqa: BLE001 - the limiter must never fail a request
            self._breaker.record_failure(now)
            observe_rate_limit_decision(rule.name, "backend_unavailable")
            log.warning(
                "rate limit backend unavailable; allowing request (rule=%s)",
                rule.name,
                exc_info=True,
            )
            return RateLimitDecision(True, rule, rule.limit, 0)
        self._breaker.record_success()
        allowed = count <= rule.limit
        observe_rate_limit_decision(rule.name, "allowed" if allowed else "throttled")
        return RateLimitDecision(
            allowed=allowed,
            rule=rule,
            remaining=max(0, rule.limit - count),
            retry_after=max(1, int(reset)),
        )


_limiter: RateLimiter | None = None


def get_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter(create_backend(settings.redis_url))
    return _limiter


def set_limiter(limiter: RateLimiter | None) -> None:
    """Install a limiter (tests, and anything that wants a chosen backend)."""
    global _limiter
    _limiter = limiter


async def close_limiter() -> None:
    """Release the backend's connection on shutdown. Never raises."""
    global _limiter
    limiter = _limiter
    _limiter = None
    if limiter is None:
        return
    close = getattr(limiter.backend, "aclose", None)
    if callable(close):
        try:
            await close()
        except Exception:  # noqa: BLE001 - shutdown must not fail on cleanup
            log.debug("rate limit backend close failed", exc_info=True)


# --------------------------------------------------------------------------
# The request hook
# --------------------------------------------------------------------------


def _detail(decision: RateLimitDecision) -> str:
    rule = decision.rule
    return (
        f"rate limit exceeded for {rule.subject}: at most {rule.limit} requests "
        f"per {rule.window_seconds}s. Retry in {decision.retry_after}s."
    )


async def throttle(decision: RateLimitDecision) -> None:
    """Raise the 429 the platform's error envelope renders.

    ``HTTPException`` goes through ``main.http_exception_handler``, so the body
    is the same ``{"detail": ..., "error": {"code", "message", "status"}}``
    every other failure uses. There is no second error shape here.
    """
    if decision.allowed:
        return
    raise HTTPException(
        429,
        _detail(decision),
        headers={
            "Retry-After": str(decision.retry_after),
            "RateLimit-Limit": str(decision.rule.limit),
            "RateLimit-Remaining": "0",
            "RateLimit-Reset": str(decision.retry_after),
            "Cache-Control": "no-store",
        },
    )


async def rate_limit_guard(request: Request) -> None:
    """Installed once on the app; a no-op for every route without a rule.

    Mounted as a global dependency rather than as middleware for two reasons:
    it runs *after* routing, so the policy is keyed on the route template
    instead of a hand-rolled path regex and ``/metrics`` still labels the
    throttled request with its real route; and it raises ``HTTPException``, so
    the response envelope is the app's, not a second one invented here.
    """
    if not settings.rate_limit_enabled:
        return
    route = request.scope.get("route")
    template = getattr(route, "path_format", None)
    if not isinstance(template, str) or not template:
        return
    rule = rule_for(request.method, template)
    if rule is None:
        return
    key = await request_key(rule, request)
    if key is None:
        # No key we are willing to bucket on: a first-party in-cluster caller.
        # Allow.
        return
    await throttle(await get_limiter().check(rule, key))
