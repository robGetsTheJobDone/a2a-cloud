# `a2a_pack.sandbox`

Code-execution sandbox surface available to agents via ``ctx.sandbox``.

The abstract :class:`SandboxClient` is what agent code programs against. The
runtime layer (host-side microsandbox + FUSE-mounted MinIO, in-cluster
DaemonSet, hosted SaaS) supplies a concrete implementation.

The sandbox is **general-purpose code execution**, not Python-only. Agents
can:

  * run arbitrary shell pipelines: ``await ctx.sandbox.run_shell("git clone … && cargo build")``
  * exec a binary with explicit args (no shell parsing): ``await sb.exec("/usr/bin/git", ["clone", url])``
  * pick any OCI image: ``run_shell("npx @openai/codex …", image="node:20-slim")``

``run_python`` is just a convenience for the common Python-snippet case.

Why an abstract here when ``microsandbox`` itself already has a Python SDK?
The platform owns the *policy* layer — bucket selection, network egress,
write-path restrictions, resource caps, audit logging. Agents must depend on
the policy-respecting surface, not on the raw SDK, so the same agent code
runs unchanged across local dev / cluster / hosted environments.

## `ExecResult`  *(class)*

```python
ExecResult(stdout: 'str', stderr: 'str' = '', exit_code: 'int' = 0, truncated: 'bool' = False, files: 'tuple[dict[str, Any], ...]' = ()) -> None
```

Result of a command run inside a sandbox.

### `__init__`  *(method)*

```python
__init__(self, stdout: 'str', stderr: 'str' = '', exit_code: 'int' = 0, truncated: 'bool' = False, files: 'tuple[dict[str, Any], ...]' = ()) -> None
```

*(no docstring)*

## `HttpSandboxClient`  *(class)*

```python
HttpSandboxClient(base_url: 'str', *, default_workspace: 'str | None' = None, timeout_seconds: 'float' = 1200.0, auth_token: 'str | None' = None, grant_token: 'str | None' = None) -> 'None'
```

SandboxClient that talks to the cluster sandbox-runtime HTTP API.

### `__init__`  *(method)*

```python
__init__(self, base_url: 'str', *, default_workspace: 'str | None' = None, timeout_seconds: 'float' = 1200.0, auth_token: 'str | None' = None, grant_token: 'str | None' = None) -> 'None'
```

*(no docstring)*

### `create`  *(method)*

```python
create(self, spec: 'SandboxSpec') -> 'SandboxHandle'
```

*(no docstring)*

### `get`  *(method)*

```python
get(self, name: 'str') -> 'SandboxHandle'
```

*(no docstring)*

### `list`  *(method)*

```python
list(self) -> 'list[str]'
```

*(no docstring)*

### `remove`  *(method)*

```python
remove(self, name: 'str') -> 'None'
```

*(no docstring)*

### `run_python`  *(method)*

```python
run_python(self, code: 'str', *, image: 'str' = 'python:3.11-slim', **kwargs: 'Any') -> 'ExecResult'
```

*(no docstring)*

### `run_shell`  *(method)*

```python
run_shell(self, script: 'str', *, image: 'str' = 'python:3.11-slim', **kwargs: 'Any') -> 'ExecResult'
```

*(no docstring)*

## `HttpSandboxHandle`  *(class)*

```python
HttpSandboxHandle(*, base_url: 'str', name: 'str', timeout_seconds: 'float' = 1200.0, auth_token: 'str | None' = None, grant_token: 'str | None' = None) -> 'None'
```

Handle for the HTTP sandbox-runtime service.

### `__init__`  *(method)*

```python
__init__(self, *, base_url: 'str', name: 'str', timeout_seconds: 'float' = 1200.0, auth_token: 'str | None' = None, grant_token: 'str | None' = None) -> 'None'
```

*(no docstring)*

### `exec`  *(method)*

```python
exec(self, cmd: 'str', args: 'Sequence[str] | None' = None, *, timeout: 'float | None' = None) -> 'ExecResult'
```

*(no docstring)*

### `kill`  *(method)*

```python
kill(self) -> 'None'
```

*(no docstring)*

### `logs`  *(method)*

```python
logs(self, *, tail: 'int | None' = None) -> 'str'
```

*(no docstring)*

### `shell`  *(method)*

```python
shell(self, script: 'str', *, timeout: 'float | None' = None) -> 'ExecResult'
```

*(no docstring)*

### `stop`  *(method)*

