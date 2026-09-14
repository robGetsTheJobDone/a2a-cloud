"""The published OpenAPI document has to be enough to integrate against.

Before ``control_plane.openapi`` existed the spec at ``/openapi.json`` had no
``securitySchemes`` at all: the credential every guarded endpoint requires was
published as an *optional* header string, so ``/docs`` had no Authorize button
and generated clients shipped with no auth. It also published the operator
surface — including ``DELETE /v1/admin/users``, which truncates tables — and
documented no failure other than FastAPI's stock 422.

These tests pin that fix, and three ways it was wrong on the first pass:

* ``Authorization`` carries three non-interchangeable tokens. Publishing them
  all as the platform ``bearerAuth`` told SCIM and hosted-agent integrators to
  send a credential that 401s. The rejection is exercised here against the real
  resolvers, not just asserted about the document.
* The failure contract was inferred from the *class* of route rather than from
  what the handlers raise, so it both invented statuses (a 409 on a read-only
  onboarding PATCH) and hid real ones (400, on a third of the surface).
* The "fail closed" default did not exist: every operation carried an explicit
  ``security``, so an operation nobody classified was published as ``[]`` —
  explicitly public — and the document-level default was unreachable.
"""
from __future__ import annotations

import json

import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.routing import APIRouter
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane.openapi import (
    AGENT_API_TOKEN_SCHEME,
    BEARER_SCHEME,
    SCIM_TOKEN_SCHEME,
)

_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}


def _schema() -> dict:
    from control_plane.main import app

    # The document is cached on the app; regenerate so ordering between tests
    # can never matter.
    app.openapi_schema = None
    return app.openapi()


def _operations(schema: dict):
    for path, item in schema["paths"].items():
        for method, operation in item.items():
            if method in _METHODS:
                yield path, method.upper(), operation


def _mounted_paths() -> set[str]:
    """Every route path on the app, whether or not it is in the schema."""
    from control_plane.main import app

    seen: set[str] = set()

    def walk(routes) -> None:
        for route in routes:
            path = getattr(route, "path", None)
            if isinstance(path, str):
                seen.add(path)
            included = getattr(route, "original_router", None)
            if included is not None:
                walk(included.routes)
                continue
            nested = getattr(route, "routes", None)
            if nested:
                walk(nested)

    walk(app.routes)
    return seen


def _documented_operations():
    """``(path, METHOD, operation, route)`` for everything in the document."""
    from control_plane.main import app
    from control_plane import openapi as module

    schema = _schema()
    paths = schema.get("paths") or {}
    for route in module._documented_routes(app):
        if not route.include_in_schema:
            continue
        item = paths.get(route.path_format)
        if not item:
            continue
        for method in route.methods or ():
            operation = item.get(method.lower())
            if isinstance(operation, dict):
                yield route.path_format, method.upper(), operation, route


# ---------------------------------------------------------------------------
# The credential is published as a scheme
# ---------------------------------------------------------------------------


def test_bearer_security_scheme_is_published() -> None:
    schemes = _schema()["components"]["securitySchemes"]

    assert schemes[BEARER_SCHEME] == {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": schemes[BEARER_SCHEME]["description"],
    }
    assert "Authorization: Bearer" in schemes[BEARER_SCHEME]["description"]


def test_authenticated_route_carries_a_security_requirement() -> None:
    schema = _schema()

    assert schema["paths"]["/v1/me"]["get"]["security"] == [{BEARER_SCHEME: []}]
    assert schema["paths"]["/v1/agents"]["post"]["security"] == [{BEARER_SCHEME: []}]
    assert schema["paths"]["/v1/agents/{name}"]["delete"]["security"] == [
        {BEARER_SCHEME: []}
    ]


def test_credentials_are_not_published_as_plain_parameters() -> None:
    """The credential belongs in ``security``, not as an optional header arg."""
    from control_plane.config import settings
    from control_plane.openapi import _HEADER_CREDENTIALS

    credentials = set(_HEADER_CREDENTIALS) | {settings.session_cookie_name.lower()}
    leaked = [
        f"{method} {path} ({parameter['in']}:{parameter['name']})"
        for path, method, operation in _operations(_schema())
        for parameter in operation.get("parameters", [])
        if parameter["name"].lower() in credentials
    ]

    assert leaked == []


