"""The control plane's HTTP rate limiter.

Before this, 30 rapid unauthenticated ``GET /v1/public/agents`` answered 200
thirty times and nothing anywhere in the service bounded a request rate.

The tests are weighted towards the ways a limiter goes wrong rather than the
way it goes right, because the expensive failure here is not "an attacker got
through", it is "a paying caller got locked out":

* a backend outage must read as *allowed*, and must stop costing latency;
* a counter that lost its expiry must not block its key forever;
* one caller's bucket must not be spendable by another - not from the same NAT,
  not by naming them, not by forging a forwarded header;
* the callers that poll (the dashboard, ``a2a deploy``, the SDK runtime, the
  in-cluster first-party callers) must stay under the ceilings at their observed rates.

The second half of the file is the regression suite for a security review that
came back "bypassable". Each of those tests is named for the bypass or the
lockout it pins, and every one of them fails against the revision the reviewer
was given.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from typing import Any

import jwt as pyjwt
import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from control_plane import rate_limit
from control_plane.auth import (
    issue_agent_invoke_token,
    issue_agent_runtime_token,
    issue_studio_job_token,
    issue_token,
)
from control_plane.config import settings
from control_plane.main import http_exception_handler
from control_plane.rate_limit import (
    ACCOUNT,
    AUDIENCE,
    GLOBAL,
    IP,
    InMemoryRateLimitBackend,
    RateLimiter,
    RateLimitRule,
    RedisRateLimitBackend,
    rate_limit_guard,
    rule_for,
)

# Genuinely globally-routable addresses. The RFC 5737 documentation ranges
# (203.0.113.0/24 and friends) cannot stand in here: Python classifies them as
# non-global, which is exactly the class this limiter exempts.
PUBLIC_IP = "8.8.8.8"
OTHER_PUBLIC_IP = "9.9.9.9"
VICTIM_IP = "45.55.44.33"


@pytest.fixture(autouse=True)
def isolated_limiter():
    """Every test gets its own counters; nothing leaks between them."""
    rate_limit.set_limiter(RateLimiter(InMemoryRateLimitBackend()))
    yield
    rate_limit.set_limiter(None)


def _app(*routes: tuple[str, str]) -> FastAPI:
    """A FastAPI app wired exactly like ``control_plane.main``.

    Same global guard, same ``HTTPException`` handler, and the route templates
    are the real ones - so what is exercised is the shipped policy lookup, not
    a fixture of it.
    """
    app = FastAPI(dependencies=[Depends(rate_limit_guard)])
    app.add_exception_handler(HTTPException, http_exception_handler)
    for method, path in routes:
        app.add_api_route(path, _ok, methods=[method])
    return app


async def _ok() -> dict[str, bool]:
    return {"ok": True}


def _headers(ip: str = PUBLIC_IP, *, user_id: int | None = None) -> dict[str, str]:
    headers = {"x-forwarded-for": ip}
    if user_id is not None:
        headers["authorization"] = f"Bearer {issue_token(user_id)}"
    return headers


def _allowed(client: TestClient, method: str, path: str, attempts: int, **kwargs) -> int:
    """How many of ``attempts`` calls came back 200."""
    return sum(
        client.request(method, path, **kwargs).status_code == 200
        for _ in range(attempts)
    )


# ---------------------------------------------------------------------------
# The policy is an allowlist
# ---------------------------------------------------------------------------


def test_only_the_named_endpoint_classes_carry_a_rule() -> None:
    assert rule_for("GET", "/v1/public/agents") is rate_limit.PUBLIC_READ
    assert rule_for("POST", "/v1/me/chat") is rate_limit.LLM_RUN
    assert rule_for("POST", "/v1/agents") is rate_limit.AGENT_BUILD
    assert rule_for("GET", "/v1/auth/cli-session/redeem") is rate_limit.AUTH_REDEEM

    # Ordinary authenticated CRUD, and the surfaces the poll loops hit.
    for method, path in [
        ("GET", "/v1/me"),
        ("GET", "/v1/agents/mine"),
        ("GET", "/v1/agents/{name}/deployments/{deployment_id}"),
        ("GET", "/v1/me/threads"),
        ("GET", "/v1/auth/session"),
        ("POST", "/v1/auth/logout"),
        ("POST", "/v1/agents/{name}/api/invoke/{skill_name}"),
        ("GET", "/healthz"),
    ]:
        assert rule_for(method, path) is None, f"{method} {path} should be unlimited"


def test_a_method_without_a_rule_is_not_caught_by_its_siblings() -> None:
    assert rule_for("GET", "/v1/agents") is None
    assert rule_for("POST", "/v1/agents") is rate_limit.AGENT_BUILD


# ---------------------------------------------------------------------------
# Enforcement, and the shape of the refusal
# ---------------------------------------------------------------------------


def test_the_limit_is_enforced_and_the_429_uses_the_platform_envelope() -> None:
    rule = rate_limit.PUBLIC_READ
    client = TestClient(_app(("GET", "/v1/public/agents")))

    for _ in range(rule.limit):
        assert client.get("/v1/public/agents", headers=_headers()).status_code == 200

    response = client.get("/v1/public/agents", headers=_headers())

    assert response.status_code == 429
    assert response.headers["Retry-After"] == response.headers["RateLimit-Reset"]
    assert 1 <= int(response.headers["Retry-After"]) <= rule.window_seconds
    assert response.headers["RateLimit-Limit"] == str(rule.limit)
    assert response.headers["RateLimit-Remaining"] == "0"
    assert response.headers["Cache-Control"] == "no-store"

    body = response.json()
    # Byte-for-byte the envelope ``main.http_exception_handler`` renders for
    # every other failure - no second error shape was invented for 429.
    assert set(body) == {"detail", "error"}
    assert body["error"]["code"] == "http_429"
    assert body["error"]["status"] == 429
    assert body["error"]["message"] == body["detail"]
    assert "at most 600 requests per 60s" in body["detail"]


async def test_the_window_resets_and_the_caller_is_served_again() -> None:
    rule = RateLimitRule("test_reset", limit=2, window_seconds=1, scope=IP, subject="x")
    limiter = RateLimiter(InMemoryRateLimitBackend())

    assert (await limiter.check(rule, "ip:1")).allowed
    assert (await limiter.check(rule, "ip:1")).allowed
    assert not (await limiter.check(rule, "ip:1")).allowed

    await asyncio.sleep(1.1)

    decision = await limiter.check(rule, "ip:1")
    assert decision.allowed
    assert decision.remaining == rule.limit - 1


def test_a_throttled_request_never_reaches_the_handler() -> None:
    """The point of running before the route's own dependencies."""
    calls: list[int] = []

    app = FastAPI(dependencies=[Depends(rate_limit_guard)])
    app.add_exception_handler(HTTPException, http_exception_handler)

    @app.get("/v1/public/agents")
    async def _handler() -> dict[str, bool]:
        calls.append(1)
        return {"ok": True}

    client = TestClient(app)
    for _ in range(rate_limit.PUBLIC_READ.limit):
        assert client.get("/v1/public/agents", headers=_headers()).status_code == 200
    assert client.get("/v1/public/agents", headers=_headers()).status_code == 429

    assert len(calls) == rate_limit.PUBLIC_READ.limit


