from __future__ import annotations

import sys
import types

import pytest
from pydantic import BaseModel

from a2a_pack import (
    A2AAgent,
    FileSystemWorkspaceClient,
    FileType,
    LocalRunContext,
    LocalWorkspaceClient,
    MinIOWorkspaceClient,
    MinIOWorkspaceView,
    NoAuth,
    RunContext,
    WorkspaceAccess,
    WorkspaceDenied,
    WorkspaceMode,
    skill,
)
from a2a_pack.deepagents import WorkspaceBackend
from a2a_pack.grants import Grant, verify_grant


_FILES: dict[str, bytes] = {
    "src/auth/login.py": b"def login(jwt): ...  # JWT auth middleware",
    "src/auth/middleware.py": b"# auth middleware for JWT validation",
    "src/payments/checkout.py": b"def checkout(): ...  # payment flow",
    "tests/test_auth.py": b"def test_login_jwt(): ...",
    "configs/app.toml": b"[auth]\njwt = true",
    "secrets/.env": b"DB_PASSWORD=oops",
    "README.md": b"# project",
}


class _Cfg(BaseModel):
    pass


class _CoderAgent(A2AAgent[_Cfg, NoAuth]):
    name = "coder"
    description = "Edits code by negotiated views"
    workspace_access = WorkspaceAccess.dynamic(
        max_files=5,
        allowed_modes=(
            WorkspaceMode.READ_ONLY,
            WorkspaceMode.READ_WRITE_OVERLAY,
        ),
        require_reason=True,
        deny_patterns=("secrets/**", ".env", "**/.env"),
    )

    @skill()
    async def find_and_patch_auth(self, ctx: RunContext[NoAuth]) -> int:
        view = await ctx.workspace.open_view(
            purpose="Fix JWT login bug",
            hints=["auth", "jwt", "login"],
            file_types=[FileType.PYTHON],
            max_files=3,
            mode=WorkspaceMode.READ_WRITE_OVERLAY,
        )
        for fm in view.files:
            content = await view.read(fm.path)
            await view.write(fm.path, content + b"\n# patched\n")
        return len(view.files)


def _client() -> LocalWorkspaceClient:
    return LocalWorkspaceClient(_FILES, access=_CoderAgent.workspace_access)


def test_installed_grant_enforces_multiple_write_prefixes() -> None:
    ws = _client()
    ws.install_grant(
        Grant(
            grant_id="g-multi",
            issuer="main",
            audience="worker",
            bucket="user-1-files",
            mode=WorkspaceMode.READ_WRITE_OVERLAY,
            allow_patterns=("data/**",),
            outputs_prefix="outputs/",
            write_prefixes=("scratch/", "reports/"),
        )
    )

    assert ws.write_prefixes == ("outputs/", "scratch/", "reports/")
    assert ws.is_writable_output("outputs/report.md")
    assert ws.is_writable_output("scratch/tmp.txt")
    assert ws.is_writable_output("reports/final.md")
    assert not ws.is_writable_output("data/input.csv")


@pytest.mark.asyncio
async def test_workspace_delegate_preserves_bounded_source_grants() -> None:
    ws = _client()
    ws.bucket = "user-1-files"
    ws.install_grant(
        Grant(
            grant_id="g-parent",
            issuer="main",
            audience="agent-studio",
            bucket="user-1-files",
            mode=WorkspaceMode.READ_WRITE_OVERLAY,
            allow_patterns=("agents/demo/**",),
            outputs_prefix="agents/demo/.agent-studio/",
            write_prefixes=("agents/demo/",),
            source_grants=({"agent": "demo", "scope": "write"},),
        )
    )

    token = await ws.delegate(
        audience="agent-builder",
        allow_patterns=("agents/demo/**",),
        mode=WorkspaceMode.READ_WRITE_OVERLAY,
        outputs_prefix="agents/demo/",
        write_prefixes=("agents/demo/",),
        source_grants=({"agent": "demo", "scope": "write"},),
    )
    child = verify_grant(token)

    assert child.audience == "agent-builder"
    assert child.write_prefixes == ("agents/demo/",)
    assert [(grant.agent, grant.scope) for grant in child.source_grants] == [("demo", "write")]


