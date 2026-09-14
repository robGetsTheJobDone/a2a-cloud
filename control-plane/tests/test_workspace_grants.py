from __future__ import annotations

from urllib.parse import quote

import pytest

from control_plane.routes import workspace_grants
from control_plane.routes.workspace_grants import (
    WorkspaceGrantDelegateIn,
    _grant_user_id,
    _source_grant_scope,
    _writable,
    _workspace_route,
)
from control_plane.models import Agent, OrganizationMember, User


class _FakeGiteaBackend:
    def __init__(self) -> None:
        self.files = {"agent.py": b"old"}
        self.writes: list[tuple[str, bytes, str]] = []
        self.deletes: list[str] = []

    async def _read_bytes(self, path: str) -> bytes:
        return self.files[path]

    async def _write_bytes(self, path: str, content: bytes, action: str) -> None:
        self.writes.append((path, content, action))
        self.files[path] = content

    async def _delete(self, path: str) -> None:
        self.deletes.append(path)
        self.files.pop(path, None)

    async def _load_tree(self):
        class _Entry:
            def __init__(self, path: str, size: int) -> None:
                self.path = path
                self.size = size
                self.is_dir = False

        return [_Entry(path, len(body)) for path, body in sorted(self.files.items())]


@pytest.mark.asyncio
async def test_delegate_workspace_grant_uses_parent_capability(monkeypatch) -> None:
    parent = {
        "grant_id": "parent-1",
        "issuer": "control-plane",
        "audience": "agent-studio",
        "bucket": "user-2-files",
        "mode": "read_write_overlay",
        "allow_patterns": ["agents/demo/**"],
        "outputs_prefix": "agents/demo/",
        "write_prefixes": ["agents/demo/"],
        "source_grants": [{"agent": "demo", "scope": "write"}],
        "llm_models": ["platform-model"],
        "llm_max_budget_usd": 20.0,
    }
    monkeypatch.setattr(workspace_grants, "_grant_from_header", lambda _token: parent)

    result = await workspace_grants.delegate_workspace_grant(
        WorkspaceGrantDelegateIn(
            audience="agent-builder",
            allow_patterns=["agents/demo/**"],
            mode="read_write_overlay",
            outputs_prefix="agents/demo/",
            write_prefixes=["agents/demo/"],
            source_grants=[{"agent": "demo", "scope": "write"}],
            llm_models=["platform-model"],
            llm_max_budget_usd=20.0,
        ),
        x_a2a_grant="parent-token",
    )

    assert result["grant_id"]
    assert result["grant"]


@pytest.mark.asyncio
async def test_delegate_workspace_grant_rejects_scope_widening(monkeypatch) -> None:
    parent = {
        "grant_id": "parent-1",
        "issuer": "control-plane",
        "audience": "agent-studio",
        "bucket": "user-2-files",
        "mode": "read_write_overlay",
        "allow_patterns": ["agents/demo/**"],
        "outputs_prefix": "agents/demo/",
        "write_prefixes": ["agents/demo/"],
    }
    monkeypatch.setattr(workspace_grants, "_grant_from_header", lambda _token: parent)

    with pytest.raises(workspace_grants.HTTPException) as exc:
        await workspace_grants.delegate_workspace_grant(
            WorkspaceGrantDelegateIn(
                audience="agent-builder",
                allow_patterns=["agents/other/**"],
                mode="read_write_overlay",
                outputs_prefix="agents/other/",
                write_prefixes=["agents/other/"],
            ),
            x_a2a_grant="parent-token",
        )

    assert exc.value.status_code == 403


class _FakeWorkspace:
    gitea_org_name = "bob-2"


class _FakeSession:
    def __init__(self, user: User | None = None) -> None:
        self.user = user

    async def execute(self, _stmt):
        class _Result:
            def scalar_one_or_none(self):
                return None

        return _Result()

    async def get(self, model, id_):
        if model is User and self.user is not None and self.user.id == id_:
            return self.user
        return None