# ---------------------------------------------------------------------------
# Keys: one caller's ceiling must never be another caller's problem
# ---------------------------------------------------------------------------


def test_per_ip_buckets_are_independent() -> None:
    client = TestClient(_app(("GET", "/v1/public/agents")))

    for _ in range(rate_limit.PUBLIC_READ.limit):
        client.get("/v1/public/agents", headers=_headers(PUBLIC_IP))

    assert client.get("/v1/public/agents", headers=_headers(PUBLIC_IP)).status_code == 429
    assert (
        client.get("/v1/public/agents", headers=_headers(OTHER_PUBLIC_IP)).status_code
        == 200
    )


def test_per_account_buckets_are_independent_of_each_other_and_of_the_ip() -> None:
    """The lockout attack, from both directions.

    Two users behind one office NAT must not share a ceiling, and an anonymous
    flood from that NAT must not spend either of their budgets.
    """
    rule = rate_limit.LLM_RUN
    client = TestClient(_app(("POST", "/v1/me/chat")))

    for _ in range(rule.limit):
        assert (
            client.post("/v1/me/chat", headers=_headers(user_id=1)).status_code == 200
        )
    assert client.post("/v1/me/chat", headers=_headers(user_id=1)).status_code == 429

    # Same address, different account: untouched.
    assert client.post("/v1/me/chat", headers=_headers(user_id=2)).status_code == 200

    # Same account, different address: still their own bucket, still spent -
    # an account ceiling that reset by moving IP would not be a ceiling.
    assert (
        client.post(
            "/v1/me/chat", headers=_headers(OTHER_PUBLIC_IP, user_id=1)
        ).status_code
        == 429
    )


def test_an_anonymous_flood_cannot_spend_a_real_accounts_budget() -> None:
    rule = rate_limit.LLM_RUN
    client = TestClient(_app(("POST", "/v1/me/chat")))

    # Unauthenticated callers fall back to the address key, so the flood is
    # bounded - but it lands in ``ip:``, never in anybody's ``user:``.
    for _ in range(rule.limit + 5):
        client.post("/v1/me/chat", headers=_headers(PUBLIC_IP))

    assert client.post("/v1/me/chat", headers=_headers(PUBLIC_IP)).status_code == 429
    assert (
        client.post("/v1/me/chat", headers=_headers(PUBLIC_IP, user_id=42)).status_code
        == 200
    )


def test_an_account_key_cannot_be_forged_by_an_unsigned_token() -> None:
    """A bucket is only ever keyed on a credential the caller really holds."""
    request = _request(headers={"authorization": f"Bearer {issue_token(7)}"})
    assert rate_limit._account_key(request) == "user:7"

    # An unsigned token claiming to be user 7 gets no account bucket at all -
    # not that user's, and not a fresh one of its own.
    forged = (
        "eyJhbGciOiJub25lIn0."
        "eyJzdWIiOiI3IiwiZXhwIjo5OTk5OTk5OTk5LCJpYXQiOjB9."
    )
    assert (
        rate_limit._account_key(_request(headers={"authorization": f"Bearer {forged}"}))
        is None
    )


def test_first_party_in_cluster_callers_are_exempt_from_the_ip_key() -> None:
    """The cronjobs, in-cluster services and agent-to-agent calls.

    They reach the API from cluster addresses; throttling them is the outage
    this module exists to avoid.
    """
    for private in ["10.42.1.7", "127.0.0.1", "192.168.4.9", "172.16.0.3", "fd00::1"]:
        assert rate_limit._ip_key(_request(headers={"x-forwarded-for": private})) is None

    assert rate_limit._ip_key(_request(headers={"x-forwarded-for": PUBLIC_IP})) == (
        f"ip:{PUBLIC_IP}"
    )
    # A forwarded value that is not an address at all is skipped rather than
    # bucketed into a shared "unknown" counter anyone could fill.
    assert rate_limit._ip_key(_request(headers={"x-forwarded-for": "unknown"})) is None


