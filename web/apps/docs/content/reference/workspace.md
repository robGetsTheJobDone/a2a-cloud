# `a2a_pack.workspace`

Workspace capability negotiation.

Agents never receive a filesystem path. They negotiate a *view* by intent::

    view = await ctx.workspace.open_view(
        purpose="Fix failing payment test",
        hints=["payment", "checkout"],
        file_types=["python"],
        max_files=10,
        mode=WorkspaceMode.READ_WRITE_OVERLAY,
    )
    for path in view.files:
        content = await view.read(path)

The runtime resolves the request (semantic search + dependency graph + git
metadata + policy + optional human approval) and returns a bounded grant.
Writes are staged as :class:`WorkspacePatch` objects, never applied directly
to the host filesystem from inside the sandbox.

## `ControlPlaneWorkspaceClient`  *(class)*

```python
ControlPlaneWorkspaceClient(*, bucket: 'str', control_plane_url: 'str', grant_token: 'str', access: 'WorkspaceAccess', issuer: 'str' = 'control-plane-runtime') -> 'None'
```

Workspace client backed by grant-scoped control-plane file routes.

### `__init__`  *(method)*

```python
__init__(self, *, bucket: 'str', control_plane_url: 'str', grant_token: 'str', access: 'WorkspaceAccess', issuer: 'str' = 'control-plane-runtime') -> 'None'
```

*(no docstring)*

### `delete_path`  *(method)*

```python
delete_path(self, path: 'str') -> 'None'
```

*(no docstring)*

### `exists`  *(method)*

```python
exists(self, path: 'str') -> 'bool'
```

*(no docstring)*

### `install_grant`  *(method)*

```python
install_grant(self, grant: "'Grant'") -> 'None'
```

*(no docstring)*

### `install_grant_token`  *(method)*

```python
install_grant_token(self, grant: "'Grant'", token: 'str') -> 'None'
```

*(no docstring)*

### `is_writable_output`  *(method)*

```python
is_writable_output(self, path: 'str') -> 'bool'
```

*(no docstring)*

### `iter_paths`  *(method)*

```python
iter_paths(self) -> 'Iterable[str]'
```

*(no docstring)*

### `list_grants`  *(method)*

```python
list_grants(self) -> 'list[WorkspaceGrant]'
```

*(no docstring)*

### `open_view`  *(method)*

```python
open_view(self, *, purpose: 'str', hints: 'Sequence[str]' = (), file_types: 'Sequence[FileType]' = (), max_files: 'int' = 10, mode: 'WorkspaceMode' = <WorkspaceMode.READ_ONLY: 'read_only'>, reason: 'str | None' = None) -> 'WorkspaceView'
```

*(no docstring)*

### `read_bytes`  *(method)*

```python
read_bytes(self, path: 'str') -> 'bytes'
```

*(no docstring)*

### `request_access`  *(method)*

```python
request_access(self, *, files: 'Sequence[FileMatch | str]', mode: 'WorkspaceMode', reason: 'str', purpose: 'str' = '') -> 'WorkspaceGrant'
```

*(no docstring)*

### `search`  *(method)*

```python
search(self, *, query: 'str', types: 'Sequence[FileType]' = (), limit: 'int' = 20) -> 'list[FileMatch]'
```

*(no docstring)*

### `write_bytes`  *(method)*

```python
write_bytes(self, path: 'str', content: 'bytes') -> 'None'
```

*(no docstring)*

## `FileMatch`  *(class)*

```python
FileMatch(*, path: str, file_type: a2a_pack.workspace.FileType, score: float = 0.0, summary: str | None = None, size_bytes: Annotated[int, Ge(ge=0)] = 0) -> None
```

Result row from :meth:`WorkspaceClient.search`.

## `FileSystemWorkspaceClient`  *(class)*

```python
FileSystemWorkspaceClient(root: 'Path | str', *, access: 'WorkspaceAccess', bucket: 'str' = 'local', issuer: 'str' = 'local-dev', allow_patterns: 'Sequence[str]' = ('**',), outputs_prefix: 'str | None' = 'outputs', write_prefixes: 'Sequence[str]' = (), mode: 'WorkspaceMode' = <WorkspaceMode.READ_WRITE_OVERLAY: 'read_write_overlay'>) -> 'None'
```

Local workspace backed by a directory on disk.

Used by ``a2a dev`` / ``a2a test`` so framework file tools and direct
``ctx.workspace`` writes are visible on the developer's machine before
the agent is uploaded.

### `__init__`  *(method)*

```python
__init__(self, root: 'Path | str', *, access: 'WorkspaceAccess', bucket: 'str' = 'local', issuer: 'str' = 'local-dev', allow_patterns: 'Sequence[str]' = ('**',), outputs_prefix: 'str | None' = 'outputs', write_prefixes: 'Sequence[str]' = (), mode: 'WorkspaceMode' = <WorkspaceMode.READ_WRITE_OVERLAY: 'read_write_overlay'>) -> 'None'
```

