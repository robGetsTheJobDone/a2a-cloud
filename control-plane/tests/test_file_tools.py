from __future__ import annotations

import json
from types import SimpleNamespace

import os

os.environ.setdefault(
    "A2A_CP_DATABASE_URL",
    "postgresql+psycopg://control_plane:control_plane@localhost/control_plane_test",
)

import pytest

import main_agent.tools.files as files
from main_agent.tools.files import (
    _WorkspaceFileStore,
    WorkspaceRoute,
    build_file_tools,
    _delete_workspace_path,
    _normalize_workspace_path,
    _workspace_route,
)


class _Paginator:
    def __init__(self, keys: list[str], calls: list[dict[str, object]]) -> None:
        self.keys = keys
        self.calls = calls

    def paginate(
        self,
        *,
        Bucket: str,
        Prefix: str,
        Delimiter: str | None = None,
        PaginationConfig: dict[str, object] | None = None,
    ) -> list[dict[str, object]]:
        del Bucket
        self.calls.append({
            "Prefix": Prefix,
            "Delimiter": Delimiter,
            "PaginationConfig": PaginationConfig,
        })
        contents: list[dict[str, object]] = []
        prefixes: set[str] = set()
        for key in self.keys:
            if not key.startswith(Prefix):
                continue
            rest = key[len(Prefix):]
            if Delimiter and Delimiter in rest.strip("/"):
                prefixes.add(Prefix + rest.split(Delimiter, 1)[0] + Delimiter)
                continue
            contents.append({"Key": key})
        page: dict[str, object] = {"Contents": contents}
        if Delimiter:
            page["CommonPrefixes"] = [{"Prefix": prefix} for prefix in sorted(prefixes)]
        return [page]


class _FakeS3:
    def __init__(self, keys: list[str]) -> None:
        self.keys = keys
        self.deleted: list[str] = []
        self.paginate_calls: list[dict[str, object]] = []

    def get_paginator(self, name: str) -> _Paginator:
        assert name == "list_objects_v2"
        return _Paginator(self.keys, self.paginate_calls)

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        del Bucket
        self.deleted.append(Key)


class _FakeGiteaBackend:
    expired_token_prefix = "expired-"
    repo_files: dict[str, dict[str, str]] = {}

    def __init__(
        self,
        gitea_url: str,
        owner: str,
        repo: str,
        *,
        ref: str = "main",
        token: str,
        author_name: str = "a2a-cloud",
        author_email: str = "noreply@example.com",
        commit_prefix: str = "",
    ) -> None:
        del gitea_url, owner, ref, author_name, author_email, commit_prefix
        self.token = token
        self.repo = repo
        self.files = self.repo_files.setdefault(
            repo,
            {
                "agent.py": "print('hello')\n",
                "README.md": "# demo\n",
            },
        )
        self.calls: list[tuple[str, str, str | None, str | None]] = []

    def _maybe_expired(self) -> None:
        if isinstance(self.token, str) and self.token.startswith(self.expired_token_prefix):
            raise files.GiteaAuthError("401 token expired")

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> SimpleNamespace:
        del offset, limit
        self._maybe_expired()
        self.calls.append(("read", file_path, None, None))
        if file_path not in self.files:
            return SimpleNamespace(error="missing", file_data=None)
        content = self.files[file_path]
        return SimpleNamespace(
            error=None,
            file_data={
                "content": content,
                "encoding": "utf-8",
                "created_at": "",
                "modified_at": "",
            },
        )

    def write(self, file_path: str, content: str) -> SimpleNamespace:
        self._maybe_expired()
        self.calls.append(("write", file_path, None, None))
        self.files[file_path] = content
        return SimpleNamespace(error=None, path="/" + file_path)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> SimpleNamespace:
        del replace_all
        self._maybe_expired()
        self.calls.append(("edit", file_path, old_string, new_string))
        if file_path not in self.files:
            return SimpleNamespace(error="missing", path=None)
        self.files[file_path] = new_string
        return SimpleNamespace(error=None, path="/" + file_path)

    def glob(self, pattern: str, path: str = "/") -> SimpleNamespace:
        del pattern
        self._maybe_expired()
        prefix = path.lstrip("/")
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        matches = []
        for file_path, content in sorted(self.files.items()):
            if prefix and not file_path.startswith(prefix):
                continue
            matches.append(
                {
                    "path": "/" + file_path,
                    "size": len(content.encode("utf-8")),
                    "modified_at": "",
                }
            )
        return SimpleNamespace(matches=matches)

    def ls(self, path: str) -> SimpleNamespace:
        self._maybe_expired()
        prefix = path.lstrip("/")
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        entries: dict[str, dict[str, object]] = {}
        for file_path, content in sorted(self.files.items()):
            if prefix and not file_path.startswith(prefix):
                continue
            rest = file_path[len(prefix):]
            if not rest:
                continue
            name = rest.split("/", 1)[0]
            full = (prefix + name).strip("/")
            if "/" in rest:
                entries[full] = {
                    "path": "/" + full + "/",
                    "is_dir": True,
                    "size": 0,
                    "modified_at": "",
                }
            else:
                entries[full] = {
                    "path": "/" + full,
                    "is_dir": False,
                    "size": len(content.encode("utf-8")),
                    "modified_at": "",
                }
        return SimpleNamespace(entries=[entries[key] for key in sorted(entries)])

    async def _delete(self, path: str) -> None:
        self._maybe_expired()
        self.calls.append(("delete", path, None, None))
        self.files.pop(path, None)