def test_optional_auth_route_still_admits_anonymous_callers() -> None:
    """``optional_current_user`` routes serve anonymous callers; say so."""
    security = _schema()["paths"]["/v1/auth/session"]["get"]["security"]

    assert security == [{}, {BEARER_SCHEME: []}]


# ---------------------------------------------------------------------------
# `Authorization` is three different credentials, not one
# ---------------------------------------------------------------------------


def test_scim_operations_advertise_the_scim_token_not_the_platform_jwt() -> None:
    """A dashboard JWT is not a SCIM credential; the document must not imply it."""
    schema = _schema()

    scim = {
        f"{method} {path}": operation.get("security")
        for path, method, operation in _operations(schema)
        if path.startswith("/v1/scim/")
    }

    assert scim, "no SCIM operations in the document"
    wrong = {
        operation: security
        for operation, security in scim.items()
        if security != [{SCIM_TOKEN_SCHEME: []}]
    }
    assert wrong == {}
    assert schema["components"]["securitySchemes"][SCIM_TOKEN_SCHEME]["scheme"] == "bearer"


def test_hosted_agent_api_advertises_the_per_agent_token() -> None:
    schema = _schema()

    invoke = schema["paths"]["/v1/agents/{name}/api/invoke/{skill_name}"]["post"]
    assert invoke["security"] == [{AGENT_API_TOKEN_SCHEME: []}]

    # The file route really does take either credential, and says so.
    files = schema["paths"]["/v1/agents/{name}/api/files/{path}"]["get"]
    assert files["security"] == [
        {AGENT_API_TOKEN_SCHEME: []},
        {BEARER_SCHEME: []},
    ]


def test_every_scheme_named_by_an_operation_is_actually_declared() -> None:
    schema = _schema()
    declared = set(schema["components"]["securitySchemes"])

    named = {
        scheme
        for _path, _method, operation in _operations(schema)
        for requirement in operation.get("security") or []
        for scheme in requirement
    }

    assert named <= declared
    assert named == declared, sorted(declared - named)


@pytest.mark.asyncio
async def test_a_platform_jwt_is_rejected_by_the_scim_and_agent_api_resolvers() -> None:
    """The document's claim, checked against the code that enforces it.

    Publishing these operations under ``bearerAuth`` said "your dashboard JWT
    works here". It does not, and this is where that would be caught.
    """
    from control_plane.auth import issue_token
    from control_plane.models import Agent, AgentApiToken, Base, Organization, User
    from control_plane.routes.agents import _agent_api_token_agent, _hash_agent_api_token
    from control_plane.routes.organizations import _hash_scim_token, _scim_context
    from control_plane.models import OrganizationScimToken

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(id=1, email="owner@acme.com", password_hash="x")
            org = Organization(id=1, slug="acme", name="Acme", created_by_id=1)
            agent = Agent(
                id=1,
                owner_id=1,
                name="demo",
                version="1",
                image="img",
                card={},
            )
            session.add_all([user, org, agent])
            await session.flush()
            session.add_all(
                [
                    OrganizationScimToken(
                        organization_id=org.id,
                        label="Okta",
                        token_hash=_hash_scim_token("a2a_scim_secret"),
                        token_last4="cret",
                        enabled=True,
                        created_by_id=user.id,
                    ),
                    AgentApiToken(
                        agent_id=agent.id,
                        user_id=user.id,
                        agent_name="demo",
                        name="app",
                        token_hash=_hash_agent_api_token("a2a_app_secret"),
                        token_last4="cret",
                        scopes=["invoke"],
                        enabled=True,
                    ),
                ]
            )
            await session.commit()

            platform_jwt = issue_token(user.id)

            with pytest.raises(HTTPException) as scim_failure:
                await _scim_context("acme", f"Bearer {platform_jwt}", session)
            assert scim_failure.value.status_code == 401

            with pytest.raises(HTTPException) as api_failure:
                await _agent_api_token_agent(
                    name="demo",
                    authorization=f"Bearer {platform_jwt}",
                    session=session,
                )
            assert api_failure.value.status_code == 401

            # Control: the credential the document *does* name is accepted.
            resolved_org, _token = await _scim_context(
                "acme", "Bearer a2a_scim_secret", session
            )
            assert resolved_org.slug == "acme"
            _api_token, resolved_agent, _user = await _agent_api_token_agent(
                name="demo",
                authorization="Bearer a2a_app_secret",
                session=session,
            )
            assert resolved_agent.name == "demo"
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------