class _FakeResult:
    def __init__(self, value) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _ExistingAgentSession:
    def __init__(
        self,
        *,
        agent: Agent,
        membership: OrganizationMember | None = None,
    ) -> None:
        self.agent = agent
        self.membership = membership
        self.calls = 0

    async def execute(self, _stmt):
        self.calls += 1
        if self.calls == 1:
            return _FakeResult(self.agent)
        return _FakeResult(self.membership)


@pytest.mark.asyncio
async def test_grant_file_stat_percent_encodes_unicode_path_header(monkeypatch) -> None:
    grant = {"bucket": "user-2-files", "mode": "read_only"}
    key = "notes/unicode-\u2603.txt"

    monkeypatch.setattr(workspace_grants, "_grant_from_header", lambda _header: grant)
    monkeypatch.setattr(workspace_grants, "_allowed", lambda _grant, _key: True)
    monkeypatch.setattr(workspace_grants, "_workspace_route", lambda _key: None)
    monkeypatch.setattr(
        workspace_grants,
        "stat_file",
        lambda _bucket, _key: {"size": 12, "content_type": "text/plain"},
    )

    response = await workspace_grants.stat_grant_file(key, x_a2a_grant="grant")

    assert response.headers["X-A2A-File-Path"] == quote(key, safe="/")
    response.headers["X-A2A-File-Path"].encode("latin-1")


def test_writable_uses_write_prefixes_over_broad_allow_patterns() -> None:
    grant = {
        "mode": "read_write_overlay",
        "allow_patterns": ["**"],
        "deny_patterns": [],
        "outputs_prefix": "outputs/",
        "write_prefixes": ["outputs/", "reports/"],
    }

    assert _writable(grant, "outputs/report.md")
    assert _writable(grant, "reports/final.md")
    assert not _writable(grant, "data/input.csv")


def test_writable_keeps_legacy_outputs_prefix_behavior() -> None:
    grant = {
        "mode": "read_write_overlay",
        "allow_patterns": ["**"],
        "deny_patterns": [],
        "outputs_prefix": "outputs/",
    }

    assert _writable(grant, "outputs/report.md")
    assert not _writable(grant, "reports/final.md")


def test_writable_falls_back_to_allow_patterns_without_write_prefixes() -> None:
    grant = {
        "mode": "read_write_overlay",
        "allow_patterns": ["data/**"],
        "deny_patterns": ["data/private/**"],
        "outputs_prefix": None,
        "write_prefixes": [],
    }

    assert _writable(grant, "data/public.csv")
    assert not _writable(grant, "data/private/secret.csv")
    assert not _writable(grant, "reports/final.md")


def test_workspace_route_detects_agent_source_paths() -> None:
    assert _workspace_route("agents/demo/agent.py") == ("demo", "agent.py")
    assert _workspace_route("agents/demo") == ("demo", "")
    assert _workspace_route("agents/demo/.a2a-builder-state.json") is None
    assert _workspace_route("agents/demo/.agent-builder/cache.json") is None
    assert _workspace_route("outputs/demo/result.txt") is None


def test_grant_user_id_falls_back_to_bucket_name() -> None:
    assert _grant_user_id({"user_id": 7, "bucket": "user-2-files"}) == 7
    assert _grant_user_id({"bucket": "user-2-files"}) == 2
    assert _grant_user_id({"bucket": "shared"}) is None


def test_source_grant_scope_requires_explicit_source_grants() -> None:
    assert (
        _source_grant_scope(
            {"source_grants": [{"agent": "demo", "scope": "write"}]},
            "demo",
        )
        == "write"
    )
    assert _source_grant_scope({"source_grants": [{"agent": "demo"}]}, "demo") == "read"
    assert _source_grant_scope({"allow_patterns": ["agents/demo/**"]}, "demo") is None
    assert _source_grant_scope({"allow_patterns": ["outputs/**"]}, "demo") is None