def test_the_private_client_exemption_has_an_operator_switch(monkeypatch) -> None:
    monkeypatch.setattr(settings, "rate_limit_exempt_private_clients", False)

    assert rate_limit._ip_key(_request(headers={"x-forwarded-for": "10.42.1.7"})) == (
        "ip:10.42.1.7"
    )


def _request(
    *,
    headers: dict[str, str] | None = None,
    path: str = "/v1/public/agents",
    peer: str = "10.0.0.1",
):
    from fastapi import Request

    raw = [(k.encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": raw,
            "client": (peer, 1234),
            "server": ("api.a2acloud.io", 443),
        }
    )


# ---------------------------------------------------------------------------
# Fail open
# ---------------------------------------------------------------------------


class _BrokenBackend:
    def __init__(self) -> None:
        self.calls = 0

    async def incr(self, key: str, window_seconds: int) -> tuple[int, int]:
        self.calls += 1
        raise ConnectionError("redis is gone")


async def test_a_dead_backend_allows_every_request() -> None:
    rule = rate_limit.PUBLIC_READ
    limiter = RateLimiter(_BrokenBackend())

    for _ in range(rule.limit * 2):
        assert (await limiter.check(rule, "ip:1")).allowed


def test_a_dead_backend_keeps_the_api_serving_end_to_end() -> None:
    rate_limit.set_limiter(RateLimiter(_BrokenBackend()))
    client = TestClient(_app(("GET", "/v1/public/agents")))

    for _ in range(rate_limit.PUBLIC_READ.limit + 20):
        assert client.get("/v1/public/agents", headers=_headers()).status_code == 200


async def test_a_dead_backend_is_taken_out_of_the_request_path() -> None:
    """Failing open on every request still pays the timeout on every request."""
    backend = _BrokenBackend()
    limiter = RateLimiter(backend, failure_threshold=3, cooldown_seconds=60.0)

    for _ in range(50):
        assert (await limiter.check(rate_limit.PUBLIC_READ, "ip:1")).allowed

    assert backend.calls == 3


async def test_the_breaker_closes_again_once_the_backend_recovers() -> None:
    backend = _BrokenBackend()
    limiter = RateLimiter(backend, failure_threshold=1, cooldown_seconds=0.05)

    assert (await limiter.check(rate_limit.PUBLIC_READ, "ip:1")).allowed
    await asyncio.sleep(0.06)
    assert (await limiter.check(rate_limit.PUBLIC_READ, "ip:1")).allowed

    assert backend.calls == 2


def test_the_limiter_can_be_switched_off_wholesale(monkeypatch) -> None:
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    client = TestClient(_app(("GET", "/v1/public/agents")))

    for _ in range(rate_limit.PUBLIC_READ.limit + 5):
        assert client.get("/v1/public/agents", headers=_headers()).status_code == 200


# ---------------------------------------------------------------------------
# The Redis backend
# ---------------------------------------------------------------------------


class _FakeRedis:
    """Enough of redis-py's async surface for the fixed-window counter."""

    def __init__(self, *, ttl_override: int | None = None) -> None:
        self.counts: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.expire_calls: list[tuple[str, int]] = []
        self._ttl_override = ttl_override

    def pipeline(self, transaction: bool = True) -> "_FakePipeline":
        return _FakePipeline(self)

    async def expire(self, key: str, seconds: int) -> bool:
        self.expire_calls.append((key, seconds))
        self.ttls[key] = seconds
        return True


class _FakePipeline:
    def __init__(self, redis: _FakeRedis) -> None:
        self._redis = redis
        self._ops: list[tuple[str, str]] = []

    def incr(self, key: str) -> None:
        self._ops.append(("incr", key))

    def ttl(self, key: str) -> None:
        self._ops.append(("ttl", key))

    async def execute(self) -> list[Any]:
        out: list[Any] = []
        for op, key in self._ops:
            if op == "incr":
                self._redis.counts[key] = self._redis.counts.get(key, 0) + 1
                out.append(self._redis.counts[key])
            else:
                if self._redis._ttl_override is not None:
                    out.append(self._redis._ttl_override)
                else:
                    out.append(self._redis.ttls.get(key, -1))
        return out


async def test_redis_backend_counts_and_arms_the_window() -> None:
    redis = _FakeRedis()
    backend = RedisRateLimitBackend(redis)

    assert await backend.incr("k", 60) == (1, 60)
    assert redis.expire_calls == [("k", 60)]
    assert await backend.incr("k", 60) == (2, 60)
    assert redis.expire_calls == [("k", 60)]


async def test_a_counter_that_lost_its_expiry_can_never_lock_a_caller_out() -> None:
    """``INCR`` landing without its ``EXPIRE`` is how a limiter bricks a key.

    The counter would keep climbing with no TTL and the key would be refused
    forever. The backend re-arms it and reports a fresh window instead.
    """
    redis = _FakeRedis(ttl_override=-1)
    backend = RedisRateLimitBackend(redis)
    limiter = RateLimiter(backend)

    for _ in range(rate_limit.PUBLIC_READ.limit * 2):
        assert (await limiter.check(rate_limit.PUBLIC_READ, "ip:1")).allowed

    assert redis.counts  # the INCRs really happened
    assert len(redis.expire_calls) == rate_limit.PUBLIC_READ.limit * 2


