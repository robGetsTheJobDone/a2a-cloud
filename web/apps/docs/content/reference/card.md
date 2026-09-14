# `a2a_pack.card`



## `AgentCard`  *(class)*

```python
AgentCard(*, name: str, description: str, version: str, skills: list[a2a_pack.card.SkillCard], capabilities: dict[str, typing.Any] = <factory>, input_modes: list[str] = <factory>, output_modes: list[str] = <factory>, required_secrets: list[str] = <factory>, required_env: list[str] = <factory>, consumer_setup: a2a_pack.consumer_setup.ConsumerSetup = <factory>, runtime: a2a_pack.runtime.AgentRuntime = <factory>, template_lineage: a2a_pack.runtime.TemplateLineage | None = None, state_schema: dict[str, typing.Any] | None = None, workspace_access: a2a_pack.workspace.WorkspaceAccess = <factory>, mcp_endpoint: str = '/mcp', connector_mcp_endpoint: str = '/connector-mcp', mcp_endpoints: dict[str, typing.Any] = <factory>) -> None
```

Public description of an agent.

Mirrors the A2A Agent Card spec: identity, capabilities, IO modes, and
the catalog of skills the agent advertises.

### `from_agent`  *(method)*

```python
from_agent(agent: "'A2AAgent'") -> "'AgentCard'"
```

*(no docstring)*

## `SkillCard`  *(class)*

```python
SkillCard(*, id: str, name: str, description: str, tags: list[str] = <factory>, scopes: list[str] = <factory>, stream: bool = False, policy: a2a_pack.runtime.SkillPolicy = <factory>, input_schema: dict[str, typing.Any], output_schema: dict[str, typing.Any]) -> None
```

Public description of a single skill, shaped for the A2A spec.


*Source: `sdk/a2a-pack/a2a_pack/card.py`*