@pytest.mark.asyncio
async def test_write_grant_file_routes_agent_source_to_gitea(monkeypatch) -> None:
    grant = {
        "bucket": "user-2-files",
        "mode": "read_write_overlay",
        "allow_patterns": ["agents/demo/**"],
        "write_prefixes": ["agents/demo/"],
    }
    backend = _FakeGiteaBackend()
    monkeypatch.setattr(workspace_grants, "_grant_from_header", lambda _header: grant)

    async def _backend(_session, _grant, *, repo: str, scope: str):
        assert repo == "demo"
        assert scope == "write"
        return backend

    monkeypatch.setattr(workspace_grants, "_gitea_backend_for_grant", _backend)
    called_minio = False

    def _upload_file(*_args, **_kwargs):
        nonlocal called_minio
        called_minio = True
        return {"ok": True}

    monkeypatch.setattr(workspace_grants, "upload_file", _upload_file)

    result = await workspace_grants.write_grant_file(
        "agents/demo/agent.py",
        b"new",
        x_a2a_grant="grant",
        session=object(),  # type: ignore[arg-type]
    )

    assert result == {
        "ok": True,
        "path": "agents/demo/agent.py",
        "size": 3,
        "backend": "gitea",
    }
    assert backend.writes == [("agent.py", b"new", "write")]
    assert called_minio is False


@pytest.mark.asyncio
async def test_read_grant_file_routes_agent_source_to_gitea(monkeypatch) -> None:
    grant = {
        "bucket": "user-2-files",
        "mode": "read_write_overlay",
        "allow_patterns": ["agents/demo/**"],
        "write_prefixes": [],
    }
    backend = _FakeGiteaBackend()
    monkeypatch.setattr(workspace_grants, "_grant_from_header", lambda _header: grant)

    async def _backend(_session, _grant, *, repo: str, scope: str):
        assert repo == "demo"
        assert scope == "read"
        return backend

    monkeypatch.setattr(workspace_grants, "_gitea_backend_for_grant", _backend)

    response = await workspace_grants.read_grant_file(
        "agents/demo/agent.py",
        x_a2a_grant="grant",
        session=object(),  # type: ignore[arg-type]
    )

    assert response.body == b"old"


@pytest.mark.asyncio
async def test_list_grant_files_merges_agent_source_from_gitea(monkeypatch) -> None:
    grant = {
        "bucket": "user-2-files",
        "mode": "read_write_overlay",
        "allow_patterns": ["agents/demo/**"],
        "write_prefixes": ["agents/demo/"],
    }
    backend = _FakeGiteaBackend()
    monkeypatch.setattr(workspace_grants, "_grant_from_header", lambda _header: grant)
    monkeypatch.setattr(workspace_grants, "list_files", lambda _bucket, prefix="": [])

    async def _backend(_session, _grant, *, repo: str, scope: str):
        assert repo == "demo"
        assert scope == "read"
        return backend

    monkeypatch.setattr(workspace_grants, "_gitea_backend_for_grant", _backend)

    rows = await workspace_grants.list_grant_files(
        x_a2a_grant="grant",
        session=object(),  # type: ignore[arg-type]
    )

    assert rows == [
        {
            "path": "agents/demo/agent.py",
            "size": 3,
            "modified_at": "",
            "content_type": "",
        }
    ]