```python
stop(self) -> 'None'
```

*(no docstring)*

## `SandboxClient`  *(class)*

```python
SandboxClient()
```

Negotiation surface handed to agents via ``ctx.sandbox``.

### `create`  *(method)*

```python
create(self, spec: 'SandboxSpec') -> 'SandboxHandle'
```

*(no docstring)*

### `get`  *(method)*

```python
get(self, name: 'str') -> 'SandboxHandle'
```

*(no docstring)*

### `list`  *(method)*

```python
list(self) -> 'list[str]'
```

*(no docstring)*

### `remove`  *(method)*

```python
remove(self, name: 'str') -> 'None'
```

*(no docstring)*

### `run_python`  *(method)*

```python
run_python(self, code: 'str', *, image: 'str' = 'python:3.11-slim', **kwargs: 'Any') -> 'ExecResult'
```

Convenience: spin a one-shot sandbox, run inline Python, tear down.

Equivalent to ``create(SandboxSpec(image=image)).exec("python", ["-c", code])``.
Use the lower-level surface when you need persistence, multiple
commands, or non-Python tools.

### `run_shell`  *(method)*

```python
run_shell(self, script: 'str', *, image: 'str' = 'python:3.11-slim', **kwargs: 'Any') -> 'ExecResult'
```

Convenience: spin a one-shot sandbox, run an arbitrary shell script,
tear down.

Pass ``image=`` to pick the toolchain (e.g. ``"node:20-slim"`` for
npm-based tools like codex, ``"rust:1-slim"`` for cargo,
``"alpine/git"`` for plain git ops). The default ``python:3.11-slim``
already has bash/coreutils/curl/git so most one-liners just work.

## `SandboxHandle`  *(class)*

```python
SandboxHandle()
```

Live handle to a running sandbox VM.

### `exec`  *(method)*

```python
exec(self, cmd: 'str', args: 'Sequence[str] | None' = None, *, timeout: 'float | None' = None) -> 'ExecResult'
```

*(no docstring)*

### `kill`  *(method)*

```python
kill(self) -> 'None'
```

*(no docstring)*

### `logs`  *(method)*

```python
logs(self, *, tail: 'int | None' = None) -> 'str'
```

*(no docstring)*

### `shell`  *(method)*

```python
shell(self, script: 'str', *, timeout: 'float | None' = None) -> 'ExecResult'
```

*(no docstring)*

### `stop`  *(method)*

```python
stop(self) -> 'None'
```

*(no docstring)*

## `SandboxRuntimeError`  *(class)*

```python
SandboxRuntimeError(operation: 'str', method: 'str', url: 'str', timeout_seconds: 'float | None' = None, status_code: 'int | None' = None, response_body: 'str | None' = None, cause_type: 'str | None' = None, cause_message: 'str | None' = None, sandbox_name: 'str | None' = None) -> None
```

Raised when the platform sandbox runtime cannot complete a request.

### `__init__`  *(method)*

```python
__init__(self, operation: 'str', method: 'str', url: 'str', timeout_seconds: 'float | None' = None, status_code: 'int | None' = None, response_body: 'str | None' = None, cause_type: 'str | None' = None, cause_message: 'str | None' = None, sandbox_name: 'str | None' = None) -> None
```

*(no docstring)*

### `to_error_payload`  *(method)*

```python
to_error_payload(self) -> 'dict[str, Any]'
```

Structured form suitable for tracking events and UI surfaces.

## `SandboxSpec`  *(class)*

```python
SandboxSpec(name: 'str', image: 'str' = 'python:3.11-slim', memory_mib: 'int' = 512, cpus: 'int' = 1, workspace: 'str | None' = None, secrets: 'tuple[str, ...]' = (), egress: 'tuple[str, ...]' = (), labels: 'dict[str, str]' = <factory>) -> None
```

Caller request shape for :meth:`SandboxClient.create`.

### `__init__`  *(method)*

```python
__init__(self, name: 'str', image: 'str' = 'python:3.11-slim', memory_mib: 'int' = 512, cpus: 'int' = 1, workspace: 'str | None' = None, secrets: 'tuple[str, ...]' = (), egress: 'tuple[str, ...]' = (), labels: 'dict[str, str]' = <factory>) -> None
```

*(no docstring)*

## `SandboxUnavailable`  *(class)*



Raised when ``ctx.sandbox`` is accessed but no runtime is attached.


*Source: `sdk/a2a-pack/a2a_pack/sandbox.py`*