async def test_the_in_memory_backend_does_not_grow_without_bound() -> None:
    """A key flood must not become a memory leak in the limiter itself."""
    backend = InMemoryRateLimitBackend()
    backend._MAX_KEYS = 100

    for index in range(500):
        await backend.incr(f"ip:{index}", 60)

    assert len(backend._hits) <= 100


# ---------------------------------------------------------------------------
# Blast radius: the callers that poll must fit under the ceilings
# ---------------------------------------------------------------------------


async def _replay(rule: RateLimitRule, key: str, requests: int) -> bool:
    """``True`` if every one of ``requests`` calls was allowed."""
    limiter = RateLimiter(InMemoryRateLimitBackend())
    for _ in range(requests):
        if not (await limiter.check(rule, key)).allowed:
            return False
    return True


async def test_normal_dashboard_and_cli_usage_stays_under_every_ceiling() -> None:
    """Observed rates, replayed against the shipped numbers.

    * dashboard SPA: 2.5s and 5s poll loops (``FileBrowser``, ``MyAgents``,
      ``ChatActivityRail``, ``AgentStudio``) - all against routes with no rule,
      plus the public reads ``api.ts`` makes on page load.
    * ``a2a deploy``: one create, then ``_wait_for_deployment`` polling the
      deployment every 4s for up to 10 minutes (``sdk/.../cli/main.py``).
    * ``production_receipt_smoke.py``: hourly, ~15 calls per run.
    """
    # A browser doing 10 full page loads a minute, 20 public fetches each.
    assert await _replay(rate_limit.PUBLIC_READ, "ip:browser", 200)

    # The whole office behind one NAT: 30 people at that rate would be 6000/min
    # and would throttle - so the ceiling is documented as per-address, and the
    # first-party paths that could realistically pile up are exempt instead.
    assert await _replay(rate_limit.PUBLIC_READ, "ip:nat", 600)
    assert not (
        await _replay(rate_limit.PUBLIC_READ, "ip:nat-over", 601)
    )

    # ``a2a deploy`` in a tight CI loop: 10 deploys in 10 minutes.
    assert await _replay(rate_limit.AGENT_BUILD, "user:ci", 10)

    # A chat session at one message every 5 seconds for the whole 5-min window.
    assert await _replay(rate_limit.LLM_RUN, "user:chatty", 60)

    # The hourly receipt smoke: 15 calls, and only the ones that build count.
    assert await _replay(rate_limit.AGENT_BUILD, "user:smoke", 15)


def test_cli_deploy_status_polling_is_not_rate_limited_at_all() -> None:
    """``_wait_for_deployment`` polls every 4s for up to 10 minutes: 150 calls."""
    assert rule_for("GET", "/v1/agents/{name}/deployments/{deployment_id}") is None
    assert rule_for("GET", "/v1/agents/{name}/deployments") is None


def test_the_sdk_runtimes_callback_into_the_control_plane_is_not_limited() -> None:
    """Every authenticated agent request verifies its caller against ``/v1/me``."""
    assert rule_for("GET", "/v1/me") is None
    assert rule_for("GET", "/v1/agents/{name}/memory") is None
    assert rule_for("POST", "/v1/agents/{name}/memory") is None


def test_every_rule_has_a_scope_the_key_derivation_understands() -> None:
    for rule in {
        *rate_limit._EXACT_RULES.values(),
        *(rule for _prefix, _methods, rule in rate_limit._PREFIX_RULES),
    }:
        assert rule.scope in {IP, ACCOUNT, AUDIENCE, GLOBAL}
        assert rule.limit > 0
        assert rule.window_seconds > 0
        assert rule.subject


def test_the_detail_message_tells_the_caller_what_to_do() -> None:
    decision = rate_limit.RateLimitDecision(
        allowed=False,
        rule=rate_limit.AUTH_REDEEM,
        remaining=0,
        retry_after=42,
    )

    detail = rate_limit._detail(decision)

    assert "one-time sign-in code redemption" in detail
    assert "120 requests per 300s" in detail
    assert "Retry in 42s" in detail
    # Never echo the key: it is an address or an account subject.
    assert "user:" not in detail and "kc:" not in detail and "agent:" not in detail


def test_the_error_body_is_json_serialisable_as_the_handler_writes_it() -> None:
    client = TestClient(_app(("GET", "/v1/public/agents")))
    for _ in range(rate_limit.PUBLIC_READ.limit + 1):
        response = client.get("/v1/public/agents", headers=_headers())

    assert response.status_code == 429
    assert json.loads(response.content)["error"]["code"] == "http_429"


# ===========================================================================
# Regression suite for the security review
#
# Everything below pins a bypass that was reproduced against the first
# revision, or a lockout that revision would have caused. Each test is named
# for the failure, not for the mechanism.
# ===========================================================================


# ---------------------------------------------------------------------------
# Bypass 1: rotating the credential string bought a fresh account budget
# ---------------------------------------------------------------------------


def _rsa_keypair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return private_pem, key.public_key()