@pytest.mark.asyncio
async def test_list_grant_files_uses_grant_prefixes_for_minio(monkeypatch) -> None:
    grant = {
        "bucket": "user-2-files",
        "mode": "read_only",
        "allow_patterns": ["tickets/**", "tickets/2026/**", "reports/q1.md"],
    }
    calls: list[str] = []

    def _list_files(_bucket: str, prefix: str = ""):
        calls.append(prefix)
        return [
            {
                "path": f"{prefix.rstrip('/')}/allowed.txt".lstrip("/"),
                "size": 1,
                "modified_at": "",
                "content_type": "text/plain",
            },
            {
                "path": "unrelated.txt",
                "size": 1,
                "modified_at": "",
                "content_type": "text/plain",
            },
        ]

    monkeypatch.setattr(workspace_grants, "_grant_from_header", lambda _header: grant)
    monkeypatch.setattr(workspace_grants, "list_files", _list_files)

    rows = await workspace_grants.list_grant_files(
        x_a2a_grant="grant",
        session=object(),  # type: ignore[arg-type]
    )

    assert calls == ["tickets", "reports/q1.md"]
    assert [row["path"] for row in rows] == ["tickets/allowed.txt"]


@pytest.mark.asyncio
async def test_list_grant_files_stops_minio_scan_at_limit(monkeypatch) -> None:
    grant = {
        "bucket": "user-2-files",
        "mode": "read_only",
        "allow_patterns": ["**"],
    }
    yielded: list[int] = []

    def _list_files(_bucket: str, prefix: str = ""):
        del prefix
        for idx in range(5):
            yielded.append(idx)
            yield {
                "path": f"file-{idx}.txt",
                "size": 1,
                "modified_at": "",
                "content_type": "text/plain",
            }

    monkeypatch.setattr(workspace_grants, "_grant_from_header", lambda _header: grant)
    monkeypatch.setattr(workspace_grants, "list_files", _list_files)

    rows = await workspace_grants.list_grant_files(
        x_a2a_grant="grant",
        session=object(),  # type: ignore[arg-type]
        limit=2,
    )

    assert yielded == [0, 1]
    assert [row["path"] for row in rows] == ["file-0.txt", "file-1.txt"]


@pytest.mark.asyncio
async def test_list_grant_files_lists_gitea_from_allowed_subtree(monkeypatch) -> None:
    grant = {
        "bucket": "user-2-files",
        "mode": "read_only",
        "allow_patterns": ["agents/demo/src/**"],
        "source_grants": [{"agent": "demo", "scope": "read"}],
    }

    class _SubtreeBackend:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def als(self, path: str):
            self.calls.append(path)
            if path == "/src":
                return type("Result", (), {
                    "entries": [
                        {"path": "/src/main.py", "is_dir": False, "size": 4},
                        {"path": "/src/lib/", "is_dir": True, "size": 0},
                    ],
                })()
            if path == "/src/lib":
                return type("Result", (), {
                    "entries": [
                        {"path": "/src/lib/util.py", "is_dir": False, "size": 5},
                    ],
                })()
            return type("Result", (), {"entries": []})()

        async def _load_tree(self):
            raise AssertionError("subtree grants should not load the full repo tree")

    backend = _SubtreeBackend()
    monkeypatch.setattr(workspace_grants, "_grant_from_header", lambda _header: grant)
    monkeypatch.setattr(workspace_grants, "list_files", lambda _bucket, prefix="": [])

    async def _backend(_session, _grant, *, repo: str, scope: str):
        assert repo == "demo"
        assert scope == "read"
        return backend

    monkeypatch.setattr(workspace_grants, "_gitea_backend_for_grant", _backend)

    rows = await workspace_grants.list_grant_files(
        x_a2a_grant="grant",
        session=object(),  # type: ignore[arg-type]
    )

    assert backend.calls == ["/src", "/src/lib"]
    assert [row["path"] for row in rows] == [
        "agents/demo/src/lib/util.py",
        "agents/demo/src/main.py",
    ]


