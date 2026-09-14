"""Publish an OpenAPI document a first-time integrator can actually use.

Three facts are true of the whole API surface and so belong on no single route
decorator. Until this module existed none of them reached the published spec:

* **How you authenticate.** Every guarded endpoint resolves its caller through
  a dependency (or a hand-rolled helper) that reads ``Authorization``, so
  FastAPI documented the credential as an ordinary *optional header string*:
  ``/docs`` had no Authorize button and generated clients shipped with no auth
  at all. Here the credential is promoted to a real ``securityScheme``, applied
  to the operations that actually require one, and the now-redundant header and
  cookie parameters are dropped from those operations.

  ``Authorization`` is not one credential. Three different opaque tokens ride
  that header - the platform JWT, an organisation's SCIM token, and an agent's
  API token - and they are not interchangeable. Which one an operation takes is
  derived from the call graph (:data:`_TOKEN_SCHEME_RESOLVERS`), not from the
  header name, so ``/v1/scim/**`` and the hosted agent API advertise the token
  that actually authenticates them.

* **What a failure looks like.** :func:`control_plane.main.http_exception_handler`
  renders every ``HTTPException`` into one envelope. That envelope is declared
  once here as a shared schema and the shared error responses point at it,
  instead of annotating hundreds of operations by hand. *Which* failures an
  operation documents is derived from the ``HTTPException`` statuses reachable
  in its own call graph - see :func:`_call_graph` - never from the class of
  route, so the document does not claim failure modes the code cannot produce
  and does not hide ones it can.

* **Which endpoints are operator-only.** Handled where the routers are defined
  (``include_in_schema=False``), not here.

Everything is derived from the mounted routes, so a route added tomorrow is
covered without editing a list in this file. The default is closed: an
operation this module cannot classify carries no operation-level ``security``
and inherits the document-level bearer requirement. Only a route proven to read
no HTTP credential is published as ``security: []``.

Where this really belongs: ``Security(HTTPBearer(auto_error=False))`` inside
``control_plane.auth.current_user`` and its siblings. FastAPI would then emit
the ``security`` requirement for each route itself and
:func:`_credential_requirement` below could be deleted. That is a change to a
request-time dependency rather than to the document, so it is deliberately not
made here.
"""
from __future__ import annotations

import ast
import inspect
import sys
import textwrap
from collections import deque
from functools import lru_cache
from typing import Any, Callable, Iterator

from fastapi import FastAPI, routing
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from . import auth as auth_module
from .admin_auth import require_admin
from .config import settings

BEARER_SCHEME = "bearerAuth"
SCIM_TOKEN_SCHEME = "scimToken"
AGENT_API_TOKEN_SCHEME = "agentApiToken"
WORKSPACE_GRANT_SCHEME = "workspaceGrant"

ERROR_BODY_SCHEMA = "ErrorBody"
ERROR_SCHEMA = "ErrorResponse"
VALIDATION_ERROR_SCHEMA = "ValidationErrorResponse"

SECURITY_SCHEMES: dict[str, dict[str, Any]] = {
    BEARER_SCHEME: {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": (
            "Platform credential, sent as `Authorization: Bearer <token>`. "
            "Either a control-plane JWT issued by the dashboard sign-in flow "
            "or a Keycloak OIDC access token.\n\n"
            "It is **not** accepted by every operation on this header: the "
            "SCIM endpoints take a `scimToken` and the hosted agent API takes "
            "an `agentApiToken`. Each operation names the scheme it accepts.\n\n"
            "The browser session cookie is deliberately *not* offered as a "
            "second scheme. It is `HttpOnly`, carries the `__Host-` prefix, "
            "and every unsafe method additionally requires the dashboard "
            "`Origin` (see `enforce_browser_session_csrf`), so it is a "
            "first-party browser credential rather than something an API "
            "client can use."
        ),
    },
    SCIM_TOKEN_SCHEME: {
        "type": "http",
        "scheme": "bearer",
        "description": (
            "Organisation SCIM token (`a2a_scim_...`), sent as "
            "`Authorization: Bearer <token>`. Minted per organisation from "
            "`POST /v1/organizations/{slug}/scim/tokens` and handed to the "
            "identity provider. It authenticates the *directory*, carries no "
            "user identity, and a platform JWT is rejected here."
        ),
    },
    AGENT_API_TOKEN_SCHEME: {
        "type": "http",
        "scheme": "bearer",
        "description": (
            "Per-agent API token, sent as `Authorization: Bearer <token>`. "
            "Minted from `POST /v1/agents/{name}/api-tokens`, scoped to that "
            "one agent and to `invoke`/`mcp`. It is the credential an "
            "integrator embeds in an app that calls a hosted agent; a platform "
            "JWT is rejected on the invoke surface."
        ),
    },
    WORKSPACE_GRANT_SCHEME: {
        "type": "apiKey",
        "in": "header",
        "name": "X-A2A-Grant",
        "description": (
            "Signed workspace capability token. Minted by the control plane "
            "and handed to a running agent; it bounds path, mode, and "
            "lifetime on its own and is not interchangeable with a platform "
            "bearer token."
        ),
    },
}