def test_normalize_workspace_delete_path_accepts_mount_paths() -> None:
    assert _normalize_workspace_path("/workspace/agents") == "agents"
    assert _normalize_workspace_path("agents/") == "agents"
    assert _normalize_workspace_path("./agents/demo") == "agents/demo"


def test_normalize_workspace_delete_path_rejects_root() -> None:
    for path in ("", ".", "/", "/workspace"):
        with pytest.raises(ValueError):
            _normalize_workspace_path(path)


def test_delete_workspace_path_deletes_prefix_objects_individually() -> None:
    s3 = _FakeS3(
        [
            "agents/research/agent.py",
            "agents/research/a2a.yaml",
            "agents/other/agent.py",
            "notes.txt",
        ]
    )

    result = _delete_workspace_path(s3, "bucket", "/workspace/agents/research")

    assert result == {
        "ok": True,
        "path": "agents/research",
        "deleted": 2,
        "mode": "prefix",
    }
    assert s3.deleted == [
        "agents/research/agent.py",
        "agents/research/a2a.yaml",
    ]


def test_workspace_route_detects_agent_sources() -> None:
    assert _workspace_route("agents/demo/agent.py") == WorkspaceRoute("agents", "demo", "agent.py")
    assert _workspace_route("agents/demo") == WorkspaceRoute("agents", "demo", "")
    assert _workspace_route("repos/control-plane/main.py") == WorkspaceRoute(
        "repos",
        "control-plane",
        "main.py",
    )
    assert _workspace_route("notes.txt") is None