def _classified(app: FastAPI) -> dict:
    from control_plane.openapi import install_openapi_schema

    install_openapi_schema(app)
    app.openapi_schema = None
    return app.openapi()


def test_document_default_is_reachable_for_an_unclassified_guarded_route() -> None:
    """A route guarded by hand must not be published as public.

    The regression this pins: writing ``security: []`` as the fallback made the
    document-level requirement dead — an operation the module cannot classify
    was published as *explicitly* public, which is worse than saying nothing.
    """
    from control_plane.auth import current_user

    app = FastAPI()
    guarded = APIRouter(dependencies=[Depends(current_user)])

    @guarded.get("/v1/team/secrets")
    async def team_secrets() -> dict:
        return {}

    hand_rolled = APIRouter()

    @hand_rolled.get("/v1/vault/keys")
    async def vault_keys(request: Request) -> dict:
        # No dependency, no declared parameter: the credential is read here.
        if not request.headers.get("authorization"):
            raise HTTPException(401, "missing bearer token")
        return {}

    public = APIRouter()

    @public.get("/v1/vault/status")
    async def vault_status() -> dict:
        return {}

    app.include_router(guarded)
    app.include_router(hand_rolled)
    app.include_router(public)
    schema = _classified(app)

    assert schema["security"] == [{BEARER_SCHEME: []}]
    # Router-level dependency: classified, requirement stated on the operation.
    assert schema["paths"]["/v1/team/secrets"]["get"]["security"] == [
        {BEARER_SCHEME: []}
    ]
    # Unclassifiable but guarded: no operation-level override, so the document
    # default applies. Publishing ``[]`` here would override it.
    assert "security" not in schema["paths"]["/v1/vault/keys"]["get"]
    # Proven to read no credential at all: explicitly public.
    assert schema["paths"]["/v1/vault/status"]["get"]["security"] == []


def test_only_operations_that_read_no_credential_are_published_as_public() -> None:
    from control_plane import openapi as module

    published_public = [
        f"{method} {path}"
        for path, method, operation, route in _documented_operations()
        if operation.get("security") == []
        and module._reads_credential_directly(route.endpoint)
    ]

    assert published_public == []


def test_public_route_is_not_marked_as_requiring_auth() -> None:
    schema = _schema()

    assert schema["paths"]["/v1/public/agents"]["get"]["security"] == []


# ---------------------------------------------------------------------------
# The failure contract is what the code raises
# ---------------------------------------------------------------------------


def test_operator_surface_is_not_published() -> None:
    schema = _schema()

    admin_paths = [path for path in schema["paths"] if path.startswith("/v1/admin")]

    assert admin_paths == []
    # Hidden from the document, not removed from the app: operators still call
    # these, they are simply not part of the customer-facing contract.
    assert "/v1/admin/users" in _mounted_paths()


def test_hidden_routes_are_exactly_the_operator_surface() -> None:
    """Name what ``include_in_schema=False`` removes, so it stays deliberate.

    The hiding lives on the ``APIRouter(...)`` constructors, which is invisible
    to anything that only reads route decorators - notably
    ``web/apps/docs/scripts/gen_control_plane_api.py``, whose generated
    reference still counts these as published. This list is the contract that
    reconciliation has to match.
    """
    from control_plane.main import app
    from control_plane import openapi as module

    hidden = sorted(
        {
            route.path_format
            for route in module._documented_routes(app)
            if not route.include_in_schema
        }
    )

    assert hidden == [
        "/metrics",
        "/v1/admin/auth/keycloak",
        "/v1/admin/feature-flags",
        "/v1/admin/feature-flags/{flag_key}",
        "/v1/admin/settings",
        "/v1/admin/settings/{key}",
        "/v1/admin/users",
        "/v1/admin/users/{user_id}/control-policy",
        "/v1/admin/users/{user_id}/feature-flags",
        "/v1/admin/users/{user_id}/platform-token",
        "/v1/platform/gitea/webhooks/runtime-push",
        "/v1/platform/gitea/webhooks/source-push",
        "/v1/platform/mailboxes/health",
    ]