# Header/cookie parameters that are really credentials. Anything listed here is
# expressed as ``security`` on the operation and removed from ``parameters``,
# so a generated client authenticates through its auth hook instead of taking a
# raw string argument.
#
# ``authorization`` maps to the platform bearer only as a *fallback*: the
# scheme an operation really takes is resolved per route from
# :data:`_TOKEN_SCHEME_RESOLVERS` first.
_HEADER_CREDENTIALS: dict[str, str] = {
    "authorization": BEARER_SCHEME,
    "x-a2a-grant": WORKSPACE_GRANT_SCHEME,
}
_SESSION_COOKIE = settings.session_cookie_name.lower()

# ``(module, qualname) -> scheme``: the functions that turn the raw
# ``Authorization`` value into a caller. Whichever of these an operation can
# reach decides which token it advertises. Keyed by name rather than imported
# so this module keeps no import edge back into ``routes``.
_TOKEN_SCHEME_RESOLVERS: dict[tuple[str, str], str] = {
    ("control_plane.routes.organizations", "_scim_context"): SCIM_TOKEN_SCHEME,
    ("control_plane.routes.agents", "_agent_api_token_agent"): AGENT_API_TOKEN_SCHEME,
    ("control_plane.auth", "user_from_token"): BEARER_SCHEME,
    ("control_plane.auth", "user_identity_from_token"): BEARER_SCHEME,
    ("control_plane.auth", "user_session_identity_from_token"): BEARER_SCHEME,
}

# status -> (component name, description, schema)
_SHARED_RESPONSES: dict[str, tuple[str, str, str]] = {
    "400": (
        "BadRequest",
        "The request is well-formed but semantically rejected: an unusable "
        "field value, an unsupported option, or a payload the target cannot "
        "accept.",
        ERROR_SCHEMA,
    ),
    "401": (
        "Unauthorized",
        "No credential was supplied, or it is expired, malformed, or of a "
        "token type that is not valid for this API.",
        ERROR_SCHEMA,
    ),
    "402": (
        "PaymentRequired",
        "The account has no remaining allowance for this operation.",
        ERROR_SCHEMA,
    ),
    "403": (
        "Forbidden",
        "The credential is valid but not permitted for this operation: not "
        "the owner, or outside the token's scope.",
        ERROR_SCHEMA,
    ),
    "404": (
        "NotFound",
        "The addressed resource does not exist, or is not visible to this "
        "caller.",
        ERROR_SCHEMA,
    ),
    "409": (
        "Conflict",
        "The request conflicts with current state - a duplicate name, an "
        "unsupported state transition, or an already-settled record.",
        ERROR_SCHEMA,
    ),
    "410": (
        "Gone",
        "The resource existed and has been retired; it will not come back at "
        "this address.",
        ERROR_SCHEMA,
    ),
    "413": (
        "PayloadTooLarge",
        "The request body exceeds the limit this operation accepts.",
        ERROR_SCHEMA,
    ),
    "415": (
        "UnsupportedMediaType",
        "The request body's content type is not one this operation accepts.",
        ERROR_SCHEMA,
    ),
    "405": (
        "MethodNotAllowed",
        "The addressed resource exists but does not serve this HTTP method.",
        ERROR_SCHEMA,
    ),
    "422": (
        "UnprocessableEntity",
        "The request failed validation; `detail` lists the offending fields.",
        VALIDATION_ERROR_SCHEMA,
    ),
    "429": (
        "TooManyRequests",
        "The caller exceeded the request-rate ceiling for this class of "
        "endpoint. `Retry-After` carries the number of seconds until the "
        "window resets. Only the endpoint classes named in "
        "`control_plane.rate_limit` carry a ceiling; the rest of the API has "
        "none.",
        ERROR_SCHEMA,
    ),
    "500": (
        "InternalServerError",
        "The control plane failed while carrying out an otherwise valid "
        "request. Safe to retry once.",
        ERROR_SCHEMA,
    ),
    "502": (
        "BadGateway",
        "A system the control plane depends on (the agent, the registry, the "
        "identity provider) answered with an error.",
        ERROR_SCHEMA,
    ),
    "503": (
        "ServiceUnavailable",
        "A capability this operation needs is not configured or is "
        "temporarily unavailable.",
        ERROR_SCHEMA,
    ),
    "504": (
        "GatewayTimeout",
        "A system the control plane depends on did not answer in time.",
        ERROR_SCHEMA,
    ),
}
# Headers a shared response carries. Only 429 has any: a client that cannot see
# how long to wait will retry immediately and stay throttled.
_SHARED_RESPONSE_HEADERS: dict[str, dict[str, Any]] = {
    "429": {
        "Retry-After": {
            "description": "Seconds until the caller's window resets.",
            "schema": {"type": "integer", "minimum": 1},
        },
        "RateLimit-Limit": {
            "description": "Requests allowed per window for this endpoint class.",
            "schema": {"type": "integer"},
        },
        "RateLimit-Reset": {
            "description": "Seconds until the window resets. Mirrors `Retry-After`.",
            "schema": {"type": "integer"},
        },
    },
}