@pytest.fixture()
def keycloak_realm(monkeypatch):
    """A real RS256 realm: real signatures, verified by the real code path.

    The only thing stubbed is the JWKS *fetch* - ``keycloak_auth`` caches the
    client, so replacing it hands the verifier this test's public key instead
    of reaching out to ``auth.a2acloud.io``. Signature, issuer and expiry are
    all still checked by ``verify_keycloak_token`` itself.
    """
    from control_plane import keycloak_auth

    private_pem, public_key = _rsa_keypair()

    class _Key:
        key = public_key
        key_id = "realm-key-1"

    class _Client:
        """The cached JWKS client, minus the network."""

        fetches = 0

        def get_signing_keys(self, refresh: bool = False) -> list[_Key]:
            type(self).fetches += 1
            return [_Key()]

        def get_signing_key_from_jwt(self, token: str) -> _Key:
            return _Key()

    client = _Client()
    monkeypatch.setattr(keycloak_auth, "_jwks_client", client)
    monkeypatch.setattr(settings, "keycloak_enabled", True)

    def mint(subject: str, *, kid: str = "realm-key-1") -> str:
        now = int(time.time())
        return pyjwt.encode(
            {
                "sub": subject,
                "iss": settings.keycloak_issuer,
                "iat": now,
                "exp": now + 300,
                # Keycloak puts a fresh jti on every access token; this is what
                # made a token-string bucket rotatable.
                "jti": secrets.token_hex(16),
            },
            private_pem,
            algorithm="RS256",
            headers={"kid": kid},
        )

    mint.client = client  # type: ignore[attr-defined]
    return mint


def test_a_rotated_keycloak_token_cannot_buy_a_fresh_account_budget(
    keycloak_realm,
) -> None:
    """The blocker from the review, end to end.

    Refresh-token rotation is off in realm ``a2acloud``, so one refresh token
    mints an unbounded stream of access tokens. When the bucket was keyed on
    ``sha256(token)`` that was an unbounded stream of *budgets*: 100 of 100
    calls got through a 20-per-10-minutes ceiling. Keyed on the verified
    ``sub`` it is one budget per human, however many tokens they hold.
    """
    rule = rate_limit.AGENT_BUILD
    client = TestClient(_app(("POST", "/v1/agents")))
    attempts = rule.limit + 40

    rotated = sum(
        client.post(
            "/v1/agents",
            headers={
                "x-forwarded-for": PUBLIC_IP,
                "authorization": f"Bearer {keycloak_realm('same-human')}",
            },
        ).status_code
        == 200
        for _ in range(attempts)
    )

    assert rotated == rule.limit, f"{rotated} of {attempts} allowed; ceiling is {rule.limit}"


def test_two_keycloak_users_behind_one_address_do_not_share_a_budget(
    keycloak_realm,
) -> None:
    """The fix must not be "bucket every Keycloak caller on the office IP"."""
    rule = rate_limit.AGENT_BUILD
    client = TestClient(_app(("POST", "/v1/agents")))

    for _ in range(rule.limit):
        assert (
            client.post(
                "/v1/agents",
                headers={
                    "x-forwarded-for": PUBLIC_IP,
                    "authorization": f"Bearer {keycloak_realm('alice')}",
                },
            ).status_code
            == 200
        )

    assert (
        client.post(
            "/v1/agents",
            headers={
                "x-forwarded-for": PUBLIC_IP,
                "authorization": f"Bearer {keycloak_realm('bob')}",
            },
        ).status_code
        == 200
    )


def test_an_unknown_signing_key_never_triggers_a_jwks_fetch_from_the_limiter(
    keycloak_realm,
) -> None:
    """An invented ``kid`` must not become a blocking HTTPS round trip.

    ``PyJWKClient.get_signing_key`` retries an unknown ``kid`` with
    ``refresh=True``, so a limiter that handed it arbitrary tokens would let an
    unauthenticated caller drive a JWKS fetch per request - on the event loop,
    before any auth has run. The limiter only ever reads the cached set.
    """
    token = keycloak_realm("someone", kid="a-key-that-does-not-exist")

    assert (
        rate_limit._account_key(_request(headers={"authorization": f"Bearer {token}"}))
        is None
    )


def test_an_unverifiable_bearer_token_gets_no_account_bucket_of_its_own() -> None:
    """The other half: garbage must not mint budgets either.

    ``current_user`` accepts exactly two credential shapes, so anything that
    verifies as neither is not an account. It falls back to the address key,
    where rotating the token buys nothing.
    """
    client = TestClient(_app(("POST", "/v1/agents")))
    rule = rate_limit.AGENT_BUILD
    attempts = rule.limit + 40

    allowed = sum(
        client.post(
            "/v1/agents",
            headers={
                "x-forwarded-for": PUBLIC_IP,
                "authorization": f"Bearer {secrets.token_urlsafe(32)}",
            },
        ).status_code
        == 200
        for _ in range(attempts)
    )

    assert allowed == rule.limit


def test_the_credentials_the_account_holds_all_share_one_ceiling() -> None:
    """Not just the browser session.

    A studio job token is HS256 over the same secret with ``sub`` = the owning
    user. ``decode_token`` refuses it for platform access - correctly - which is
    exactly why the earlier revision dropped it into a per-token-string bucket
    that could be rotated. For *bucketing* it is the same human.
    """
    tokens = [
        issue_token(11),
        issue_studio_job_token(
            11, run_id="r1", target_agent="agent-builder", ttl_seconds=300
        ),
    ]

    keys = {
        rate_limit._account_key(
            _request(headers={"authorization": f"Bearer {token}"})
        )
        for token in tokens
    }

    assert keys == {"user:11"}