def test_workspace_store_routes_agent_paths_to_gitea(monkeypatch) -> None:
    s3 = _FakeS3(["notes.txt", "agents/demo/minio-shadow.txt"])
    _FakeGiteaBackend.repo_files = {
        "demo": {
            "agent.py": "print('hello')\n",
            "README.md": "# demo\n",
            "skills/one/SKILL.md": "# skill\n",
        }
    }
    monkeypatch.setattr(files, "GiteaBackend", _FakeGiteaBackend)
    monkeypatch.setattr(
        files,
        "_mint_gitea_token",
        lambda _ctx, repo, **kwargs: {"token": "secret", "owner": "agents", "repo": repo, **kwargs},
    )
    live_repos = [files.RepoMount(repo="demo", owner="agents", mount_path="agents/demo/")]
    ctx = SimpleNamespace(
        bucket="bucket",
        jwt="jwt",
        settings=SimpleNamespace(
            cp_url="http://cp",
            minio_endpoint="http://minio",
            minio_access_key="a",
            minio_secret_key="b",
        ),
    )
    store = _WorkspaceFileStore(ctx, s3=s3)
    monkeypatch.setattr(store, "_live_gitea_repos", lambda: live_repos)
    monkeypatch.setattr(store, "_configured_repo_mounts", lambda: [])

    payload = json.loads(store.list_files())
    listing = payload["files"]
    assert payload["recursive"] is False
    assert {"path": "agents/demo/", "size": 0, "modified_at": None} in listing
    assert {"path": "notes.txt", "size": 0, "modified_at": None} in listing
    assert all(not item["path"].startswith("agents/demo/minio-shadow") for item in listing)
    assert all(item["path"] != "agents/demo/agent.py" for item in listing)

    repo_listing = json.loads(store.list_files("agents/demo"))["files"]
    assert {
        "path": "agents/demo/agent.py",
        "size": len("print('hello')\n".encode("utf-8")),
        "modified_at": "",
    } in repo_listing
    assert {
        "path": "agents/demo/README.md",
        "size": len("# demo\n".encode("utf-8")),
        "modified_at": "",
    } in repo_listing
    assert {"path": "agents/demo/skills/", "size": 0, "modified_at": ""} in repo_listing
    assert all(item["path"] != "agents/demo/skills/one/SKILL.md" for item in repo_listing)

    skill_listing = json.loads(store.list_files("agents/demo/skills"))["files"]
    assert {"path": "agents/demo/skills/one/", "size": 0, "modified_at": ""} in skill_listing


def test_workspace_store_caches_gitea_mounts_and_read_backend(monkeypatch) -> None:
    s3 = _FakeS3(["notes.txt"])
    _FakeGiteaBackend.repo_files = {
        "demo": {
            "agent.py": "print('hello')\n",
            "README.md": "# demo\n",
        }
    }
    monkeypatch.setattr(files, "GiteaBackend", _FakeGiteaBackend)
    minted: list[tuple[str, str]] = []

    def _mint(_ctx, repo, **kwargs):
        minted.append((repo, kwargs.get("scope", "read")))
        return {"token": "secret", "owner": "agents", "repo": repo}

    monkeypatch.setattr(files, "_mint_gitea_token", _mint)
    ctx = SimpleNamespace(
        bucket="bucket",
        jwt="jwt",
        settings=SimpleNamespace(
            cp_url="http://cp",
            minio_endpoint="http://minio",
            minio_access_key="a",
            minio_secret_key="b",
        ),
    )
    store = _WorkspaceFileStore(ctx, s3=s3)
    live_calls = 0

    def _live_repos():
        nonlocal live_calls
        live_calls += 1
        return [files.RepoMount(repo="demo", owner="agents", mount_path="agents/demo/")]

    monkeypatch.setattr(store, "_live_gitea_repos", _live_repos)
    monkeypatch.setattr(store, "_configured_repo_mounts", lambda: [])

    assert {"path": "agents/demo/", "size": 0, "modified_at": None} in json.loads(
        store.list_files()
    )["files"]
    assert json.loads(store.list_files("agents/demo"))["files"]
    assert json.loads(store.read_file("agents/demo/README.md"))["content"] == "# demo\n"
    assert live_calls == 1
    assert minted == [("demo", "read")]


def test_workspace_store_recursive_gitea_listing_is_bounded(monkeypatch) -> None:
    class _CountingGiteaBackend(_FakeGiteaBackend):
        glob_calls = 0
        ls_paths: list[str] = []

        def glob(self, pattern: str, path: str = "/") -> SimpleNamespace:
            del pattern, path
            type(self).glob_calls += 1
            raise AssertionError("recursive list should not load the full tree")

        def ls(self, path: str) -> SimpleNamespace:
            type(self).ls_paths.append(path)
            return super().ls(path)

    s3 = _FakeS3([])
    _CountingGiteaBackend.repo_files = {
        "demo": {
            "agent.py": "print('hello')\n",
            "README.md": "# demo\n",
            "skills/one/SKILL.md": "# skill\n",
        }
    }
    monkeypatch.setattr(files, "GiteaBackend", _CountingGiteaBackend)
    monkeypatch.setattr(
        files,
        "_mint_gitea_token",
        lambda _ctx, repo, **kwargs: {"token": "secret", "owner": "agents", "repo": repo},
    )
    ctx = SimpleNamespace(
        bucket="bucket",
        jwt="jwt",
        settings=SimpleNamespace(
            cp_url="http://cp",
            minio_endpoint="http://minio",
            minio_access_key="a",
            minio_secret_key="b",
        ),
    )
    store = _WorkspaceFileStore(ctx, s3=s3)
    monkeypatch.setattr(
        store,
        "_live_gitea_repos",
        lambda: [files.RepoMount(repo="demo", owner="agents", mount_path="agents/demo/")],
    )
    monkeypatch.setattr(store, "_configured_repo_mounts", lambda: [])

    payload = json.loads(store.list_files("agents/demo", recursive=True, limit=2))

    assert payload["truncated"] is True
    assert [item["path"] for item in payload["files"]] == [
        "agents/demo/README.md",
        "agents/demo/agent.py",
    ]
    assert _CountingGiteaBackend.glob_calls == 0
    assert _CountingGiteaBackend.ls_paths == ["/"]


