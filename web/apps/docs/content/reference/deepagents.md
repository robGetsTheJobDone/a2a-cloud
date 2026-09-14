# `a2a_pack.deepagents`

DeepAgents integration backed by an A2A workspace.

DeepAgents defaults to an in-graph StateBackend. That is useful for scratch
state, but it is the wrong default for hosted A2A agents because files the
model writes disappear into LangGraph state instead of landing in the caller's
MinIO/FUSE workspace. This backend makes the invocation workspace the source
of truth for DeepAgents' built-in file tools and, when a sandbox is attached,
routes ``execute`` through the same workspace-mounted sandbox runtime.

## `WorkspaceBackend`  *(class)*

```python
WorkspaceBackend(workspace: 'WorkspaceClient', *, sandbox: 'SandboxClient | None' = None, default_image: 'str' = 'python:3.11-slim') -> 'None'
```

Workspace-backed file/runtime backend for agent frameworks.

The first adapter target is DeepAgents' ``BackendProtocol``, but the
authority is A2A's workspace/sandbox contract, not DeepAgents state.

### `__init__`  *(method)*

```python
__init__(self, workspace: 'WorkspaceClient', *, sandbox: 'SandboxClient | None' = None, default_image: 'str' = 'python:3.11-slim') -> 'None'
```

*(no docstring)*

### `aexecute`  *(method)*

```python
aexecute(self, command: 'str', *, timeout: 'int | None' = None) -> 'Any'
```

*(no docstring)*

### `download_files`  *(method)*

```python
download_files(self, paths: 'list[str]') -> 'list[Any]'
```

*(no docstring)*

### `edit`  *(method)*

```python
edit(self, file_path: 'str', old_string: 'str', new_string: 'str', replace_all: 'bool' = False) -> 'Any'
```

*(no docstring)*

### `execute`  *(method)*

```python
execute(self, command: 'str', *, timeout: 'int | None' = None) -> 'Any'
```

*(no docstring)*

### `glob`  *(method)*

```python
glob(self, pattern: 'str', path: 'str' = '/') -> 'Any'
```

*(no docstring)*

### `grep`  *(method)*

```python
grep(self, pattern: 'str', path: 'str | None' = None, glob: 'str | None' = None) -> 'Any'
```

*(no docstring)*

### `ls`  *(method)*

```python
ls(self, path: 'str') -> 'Any'
```

*(no docstring)*

### `read`  *(method)*

```python
read(self, file_path: 'str', offset: 'int' = 0, limit: 'int' = 2000) -> 'Any'
```

*(no docstring)*

### `upload_files`  *(method)*

```python
upload_files(self, files: 'list[tuple[str, bytes]]') -> 'list[Any]'
```

*(no docstring)*

### `write`  *(method)*

```python
write(self, file_path: 'str', content: 'str') -> 'Any'
```

*(no docstring)*

## `WorkspaceDeepAgentsBackend`  *(class)*

```python
WorkspaceDeepAgentsBackend(workspace: 'WorkspaceClient', *, sandbox: 'SandboxClient | None' = None, default_image: 'str' = 'python:3.11-slim') -> 'None'
```

Workspace-backed file/runtime backend for agent frameworks.

The first adapter target is DeepAgents' ``BackendProtocol``, but the
authority is A2A's workspace/sandbox contract, not DeepAgents state.

### `__init__`  *(method)*

```python
__init__(self, workspace: 'WorkspaceClient', *, sandbox: 'SandboxClient | None' = None, default_image: 'str' = 'python:3.11-slim') -> 'None'
```

*(no docstring)*

### `aexecute`  *(method)*

```python
aexecute(self, command: 'str', *, timeout: 'int | None' = None) -> 'Any'
```

*(no docstring)*

### `download_files`  *(method)*

```python
download_files(self, paths: 'list[str]') -> 'list[Any]'
```

*(no docstring)*

### `edit`  *(method)*

```python
edit(self, file_path: 'str', old_string: 'str', new_string: 'str', replace_all: 'bool' = False) -> 'Any'
```

*(no docstring)*

### `execute`  *(method)*

```python
execute(self, command: 'str', *, timeout: 'int | None' = None) -> 'Any'
```

*(no docstring)*

### `glob`  *(method)*

```python
glob(self, pattern: 'str', path: 'str' = '/') -> 'Any'
```

*(no docstring)*

### `grep`  *(method)*

```python
grep(self, pattern: 'str', path: 'str | None' = None, glob: 'str | None' = None) -> 'Any'
```

*(no docstring)*

### `ls`  *(method)*

```python
ls(self, path: 'str') -> 'Any'
```

*(no docstring)*

### `read`  *(method)*

```python
read(self, file_path: 'str', offset: 'int' = 0, limit: 'int' = 2000) -> 'Any'
```

*(no docstring)*

### `upload_files`  *(method)*

```python
upload_files(self, files: 'list[tuple[str, bytes]]') -> 'list[Any]'
```

*(no docstring)*

### `write`  *(method)*

```python
write(self, file_path: 'str', content: 'str') -> 'Any'
```

*(no docstring)*

## `a2a_deepagents_model_middleware`  *(function)*

```python
a2a_deepagents_model_middleware(creds: 'Any', *, model: 'str | None' = None, default_temperature: 'float | None' = None) -> 'Any'
```

Return middleware that resolves the DeepAgents model from A2A LLM creds.

## `create_a2a_deep_agent`  *(function)*

```python
create_a2a_deep_agent(ctx: 'Any', *, creds: 'Any | None' = None, model: 'str | None' = None, default_temperature: 'float | None' = None, middleware: 'tuple[Any, ...] | list[Any]' = (), **kwargs: 'Any') -> 'Any'
```

Create a DeepAgent whose chat model is resolved from ``ctx.llm``.

The helper keeps generated agents out of provider-specific model classes.
It uses LangChain's ``init_chat_model`` with the A2A/LiteLLM endpoint and
also installs a model-call middleware so later invocations can resolve the
current runtime model without hard-coding ``ChatOpenAI``.


*Source: `sdk/a2a-pack/a2a_pack/deepagents.py`*