def test_shared_error_responses_are_declared_once() -> None:
    from control_plane.openapi import _SHARED_RESPONSES

    components = _schema()["components"]

    assert set(components["responses"]) == {
        name for name, _description, _schema in _SHARED_RESPONSES.values()
    }
    for response in components["responses"].values():
        ref = response["content"]["application/json"]["schema"]["$ref"]
        assert ref.rsplit("/", 1)[-1] in {"ErrorResponse", "ValidationErrorResponse"}


def test_no_operation_declares_a_failure_its_code_cannot_raise() -> None:
    """Every shared 4xx/5xx on an operation must trace to a real ``raise``.

    This is the rule the first pass broke: 403 and 409 were attached to whole
    classes of route, so 42 operations claimed a 409 that appears nowhere in
    their module. Only statuses :func:`_call_graph` found are attachable now,
    and this fails the moment a class-level shortcut comes back.
    """
    from control_plane import openapi as module

    invented = []
    for path, method, operation, route in _documented_operations():
        reachable, _schemes = module._call_graph(route)
        for status, response in operation.get("responses", {}).items():
            if not isinstance(response, dict):
                continue
            ref = response.get("$ref", "")
            if not ref.startswith("#/components/responses/"):
                continue  # declared by the route itself, not by this module
            if status == "422":
                continue  # FastAPI's own validation response, always present
            if status == "429":
                continue  # from the rate-limit policy table, not the call graph
            if int(status) not in reachable:
                invented.append(f"{method} {path} -> {status}")

    assert invented == []


def test_no_handler_body_status_is_left_undeclared() -> None:
    """The other direction: 400 used to be raised 185 times and documented 0."""
    from control_plane import openapi as module

    undocumented = []
    for path, method, operation, route in _documented_operations():
        raised, _called = module._function_facts(route.endpoint)
        published = set(operation.get("responses", {}))
        for status in sorted(raised):
            if str(status) not in published:
                undocumented.append(f"{method} {path} -> {status}")

    assert undocumented == []


def test_read_only_endpoints_do_not_claim_write_conflicts() -> None:
    """The reviewer's example, pinned.

    ``routes/onboarding.py`` contains no 409 and no 403 of its own; the 403 it
    does document comes from the Keycloak provisioning branch of ``current_user``
    (``keycloak_auth.provision_user_from_claims``), which every operation that
    accepts an OIDC access token can hit.
    """
    import inspect

    from control_plane.routes import onboarding as onboarding_module

    source = inspect.getsource(onboarding_module)
    assert "409" not in source
    assert "404" not in source

    documented = _schema()["paths"]["/v1/me/onboarding"]["patch"]["responses"]

    assert "409" not in documented
    assert "404" not in documented
    assert {"400", "401", "403"} <= set(documented)


def test_guarded_operations_document_their_auth_failures() -> None:
    missing = [
        f"{method} {path}"
        for path, method, operation in _operations(_schema())
        if operation.get("security") == [{BEARER_SCHEME: []}]
        and "401" not in operation.get("responses", {})
    ]

    assert missing == []


def test_validation_failures_point_at_the_envelope_the_app_returns() -> None:
    """FastAPI's stock 422 documents only ``detail``; the app also sends ``error``."""
    schema = _schema()
    responses = schema["paths"]["/v1/me"]["get"]["responses"]

    assert responses["422"] == {"$ref": "#/components/responses/UnprocessableEntity"}


async def test_documented_error_schema_matches_the_handler_output() -> None:
    """The declared envelope must be the one the app actually emits."""
    from control_plane.main import http_exception_handler

    response = await http_exception_handler(None, HTTPException(404, "agent not found"))
    body = json.loads(bytes(response.body))

    schemas = _schema()["components"]["schemas"]
    assert set(schemas["ErrorResponse"]["required"]) <= set(body)
    assert set(schemas["ErrorBody"]["required"]) <= set(body["error"])
    assert body["error"] == {
        "code": "http_404",
        "message": "agent not found",
        "status": 404,
    }


