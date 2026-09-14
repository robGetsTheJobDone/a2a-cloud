# `a2a_pack.runtime`

Declarative runtime/deployment metadata.

These types describe *how* the platform should run an agent: lifecycle,
state needs, isolation level, resource budget, egress policy. They are
read by the deployer and by the registry; agent code itself should not
depend on which runtime is selected.

## `AccountAccess`  *(class)*

```python
AccountAccess(*, required: bool = False, platform_skill_calls: Annotated[int, Ge(ge=0)] = 0, after_trial: Literal['byok'] = 'byok') -> None
```

Account-gated, platform-funded trial policy for an agent.

When enabled, callers must have an A2A Cloud account. The platform funds
up to ``platform_skill_calls`` invocations for each account and agent;
subsequent invocations use that account's saved LLM credential.

## `AgentComposition`  *(class)*

```python
AgentComposition(*, sub_agents: tuple[a2a_pack.runtime.CompositionSubAgent, ...] = (), planning: Literal['llm_dag', 'deterministic_dag'] = 'llm_dag', max_nodes: Annotated[int, Gt(gt=0)] = 8, max_parallel: Annotated[int, Gt(gt=0)] = 3, max_replans: Annotated[int, Ge(ge=0)] = 1) -> None
```

Declarative composition block from ``a2a.yaml``.

### `public_payload`  *(method)*

```python
public_payload(self) -> 'dict[str, Any]'
```

*(no docstring)*

## `AgentDatabase`  *(class)*

```python
AgentDatabase(*, name: str, engine: Literal['postgres'] = 'postgres', provider: Literal['neon'] = 'neon', scope: Literal['user', 'org'] = 'user', branch: str = 'main', access_mode: Literal['read_only', 'read_write', 'owner'] = 'read_write', env: a2a_pack.runtime.AgentDatabaseEnv = <factory>, migrations: a2a_pack.runtime.AgentDatabaseMigrations | None = None, scale_to_zero: bool = True) -> None
```

Platform-managed Neon/Postgres database declaration.

### `public_payload`  *(method)*

```python
public_payload(self) -> 'dict[str, Any]'
```

*(no docstring)*

## `AgentDatabaseEnv`  *(class)*

```python
AgentDatabaseEnv(*, url: str = 'DATABASE_URL') -> None
```

Environment variables populated with platform-managed DB credentials.

### `public_payload`  *(method)*

```python
public_payload(self) -> 'dict[str, Any]'
```

*(no docstring)*

## `AgentDatabaseMigrations`  *(class)*

```python
AgentDatabaseMigrations(*, path: str | None = None) -> None
```

Optional migrations path for a platform-managed database.

### `public_payload`  *(method)*

```python
public_payload(self) -> 'dict[str, Any]'
```

*(no docstring)*

## `AgentEndpoint`  *(class)*

```python
AgentEndpoint(*, name: str | None = None, path: str, methods: tuple[typing.Literal['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'], ...] = ('POST',), skill: str, body_arg: str = 'body', headers_arg: str | None = None, query_arg: str | None = None) -> None
```

Raw HTTP endpoint adapter exposed by the runtime.

The endpoint path is not a skill URL. The runtime accepts the provider's
native HTTP request, maps it into handler arguments, and dispatches to the
named skill through the normal agent execution path.

## `AgentGoal`  *(class)*

```python
AgentGoal(*, objective: str = '', success_criteria: tuple[str, ...] = (), constraints: tuple[str, ...] = ()) -> None
```

Durable objective advertised by a composable/meta agent.

### `public_payload`  *(method)*

```python
public_payload(self) -> 'dict[str, Any]'
```

*(no docstring)*

## `AgentMemory`  *(class)*

```python
AgentMemory(*, tiers: tuple[typing.Literal['files', 'kv', 'vector'], ...] = (), namespace: str | None = None, scope: Literal['agent', 'user', 'thread'] = 'agent', retention: Literal['ephemeral', 'durable'] = 'durable') -> None
```

Long-term memory declaration for composable/meta agents.

### `public_payload`  *(method)*

```python
public_payload(self) -> 'dict[str, Any]'
```