def test_a_credential_held_by_agent_code_cannot_spend_its_callers_ceiling() -> None:
    """The mirror image of the rotation fix, and just as important.

    An agent invoke token is minted for the caller and handed to the *invoked
    agent's* pod; an agent session token lives in a visitor's browser at the
    agent's own origin. If either keyed a bucket on its ``sub``, a hostile
    hosted agent could replay it at the control plane and burn the ceiling of
    whoever called it - turning the limiter into a way to stop a competitor
    deploying. They get no account bucket at all.
    """
    from control_plane.agent_frontend_session import mint_agent_session_token

    session_token, _expires = mint_agent_session_token(
        user_id=11, agent="someone-elses-agent", ttl_seconds=300
    )
    for token in [
        issue_agent_invoke_token(11, agent="someone-elses-agent", ttl_seconds=300),
        issue_agent_runtime_token(11, agent="someone-elses-agent", ttl_seconds=300),
        session_token,
    ]:
        assert (
            rate_limit._account_key(
                _request(headers={"authorization": f"Bearer {token}"})
            )
            is None
        )


def test_no_account_scoped_route_accepts_an_agent_held_credential() -> None:
    """What makes the exclusion above free rather than a hole.

    If a route with an account-scoped ceiling ever started accepting an agent
    invoke token, that traffic would fall back to the address key - and every
    hosted agent shares one cluster egress address. This is the tripwire.
    """
    from fastapi.routing import APIRoute

    from control_plane.main import app

    account_scoped = {
        key
        for key, rule in rate_limit._EXACT_RULES.items()
        if rule.scope == ACCOUNT
    }
    offenders = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods or ():
            if (method, route.path_format) not in account_scoped:
                continue
            if "current_user_or_agent_invoke" in _dependency_names(route.dependant):
                offenders.append(f"{method} {route.path_format}")

    assert offenders == []


def _dependency_names(dependant) -> set[str]:
    names = set()
    stack = [dependant]
    while stack:
        node = stack.pop()
        call = getattr(node, "call", None)
        if call is not None:
            names.add(getattr(call, "__name__", ""))
        stack.extend(getattr(node, "dependencies", []) or [])
    return names


def test_an_expired_platform_token_cannot_spend_its_users_budget() -> None:
    """Otherwise a stale copy of someone's token is a lockout tool.

    The route would refuse it anyway, so keying it to the named user would be
    all cost and no benefit.
    """
    expired = issue_token(12, ttl_seconds=-60)

    assert (
        rate_limit._account_key(_request(headers={"authorization": f"Bearer {expired}"}))
        is None
    )


# ---------------------------------------------------------------------------
# Bypass 2: unlimited siblings reached the same cost
# ---------------------------------------------------------------------------


def test_every_endpoint_that_executes_an_agent_carries_a_ceiling() -> None:
    """Limiting ``/v1/me/chat`` alone bounded one door in a corridor.

    Each of these selects and runs a hosted agent (waking a scale-to-zero
    revision and spending its LLM calls) or steps an orchestrator run that
    does. None of them was rate-limited the way the hosted
    invoke path is.
    """
    for path in [
        "/v1/me/chat",
        "/v1/me/agent-proofs/{name}/run",
        "/v1/me/trial-rooms/{slug}/runs",
        "/v1/me/schedules/{schedule_id}/run",
        "/v1/me/subagent-runs/{grant_id}/rerun",
        "/v1/me/collective-runtime/runs",
        "/v1/me/collective-runtime/runs/{dag_run_id}/advance",
        "/v1/me/collective-runtime/memories/extract",
        "/v1/me/kernel-simulations/runs",
        "/v1/me/kernel-evolution/runs",
        "/v1/agents/studio/runs",
        "/v1/agents/studio/idea-factory",
        "/v1/agents/studio/autopilot/run",
    ]:
        assert rule_for("POST", path) is not None, f"POST {path} still unlimited"


def test_the_run_family_shares_an_account_ceiling_across_its_endpoints() -> None:
    """Spreading a burst over the siblings must not multiply the budget."""
    rule = rate_limit.AGENT_RUN
    paths = [
        "/v1/me/agent-proofs/{name}/run",
        "/v1/me/schedules/{schedule_id}/run",
        "/v1/me/collective-runtime/runs",
    ]
    client = TestClient(_app(*[("POST", path) for path in paths]))
    urls = [
        "/v1/me/agent-proofs/a/run",
        "/v1/me/schedules/s/run",
        "/v1/me/collective-runtime/runs",
    ]

    allowed = sum(
        client.post(urls[index % len(urls)], headers=_headers(user_id=5)).status_code
        == 200
        for index in range(rule.limit + 30)
    )

    assert allowed == rule.limit


def test_the_sdk_runtime_writebacks_stay_unlimited() -> None:
    """``a2a_pack`` posts one of each per invocation.

    ``meta_runs.py`` and ``protocol_simulations.py`` write to these from inside
    running agents. They look like the run family and are not: a ceiling here
    would throttle the fleet in proportion to how much work it is doing, which
    is precisely backwards.
    """
    for path in [
        "/v1/agents/{name}/meta-runs",
        "/v1/agents/{name}/protocol-simulations",
        "/v1/agents/{name}/protocol-simulations/{job_id}/events",
        "/v1/agents/{name}/protocol-simulations/{job_id}/scenario-runs",
    ]:
        assert rule_for("POST", path) is None, f"POST {path} must stay unlimited"


# ---------------------------------------------------------------------------
# Bypass 3: a second anonymous route served the same data
# ---------------------------------------------------------------------------


