# `a2a_pack.auth`

Pluggable auth principal models.

These describe *who* is invoking a skill. The runtime auth provider produces
an instance of the agent's declared ``auth_model`` and hands it to the
:class:`RunContext`.

## `APIKeyAuth`  *(class)*

```python
APIKeyAuth(*, api_key_id: str, scopes: list[str] = <factory>) -> None
```

Caller authenticated by a long-lived API key.

## `APIKeyAuthResolver`  *(class)*

```python
APIKeyAuthResolver(*, accepted_key: 'str | None' = None, api_key_id: 'str' = 'configured', scopes: 'list[str] | None' = None) -> 'None'
```

Default resolver for ``auth_model = APIKeyAuth``.

Requests must provide the configured bearer value. Public agents should use
``NoAuth`` rather than an API-key model without configured verification.

### `__init__`  *(method)*

```python
__init__(self, *, accepted_key: 'str | None' = None, api_key_id: 'str' = 'configured', scopes: 'list[str] | None' = None) -> 'None'
```

*(no docstring)*

### `principal_id`  *(method)*

```python
principal_id(auth_principal: 'Any') -> 'str'
```

Return a short prefix of the api_key_id so receipts can pivot on it
without leaking the full key material.

### `resolve`  *(method)*

```python
resolve(self, token: 'str | None', *, headers: 'Mapping[str, str]', agent: 'Any') -> 'APIKeyAuth'
```

*(no docstring)*

## `AuthError`  *(class)*

```python
AuthError(message: 'str', *, status_code: 'int' = 401) -> 'None'
```

Raised by auth resolvers when a caller token is missing or invalid.

### `__init__`  *(method)*

```python
__init__(self, message: 'str', *, status_code: 'int' = 401) -> 'None'
```

*(no docstring)*

## `AuthResolver`  *(class)*

```python
AuthResolver()
```

Resolve an inbound bearer token into an agent's typed auth principal.

Agent authors set ``auth_model`` to the principal shape their skills want
and optionally set ``auth_resolver`` to an instance of this class. The
resolver may validate JWTs, call a customer API, hit an OIDC userinfo or
introspection endpoint, or bridge any other identity system.

### `principal_id`  *(method)*

```python
principal_id(auth_principal: 'Any') -> 'str'
```

Stable principal identifier for receipts / replay sessions.

Default returns ``""`` for resolvers that cannot derive a caller id.
Subclasses override to surface the natural id of their auth model.

### `resolve`  *(method)*

```python
resolve(self, token: 'str | None', *, headers: 'Mapping[str, str]', agent: 'Any') -> 'AuthT'
```

*(no docstring)*

## `JWTAuth`  *(class)*

```python
JWTAuth(*, sub: str, org_id: str | None = None, email: str | None = None, scopes: list[str] = <factory>) -> None
```

Caller authenticated by a JWT (typically from a user-facing login).

## `NoAuth`  *(class)*

```python
NoAuth() -> None
```

Public agent: no caller identity required.

## `NoAuthResolver`  *(class)*

```python
NoAuthResolver()
```

*(no docstring)*

### `principal_id`  *(method)*

```python
principal_id(auth_principal: 'Any') -> 'str'
```

*(no docstring)*

### `resolve`  *(method)*

```python
resolve(self, token: 'str | None', *, headers: 'Mapping[str, str]', agent: 'Any') -> 'NoAuth'
```

*(no docstring)*

## `OIDCUserInfoAuthResolver`  *(class)*

```python
OIDCUserInfoAuthResolver(url: 'str', *, auth_model: 'type[AuthT]', method: 'str' = 'GET', timeout_seconds: 'float' = 5.0, token_header: 'str' = 'authorization', token_prefix: 'str' = 'bearer', active_field: 'str | None' = 'active', subject_field: 'str' = 'sub', email_field: 'str' = 'email', org_field: 'str' = 'org_id', scopes_field: 'str' = 'scope', extra_fields: 'Mapping[str, str] | None' = None) -> 'None'
```

Resolve bearer tokens by calling an external API.

Works with OIDC ``userinfo`` endpoints and most homegrown
``GET /me``/``POST /introspect`` APIs. The response JSON is mapped into
``auth_model``. For SAML-backed apps, put your SAML/session exchange
behind a bearer-token endpoint and use this same resolver.

### `__init__`  *(method)*

```python
__init__(self, url: 'str', *, auth_model: 'type[AuthT]', method: 'str' = 'GET', timeout_seconds: 'float' = 5.0, token_header: 'str' = 'authorization', token_prefix: 'str' = 'bearer', active_field: 'str | None' = 'active', subject_field: 'str' = 'sub', email_field: 'str' = 'email', org_field: 'str' = 'org_id', scopes_field: 'str' = 'scope', extra_fields: 'Mapping[str, str] | None' = None) -> 'None'
```