# 429 is the one shared status that is *not* derived from the call graph. No
# handler raises it: it comes from the ``rate_limit_guard`` dependency mounted
# on the whole app, which would otherwise make every one of the ~300 operations
# claim a ceiling that only a named minority have. So it is attached from the same
# declarative policy table that enforces it - see
# :func:`control_plane.rate_limit.rule_for` - and the guard itself is excluded
# from the status walk below. Add an endpoint to that table and it documents
# itself; there is no second list to keep in sync.
#
# Nothing else is absent - this table covers every status the control plane
# *raises*, so "not documented" means "the code cannot produce it".
#
# Two surfaces answer with a status they did not choose, and neither is a claim
# by the control plane about itself:
#   * the agent-ingress data plane (``agent_ingress.py``) is a different
#     service, and its concurrency bulkhead is where this repo's only literal
#     429 lives;
#   * the Agent API invoke routes relay a hosted or imported agent's own 4xx
#     verbatim (``routes/agents._agent_error_status``), so a status outside
#     this table can appear there. It is always tagged: ``detail.error`` is
#     ``"agent_error"`` and ``detail.agent_status`` is the agent's own code.
#     An agent 5xx is *not* relayed - it becomes a 502, because re-emitting it
#     would claim the control plane itself failed.

# ``enforce_browser_session_csrf`` raises the only 403 that a documented
# credential can never trigger: it returns early for safe methods *and* for any
# request carrying a bearer token (auth.py), so it fires only for a browser
# cookie - which is deliberately not published as a scheme. Excluding it keeps
# 403 off the ~100 read-only operations that cannot actually produce one.
#
# ``rate_limit_guard`` is excluded for the mirror-image reason: it is mounted on
# every route, so walking it would attach its 429 - and the statuses reachable
# from the credential verification its key derivation calls - to the entire
# surface. Its 429 is attached per operation from the policy table instead.
_STATUS_SOURCE_EXCLUSIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("control_plane.auth", "enforce_browser_session_csrf"),
        ("control_plane.rate_limit", "rate_limit_guard"),
    }
)

# How far to follow calls out of a handler when collecting failure statuses.
# 3 reaches the "handler -> route helper -> shared helper" shape this codebase
# actually uses - and exactly reaches ``current_user -> user_from_token ->
# _keycloak_user_from_token -> provision_user_from_claims``, the 403 every
# operation that accepts a Keycloak token can return.
_MAX_CALL_DEPTH = 3