def test_filesystem_workspace_creates_all_write_prefix_dirs(tmp_path):
    ws = FileSystemWorkspaceClient(
        tmp_path,
        access=WorkspaceAccess.dynamic(
            allowed_modes=(WorkspaceMode.READ_WRITE_OVERLAY,),
            require_reason=False,
        ),
        outputs_prefix="outputs/",
        write_prefixes=("reports/", "scratch"),
    )

    assert ws.write_prefixes == ("outputs/", "reports/", "scratch/")
    assert (tmp_path / "outputs").is_dir()
    assert (tmp_path / "reports").is_dir()
    assert (tmp_path / "scratch").is_dir()


def _workspace_for_deepagents() -> LocalWorkspaceClient:
    ws = LocalWorkspaceClient(
        {"data/input.txt": b"hello\n"},
        access=WorkspaceAccess.dynamic(
            allowed_modes=(
                WorkspaceMode.READ_ONLY,
                WorkspaceMode.READ_WRITE_OVERLAY,
            ),
            require_reason=False,
        ),
    )
    ws.install_grant(
        Grant(
            grant_id="g1",
            issuer="main",
            audience="worker",
            bucket="user-1-files",
            mode=WorkspaceMode.READ_WRITE_OVERLAY,
            allow_patterns=("**",),
            deny_patterns=(),
            outputs_prefix="outputs/",
        )
    )
    return ws


async def test_open_view_grants_relevant_files_only():
    agent = _CoderAgent()
    n = await agent.local_invoke("find_and_patch_auth", workspace=_client())
    assert n >= 1
    assert n <= 3


async def test_search_excludes_denied_patterns():
    ws = _client()
    matches = await ws.search(query="DB_PASSWORD env secret", limit=20)
    assert all("secrets/" not in m.path for m in matches)
    assert all(not m.path.endswith(".env") for m in matches)


async def test_request_access_rejects_denied_path():
    ws = _client()
    with pytest.raises(WorkspaceDenied, match="denied by policy"):
        await ws.request_access(
            files=["secrets/.env"],
            mode=WorkspaceMode.READ_ONLY,
            reason="trying to read secrets",
        )


async def test_request_access_rejects_disallowed_mode():
    ws = _client()
    with pytest.raises(WorkspaceDenied, match="not in allowed_modes"):
        await ws.request_access(
            files=["src/auth/login.py"],
            mode=WorkspaceMode.READ_WRITE_DIRECT,
            reason="needs direct write",
        )


async def test_request_access_requires_reason():
    ws = _client()
    with pytest.raises(WorkspaceDenied, match="reason required"):
        await ws.request_access(
            files=["src/auth/login.py"],
            mode=WorkspaceMode.READ_ONLY,
            reason="",
        )


async def test_request_access_enforces_max_files():
    ws = _client()
    paths = [p for p in _FILES if not p.startswith("secrets") and not p.endswith(".env")]
    assert len(paths) > 5  # confirm fixture
    with pytest.raises(WorkspaceDenied, match="max_files"):
        await ws.request_access(
            files=paths[:6],
            mode=WorkspaceMode.READ_ONLY,
            reason="too many",
        )


async def test_writes_are_staged_as_patches_not_applied():
    ws = _client()
    grant = await ws.request_access(
        files=["src/auth/login.py"],
        mode=WorkspaceMode.READ_WRITE_OVERLAY,
        reason="patch",
    )
    from a2a_pack.workspace import LocalWorkspaceView

    view = LocalWorkspaceView(grant, ws)
    patch = await view.write("src/auth/login.py", b"new content")
    assert patch.operation == "update"
    assert patch.content == b"new content"
    # original file untouched in the in-memory store
    assert ws._files["src/auth/login.py"].startswith(b"def login")
    patches = await view.patches()
    assert len(patches) == 1