def test_the_unprefixed_bounty_detail_is_capped_like_its_public_twin() -> None:
    """``GET /v1/bounties/{slug}`` is anonymous too ("no auth required for v1").

    A scraper enumerating slugs simply used the path without the ``/public``
    prefix.
    """
    assert rule_for("GET", "/v1/bounties/{slug}") is rate_limit.PUBLIC_READ
    assert rule_for("GET", "/v1/public/bounties/{slug}") is rate_limit.PUBLIC_READ


# ---------------------------------------------------------------------------
# Bypass 4: X-Forwarded-For was the whole security boundary
# ---------------------------------------------------------------------------


def _forwarded(chain: str) -> str | None:
    return rate_limit._ip_key(_request(headers={"x-forwarded-for": chain}))


def test_a_rotating_forwarded_header_cannot_buy_a_fresh_bucket() -> None:
    """The ingress appends the real peer to the right of anything sent."""
    client = TestClient(_app(("GET", "/v1/public/agents")))
    rule = rate_limit.PUBLIC_READ
    attempts = rule.limit + 200

    allowed = sum(
        client.get(
            "/v1/public/agents",
            headers={"x-forwarded-for": f"8.8.{index // 250}.{index % 250 + 1}, {PUBLIC_IP}"},
        ).status_code
        == 200
        for index in range(attempts)
    )

    assert allowed == rule.limit


def test_a_private_forwarded_hop_cannot_buy_the_first_party_exemption() -> None:
    """``X-Forwarded-For: 10.0.0.1`` used to mean "exempt from everything"."""
    assert _forwarded(f"10.0.0.1, {PUBLIC_IP}") == f"ip:{PUBLIC_IP}"
    assert _forwarded(f"not-an-ip, {PUBLIC_IP}") == f"ip:{PUBLIC_IP}"
    assert _forwarded(f"{PUBLIC_IP}, 10.42.0.9, 127.0.0.1") == f"ip:{PUBLIC_IP}"


def test_a_spoofed_forwarded_header_cannot_lock_a_named_user_out() -> None:
    """The worst version of this bug: the limiter as a weapon.

    Setting a victim's address let an attacker spend the victim's bucket and
    take them out of ``/v1/public/*`` and out of sign-in.
    """
    client = TestClient(_app(("GET", "/v1/public/agents")))
    attacker = {"x-forwarded-for": f"{VICTIM_IP}, {OTHER_PUBLIC_IP}"}

    for _ in range(rate_limit.PUBLIC_READ.limit + 50):
        client.get("/v1/public/agents", headers=attacker)

    # The attacker's own address is spent; the victim's is untouched.
    assert client.get("/v1/public/agents", headers=attacker).status_code == 429
    victim = client.get(
        "/v1/public/agents", headers={"x-forwarded-for": f"{VICTIM_IP}, {VICTIM_IP}"}
    )
    assert victim.status_code == 200


def test_a_forwarded_chain_of_only_our_own_hops_is_first_party() -> None:
    """The in-cluster callers this exemption exists for still get it."""
    assert _forwarded("10.42.1.7, 10.0.0.1") is None
    assert rate_limit._ip_key(_request(headers={}, peer="10.0.0.1")) is None
    # ...and a direct public peer with no forwarded header is still bucketed.
    assert rate_limit._ip_key(_request(headers={}, peer=PUBLIC_IP)) == f"ip:{PUBLIC_IP}"


def test_an_operator_can_declare_a_public_hop_as_our_own(monkeypatch) -> None:
    """The escape hatch for a load balancer that appends its own public IP.

    Without it, such a hop would become every caller's bucket key at once. The
    ``forwarded_ambiguous`` metric is what tells an operator to reach for this.
    """
    rate_limit._trusted_proxy_networks.cache_clear()
    monkeypatch.setattr(settings, "rate_limit_trusted_proxy_cidrs", "9.9.9.0/24")
    try:
        assert _forwarded(f"{PUBLIC_IP}, {OTHER_PUBLIC_IP}") == f"ip:{PUBLIC_IP}"
        assert _forwarded(OTHER_PUBLIC_IP) is None
    finally:
        rate_limit._trusted_proxy_networks.cache_clear()


def test_a_forwarded_hop_with_a_port_still_parses() -> None:
    assert _forwarded(f"{PUBLIC_IP}:41234") == f"ip:{PUBLIC_IP}"
    assert _forwarded("[2001:4860:4860::8888]:443") == "ip:2001:4860:4860::8888"


# ---------------------------------------------------------------------------
# Lockout 1: hosted-agent sign-in, platform-wide
# ---------------------------------------------------------------------------


def test_hosted_agent_sign_in_buckets_per_agent_not_per_cluster_egress() -> None:
    """``k8s.py`` injects ``A2A_CP_URL=https://api.a2acloud.io`` into every pod.

    So ``a2a_pack/serve/asgi.py`` posts the visitor's exchange back through the
    public ingress, and every agent on the platform arrives from one egress
    address. An IP key there is a fleet-wide sign-in outage waiting for one
    busy agent; the rule keys on the audience instead.
    """
    rule = rate_limit.AGENT_SESSION_EXCHANGE
    client = TestClient(_app(("POST", "/v1/auth/agent-session/exchange")))
    egress = {"x-forwarded-for": PUBLIC_IP}

    for _ in range(rule.limit):
        assert (
            client.post(
                "/v1/auth/agent-session/exchange",
                json={"code": secrets.token_urlsafe(32), "audience": "busy-agent"},
                headers=egress,
            ).status_code
            == 200
        )
    assert (
        client.post(
            "/v1/auth/agent-session/exchange",
            json={"code": secrets.token_urlsafe(32), "audience": "busy-agent"},
            headers=egress,
        ).status_code
        == 429
    )

    # Every other agent on the same egress address is unaffected.
    assert (
        client.post(
            "/v1/auth/agent-session/exchange",
            json={"code": secrets.token_urlsafe(32), "audience": "quiet-agent"},
            headers=egress,
        ).status_code
        == 200
    )