*(no docstring)*

## `AgentPlatformResources`  *(class)*

```python
AgentPlatformResources(*, memory: a2a_pack.runtime.AgentMemory | None = None, databases: tuple[a2a_pack.runtime.AgentDatabase, ...] = (), mailbox: bool | dict[str, typing.Any] | None = None) -> None
```

Platform-managed resources declared in ``a2a.yaml``.

### `from_mapping`  *(method)*

```python
from_mapping(data: 'Mapping[str, Any] | None') -> "'AgentPlatformResources'"
```

*(no docstring)*

### `public_payload`  *(method)*

```python
public_payload(self) -> 'dict[str, Any]'
```

*(no docstring)*

## `AgentRuntime`  *(class)*

```python
AgentRuntime(*, lifecycle: a2a_pack.runtime.Lifecycle = <Lifecycle.EPHEMERAL: 'ephemeral'>, availability: a2a_pack.runtime.RuntimeAvailability = <RuntimeAvailability.ON_DEMAND: 'on_demand'>, state: a2a_pack.runtime.State = <State.NONE: 'none'>, sandbox: a2a_pack.runtime.Sandbox = <Sandbox.MICROSANDBOX: 'microsandbox'>, resources: a2a_pack.runtime.Resources = <factory>, concurrency: Annotated[int, Gt(gt=0)] = 1, egress: a2a_pack.runtime.EgressPolicy = <factory>, tools_used: tuple[str, ...] = (), llm_provisioning: a2a_pack.runtime.LLMProvisioning = <LLMProvisioning.PLATFORM: 'platform'>, account_access: a2a_pack.runtime.AccountAccess = <factory>, wants_cp_jwt: bool = False, platform_resources: a2a_pack.runtime.AgentPlatformResources = <factory>, endpoints: tuple[a2a_pack.runtime.AgentEndpoint, ...] = (), apt_packages: tuple[str, ...] = ()) -> None
```

Aggregate runtime declaration; published on the Agent Card.

## `CompositionSubAgent`  *(class)*

```python
CompositionSubAgent(*, name: str | None = None, tag: str | None = None, version: str | None = None, skills: tuple[str, ...] = (), default_args: dict[str, typing.Any] = <factory>, required: bool = True) -> None
```

One callable dependency declared by a composable/meta agent.

### `public_payload`  *(method)*

```python
public_payload(self) -> 'dict[str, Any]'
```

*(no docstring)*

## `EgressPolicy`  *(class)*

```python
EgressPolicy(*, allow_hosts: tuple[str, ...] = (), allow_internal_services: tuple[str, ...] = (), deny_internet_by_default: bool = True) -> None
```

What external hosts the agent is allowed to talk to.

## `LLMProvisioning`  *(class)*

```python
LLMProvisioning(*values)
```

Where the agent's LLM credentials come from.

Drives both runtime behaviour (``ctx.llm`` resolution) and what the
marketplace can charge the caller.

## `Lifecycle`  *(class)*

```python
Lifecycle(*values)
```

How long an instance of the agent process lives.

## `MetaAgentManifest`  *(class)*

```python
MetaAgentManifest(*, composition: a2a_pack.runtime.AgentComposition | None = None, goal: a2a_pack.runtime.AgentGoal | None = None, memory: a2a_pack.runtime.AgentMemory | None = None) -> None
```

Typed meta-agent contract parsed from ``a2a.yaml`` or class attrs.

### `from_mapping`  *(method)*

```python
from_mapping(data: 'Mapping[str, Any] | None') -> "'MetaAgentManifest'"
```

*(no docstring)*

### `public_payload`  *(method)*

```python
public_payload(self) -> 'dict[str, Any]'
```

*(no docstring)*

## `Resources`  *(class)*

```python
Resources(*, cpu: str = '100m', memory: str = '256Mi', gpu: Annotated[int, Ge(ge=0)] = 0, max_runtime_seconds: Annotated[int, Gt(gt=0)] = 600) -> None
```

Resource budget hint for the deployer.

## `RuntimeAvailability`  *(class)*

```python
RuntimeAvailability(*values)
```

