# `a2a_pack.frontend`



## `FrontEndSessionHook`  *(class)*

```python
FrontEndSessionHook(require_session: 'Callable[[Request], Awaitable[Response | None]] | None') -> 'None'
```

*(no docstring)*

### `__init__`  *(method)*

```python
__init__(self, require_session: 'Callable[[Request], Awaitable[Response | None]] | None') -> 'None'
```

*(no docstring)*

## `FrontendConfig`  *(class)*

```python
FrontendConfig(path: 'str' = 'frontend', dist: 'str' = 'dist', mount: 'str' = '/', build: 'str | None' = None, auth: 'str' = 'inherit', kind: 'str' = 'static', docs_url: 'str' = 'https://docs.a2acloud.io/', framework: 'str | None' = None, start: 'str | None' = None, port: 'int' = 3000) -> None
```

Manifest declaration for a frontend packed with an agent.

### `__init__`  *(method)*

```python
__init__(self, path: 'str' = 'frontend', dist: 'str' = 'dist', mount: 'str' = '/', build: 'str | None' = None, auth: 'str' = 'inherit', kind: 'str' = 'static', docs_url: 'str' = 'https://docs.a2acloud.io/', framework: 'str | None' = None, start: 'str | None' = None, port: 'int' = 3000) -> None
```

*(no docstring)*

### `dist_dir`  *(method)*

```python
dist_dir(self, project: 'Path') -> 'Path'
```

*(no docstring)*

### `server_ready_path`  *(method)*

```python
server_ready_path(self, project: 'Path') -> 'Path'
```

*(no docstring)*

### `source_dir`  *(method)*

```python
source_dir(self, project: 'Path') -> 'Path'
```

*(no docstring)*

## `PackedFrontend`  *(class)*

```python
PackedFrontend(dist_dir: 'Path | None' = None, mount: 'str' = '/', auth: 'str' = 'inherit', docs_url: 'str' = 'https://docs.a2acloud.io/', kind: 'str' = 'static-spa', framework: 'str | None' = None, proxy_url: 'str | None' = None, start: 'str | None' = None, workdir: 'Path | None' = None, port: 'int' = 3000) -> None
```

Resolved frontend ready to serve or proxy from the agent process.

### `__init__`  *(method)*

```python
__init__(self, dist_dir: 'Path | None' = None, mount: 'str' = '/', auth: 'str' = 'inherit', docs_url: 'str' = 'https://docs.a2acloud.io/', kind: 'str' = 'static-spa', framework: 'str | None' = None, proxy_url: 'str | None' = None, start: 'str | None' = None, workdir: 'Path | None' = None, port: 'int' = 3000) -> None
```

*(no docstring)*

## `export_frontend_env`  *(function)*

```python
export_frontend_env(frontend: 'PackedFrontend | None') -> 'None'
```

Expose a resolved frontend to ``build_app`` across uvicorn reloads.

## `frontend_agent_api_path`  *(function)*

```python
frontend_agent_api_path(frontend: 'PackedFrontend | FrontendConfig | None', path: 'str') -> 'str'
```

*(no docstring)*

## `frontend_auth_metadata`  *(function)*

```python
frontend_auth_metadata(frontend: 'PackedFrontend', request: 'Request | None' = None) -> 'dict[str, Any]'
```

*(no docstring)*

## `frontend_config_payload`  *(function)*

```python
frontend_config_payload(agent: 'A2AAgent', frontend: 'PackedFrontend', request: 'Request') -> 'dict[str, Any]'
```

*(no docstring)*

## `frontend_requires_session`  *(function)*

```python
frontend_requires_session(frontend: 'PackedFrontend | FrontendConfig') -> 'bool'
```

*(no docstring)*

## `frontend_ui_metadata`  *(function)*

```python
frontend_ui_metadata(frontend: 'PackedFrontend', request: 'Request | None' = None) -> 'dict[str, Any]'
```

*(no docstring)*

## `load_frontend_config`  *(function)*

```python
load_frontend_config(project: 'Path', config: 'dict[str, Any] | None' = None) -> 'FrontendConfig | None'
```

Read the optional ``frontend`` section from ``a2a.yaml``.

## `mount_packed_frontend`  *(function)*

```python
mount_packed_frontend(app: 'FastAPI', agent: 'A2AAgent', frontend: 'PackedFrontend', *, require_session: 'Callable[[Request], Awaitable[Response | None]] | None' = None) -> 'None'
```

*(no docstring)*

## `packed_frontend_from_env`  *(function)*

```python
packed_frontend_from_env() -> 'PackedFrontend | None'
```

*(no docstring)*

## `proxy_frontend_request`  *(function)*

```python
proxy_frontend_request(frontend: 'PackedFrontend', request: 'Request') -> 'Response'
```

*(no docstring)*

## `resolve_packed_frontend`  *(function)*

```python
resolve_packed_frontend(project: 'Path', config: 'dict[str, Any] | None' = None, *, require_dist: 'bool' = True) -> 'PackedFrontend | None'
```

*(no docstring)*

## `skills_payload`  *(function)*

```python
skills_payload(agent: 'A2AAgent', request: 'Request | None' = None) -> 'dict[str, Any]'
```

*(no docstring)*

## `start_frontend_process`  *(function)*

```python
start_frontend_process(app: 'FastAPI', frontend: 'PackedFrontend') -> 'None'
```

*(no docstring)*


*Source: `sdk/a2a-pack/a2a_pack/frontend.py`*