@pytest.mark.asyncio
async def test_list_grant_files_stops_gitea_walk_at_limit(monkeypatch) -> None:
    grant = {
        "bucket": "user-2-files",
        "mode": "read_only",
        "allow_patterns": ["agents/demo/src/**"],
        "source_grants": [{"agent": "demo", "scope": "read"}],
    }

    class _SubtreeBackend:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def als(self, path: str):
            self.calls.append(path)
            if path == "/src":
                return type("Result", (), {
                    "entries": [
                        {"path": "/src/main.py", "is_dir": False, "size": 4},
                        {"path": "/src/lib/", "is_dir": True, "size": 0},
                    ],
                })()
            if path == "/src/lib":
                raise AssertionError("gitea listing should stop after the limit")
            return type("Result", (), {"entries": []})()

        async def _load_tree(self):
            raise AssertionError("limited subtree listing should not load the full tree")

    backend = _SubtreeBackend()
    monkeypatch.setattr(workspace_grants, "_grant_from_header", lambda _header: grant)
    monkeypatch.setattr(workspace_grants, "list_files", lambda _bucket, prefix="": [])

    async def _backend(_session, _grant, *, repo: str, scope: str):
        assert repo == "demo"
        assert scope == "read"
        return backend

    monkeypatch.setattr(workspace_grants, "_gitea_backend_for_grant", _backend)

    rows = await workspace_grants.list_grant_files(
        x_a2a_grant="grant",
        session=object(),  # type: ignore[arg-type]
        limit=1,
    )

    assert backend.calls == ["/src"]
    assert [row["path"] for row in rows] == ["agents/demo/src/main.py"]


@pytest.mark.asyncio
async def test_list_grant_files_maps_wildcard_agent_pattern_to_source_repo(
    monkeypatch,
) -> None:
    grant = {
        "bucket": "user-2-files",
        "mode": "read_only",
        "allow_patterns": ["agents/*/src/**"],
        "source_grants": [{"agent": "demo", "scope": "read"}],
    }

    class _SubtreeBackend:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def als(self, path: str):
            self.calls.append(path)
            return type("Result", (), {
                "entries": [
                    {"path": "/src/main.py", "is_dir": False, "size": 4},
                    {"path": "/tests/test_main.py", "is_dir": False, "size": 5},
                ],
            })()

        async def _load_tree(self):
            raise AssertionError("wildcard repo pattern should still use subtree listing")

    backend = _SubtreeBackend()
    monkeypatch.setattr(workspace_grants, "_grant_from_header", lambda _header: grant)
    monkeypatch.setattr(workspace_grants, "list_files", lambda _bucket, prefix="": [])

    async def _backend(_session, _grant, *, repo: str, scope: str):
        assert repo == "demo"
        assert scope == "read"
        return backend

    monkeypatch.setattr(workspace_grants, "_gitea_backend_for_grant", _backend)

    rows = await workspace_grants.list_grant_files(
        x_a2a_grant="grant",
        session=object(),  # type: ignore[arg-type]
    )

    assert backend.calls == ["/src"]
    assert [row["path"] for row in rows] == ["agents/demo/src/main.py"]


@pytest.mark.asyncio
async def test_write_gitea_bytes_retries_transient_content_error(monkeypatch) -> None:
    backend = _FakeGiteaBackend()
    calls = 0

    async def _write(path: str, content: bytes, action: str) -> None:
        nonlocal calls
        calls += 1
        if calls < 5:
            raise workspace_grants.GiteaError("branch head changed")
        await _FakeGiteaBackend._write_bytes(backend, path, content, action)

    async def _sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(backend, "_write_bytes", _write)
    monkeypatch.setattr(workspace_grants.asyncio, "sleep", _sleep)

    await workspace_grants._write_gitea_bytes_with_retry(
        backend, "requirements.txt", b"a2a-pack\n", action="write"
    )

    assert calls == 5
    assert backend.files["requirements.txt"] == b"a2a-pack\n"