*(no docstring)*

### `delete_path`  *(method)*

```python
delete_path(self, path: 'str') -> 'None'
```

*(no docstring)*

### `write_bytes`  *(method)*

```python
write_bytes(self, path: 'str', content: 'bytes') -> 'None'
```

*(no docstring)*

## `FileType`  *(class)*

```python
FileType(*values)
```

*(no docstring)*

## `FileUpload`  *(class)*

```python
FileUpload(*, accept: 'Sequence[str]' = (), max_bytes: 'int | None' = None, multiple: 'bool' = False, description: 'str | None' = None) -> 'None'
```

Schema marker for a required or optional file upload parameter.

Use with ``typing.Annotated``::

    document: Annotated[UploadedFile, FileUpload(accept=["application/pdf"])]

The marker is carried as ``x-a2a-file-upload`` in the skill input schema so
OpenAPI exporters can render the field as ``multipart/form-data`` while the
runtime still invokes the skill with JSON-safe ``UploadedFile`` metadata.

### `__init__`  *(method)*

```python
__init__(self, *, accept: 'Sequence[str]' = (), max_bytes: 'int | None' = None, multiple: 'bool' = False, description: 'str | None' = None) -> 'None'
```

*(no docstring)*

### `schema_extra`  *(method)*

```python
schema_extra(self) -> 'dict[str, Any]'
```

*(no docstring)*

## `LocalWorkspaceClient`  *(class)*

```python
LocalWorkspaceClient(files: 'dict[str, bytes]', *, access: 'WorkspaceAccess', bucket: 'str' = 'local', issuer: 'str' = 'local') -> 'None'
```

In-memory workspace for local dev and tests.

Search is naive substring match; ranking is keyword-overlap. Real
runtime implementations replace this with embeddings/dep-graph search.
Policy enforcement (:class:`WorkspaceAccess`) IS applied here so tests
cover the rejection paths.

### `__init__`  *(method)*

```python
__init__(self, files: 'dict[str, bytes]', *, access: 'WorkspaceAccess', bucket: 'str' = 'local', issuer: 'str' = 'local') -> 'None'
```

*(no docstring)*

### `delete_path`  *(method)*

```python
delete_path(self, path: 'str') -> 'None'
```

*(no docstring)*

### `exists`  *(method)*

```python
exists(self, path: 'str') -> 'bool'
```

*(no docstring)*

### `install_grant`  *(method)*

```python
install_grant(self, grant: "'Grant'") -> 'None'
```

*(no docstring)*

### `is_writable_output`  *(method)*

```python
is_writable_output(self, path: 'str') -> 'bool'
```

*(no docstring)*

### `iter_paths`  *(method)*

```python
iter_paths(self) -> 'Iterable[str]'
```

*(no docstring)*

### `list_grants`  *(method)*

```python
list_grants(self) -> 'list[WorkspaceGrant]'
```

*(no docstring)*

### `open_view`  *(method)*

```python
open_view(self, *, purpose: 'str', hints: 'Sequence[str]' = (), file_types: 'Sequence[FileType]' = (), max_files: 'int' = 10, mode: 'WorkspaceMode' = <WorkspaceMode.READ_ONLY: 'read_only'>, reason: 'str | None' = None) -> 'WorkspaceView'
```

*(no docstring)*

### `read_bytes`  *(method)*

```python
read_bytes(self, path: 'str') -> 'bytes'
```

*(no docstring)*

### `request_access`  *(method)*

```python
request_access(self, *, files: 'Sequence[FileMatch | str]', mode: 'WorkspaceMode', reason: 'str', purpose: 'str' = '') -> 'WorkspaceGrant'
```

*(no docstring)*

### `search`  *(method)*

```python
search(self, *, query: 'str', types: 'Sequence[FileType]' = (), limit: 'int' = 20) -> 'list[FileMatch]'
```

*(no docstring)*

### `write_bytes`  *(method)*

```python
write_bytes(self, path: 'str', content: 'bytes') -> 'None'
```

*(no docstring)*

## `LocalWorkspaceView`  *(class)*

```python
LocalWorkspaceView(grant: 'WorkspaceGrant', client: "'LocalWorkspaceClient'") -> 'None'
```

*(no docstring)*

### `__init__`  *(method)*

```python
__init__(self, grant: 'WorkspaceGrant', client: "'LocalWorkspaceClient'") -> 'None'
```

*(no docstring)*

### `delete`  *(method)*

```python
delete(self, path: 'str') -> 'WorkspacePatch'
```

*(no docstring)*

### `patches`  *(method)*

```python
patches(self) -> 'tuple[WorkspacePatch, ...]'
```