async def test_view_rejects_writes_in_read_only_mode():
    ws = _client()
    grant = await ws.request_access(
        files=["src/auth/login.py"],
        mode=WorkspaceMode.READ_ONLY,
        reason="reading",
    )
    from a2a_pack.workspace import LocalWorkspaceView

    view = LocalWorkspaceView(grant, ws)
    with pytest.raises(WorkspaceDenied, match="read-only"):
        await view.write("src/auth/login.py", b"x")


async def test_view_rejects_path_outside_grant():
    ws = _client()
    grant = await ws.request_access(
        files=["src/auth/login.py"],
        mode=WorkspaceMode.READ_WRITE_OVERLAY,
        reason="patching",
    )
    from a2a_pack.workspace import LocalWorkspaceView

    view = LocalWorkspaceView(grant, ws)
    with pytest.raises(WorkspaceDenied, match="not in grant"):
        await view.read("src/payments/checkout.py")


async def test_workspace_disabled_by_default():
    from a2a_pack import SkillInvocationError

    class _Plain(A2AAgent):
        name = "plain"
        description = ""

        @skill()
        async def touch(self, ctx: RunContext[NoAuth]) -> str:
            await ctx.workspace.search(query="x")
            return "ok"

    agent = _Plain()
    with pytest.raises(SkillInvocationError) as ei:
        await agent.local_invoke("touch")
    assert isinstance(ei.value.__cause__, PermissionError)


def test_workspace_access_propagates_to_card():
    card = _CoderAgent().card()
    wa = card.workspace_access
    assert wa.enabled is True
    assert wa.max_files == 5
    assert WorkspaceMode.READ_WRITE_OVERLAY in wa.allowed_modes
    assert "secrets/**" in wa.deny_patterns


def test_deepagents_backend_persists_to_workspace_outputs():
    ws = _workspace_for_deepagents()
    backend = WorkspaceBackend(ws)

    result = backend.write("/outputs/report.md", "done")

    assert result.error is None
    assert ws.read_bytes("outputs/report.md") == b"done"
    read = backend.read("/outputs/report.md")
    assert read.error is None
    assert read.file_data["content"] == "done"


def test_deepagents_backend_rejects_writes_outside_outputs_prefix():
    ws = _workspace_for_deepagents()
    backend = WorkspaceBackend(ws)

    result = backend.write("/scratch.md", "not allowed")

    assert "write denied" in result.error
    assert not ws.exists("scratch.md")


class _RemoteScopedWorkspace:
    bucket = "user-1-files"
    current_grant_id = "g-remote"
    current_mode = WorkspaceMode.READ_WRITE_OVERLAY
    allow_patterns = ("outputs/**",)
    outputs_prefix = "outputs/"
    write_prefixes = ("outputs/",)

    def exists(self, path: str) -> bool:
        if not path.startswith("outputs/"):
            raise WorkspaceDenied("workspace stat failed: 403")
        return False

    def is_writable_output(self, path: str) -> bool:
        return path.startswith("outputs/")

    def iter_paths(self):
        return iter(())

    def write_bytes(self, path: str, content: bytes) -> None:
        raise AssertionError(f"unexpected write to {path}")


def test_deepagents_backend_denied_remote_stat_becomes_write_denied():
    backend = WorkspaceBackend(_RemoteScopedWorkspace())

    result = backend.write("/large_tool_results/call_1", "too large")

    assert "write denied" in result.error


class _StaleListedWorkspace:
    bucket = "user-1-files"
    current_grant_id = "g-stale"
    current_mode = WorkspaceMode.READ_WRITE_OVERLAY
    allow_patterns = ("**",)
    outputs_prefix = "outputs/"
    write_prefixes = ("outputs/",)

    def __init__(self) -> None:
        self.objects = {"data/input.txt": b"hello\n"}

    def exists(self, path: str) -> bool:
        return path in self.objects or path == "ghost.txt"

    def is_writable_output(self, path: str) -> bool:
        return path.startswith("outputs/")

    def iter_paths(self):
        return iter(["data/input.txt", "ghost.txt"])

    def read_bytes(self, path: str) -> bytes:
        if path == "ghost.txt":
            raise FileNotFoundError(path)
        return self.objects[path]

    def write_bytes(self, path: str, content: bytes) -> None:
        self.objects[path] = content