# Packages the walk will follow calls into. Our own code only: the mounted MCP
# endpoints live in the SDK (``a2a_pack.mcp.http``) and reject callers there,
# so stopping at the ``control_plane`` boundary would hide their 401.
_FIRST_PARTY_PREFIXES = ("control_plane.", "a2a_pack.", "main_agent.")


def install_openapi_schema(app: FastAPI) -> None:
    """Wrap ``app.openapi`` so the document carries auth and error contracts."""
    generate = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = generate()
        _decorate(app, schema)
        app.openapi_schema = schema
        return schema

    app.openapi = openapi  # type: ignore[method-assign]


def _decorate(app: FastAPI, schema: dict[str, Any]) -> None:
    components = schema.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    schemas.update(_error_schemas(schemas))
    components.setdefault("securitySchemes", {}).update(SECURITY_SCHEMES)
    components.setdefault("responses", {}).update(_shared_response_components())
    # Fail closed. An operation this module cannot classify carries *no*
    # operation-level ``security`` and so inherits this requirement, rather
    # than being published as public. Only a route proven to read no HTTP
    # credential at all gets an explicit ``security: []``.
    schema["security"] = [{BEARER_SCHEME: []}]
    for route, method, operation in _route_operations(app, schema):
        statuses, resolved = _call_graph(route)
        _apply_security(route, operation, resolved)
        _apply_error_responses(operation, statuses)
        _apply_rate_limit_response(route, method, operation)


def _documented_routes(app: FastAPI) -> Iterator[Any]:
    """The prefix-resolved routes ``get_openapi`` actually documents.

    ``include_router`` nests routers rather than flattening them onto
    ``app.routes`` (FastAPI >= 0.136), so the mounted path and the inherited
    ``include_in_schema`` only exist on the route *context*. Fall back to the
    flat list on older FastAPI, where ``app.routes`` already holds every route.
    """
    iter_contexts = getattr(routing, "iter_route_contexts", None)
    if iter_contexts is None:  # pragma: no cover - older FastAPI
        yield from (route for route in app.routes if isinstance(route, APIRoute))
        return
    for context in iter_contexts(app.routes):
        if isinstance(context.original_route, APIRoute):
            yield context


def _route_operations(
    app: FastAPI,
    schema: dict[str, Any],
) -> Iterator[tuple[Any, str, dict[str, Any]]]:
    paths = schema.get("paths") or {}
    for route in _documented_routes(app):
        if not route.include_in_schema:
            continue
        item = paths.get(route.path_format)
        if not item:
            continue
        for method in route.methods or ():
            operation = item.get(method.lower())
            if isinstance(operation, dict):
                yield route, method.upper(), operation


# --------------------------------------------------------------------------
# Static call-graph analysis
#
# Both halves of the document need the same question answered: *what does the
# code behind this operation actually do?* - which credential resolver it
# reaches, and which ``HTTPException`` statuses it can raise. Both are read off
# one bounded walk of the handler's call graph.
# --------------------------------------------------------------------------


def _key(func: Any) -> tuple[str, str] | None:
    module = getattr(func, "__module__", None)
    qualname = getattr(func, "__qualname__", None)
    if isinstance(module, str) and isinstance(qualname, str):
        return module, qualname
    return None


def _is_first_party(func: Any) -> bool:
    module = f"{getattr(func, '__module__', '')}."
    return module.startswith(_FIRST_PARTY_PREFIXES)


def _source_ast(func: Any) -> ast.AST | None:
    try:
        source = textwrap.dedent(inspect.getsource(inspect.unwrap(func)))
    except (OSError, TypeError, IndentationError):  # pragma: no cover - defensive
        return None
    try:
        return ast.parse(source)
    except SyntaxError:  # pragma: no cover - defensive
        return None


def _local_names(tree: ast.AST, func: Any) -> dict[str, Any]:
    """Names a function imports *inside its own body*.

    This codebase defers plenty of imports into handlers (``from ..self_healing
    import maybe_enqueue_runtime_failure``) precisely because they are heavy; without this
    the walk would stop at the deferred boundary. Only already-imported modules
    are consulted - nothing is imported as a side effect of building a document.
    """
    package = getattr(sys.modules.get(func.__module__, None), "__package__", "") or ""
    resolved: dict[str, Any] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        base = node.module or ""
        if node.level:
            parts = package.split(".")
            parts = parts[: len(parts) - node.level + 1] if node.level > 1 else parts
            base = ".".join([*parts, base]) if base else ".".join(parts)
        module = sys.modules.get(base)
        if module is None:
            continue
        for alias in node.names:
            value = getattr(module, alias.name, None)
            if value is not None:
                resolved[alias.asname or alias.name] = value
    return resolved


