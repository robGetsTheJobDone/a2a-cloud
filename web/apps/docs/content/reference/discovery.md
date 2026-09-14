# `a2a_pack.discovery`

Agent discovery surface available via ``ctx.discover``.

Agents find each other by capability/tag/skill — *never* by hardcoded URL.
The runtime attaches a :class:`DiscoveryClient`; the canonical impl
queries the platform's agent registry (control plane).

## `ControlPlaneDiscovery`  *(class)*

```python
ControlPlaneDiscovery(api_url: 'str', *, token: 'str | None' = None, timeout: 'float' = 10.0) -> 'None'
```

Hits the platform's agent registry (control plane).

### `__init__`  *(method)*

```python
__init__(self, api_url: 'str', *, token: 'str | None' = None, timeout: 'float' = 10.0) -> 'None'
```

*(no docstring)*

### `find_agents`  *(method)*

```python
find_agents(self, *, tags: 'Sequence[str]' = (), capability: 'str | None' = None, skill: 'str | None' = None, limit: 'int' = 10) -> 'list[DiscoveredAgent]'
```

*(no docstring)*

### `get_agent`  *(method)*

```python
get_agent(self, name: 'str') -> 'DiscoveredAgent'
```

*(no docstring)*

## `DiscoveredAgent`  *(class)*

```python
DiscoveredAgent(name: 'str', url: 'str | None', card: 'AgentCard') -> None
```

A registry hit. ``url`` is what the caller hands to ``ctx.call``.

### `__init__`  *(method)*

```python
__init__(self, name: 'str', url: 'str | None', card: 'AgentCard') -> None
```

*(no docstring)*

## `DiscoveryClient`  *(class)*

```python
DiscoveryClient()
```

Discovery surface.

### `find_agents`  *(method)*

```python
find_agents(self, *, tags: 'Sequence[str]' = (), capability: 'str | None' = None, skill: 'str | None' = None, limit: 'int' = 10) -> 'list[DiscoveredAgent]'
```

*(no docstring)*

### `get_agent`  *(method)*

```python
get_agent(self, name: 'str') -> 'DiscoveredAgent'
```

*(no docstring)*

## `InMemoryDiscovery`  *(class)*

```python
InMemoryDiscovery(agents: 'dict[str, DiscoveredAgent]') -> 'None'
```

*(no docstring)*

### `__init__`  *(method)*

```python
__init__(self, agents: 'dict[str, DiscoveredAgent]') -> 'None'
```

*(no docstring)*

### `find_agents`  *(method)*

```python
find_agents(self, *, tags: 'Sequence[str]' = (), capability: 'str | None' = None, skill: 'str | None' = None, limit: 'int' = 10) -> 'list[DiscoveredAgent]'
```

*(no docstring)*

### `get_agent`  *(method)*

```python
get_agent(self, name: 'str') -> 'DiscoveredAgent'
```

*(no docstring)*


*Source: `sdk/a2a-pack/a2a_pack/discovery.py`*