def test_the_documented_429_is_exactly_the_one_the_app_enforces() -> None:
    """A 429 in the document must trace to a rule in the policy table.

    This test used to assert the opposite - that no operation claimed a 429,
    because nothing enforced one. Now that ``control_plane.rate_limit`` does,
    the claim has to be neither invented nor missing: the document is derived
    from the same ``rule_for`` the request path calls, so the two cannot drift.
    """
    from control_plane.rate_limit import rule_for

    documented = {
        (method, path)
        for path, method, operation in _operations(_schema())
        if "429" in operation.get("responses", {})
    }
    enforced = {
        (method, path)
        for path, method, _operation in _operations(_schema())
        if rule_for(method, path) is not None
    }

    assert documented == enforced
    assert enforced, "the policy table matched no mounted operation at all"


def test_the_429_component_tells_a_client_how_long_to_wait() -> None:
    component = _schema()["components"]["responses"]["TooManyRequests"]

    assert component["headers"]["Retry-After"]["schema"] == {
        "type": "integer",
        "minimum": 1,
    }
    assert (
        component["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/ErrorResponse"
    )


def test_rate_limited_operations_cover_the_surfaces_that_needed_one() -> None:
    """The policy is an allowlist; pin what is on it and what is not.

    The failure this guards against is a later refactor quietly widening the
    table onto the dashboard's poll loops or the hosted-agent invoke path -
    the callers whose request rates the limits were sized around.
    """
    limited = {
        (method, path)
        for path, method, operation in _operations(_schema())
        if "429" in operation.get("responses", {})
    }

    assert ("GET", "/v1/public/agents") in limited
    assert ("POST", "/v1/auth/agent-session/exchange") in limited
    assert ("POST", "/v1/me/chat") in limited
    assert ("POST", "/v1/agents") in limited

    # Anonymous reads outside /v1/public that serve the same records, and the
    # run endpoints that reach agent execution without going through
    # ``/v1/me/chat``. Both were doors left open next to a locked one.
    assert ("GET", "/v1/bounties/{slug}") in limited
    assert ("POST", "/v1/me/agent-proofs/{name}/run") in limited
    assert ("POST", "/v1/me/schedules/{schedule_id}/run") in limited
    assert ("POST", "/v1/me/collective-runtime/runs") in limited

    # Deliberately unlimited: the SDK runtime's token check and writebacks, the
    # dashboard and CLI poll targets, the metered hosted-agent data plane, and
    # the agent toggle that starts no build.
    assert ("GET", "/v1/me") not in limited
    assert ("GET", "/v1/agents/mine") not in limited
    assert ("GET", "/v1/auth/session") not in limited
    assert ("POST", "/v1/agents/{name}/api/invoke/{skill_name}") not in limited
    assert ("POST", "/v1/agents/{name}/meta-runs") not in limited
    assert ("POST", "/v1/agents/{name}/protocol-simulations") not in limited
    assert ("POST", "/v1/agents/{name}/code-editor") not in limited


def test_call_graph_analysis_actually_walks_the_code() -> None:
    """Guard the machinery itself.

    Every derived claim in this module rests on ``_call_graph`` resolving names
    through ``__globals__`` and deferred in-function imports. If a FastAPI
    upgrade or a refactor breaks that resolution it degrades silently to "no
    statuses, no schemes", so pin one chain end to end: the 403 that only
    exists three calls below ``current_user``.
    """
    from control_plane import openapi as module
    from control_plane.auth import current_user
    from control_plane.routes.agent_receipts import post_agent_receipt

    class _Route:
        endpoint = staticmethod(post_agent_receipt)

        class dependant:
            call = None
            dependencies: list = []

    statuses, _schemes = module._call_graph(_Route())
    # Raised in the handler body, and behind a deferred ``from ..self_healing import``.
    assert {400, 401, 409} <= statuses

    header = type("D", (), {"call": current_user, "dependencies": []})
    root = type("D", (), {"call": None, "dependencies": [header]})
    guarded = type("R", (), {"endpoint": None, "dependant": root})
    statuses, _schemes = module._call_graph(guarded)
    assert 401 in statuses  # auth.current_user
    assert 403 in statuses  # keycloak_auth.provision_user_from_claims, 3 deep
