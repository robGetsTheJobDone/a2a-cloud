# `a2a_pack.cli.local`



## `LocalCheck`  *(class)*

```python
LocalCheck(name: 'str', ok: 'bool', message: 'str') -> None
```

LocalCheck(name: 'str', ok: 'bool', message: 'str')

### `__init__`  *(method)*

```python
__init__(self, name: 'str', ok: 'bool', message: 'str') -> None
```

*(no docstring)*

## `LocalProject`  *(class)*

```python
LocalProject(project: 'Path', config: 'dict[str, Any]', entrypoint: 'str', agent_cls: 'type[A2AAgent]') -> None
```

LocalProject(project: 'Path', config: 'dict[str, Any]', entrypoint: 'str', agent_cls: 'type[A2AAgent]')

### `__init__`  *(method)*

```python
__init__(self, project: 'Path', config: 'dict[str, Any]', entrypoint: 'str', agent_cls: 'type[A2AAgent]') -> None
```

*(no docstring)*

## `build_local_workspace`  *(function)*

```python
build_local_workspace(agent_cls: 'type[A2AAgent]', root: 'Path') -> 'FileSystemWorkspaceClient | None'
```

*(no docstring)*

## `ensure_local_workspace`  *(function)*

```python
ensure_local_workspace(project: 'Path', *, workspace: 'Path | None' = None) -> 'Path'
```

*(no docstring)*

## `load_env_file`  *(function)*

```python
load_env_file(path: 'Path', *, override: 'bool' = False) -> 'dict[str, str]'
```

*(no docstring)*

## `load_local_project`  *(function)*

```python
load_local_project(project: 'Path') -> 'LocalProject'
```

*(no docstring)*

## `run_local_preflight`  *(function)*

```python
run_local_preflight(project: 'Path', *, env_file: 'Path', workspace: 'Path | None' = None, invoke: 'bool' = False, skill_name: 'str | None' = None, args_json: 'str | None' = None) -> 'tuple[list[LocalCheck], dict[str, Any] | None]'
```

*(no docstring)*

## `run_preflight_sync`  *(function)*

```python
run_preflight_sync(*args: 'Any', **kwargs: 'Any') -> 'tuple[list[LocalCheck], dict[str, Any] | None]'
```

*(no docstring)*

## `sample_args_from_schema`  *(function)*

```python
sample_args_from_schema(schema: 'dict[str, Any] | None') -> 'dict[str, Any]'
```

*(no docstring)*


*Source: `sdk/a2a-pack/a2a_pack/cli/local.py`*