*(no docstring)*

### `read`  *(method)*

```python
read(self, path: 'str') -> 'bytes'
```

*(no docstring)*

### `write`  *(method)*

```python
write(self, path: 'str', content: 'bytes') -> 'WorkspacePatch'
```

*(no docstring)*

## `MinIOWorkspaceClient`  *(class)*

```python
MinIOWorkspaceClient(*, bucket: 'str', endpoint_url: 'str', access_key_id: 'str', secret_access_key: 'str', access: 'WorkspaceAccess', issuer: 'str' = 'minio-runtime') -> 'None'
```

Workspace client backed by the user's MinIO/S3 bucket.

The control plane mints a grant with bucket + path policy during handoff.
Deployed agents use this client so ``ctx.workspace`` can read the caller's
actual files and persist writes back to the same bucket.

### `__init__`  *(method)*

```python
__init__(self, *, bucket: 'str', endpoint_url: 'str', access_key_id: 'str', secret_access_key: 'str', access: 'WorkspaceAccess', issuer: 'str' = 'minio-runtime') -> 'None'
```

*(no docstring)*

### `delete_path`  *(method)*

```python
delete_path(self, path: 'str') -> 'None'
```

*(no docstring)*

### `exists`  *(method)*

```python
exists(self, path: 'str') -> 'bool'
```

*(no docstring)*

### `install_grant`  *(method)*

```python
install_grant(self, grant: "'Grant'") -> 'None'
```

*(no docstring)*

### `is_writable_output`  *(method)*

```python
is_writable_output(self, path: 'str') -> 'bool'
```

*(no docstring)*

### `iter_paths`  *(method)*

```python
iter_paths(self) -> 'Iterable[str]'
```

*(no docstring)*

### `list_grants`  *(method)*

```python
list_grants(self) -> 'list[WorkspaceGrant]'
```

*(no docstring)*

### `open_view`  *(method)*

```python
open_view(self, *, purpose: 'str', hints: 'Sequence[str]' = (), file_types: 'Sequence[FileType]' = (), max_files: 'int' = 10, mode: 'WorkspaceMode' = <WorkspaceMode.READ_ONLY: 'read_only'>, reason: 'str | None' = None) -> 'WorkspaceView'
```

*(no docstring)*

### `read_bytes`  *(method)*

```python
read_bytes(self, path: 'str') -> 'bytes'
```

*(no docstring)*

### `request_access`  *(method)*

```python
request_access(self, *, files: 'Sequence[FileMatch | str]', mode: 'WorkspaceMode', reason: 'str', purpose: 'str' = '') -> 'WorkspaceGrant'
```

*(no docstring)*

### `search`  *(method)*

```python
search(self, *, query: 'str', types: 'Sequence[FileType]' = (), limit: 'int' = 20) -> 'list[FileMatch]'
```

*(no docstring)*

### `write_bytes`  *(method)*

```python
write_bytes(self, path: 'str', content: 'bytes') -> 'None'
```

*(no docstring)*

## `MinIOWorkspaceView`  *(class)*

```python
MinIOWorkspaceView(grant: 'WorkspaceGrant', client: "'MinIOWorkspaceClient'") -> 'None'
```

*(no docstring)*

### `__init__`  *(method)*

```python
__init__(self, grant: 'WorkspaceGrant', client: "'MinIOWorkspaceClient'") -> 'None'
```

*(no docstring)*

### `delete`  *(method)*

```python
delete(self, path: 'str') -> 'WorkspacePatch'
```

*(no docstring)*

### `patches`  *(method)*

```python
patches(self) -> 'tuple[WorkspacePatch, ...]'
```

*(no docstring)*

### `read`  *(method)*

```python
read(self, path: 'str') -> 'bytes'
```

*(no docstring)*

### `write`  *(method)*

```python
write(self, path: 'str', content: 'bytes') -> 'WorkspacePatch'
```

*(no docstring)*

## `UploadedFile`  *(class)*

```python
UploadedFile(*, path: str, filename: str, media_type: str, size_bytes: Annotated[int, Ge(ge=0)] = 0) -> None
```

Workspace-backed file staged for a skill invocation.

Agent API multipart uploads are persisted into the caller workspace before
the skill runs. The handler receives this metadata object and reads bytes
through ``ctx.workspace`` using ``path``.

## `WorkspaceAccess`  *(class)*

```python
WorkspaceAccess(*, enabled: bool = False, max_files: Annotated[int, Ge(ge=0)] = 0, allowed_modes: tuple[a2a_pack.workspace.WorkspaceMode, ...] = (), require_reason: bool = True, deny_patterns: tuple[str, ...] = (), require_human_approval: bool = False, max_total_size_bytes: Annotated[int, Gt(gt=0)] = 104857600) -> None
```

