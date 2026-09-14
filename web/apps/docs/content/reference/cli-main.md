# `a2a_pack.cli.main`

``a2a`` CLI: scaffold, validate, build, deploy.

## `ListingDeclarationError`  *(class)*



`expose.public` is present in a2a.yaml but is not a boolean.

## `auth_api_key`  *(function)*

```python
auth_api_key(agent: 'str', value: 'str', name: 'str', location: 'str', scheme_name: 'str | None', api: 'str | None') -> 'None'
```

Connect API-key auth for an imported agent.

## `auth_bearer`  *(function)*

```python
auth_bearer(agent: 'str', token: 'str', scheme_name: 'str | None', scheme: 'str', api: 'str | None') -> 'None'
```

Connect HTTP/Bearer auth for an imported agent.

## `auth_delete`  *(function)*

```python
auth_delete(agent: 'str', connection_id: 'int', api: 'str | None') -> 'None'
```

Remove an imported-agent auth connection.

## `auth_mtls`  *(function)*

```python
auth_mtls(agent: 'str', cert: 'Path', key: 'Path', ca: 'Path | None', scheme_name: 'str | None', api: 'str | None') -> 'None'
```

Connect mTLS client certificate auth for an imported agent.

## `auth_oauth_token`  *(function)*

```python
auth_oauth_token(agent: 'str', access_token: 'str', scheme_type: 'str', scheme_name: 'str | None', refresh_token: 'str | None', token_url: 'str | None', client_id: 'str | None', client_secret: 'str | None', expires_in: 'int | None', api: 'str | None') -> 'None'
```

Connect OAuth/OIDC with an existing access token and optional refresh config.

## `auth_status`  *(function)*

```python
auth_status(agent: 'str', api: 'str | None') -> 'None'
```

Show detected auth requirements and configured connections.

## `build`  *(function)*

```python
build(project: 'Path', registry: 'str | None', push: 'bool') -> 'None'
```

Build (and optionally push) the container image for the agent.

## `build_frontend`  *(function)*

```python
build_frontend(project: 'Path') -> 'None'
```

Build the packed frontend declared in a2a.yaml.

## `card`  *(function)*

```python
card(project: 'Path') -> 'None'
```

Print the Agent Card JSON for the project's agent.

## `chat`  *(function)*

```python
chat(project: 'Path', host: 'str', port: 'int', reload: 'bool', env_file: 'Path', workspace: 'Path | None', build_image: 'bool', up: 'bool', detach: 'bool', down: 'bool', volumes: 'bool', print_compose: 'bool', base_image: 'str') -> 'None'
```

Run one agent plus declared local Qdrant/Postgres resources.

## `compile_dsl`  *(function)*

```python
compile_dsl(project: 'Path', out: 'Path | None', stdout: 'bool') -> 'None'
```

Compile the project declaration to the sidecar Agent DSL.

## `deploy`  *(function)*

```python
deploy(project_path: "Annotated[Path | None, typer.Argument(metavar='PROJECT', help='Project directory containing a2a.yaml')]" = None, project: 'Path', public: 'bool | None', wait: 'bool', api: 'str | None') -> 'None'
```

Ship the agent.

Tarballs your source, uploads to the control plane, and prints the
URL when it's live. No local docker. No git. No knowledge of how the
platform builds or deploys.

## `dev`  *(function)*

```python
dev(project: 'Path', host: 'str', port: 'int', reload: 'bool', env_file: 'Path', workspace: 'Path | None', docker: 'bool', local: 'bool', agent: 'str | None', api: 'str | None') -> 'None'
```

Edit locally, run in the cloud: sync this project to the agent's scale-to-zero
dev box, hot-reload it there, and serve it on a public URL. Use --local to run
on this machine instead.

## `frontend_info`  *(function)*

```python
frontend_info(project: 'Path') -> 'None'
```

Print the packed frontend config resolved from a2a.yaml.

## `generate_openapi`  *(function)*

```python
generate_openapi(url: 'str', name: 'str | None', description: 'str | None', base_url: 'str | None', public: 'bool | None', api: 'str | None') -> 'None'
```

Generate an editable A2APack source repo and start deployment.

## `import_agent`  *(function)*

```python
import_agent(url: 'str', name: 'str | None', public: 'bool', auth_bearer: 'str | None', auth_api_key: 'str | None', auth_name: 'str | None', auth_location: 'str', auth_scheme: 'str', api: 'str | None') -> 'None'
```

Import an already-running A2A agent into the registry.

## `init`  *(function)*

```python
init(name: 'str', description: 'str', target: 'Path', language: 'str', frontend: 'str | None', frontend_mode: 'str', auth: 'str') -> 'None'
```

Scaffold a new agent project.

## `list_agents`  *(function)*

```python
list_agents(api: 'str | None') -> 'None'
```

List agents visible to the current user.

## `local_cleanup`  *(function)*

```python
local_cleanup(name: 'str', api: 'str | None', token: 'str | None', token_email: 'str', docker_token: 'bool', wait_control_plane: 'bool', ignore_missing: 'bool', json_output: 'bool') -> 'None'
```

Delete a local devcontainer agent and managed local resources.

## `local_deploy`  *(function)*

```python
local_deploy(project_path: "Annotated[Path | None, typer.Argument(metavar='PROJECT', help='Project directory containing a2a.yaml')]" = None, project: 'Path', public: 'bool | None', api: 'str | None', token: 'str | None', token_email: 'str', docker_token: 'bool', wait_control_plane: 'bool', wait_agent: 'bool', json_output: 'bool') -> 'None'
```