def test_deepagents_backend_skips_stale_listed_paths():
    backend = WorkspaceBackend(_StaleListedWorkspace())

    ls_result = backend.ls("/")
    glob_result = backend.glob("*.txt")
    grep_result = backend.grep("hello")
    read_result = backend.read("/ghost.txt")
    download_result = backend.download_files(["/ghost.txt", "/data/input.txt"])

    assert ls_result.error is None
    assert [entry["path"] for entry in ls_result.entries] == ["/data/"]
    assert glob_result.error is None
    assert [match["path"] for match in glob_result.matches] == ["/data/input.txt"]
    assert grep_result.error is None
    assert [match["path"] for match in grep_result.matches] == ["/data/input.txt"]
    assert "not found" in read_result.error
    assert download_result[0].error == "file_not_found"
    assert download_result[1].content == b"hello\n"


def test_run_context_exposes_workspace_deepagents_backend():
    ws = _workspace_for_deepagents()
    ctx = LocalRunContext(auth=NoAuth(), workspace=ws)

    backend = ctx.workspace_backend()
    result = backend.write("/outputs/from-context.txt", "ok")

    assert result.error is None
    assert result.path == "/outputs/from-context.txt"
    assert ws.read_bytes("outputs/from-context.txt") == b"ok"

    mounted = backend.write("/workspace/outputs/from-mount.txt", "ok")

    assert mounted.error is None
    assert mounted.path == "/outputs/from-mount.txt"
    assert ws.read_bytes("outputs/from-mount.txt") == b"ok"


def test_run_context_workspace_backend_routes_artifacts_under_write_prefix(monkeypatch):
    class _FakeCompositeBackend:
        def __init__(self, default, routes, *, artifacts_root="/"):
            self.default = default
            self.routes = routes
            self.artifacts_root = artifacts_root

        def write(self, file_path: str, content: str):
            return self.default.write(file_path, content)

    fake_deepagents = types.ModuleType("deepagents")
    fake_backends = types.ModuleType("deepagents.backends")
    fake_backends.CompositeBackend = _FakeCompositeBackend
    monkeypatch.setitem(sys.modules, "deepagents", fake_deepagents)
    monkeypatch.setitem(sys.modules, "deepagents.backends", fake_backends)

    ws = _workspace_for_deepagents()
    ctx = LocalRunContext(auth=NoAuth(), workspace=ws)

    backend = ctx.workspace_backend()

    assert getattr(backend, "artifacts_root", None) == "/outputs/.a2a-artifacts"
    result = backend.write(
        "/outputs/.a2a-artifacts/large_tool_results/call_1",
        "offloaded",
    )
    assert result.error is None
    assert ws.read_bytes("outputs/.a2a-artifacts/large_tool_results/call_1") == b"offloaded"


def test_deepagents_backend_grep_searches_exact_file_path():
    ws = _workspace_for_deepagents()
    ws.write_bytes(
        "large_tool_results/call_1",
        b'{"agents":["acquisition-swarm","apify-swarm-agent"]}\n',
    )
    backend = WorkspaceBackend(ws)

    result = backend.grep("swarm", path="/large_tool_results/call_1")

    assert result.error is None
    assert result.matches == [
        {
            "path": "/large_tool_results/call_1",
            "line": 1,
            "text": '{"agents":["acquisition-swarm","apify-swarm-agent"]}',
        }
    ]