def _resolve(node: ast.AST, names: dict[str, Any]) -> Any:
    if isinstance(node, ast.Name):
        return names.get(node.id)
    if isinstance(node, ast.Attribute):
        base = _resolve(node.value, names)
        return getattr(base, node.attr, None) if base is not None else None
    return None


def _literal_status(call: ast.Call) -> int | None:
    if call.args and isinstance(call.args[0], ast.Constant):
        value = call.args[0].value
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    for keyword in call.keywords:
        if keyword.arg == "status_code" and isinstance(keyword.value, ast.Constant):
            value = keyword.value.value
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    return None


@lru_cache(maxsize=None)
def _function_facts(func: Any) -> tuple[frozenset[int], tuple[Any, ...]]:
    """``(HTTPException statuses raised here, control-plane callables called)``."""
    tree = _source_ast(func)
    if tree is None:
        return frozenset(), ()
    names = {**getattr(func, "__globals__", {}), **_local_names(tree, func)}
    statuses: set[int] = set()
    called: list[Any] = []
    seen: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = _resolve(node.func, names)
        if target is None:
            continue
        if getattr(target, "__name__", None) == "HTTPException":
            status = _literal_status(node)
            if status is not None:
                statuses.add(status)
            continue
        if not (inspect.isfunction(target) or inspect.iscoroutinefunction(target)):
            continue
        if not _is_first_party(target):
            continue
        if id(target) not in seen:
            seen.add(id(target))
            called.append(target)
    return frozenset(statuses), tuple(called)


def _call_graph(route: Any) -> tuple[frozenset[int], frozenset[str]]:
    """Statuses and credential schemes reachable from one operation.

    Seeded with the endpoint plus every dependency FastAPI resolves for it, so
    ``Depends(current_user)``'s 401 and a helper's 409 are found the same way.
    Bounded by :data:`_MAX_CALL_DEPTH`; a status is an over-approximation only
    in the sense that a branch may be unreachable at run time.

    Breadth-first on purpose: with one shared ``visited`` set, a depth-first
    walk would reach a shared helper at depth 3, mark it seen, and then refuse
    to expand it when the same helper turns up at depth 1 - so the answer would
    depend on traversal order. Breadth-first reaches every function at its
    shortest distance, which makes the result stable.
    """
    seeds: list[Any] = [route.endpoint, *(d.call for d in _iter_dependants(route.dependant))]
    statuses: set[int] = set()
    schemes: set[str] = set()
    visited: set[int] = set()
    frontier = deque((func, 0) for func in seeds if callable(func))
    while frontier:
        func, depth = frontier.popleft()
        key = _key(func)
        if key in _STATUS_SOURCE_EXCLUSIONS:
            continue
        if id(func) in visited:
            continue
        visited.add(id(func))
        if key in _TOKEN_SCHEME_RESOLVERS:
            schemes.add(_TOKEN_SCHEME_RESOLVERS[key])
        # Seeds are analysed whatever package they come from - the mounted MCP
        # endpoints are third-party closures. ``_function_facts`` only ever
        # hands back first-party callables, so the walk stays inside
        # ``control_plane`` from depth 1 on.
        raised, called = _function_facts(func)
        statuses |= raised
        if depth < _MAX_CALL_DEPTH:
            frontier.extend((target, depth + 1) for target in called)
    return frozenset(statuses), frozenset(schemes)


def _iter_dependants(dependant: Dependant) -> Iterator[Dependant]:
    stack = [dependant]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(current.dependencies)


def _auth_dependencies() -> tuple[frozenset[Any], frozenset[Any]]:
    """Split ``control_plane.auth``'s caller-resolving dependencies.

    Discovered by name - ``current_*`` rejects a request without a credential,
    ``optional_current_*`` degrades to anonymous - so a dependency added to
    ``auth.py`` later is picked up without editing this module.
    """
    required: set[Any] = {require_admin}
    optional: set[Any] = set()
    for name, obj in vars(auth_module).items():
        if not inspect.isfunction(obj) or obj.__module__ != auth_module.__name__:
            continue
        if name.startswith("optional_current_"):
            optional.add(obj)
        elif name.startswith("current_"):
            required.add(obj)
    return frozenset(required), frozenset(optional)