@pytest.mark.asyncio
async def test_missing_agent_write_bootstraps_gitea_repo(monkeypatch) -> None:
    user = User(id=2, email="bob@example.com", password_hash="x")
    grant = {
        "bucket": "user-2-files",
        "mode": "read_write_overlay",
        "allow_patterns": ["agents/new-agent/**"],
        "write_prefixes": ["agents/new-agent/"],
        "source_grants": [{"agent": "new-agent", "scope": "write"}],
        "expires_at": 9999999999,
        "audience": "test",
    }
    created: list[tuple[str, str]] = []
    minted: list[tuple[str, str, int]] = []

    async def _resolve(_session, _user, *, source: str):
        assert _user is user
        assert source == "workspace_grants"
        return object(), _FakeWorkspace()

    def _ensure_repo(name: str, _description: str, *, owner: str | None = None):
        created.append((owner or "", name))
        return "push", "internal"

    async def _mint(_session, *, scope, owner, repo, ttl_seconds, issued_by_user_id, purpose):
        minted.append((owner, repo, issued_by_user_id))
        return object(), "secret"

    class _Backend:
        def __init__(
            self,
            *,
            gitea_url: str,
            owner: str,
            repo: str,
            token: str,
            commit_prefix: str,
        ):
            self.gitea_url = gitea_url
            self.owner = owner
            self.repo = repo
            self.token = token
            self.commit_prefix = commit_prefix

    monkeypatch.setattr(workspace_grants, "resolve_agent_gitea_workspace", _resolve)
    monkeypatch.setattr(workspace_grants, "ensure_repo", _ensure_repo)
    monkeypatch.setattr(workspace_grants, "mint_scoped_token", _mint)
    monkeypatch.setattr(workspace_grants, "GiteaBackend", _Backend)

    backend = await workspace_grants._gitea_backend_for_grant(
        _FakeSession(user),  # type: ignore[arg-type]
        grant,
        repo="new-agent",
        scope="write",
    )

    assert created == [("bob-2", "new-agent")]
    assert minted == [("bob-2", "new-agent", 2)]
    assert backend.owner == "bob-2"
    assert backend.repo == "new-agent"
    assert backend.commit_prefix == "a2a-source-edit"


@pytest.mark.asyncio
async def test_missing_agent_read_requires_existing_gitea_repo(monkeypatch) -> None:
    user = User(id=2, email="bob@example.com", password_hash="x")
    grant = {
        "bucket": "user-2-files",
        "mode": "read_write_overlay",
        "allow_patterns": ["agents/new-agent/**"],
        "write_prefixes": ["agents/new-agent/"],
        "source_grants": [{"agent": "new-agent", "scope": "read"}],
    }

    async def _resolve(_session, _user, *, source: str):
        return object(), _FakeWorkspace()

    monkeypatch.setattr(workspace_grants, "resolve_agent_gitea_workspace", _resolve)
    monkeypatch.setattr(workspace_grants, "repo_exists", lambda _repo, *, owner=None: False)

    with pytest.raises(workspace_grants.HTTPException) as exc:
        await workspace_grants._gitea_backend_for_grant(
            _FakeSession(user),  # type: ignore[arg-type]
            grant,
            repo="new-agent",
            scope="read",
        )

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_existing_agent_source_write_allows_owner(monkeypatch) -> None:
    agent = Agent(id=1, name="demo", owner_id=2, gitea_owner="a2a-personal-2")
    grant = {
        "bucket": "user-2-files",
        "mode": "read_write_overlay",
        "source_grants": [{"agent": "demo", "scope": "write"}],
        "expires_at": 9999999999,
        "audience": "agent-studio",
    }
    minted: list[tuple[str, str, int, str]] = []

    async def _mint(_session, *, scope, owner, repo, ttl_seconds, issued_by_user_id, purpose):
        minted.append((owner, repo, issued_by_user_id, scope))
        return object(), "secret"

    class _Backend:
        def __init__(
            self,
            *,
            gitea_url: str,
            owner: str,
            repo: str,
            token: str,
            commit_prefix: str,
        ):
            self.owner = owner
            self.repo = repo
            self.token = token
            self.commit_prefix = commit_prefix

    monkeypatch.setattr(workspace_grants, "mint_scoped_token", _mint)
    monkeypatch.setattr(workspace_grants, "GiteaBackend", _Backend)

    backend = await workspace_grants._gitea_backend_for_grant(
        _ExistingAgentSession(agent=agent),  # type: ignore[arg-type]
        grant,
        repo="demo",
        scope="write",
    )

    assert minted == [("a2a-personal-2", "demo", 2, "write")]
    assert backend.owner == "a2a-personal-2"
    assert backend.repo == "demo"
    assert backend.commit_prefix == "a2a-source-edit"