def test_workspace_store_routes_repo_mount_paths_to_gitea(monkeypatch) -> None:
    s3 = _FakeS3(["repos/control-plane/minio-shadow.txt"])
    _FakeGiteaBackend.repo_files = {
        "control-plane": {
            "README.md": "# control plane\n",
        }
    }
    monkeypatch.setattr(files, "GiteaBackend", _FakeGiteaBackend)
    minted: list[tuple[str, str | None, str]] = []

    def _mint(_ctx, repo, **kwargs):
        minted.append((repo, kwargs.get("owner"), kwargs.get("scope", "read")))
        return {"token": "secret", "owner": kwargs.get("owner") or "gitea_admin", "repo": repo}

    monkeypatch.setattr(files, "_mint_gitea_token", _mint)
    ctx = SimpleNamespace(
        bucket="bucket",
        jwt="jwt",
        settings=SimpleNamespace(
            cp_url="http://cp",
            minio_endpoint="http://minio",
            minio_access_key="a",
            minio_secret_key="b",
            repo_mounts="gitea_admin/control-plane",
        ),
    )
    store = _WorkspaceFileStore(ctx, s3=s3)
    monkeypatch.setattr(store, "_live_gitea_repos", lambda: [])

    listing = json.loads(store.list_files())["files"]
    assert {"path": "repos/control-plane/", "size": 0, "modified_at": None} in listing
    assert all(item["path"] != "repos/control-plane/README.md" for item in listing)
    assert {
        "path": "repos/control-plane/README.md",
        "size": len("# control plane\n".encode("utf-8")),
        "modified_at": "",
    } in json.loads(store.list_files("repos/control-plane"))["files"]
    assert all(not item["path"].startswith("repos/control-plane/minio-shadow") for item in listing)
    assert (
        json.loads(store.read_file("repos/control-plane/README.md"))["content"]
        == "# control plane\n"
    )
    assert json.loads(store.write_file("repos/control-plane/README.md", "# updated\n")) == {
        "ok": True,
        "path": "repos/control-plane/README.md",
        "size": len("# updated\n".encode("utf-8")),
    }
    assert ("control-plane", "gitea_admin", "read") in minted
    assert ("control-plane", "gitea_admin", "write") in minted


def test_workspace_store_list_files_is_shallow_for_minio() -> None:
    s3 = _FakeS3(["data/input.txt", "data/deep/result.json", "notes.txt"])
    ctx = SimpleNamespace(
        bucket="bucket",
        jwt=None,
        settings=SimpleNamespace(
            cp_url="http://cp",
            minio_endpoint="http://minio",
            minio_access_key="a",
            minio_secret_key="b",
        ),
    )
    store = _WorkspaceFileStore(ctx, s3=s3)

    root = json.loads(store.list_files())
    assert root["files"] == [
        {"path": "data/", "size": 0, "modified_at": None},
        {"path": "notes.txt", "size": 0, "modified_at": None},
    ]

    data = json.loads(store.list_files("data"))
    assert data["files"] == [
        {"path": "data/deep/", "size": 0, "modified_at": None},
        {"path": "data/input.txt", "size": 0, "modified_at": None},
    ]