Developer-facing availability intent for hosted deployments.

## `Sandbox`  *(class)*

```python
Sandbox(*values)
```

Isolation level. The platform always runs agents under microsandbox.

Modeled as an enum (rather than a constant) so the wire format stays
stable if more isolation tiers are added later, but only one value is
currently valid: every agent runs in a microvm-class sandbox.

## `SelfHealingPolicy`  *(class)*

```python
SelfHealingPolicy(*, enabled: bool = True, consecutive_failures: Annotated[int, Ge(ge=1), Le(le=10)] = 1, window_seconds: Annotated[int, Ge(ge=30), Le(le=3600)] = 300, cooldown_seconds: Annotated[int, Ge(ge=60), Le(le=86400)] = 900, max_repairs_per_day: Annotated[int, Ge(ge=1), Le(le=20)] = 3, max_turns: Annotated[int, Ge(ge=1), Le(le=100)] = 30, deployment_timeout_seconds: Annotated[int, Ge(ge=60), Le(le=7200)] = 1800, require_tests: bool = True) -> None
```

Bounded, explicit source self-repair policy from ``a2a.yaml``.

This is deliberately an opt-in deployment capability rather than an
implicit runtime behavior.  The control plane applies the limits; the SDK
validates and advertises the public, non-secret policy on the Agent Card.

### `public_payload`  *(method)*

```python
public_payload(self) -> 'dict[str, Any]'
```

*(no docstring)*

## `SkillPolicy`  *(class)*

```python
SkillPolicy(*, timeout_seconds: float | None = None, idempotent: bool = False, max_retries: Annotated[int, Ge(ge=0)] = 0, cost_class: str | None = None, allow_scope_expansion: bool = False, grant_mode: str | None = None, grant_allow_patterns: tuple[str, ...] = (), grant_deny_patterns: tuple[str, ...] = (), grant_outputs_prefix: str | None = None, grant_write_prefixes: tuple[str, ...] = (), grant_ttl_seconds: Optional[Annotated[int, Gt(gt=0)]] = None, grant_run_timeout_seconds: Optional[Annotated[int, Gt(gt=0)]] = None, grant_approval_timeout_seconds: Optional[Annotated[int, Gt(gt=0)]] = None, grant_scope_approval_timeout_seconds: Optional[Annotated[int, Gt(gt=0)]] = None) -> None
```

Per-skill operational policy advertised on the Agent Card.

## `State`  *(class)*

```python
State(*values)
```

What kind of state the agent retains between invocations.

## `TemplateLineage`  *(class)*

```python
TemplateLineage(*, schema_version: str = '2026-06-02', template_ref: str | None = None, template_version: str | None = None, template_digest: str | None = None, source_agent: str | None = None, source_agent_version: str | None = None, source_repo_url: str | None = None, source_revision: str | None = None, instance_id: str | None = None, instance_version: str | None = None, update_policy: Literal['none', 'notify', 'propose', 'auto_patch'] = 'none', update_channel: str | None = None, migration_skill: str | None = None) -> None
```

Opt-in source-template lineage and update policy.

Agents generated from templates can publish this on their Agent Card so
platforms can detect available template updates and decide how to handle
them. The policy is intentionally advisory: callers/platforms remain the
authority for whether an update is proposed, applied, reviewed, or denied.

### `from_mapping`  *(method)*

```python
from_mapping(data: 'Mapping[str, Any] | str | None') -> "'TemplateLineage'"
```

*(no docstring)*

### `none`  *(method)*

```python
none() -> "'TemplateLineage'"
```

*(no docstring)*

## `apply_project_manifest`  *(function)*

```python
apply_project_manifest(agent_cls: 'type[Any]', config: 'Mapping[str, Any] | None') -> 'MetaAgentManifest'
```

Attach parsed ``a2a.yaml`` platform metadata to an agent class.

``a2a run`` receives only an entrypoint, but the working directory still
contains ``a2a.yaml``. Applying parsed metadata here makes local and
deployed Agent Cards reflect declarative composition/lineage without
importing control-plane code.


*Source: `sdk/a2a-pack/a2a_pack/runtime.py`*