Class-level workspace policy.

Use :meth:`none` for agents that do not touch any workspace, or
:meth:`dynamic` to allow capability negotiation under bounds.

### `dynamic`  *(method)*

```python
dynamic(*, max_files: 'int' = 25, allowed_modes: 'Sequence[WorkspaceMode]' = (<WorkspaceMode.READ_ONLY: 'read_only'>,), require_reason: 'bool' = True, deny_patterns: 'Sequence[str]' = (), require_human_approval: 'bool' = False, max_total_size_bytes: 'int' = 104857600) -> "'WorkspaceAccess'"
```

*(no docstring)*

### `none`  *(method)*

```python
none() -> "'WorkspaceAccess'"
```

*(no docstring)*

## `WorkspaceClient`  *(class)*

```python
WorkspaceClient()
```

Negotiation surface handed to the agent via ``ctx.workspace``.

The concrete implementation is provided by the runtime; agents must
program against this interface only.

### `delegate`  *(method)*

```python
delegate(self, *, audience: 'str', allow_patterns: 'Sequence[str]' = ('**',), deny_patterns: 'Sequence[str]' = (), mode: 'WorkspaceMode' = <WorkspaceMode.READ_ONLY: 'read_only'>, outputs_prefix: 'str | None' = None, write_prefixes: 'Sequence[str]' = (), source_grants: 'Sequence[Any]' = (), ttl_seconds: 'int' = 300) -> 'str'
```

Mint a signed grant token the caller can hand to ``ctx.call``.

The default implementation requires the workspace to expose
``self.bucket`` and ``self.issuer`` — override in concrete clients
that don't fit that shape.

### `install_grant`  *(method)*

```python
install_grant(self, grant: "'Grant'") -> 'None'
```

Replace this client's access policy with a superseding grant.

Called by :meth:`RunContext.request_scope` after the platform mints a
new grant covering additional read patterns / longer TTL / new write
prefix. Subsequent reads/writes use the new policy.

Default impl raises — concrete clients (LocalWorkspaceClient or the
runtime's MinIO-backed variant) override.

### `list_grants`  *(method)*

```python
list_grants(self) -> 'list[WorkspaceGrant]'
```

*(no docstring)*

### `open_view`  *(method)*

```python
open_view(self, *, purpose: 'str', hints: 'Sequence[str]' = (), file_types: 'Sequence[FileType]' = (), max_files: 'int' = 10, mode: 'WorkspaceMode' = <WorkspaceMode.READ_ONLY: 'read_only'>, reason: 'str | None' = None) -> 'WorkspaceView'
```

*(no docstring)*

### `request_access`  *(method)*

```python
request_access(self, *, files: 'Sequence[FileMatch | str]', mode: 'WorkspaceMode', reason: 'str', purpose: 'str' = '') -> 'WorkspaceGrant'
```

*(no docstring)*

### `search`  *(method)*

```python
search(self, *, query: 'str', types: 'Sequence[FileType]' = (), limit: 'int' = 20) -> 'list[FileMatch]'
```

*(no docstring)*

## `WorkspaceDenied`  *(class)*



Raised when a workspace request violates the agent's policy.

## `WorkspaceGrant`  *(class)*

```python
WorkspaceGrant(*, grant_id: str, purpose: str, files: tuple[a2a_pack.workspace.FileMatch, ...], mode: a2a_pack.workspace.WorkspaceMode, reason: str, expires_at: datetime.datetime | None = None, requires_human_approval: bool = False) -> None
```

An approved access grant for a bounded set of files.

## `WorkspaceMode`  *(class)*

```python
WorkspaceMode(*values)
```

*(no docstring)*

## `WorkspacePatch`  *(class)*

```python
WorkspacePatch(*, grant_id: str, path: str, operation: Literal['create', 'update', 'delete'], content: bytes | None = None) -> None
```

A staged write. Not applied until the runtime/approver commits it.

## `WorkspaceView`  *(class)*

```python
WorkspaceView()
```

A bounded view over a granted set of files.

Returned by :meth:`WorkspaceClient.open_view`. Reads always go to the
granted view; writes return :class:`WorkspacePatch` objects that the
runtime will commit (or reject) outside the sandbox.

### `delete`  *(method)*

```python
delete(self, path: 'str') -> 'WorkspacePatch'
```

*(no docstring)*

### `patches`  *(method)*

```python
patches(self) -> 'tuple[WorkspacePatch, ...]'
```

*(no docstring)*

### `read`  *(method)*

```python
read(self, path: 'str') -> 'bytes'
```

*(no docstring)*

### `write`  *(method)*

```python
write(self, path: 'str', content: 'bytes') -> 'WorkspacePatch'
```

*(no docstring)*


*Source: `sdk/a2a-pack/a2a_pack/workspace.py`*