_REQUIRED_AUTH_DEPENDENCIES, _OPTIONAL_AUTH_DEPENDENCIES = _auth_dependencies()


def _credential_requirement(route: Any) -> str | None:
    calls = {dependant.call for dependant in _iter_dependants(route.dependant)}
    if calls & _REQUIRED_AUTH_DEPENDENCIES:
        return "required"
    if calls & _OPTIONAL_AUTH_DEPENDENCIES:
        return "optional"
    return None


@lru_cache(maxsize=None)
def _reads_credential_directly(endpoint: Callable[..., Any] | None) -> bool:
    """Does this handler pull a credential out of the request itself?

    A handler that reads ``request.headers["authorization"]`` and rejects on it
    is guarded, but has no dependency and no declared parameter to show for it.
    Such a route must not be published as public - it is left unclassified so
    the document-level requirement applies.
    """
    if endpoint is None:
        return False
    tree = _source_ast(endpoint)
    if tree is None:
        return False
    wanted = set(_HEADER_CREDENTIALS) | {_SESSION_COOKIE}
    return any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.lower() in wanted
        for node in ast.walk(tree)
    )


def _parameter_scheme(parameter: dict[str, Any]) -> str | None:
    name = str(parameter.get("name") or "").lower()
    location = parameter.get("in")
    if location == "header":
        return _HEADER_CREDENTIALS.get(name)
    if location == "cookie" and name == _SESSION_COOKIE:
        # Accepted by the same dependency that reads the bearer header; the
        # header is the scheme an API client can actually use.
        return BEARER_SCHEME
    return None


def _apply_security(
    route: Any,
    operation: dict[str, Any],
    resolved: frozenset[str],
) -> str | None:
    parameters = operation.get("parameters") or []
    declared = {
        scheme
        for scheme in (_parameter_scheme(parameter) for parameter in parameters)
        if scheme is not None
    }
    schemes: set[str] = set()
    for scheme in declared:
        if scheme != BEARER_SCHEME:
            schemes.add(scheme)
            continue
        # The platform JWT is only one of the tokens that rides this header.
        # Prefer whatever the route's own call graph proves it accepts.
        schemes |= resolved or {BEARER_SCHEME}

    credential = _credential_requirement(route)
    if credential is not None:
        # A ``current_*``/``optional_current_*`` dependency always resolves a
        # platform credential, whatever else the handler also accepts.
        schemes.add(BEARER_SCHEME)
    elif schemes:
        # A route whose only credential is a raw header (SCIM, the hosted agent
        # API, workspace grants) still requires it.
        credential = "required"

    if not schemes:
        if _reads_credential_directly(route.endpoint):
            # Guarded by something this module could not name. Leave the
            # operation without a ``security`` key so it inherits the
            # document-level bearer requirement instead of reading as public.
            operation.pop("security", None)
            return "unclassified"
        # Proven to take no HTTP credential. Some of these still reject the
        # request - the receipt and session intake routes authenticate an
        # Ed25519 signature *inside the body*, which no OpenAPI security scheme
        # can express - so they publish the 401 without publishing a scheme.
        operation["security"] = []
        return None

    requirement: list[dict[str, list[str]]] = [
        {scheme: []} for scheme in sorted(schemes)
    ]
    # An optional-auth route serves anonymous callers too, which OpenAPI spells
    # as an empty requirement alongside the credentialed one.
    operation["security"] = (
        [{}, *requirement] if credential == "optional" else requirement
    )
    remaining = [
        parameter for parameter in parameters if _parameter_scheme(parameter) is None
    ]
    if remaining:
        operation["parameters"] = remaining
    else:
        operation.pop("parameters", None)
    return credential