*(no docstring)*

### `principal_id`  *(method)*

```python
principal_id(auth_principal: 'Any') -> 'str'
```

Best-effort caller id: ``sub`` if present, else ``email``.

### `resolve`  *(method)*

```python
resolve(self, token: 'str | None', *, headers: 'Mapping[str, str]', agent: 'Any') -> 'AuthT'
```

*(no docstring)*

## `PlatformUserAuth`  *(class)*

```python
PlatformUserAuth(*, sub: str, user_id: int | None = None, email: str | None = None, org_id: str | None = None, org_slug: str | None = None, scopes: list[str] = <factory>) -> None
```

Caller authenticated by the A2A platform session.

Use this for packed frontend apps that should require a logged-in A2A
Cloud user without wiring a custom OAuth/OIDC resolver.

## `PlatformUserAuthResolver`  *(class)*

```python
PlatformUserAuthResolver(*, allow_local_dev: 'bool' = True, trust_headers: 'bool | None' = None, cookie_name: 'str' = 'a2a_session') -> 'None'
```

Resolve the user identity supplied by the A2A platform.

Hosted deployments verify the HttpOnly platform session cookie against
``A2A_CP_URL/v1/me``. Trusted ``x-a2a-*`` identity headers are supported
only when explicitly enabled for gateway deployments.

### `__init__`  *(method)*

```python
__init__(self, *, allow_local_dev: 'bool' = True, trust_headers: 'bool | None' = None, cookie_name: 'str' = 'a2a_session') -> 'None'
```

*(no docstring)*

### `principal_id`  *(method)*

```python
principal_id(auth_principal: 'Any') -> 'str'
```

*(no docstring)*

### `resolve`  *(method)*

```python
resolve(self, token: 'str | None', *, headers: 'Mapping[str, str]', agent: 'Any') -> 'PlatformUserAuth'
```

*(no docstring)*

## `RemoteBearerAuthResolver`  *(class)*

```python
RemoteBearerAuthResolver(url: 'str', *, auth_model: 'type[AuthT]', method: 'str' = 'GET', timeout_seconds: 'float' = 5.0, token_header: 'str' = 'authorization', token_prefix: 'str' = 'bearer', active_field: 'str | None' = 'active', subject_field: 'str' = 'sub', email_field: 'str' = 'email', org_field: 'str' = 'org_id', scopes_field: 'str' = 'scope', extra_fields: 'Mapping[str, str] | None' = None) -> 'None'
```

Resolve bearer tokens by calling an external API.

Works with OIDC ``userinfo`` endpoints and most homegrown
``GET /me``/``POST /introspect`` APIs. The response JSON is mapped into
``auth_model``. For SAML-backed apps, put your SAML/session exchange
behind a bearer-token endpoint and use this same resolver.

### `__init__`  *(method)*

```python
__init__(self, url: 'str', *, auth_model: 'type[AuthT]', method: 'str' = 'GET', timeout_seconds: 'float' = 5.0, token_header: 'str' = 'authorization', token_prefix: 'str' = 'bearer', active_field: 'str | None' = 'active', subject_field: 'str' = 'sub', email_field: 'str' = 'email', org_field: 'str' = 'org_id', scopes_field: 'str' = 'scope', extra_fields: 'Mapping[str, str] | None' = None) -> 'None'
```

*(no docstring)*

### `principal_id`  *(method)*

```python
principal_id(auth_principal: 'Any') -> 'str'
```

Best-effort caller id: ``sub`` if present, else ``email``.

### `resolve`  *(method)*

```python
resolve(self, token: 'str | None', *, headers: 'Mapping[str, str]', agent: 'Any') -> 'AuthT'
```

*(no docstring)*

## `StaticAuthResolver`  *(class)*

```python
StaticAuthResolver(principal: 'AuthT') -> 'None'
```

Resolver for tests/local adapters that always returns one principal.

### `__init__`  *(method)*

```python
__init__(self, principal: 'AuthT') -> 'None'
```

*(no docstring)*

### `principal_id`  *(method)*

```python
principal_id(auth_principal: 'Any') -> 'str'
```

*(no docstring)*

### `resolve`  *(method)*

```python
resolve(self, token: 'str | None', *, headers: 'Mapping[str, str]', agent: 'Any') -> 'AuthT'
```

*(no docstring)*

## `resolve_auth`  *(function)*

```python
resolve_auth(resolver: 'AuthResolver[AuthT]', token: 'str | None', *, headers: 'Mapping[str, str]', agent: 'Any') -> 'AuthT'
```

Call sync or async custom resolver implementations.


*Source: `sdk/a2a-pack/a2a_pack/auth.py`*