async def test_the_audience_key_survives_a_body_with_no_content_length() -> None:
    """A chunked request must not silently fall back to the shared egress key.

    That fallback is the fleet-wide bucket this rule exists to avoid, so the
    size guard re-checks the body it actually read instead of trusting a header
    that may not be there.
    """
    from fastapi import Request

    body = json.dumps({"code": "x" * 43, "audience": "chunked-agent"}).encode()
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        chunk = b"" if sent else body
        sent = True
        return {"type": "http.request", "body": chunk, "more_body": False}

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/v1/auth/agent-session/exchange",
            "raw_path": b"/v1/auth/agent-session/exchange",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("10.0.0.1", 1234),
            "server": ("api.a2acloud.io", 443),
        },
        receive,
    )

    assert await rate_limit._audience_key(request) == "agent:chunked-agent"


def test_the_audience_key_falls_back_rather_than_failing_open_on_a_bad_body() -> None:
    """A body with no audience is about to 422; it still must not be free."""
    rule = rate_limit.AGENT_SESSION_EXCHANGE
    client = TestClient(_app(("POST", "/v1/auth/agent-session/exchange")))

    allowed = _allowed(
        client,
        "POST",
        "/v1/auth/agent-session/exchange",
        rule.limit + 20,
        json={"code": "x"},
        headers={"x-forwarded-for": PUBLIC_IP},
    )

    assert allowed == rule.limit


# ---------------------------------------------------------------------------
# Lockout 2: sign-in behind a shared egress address
# ---------------------------------------------------------------------------


async def test_a_large_office_can_all_sign_in_inside_one_window() -> None:
    """Sized against people, not against round numbers.

    A browser login is ``oidc/start`` + ``oidc/callback``; a CLI login adds one
    redeem. The earlier 60-per-5-minutes bucket covered ~30 logins from a whole
    NAT'd office, VPN concentrator or campus - and the 429 landed on
    ``/oidc/callback``, i.e. after Keycloak had already authenticated the person
    and consumed the code.
    """
    # 300 browser logins from one egress address in five minutes.
    assert await _replay(rate_limit.AUTH_FLOW, "ip:office", 2 * 300)
    # 120 CLI logins from the same address in the same window.
    assert await _replay(rate_limit.AUTH_REDEEM, "ip:office", 120)
    # 30 people signing in - the case the review called out - is nowhere near.
    assert await _replay(rate_limit.AUTH_FLOW, "ip:small-office", 60)


def test_the_guessable_code_endpoint_is_still_tightly_capped() -> None:
    """Loosening the flow must not loosen what is actually brute-forceable."""
    assert rule_for("GET", "/v1/auth/cli-session/redeem") is rate_limit.AUTH_REDEEM
    assert rule_for("POST", "/v1/auth/cli-session/confirm") is rate_limit.AUTH_REDEEM
    # An order of magnitude tighter than the redirect dance it sits next to.
    assert rate_limit.AUTH_REDEEM.limit < rate_limit.AUTH_FLOW.limit


# ---------------------------------------------------------------------------
# Lockout 3: cheap toggles shared the build budget
# ---------------------------------------------------------------------------


def test_a_cheap_agent_toggle_does_not_spend_the_build_budget() -> None:
    """``POST /v1/agents/{name}/code-editor`` starts nothing.

    It checks that a source repo exists and writes an opt-in row. Sharing the
    build ceiling meant flipping a switch could refuse a deploy, with no way to
    tell the two apart from the error.
    """
    assert rule_for("POST", "/v1/agents/{name}/code-editor") is None


async def test_a_fleet_deploy_script_is_not_refused_mid_run() -> None:
    """40 deploys in ten minutes was the case the review raised."""
    assert await _replay(rate_limit.AGENT_BUILD, "user:fleet", 40)
    assert await _replay(rate_limit.AGENT_BUILD, "user:fleet-2", rate_limit.AGENT_BUILD.limit)


def test_the_build_rule_still_covers_everything_that_builds() -> None:
    for path in [
        "/v1/agents",
        "/v1/agents/import",
        "/v1/agents/from-source",
        "/v1/agents/from-openapi",
        "/v1/agents/compose",
        "/v1/agents/from-tarball",
        "/v1/agents/{name}/source/deploy",
        "/v1/agents/{name}/runtime-upgrade",
        "/v1/agents/{name}/template-update",
    ]:
        assert rule_for("POST", path) is rate_limit.AGENT_BUILD


# ---------------------------------------------------------------------------
# The guard is live on the real app, not just on the fixture
# ---------------------------------------------------------------------------


def test_every_rule_in_the_table_matches_a_mounted_operation() -> None:
    """A typo in a template is a rule that silently never fires."""
    from control_plane.main import app

    mounted = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
        if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}
    }

    missing = sorted(key for key in rate_limit.limited_operations() if key not in mounted)

    assert missing == []