def _apply_error_responses(
    operation: dict[str, Any],
    statuses: frozenset[int],
) -> None:
    """Attach the shared failures this operation can actually produce.

    There is no rule by route class here - no "authenticated writes can 409",
    no "path-addressed routes can 404". Every status is one :func:`_call_graph`
    found a literal ``raise HTTPException(<status>, ...)`` for, reachable from
    this operation's own handler and dependencies. So a read-only endpoint that
    cannot 403 does not claim it, ``PATCH /v1/me/onboarding`` does not claim a
    409 its module never raises, and the ~100 handlers that can 400 finally say
    so.

    It is an over-approximation in exactly one direction: a ``raise`` may sit
    on a branch unreachable for some inputs. It never claims a status the code
    has no way to produce.

    ``setdefault`` leaves any response a route documents itself untouched.
    """
    responses = operation.setdefault("responses", {})
    for status in sorted(statuses):
        key = str(status)
        if key in _SHARED_RESPONSES:
            _use_shared_response(responses, key)
    _retarget_validation_response(responses)


def _apply_rate_limit_response(
    route: Any,
    method: str,
    operation: dict[str, Any],
) -> None:
    """Document the 429 on exactly the operations the limiter enforces one on.

    Read off :func:`control_plane.rate_limit.rule_for`, the same table the
    request path consults, so the document cannot drift from the behaviour.
    """
    from .rate_limit import rule_for

    if rule_for(method, route.path_format) is not None:
        _use_shared_response(operation.setdefault("responses", {}), "429")


def _use_shared_response(responses: dict[str, Any], status: str) -> None:
    component = _SHARED_RESPONSES[status][0]
    responses.setdefault(status, {"$ref": f"#/components/responses/{component}"})


def _retarget_validation_response(responses: dict[str, Any]) -> None:
    """Point FastAPI's stock 422 at the envelope the app really returns."""
    existing = responses.get("422")
    if not isinstance(existing, dict):
        return
    schema = (
        (existing.get("content") or {}).get("application/json", {}).get("schema", {})
    )
    if schema.get("$ref") != "#/components/schemas/HTTPValidationError":
        return
    responses["422"] = {
        "$ref": f"#/components/responses/{_SHARED_RESPONSES['422'][0]}"
    }


def _shared_response_components() -> dict[str, dict[str, Any]]:
    components: dict[str, dict[str, Any]] = {}
    for status, (component, description, schema) in _SHARED_RESPONSES.items():
        body: dict[str, Any] = {
            "description": description,
            "content": {
                "application/json": {
                    "schema": {"$ref": f"#/components/schemas/{schema}"}
                }
            },
        }
        headers = _SHARED_RESPONSE_HEADERS.get(status)
        if headers:
            body["headers"] = headers
        components[component] = body
    return components


def _error_schemas(existing: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The envelope every failure is rendered in, as one reusable schema.

    Mirrors ``main.http_exception_handler`` / ``main.validation_exception_handler``:
    the legacy ``detail`` field plus a machine-readable ``error`` object.
    """
    validation_item: dict[str, Any] = (
        {"$ref": "#/components/schemas/ValidationError"}
        if "ValidationError" in existing
        else {"type": "object"}
    )
    return {
        ERROR_BODY_SCHEMA: {
            "title": ERROR_BODY_SCHEMA,
            "type": "object",
            "required": ["code", "message", "status"],
            "properties": {
                "code": {
                    "title": "Code",
                    "type": "string",
                    "description": (
                        "Stable machine-readable code: `http_<status>` for "
                        "aborted requests, `validation_error` for a rejected "
                        "request body."
                    ),
                },
                "message": {"title": "Message", "type": "string"},
                "status": {"title": "Status", "type": "integer"},
            },
        },
        ERROR_SCHEMA: {
            "title": ERROR_SCHEMA,
            "type": "object",
            "required": ["detail", "error"],
            "properties": {
                "detail": {
                    "title": "Detail",
                    "type": "string",
                    "description": "Human-readable reason. Prefer `error.code`.",
                },
                "error": {"$ref": f"#/components/schemas/{ERROR_BODY_SCHEMA}"},
            },
        },
        VALIDATION_ERROR_SCHEMA: {
            "title": VALIDATION_ERROR_SCHEMA,
            "type": "object",
            "required": ["detail", "error"],
            "properties": {
                "detail": {
                    "title": "Detail",
                    "type": "array",
                    "items": validation_item,
                },
                "error": {"$ref": f"#/components/schemas/{ERROR_BODY_SCHEMA}"},
            },
        },
    }