@pytest.mark.asyncio
async def test_existing_agent_source_write_denies_unrelated_grant_user(monkeypatch) -> None:
    agent = Agent(id=1, name="demo", owner_id=3, gitea_owner="a2a-personal-3")
    grant = {
        "bucket": "user-2-files",
        "mode": "read_write_overlay",
        "allow_patterns": ["agents/demo/**"],
        "write_prefixes": ["agents/demo/"],
        "source_grants": [{"agent": "demo", "scope": "write"}],
    }

    async def _mint(*_args, **_kwargs):
        raise AssertionError("must not mint a token for an unrelated grant user")

    monkeypatch.setattr(workspace_grants, "mint_scoped_token", _mint)

    with pytest.raises(workspace_grants.HTTPException) as exc:
        await workspace_grants._gitea_backend_for_grant(
            _ExistingAgentSession(agent=agent),  # type: ignore[arg-type]
            grant,
            repo="demo",
            scope="write",
        )

    assert exc.value.status_code == 403
    assert "not permitted" in exc.value.detail


@pytest.mark.asyncio
async def test_existing_agent_source_write_requires_org_admin(monkeypatch) -> None:
    agent = Agent(id=1, name="demo", owner_id=3, organization_id=4, gitea_owner="acme")
    grant = {
        "bucket": "user-2-files",
        "mode": "read_write_overlay",
        "source_grants": [{"agent": "demo", "scope": "write"}],
    }
    membership = OrganizationMember(
        id=1,
        organization_id=4,
        user_id=2,
        role="member",
        active=True,
    )

    with pytest.raises(workspace_grants.HTTPException) as exc:
        await workspace_grants._gitea_backend_for_grant(
            _ExistingAgentSession(agent=agent, membership=membership),  # type: ignore[arg-type]
            grant,
            repo="demo",
            scope="write",
        )

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_existing_agent_source_write_allows_org_admin(monkeypatch) -> None:
    agent = Agent(id=1, name="demo", owner_id=3, organization_id=4, gitea_owner="acme")
    grant = {
        "bucket": "user-2-files",
        "mode": "read_write_overlay",
        "source_grants": [{"agent": "demo", "scope": "write"}],
        "expires_at": 9999999999,
        "audience": "agent-builder",
    }
    membership = OrganizationMember(
        id=1,
        organization_id=4,
        user_id=2,
        role="admin",
        active=True,
    )
    minted: list[tuple[str, str, int, str]] = []

    async def _mint(_session, *, scope, owner, repo, ttl_seconds, issued_by_user_id, purpose):
        minted.append((owner, repo, issued_by_user_id, scope))
        return object(), "secret"

    class _Backend:
        def __init__(
            self,
            *,
            gitea_url: str,
            owner: str,
            repo: str,
            token: str,
            commit_prefix: str,
        ):
            self.owner = owner
            self.repo = repo
            self.token = token
            self.commit_prefix = commit_prefix

    monkeypatch.setattr(workspace_grants, "mint_scoped_token", _mint)
    monkeypatch.setattr(workspace_grants, "GiteaBackend", _Backend)

    backend = await workspace_grants._gitea_backend_for_grant(
        _ExistingAgentSession(agent=agent, membership=membership),  # type: ignore[arg-type]
        grant,
        repo="demo",
        scope="write",
    )

    assert minted == [("acme", "demo", 2, "write")]
    assert backend.owner == "acme"
    assert backend.commit_prefix == "a2a-source-edit"