def test_workspace_store_list_files_default_limit_bounds_payload_and_s3_page() -> None:
    s3 = _FakeS3([f"data/file-{idx:03d}.txt" for idx in range(90)])
    ctx = SimpleNamespace(
        bucket="bucket",
        jwt=None,
        settings=SimpleNamespace(
            cp_url="http://cp",
            minio_endpoint="http://minio",
            minio_access_key="a",
            minio_secret_key="b",
        ),
    )
    store = _WorkspaceFileStore(ctx, s3=s3)

    payload = json.loads(store.list_files("data", recursive=True))

    assert payload["limit"] == files.LIST_FILES_DEFAULT_LIMIT == 80
    assert payload["truncated"] is True
    assert len(payload["files"]) == 80
    assert s3.paginate_calls[-1]["PaginationConfig"] == {"PageSize": 81}


def test_workspace_store_refreshes_gitea_token_on_expired_access(monkeypatch) -> None:
    s3 = _FakeS3([])
    _FakeGiteaBackend.repo_files = {}
    monkeypatch.setattr(files, "GiteaBackend", _FakeGiteaBackend)
    minted: list[str] = []

    def _mint(_ctx, repo, **kwargs):
        del kwargs
        token = "expired-1" if not minted else "fresh-2"
        minted.append(token)
        return {"token": token, "owner": "agents", "repo": repo}

    monkeypatch.setattr(
        files,
        "_mint_gitea_token",
        _mint,
    )
    ctx = SimpleNamespace(
        bucket="bucket",
        jwt="jwt",
        settings=SimpleNamespace(
            cp_url="http://cp",
            minio_endpoint="http://minio",
            minio_access_key="a",
            minio_secret_key="b",
        ),
    )
    store = _WorkspaceFileStore(ctx, s3=s3)
    monkeypatch.setattr(
        store,
        "_live_gitea_repos",
        lambda: [files.RepoMount(repo="demo", owner="agents", mount_path="agents/demo/")],
    )
    monkeypatch.setattr(store, "_configured_repo_mounts", lambda: [])

    assert json.loads(store.write_file("agents/demo/agent.py", "print('updated')\n")) == {
        "ok": True,
        "path": "agents/demo/agent.py",
        "size": len("print('updated')\n".encode("utf-8")),
    }
    assert minted == ["expired-1", "fresh-2"]

    assert json.loads(store.read_file("/workspace/agents/demo/agent.py")) == {
        "path": "/workspace/agents/demo/agent.py",
        "content_type": "text/plain; charset=utf-8",
        "size": len("print('updated')\n".encode("utf-8")),
        "content": "print('updated')\n",
    }
    assert json.loads(store.delete_file("agents/demo")) == {
        "ok": True,
        "path": "agents/demo",
        "deleted": 2,
        "mode": "prefix",
    }


@pytest.mark.asyncio
async def test_deploy_agent_source_tool_calls_control_plane(monkeypatch) -> None:
    posts: list[dict[str, object]] = []

    class _Response:
        status_code = 200
        text = '{"ok": true}'
        content = b'{"ok": true}'

        def json(self) -> dict[str, object]:
            return {"ok": True, "deploy_id": "dpl_123"}

    class _Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, *, headers: dict[str, str]) -> _Response:
            posts.append({"url": url, "headers": headers})
            return _Response()

    monkeypatch.setattr(files.httpx, "AsyncClient", _Client)
    ctx = SimpleNamespace(
        bucket="bucket",
        jwt="jwt-user",
        settings=SimpleNamespace(
            cp_url="http://cp",
            minio_endpoint="http://minio",
            minio_access_key="a",
            minio_secret_key="b",
        ),
    )
    deploy = next(tool for tool in build_file_tools(ctx) if tool.name == "deploy_agent_source")

    result = json.loads(await deploy.ainvoke({"name": "demo-agent"}))

    assert result == {"ok": True, "deploy_id": "dpl_123"}
    assert posts == [
        {
            "url": "http://cp/v1/agents/demo-agent/source/deploy",
            "headers": {"authorization": "bearer jwt-user"},
        }
    ]