Deploy an agent to the local devcontainer control plane.

This is the non-interactive local harness for tests and generated agents:
it mints an e2e bearer token from the local control-plane container when
no token env var is present, then uses the same tarball upload path as
`a2a deploy`.

## `login`  *(function)*

```python
login(api: 'str', issuer: 'str', client_id: 'str', scope: 'str', port: 'int', token: 'str | None', open_browser: 'bool') -> 'None'
```

Authenticate with Keycloak and cache OAuth tokens.

## `logout`  *(function)*

```python
logout() -> 'None'
```

Forget the cached JWT.

## `logs`  *(function)*

```python
logs(agent: 'str | None', deploy: 'str | None', follow: 'bool', tail: 'int', project: 'Path', api: 'str | None') -> 'None'
```

Read the build and runtime logs behind a deploy.

This is what to run when `a2a deploy` says a build failed: it prints the
same Gitea Actions / pod / Argo output the dashboard's deployment timeline
shows, for the deployment you name or the most recent one.

## `mcp_url`  *(function)*

```python
mcp_url(name: 'str | None', project: 'Path', api: 'str | None') -> 'None'
```

Print the MCP connect config for a deployed agent.

Every shipped agent auto-exposes ``POST /mcp`` (Streamable HTTP).
This command prints the JSON snippet you paste into Claude Code,
Cursor, or any other MCP client to wire up the agent.

## `openapi_client`  *(function)*

```python
openapi_client(project: 'Path', out: 'Path', base_url: 'str | None', skill: 'list[str] | None', require_bearer: 'bool | None', spec_out: 'Path | None') -> 'None'
```

Generate a TypeScript client for this agent's tool APIs.

## `openapi_spec`  *(function)*

```python
openapi_spec(project: 'Path', out: 'Path | None', base_url: 'str | None', require_bearer: 'bool | None') -> 'None'
```

Generate an OpenAPI spec for this agent's direct tool APIs.

## `preview_openapi`  *(function)*

```python
preview_openapi(url: 'str', name: 'str | None', description: 'str | None', base_url: 'str | None', public: 'bool | None', api: 'str | None') -> 'None'
```

Preview generated tools, setup fields, and source files.

## `run`  *(function)*

```python
run(entrypoint: 'str', host: 'str', port: 'int', project: 'Path') -> 'None'
```

Run the agent's HTTP server locally (used inside the container too).

## `sidecar`  *(function)*

```python
sidecar(dsl: 'Path', worker_url: 'str', host: 'str', port: 'int') -> 'None'
```

Run the common sidecar runtime for a compiled Agent DSL.

## `signup`  *(function)*

```python
signup(api: 'str', issuer: 'str', client_id: 'str', scope: 'str', port: 'int', token: 'str | None', open_browser: 'bool') -> 'None'
```

Create or sign into a Keycloak account and cache OAuth tokens.

## `single_skill_notice_markdown`  *(function)*

```python
single_skill_notice_markdown() -> 'str'
```

The disclosure as Markdown, for READMEs and generated docs pages.

## `single_skill_notice_text`  *(function)*

```python
single_skill_notice_text() -> 'str'
```

The same disclosure as plain terminal text (no Markdown ticks).

Deliberately *unwrapped*: Rich owns the wrapping so the panel reflows to the
real console width. Pre-wrapping here at a fixed column left orphaned
fragments on their own lines under 80 columns, because Rich re-wraps the
already-wrapped lines.

## `ssh`  *(function)*

```python
ssh(agent: "Annotated[str | None, typer.Argument(help='Agent name (defaults to a2a.yaml in the current repo)')]" = None, print_only: "Annotated[bool, typer.Option('--print', help='Print connection info instead of opening a shell')]" = False, tunnel: 'Annotated[bool, typer.Option(\'--tunnel\', help="Serve the dev box on a local TCP port for SSH tools that can\'t use ~/.ssh/config (GUIs, IDEs, sftp clients)")]' = False, port: "Annotated[int, typer.Option('--port', help='Local port for --tunnel (0 = pick a free one)')]" = 0, json_output: "Annotated[bool, typer.Option('--json', help='Print connection details as JSON (implies --print)')]" = False, api: "Annotated[str | None, typer.Option('--api', hidden=True)]" = None) -> 'None'
```

Open a shell in a throwaway dev box for AGENT.

The box has node, python, a2a-pack, and the agent repo already loaded, and
scales to zero when you disconnect. Also usable from VS Code Remote-SSH,
Cursor, scp, and rsync via the host alias it writes to ~/.ssh/config.

## `ssh_proxy`  *(function)*

```python
ssh_proxy(agent: 'Annotated[str, typer.Argument()]', api: "Annotated[str | None, typer.Option('--api', hidden=True)]" = None) -> 'None'
```

Internal: stdio<->wss bridge used as an ssh ProxyCommand.

## `test_agent`  *(function)*

```python
test_agent(project: 'Path', env_file: 'Path', workspace: 'Path | None', invoke: 'bool', skill: 'str | None', args_json: 'str | None') -> 'None'
```

Run local preflight checks before deploying.

## `validate`  *(function)*

```python
validate(project: 'Path') -> 'None'
```

Load the agent and print its Card schema. Exits non-zero on errors.

## `whoami`  *(function)*

```python
whoami() -> 'None'
```

Show the currently logged-in user.


*Source: `sdk/a2a-pack/a2a_pack/cli/main.py`*