def test_deepagents_backend_grep_glob_matches_relative_path_under_base():
    ws = _workspace_for_deepagents()
    ws.write_bytes(
        "agents/github-repo-inspector/agent.py",
        b"def _github_client():\n    return object()\n",
    )
    backend = WorkspaceBackend(ws)

    result = backend.grep(
        "_github_client",
        path="/agents/github-repo-inspector",
        glob="agent.py",
    )

    assert result.error is None
    assert result.matches == [
        {
            "path": "/agents/github-repo-inspector/agent.py",
            "line": 1,
            "text": "def _github_client():",
        }
    ]


class _FakeS3Error(Exception):
    def __init__(self, code: str) -> None:
        self.response = {"Error": {"Code": code}}
        super().__init__(code)


class _FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {
            "data/input.csv": (b"name,value\nada,1\n", "text/csv"),
        }

    def head_bucket(self, Bucket: str) -> None:
        assert Bucket == "user-1-files"

    def create_bucket(self, Bucket: str) -> None:
        assert Bucket == "user-1-files"

    def head_object(self, Bucket: str, Key: str) -> dict:
        if Key not in self.objects:
            raise _FakeS3Error("404")
        data, content_type = self.objects[Key]
        return {"ContentLength": len(data), "ContentType": content_type}

    def get_object(self, Bucket: str, Key: str) -> dict:
        data, content_type = self.objects[Key]

        class Body:
            def read(self) -> bytes:
                return data

        return {"Body": Body(), "ContentType": content_type}

    def put_object(self, Bucket: str, Key: str, Body: bytes, ContentType: str) -> None:
        self.objects[Key] = (Body, ContentType)

    def delete_object(self, Bucket: str, Key: str) -> None:
        self.objects.pop(Key, None)

    def get_paginator(self, name: str):
        assert name == "list_objects_v2"
        s3 = self

        class Paginator:
            def paginate(self, Bucket: str):
                yield {
                    "Contents": [
                        {"Key": key, "Size": len(data)}
                        for key, (data, _) in s3.objects.items()
                    ]
                }

        return Paginator()


@pytest.fixture
def fake_boto3(monkeypatch):
    fake = _FakeS3()
    boto3 = types.SimpleNamespace(client=lambda *args, **kwargs: fake)
    botocore = types.ModuleType("botocore")
    botocore_config = types.ModuleType("botocore.config")
    botocore_config.Config = lambda *args, **kwargs: object()
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.config", botocore_config)
    return fake


async def test_minio_workspace_reads_inputs_and_persists_outputs(fake_boto3):
    ws = MinIOWorkspaceClient(
        bucket="user-1-files",
        endpoint_url="http://minio:9000",
        access_key_id="minioadmin",
        secret_access_key="minioadmin",
        access=WorkspaceAccess.dynamic(
            max_files=4,
            allowed_modes=(WorkspaceMode.READ_ONLY, WorkspaceMode.READ_WRITE_OVERLAY),
            require_reason=False,
        ),
    )
    ws.install_grant(
        Grant(
            grant_id="g1",
            issuer="main",
            audience="worker",
            bucket="user-1-files",
            mode=WorkspaceMode.READ_WRITE_OVERLAY,
            allow_patterns=("data/**",),
            outputs_prefix="outputs/",
            expires_at=9999999999,
            issued_at=1,
        )
    )

    grant = await ws.request_access(
        files=["data/input.csv", "outputs/report.md"],
        mode=WorkspaceMode.READ_WRITE_OVERLAY,
        reason="summarize input",
    )
    view = MinIOWorkspaceView(grant, ws)

    assert await view.read("data/input.csv") == b"name,value\nada,1\n"
    patch = await view.write("outputs/report.md", b"# Report\n")
    assert patch.operation == "create"
    assert fake_boto3.objects["outputs/report.md"][0] == b"# Report\n"

    with pytest.raises(WorkspaceDenied, match="write_prefixes"):
        await view.write("private/report.md", b"nope")


def test_default_sandbox_is_microsandbox():
    from a2a_pack import Sandbox

    assert _CoderAgent.runtime().sandbox is Sandbox.MICROSANDBOX
