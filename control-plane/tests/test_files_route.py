from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastapi import Response

from control_plane.routes import files as files_route


@pytest.mark.asyncio
async def test_list_view_merges_minio_root_and_live_agents(monkeypatch) -> None:
    bucket = "user-1-files"
    user = SimpleNamespace(id=1)
    session = SimpleNamespace()

    monkeypatch.setattr(
        files_route,
        "list_children",
        lambda _bucket, prefix="": [
            {"path": "notes.txt", "size": 7, "modified_at": "2026-06-01T00:00:00+00:00", "content_type": "text/plain"},
            {"path": "agents/minio-shadow.txt", "size": 5, "modified_at": "", "content_type": ""},
            {"path": "projects", "size": 0, "modified_at": "", "content_type": "", "is_dir": True},
        ]
        if prefix == ""
        else [{"path": f"{prefix}/child.txt", "size": 1, "modified_at": "", "content_type": "text/plain"}],
    )
    async def fake_live_agent_repos(*_args, **_kwargs):
        return [("demo", "agents")]

    async def fake_gitea_children(owner, repo, prefix):
        del owner
        return [
            {
                "path": f"agents/{repo}/{prefix + '/' if prefix else ''}agent.py".rstrip("/"),
                "size": 11,
                "modified_at": "",
                "content_type": "text/x-python",
                "is_dir": False,
                "writable": False,
            }
        ]

    monkeypatch.setattr(files_route, "_live_agent_repos", fake_live_agent_repos)
    monkeypatch.setattr(files_route, "_gitea_children", fake_gitea_children)

    root_rows = await files_route._list_view(bucket, user, session, "")
    agents_rows = await files_route._list_view(bucket, user, session, "agents")
    repo_rows = await files_route._list_view(bucket, user, session, "agents/demo")
    nested_rows = await files_route._list_view(bucket, user, session, "projects")

    assert [row["path"] for row in root_rows] == ["agents", "notes.txt", "projects"]
    assert all(not row["path"].startswith("agents/minio-shadow") for row in root_rows)
    assert agents_rows == [
        {
            "path": "agents/demo",
            "size": 0,
            "modified_at": "",
            "content_type": "",
            "is_dir": True,
            "writable": False,
        }
    ]
    assert repo_rows == [
        {
            "path": "agents/demo/agent.py",
            "size": 11,
            "modified_at": "",
            "content_type": "text/x-python",
            "is_dir": False,
            "writable": False,
        }
    ]
    assert nested_rows == [
        {
            "path": "projects/child.txt",
            "size": 1,
            "modified_at": "",
            "content_type": "text/plain",
        }
    ]


@pytest.mark.asyncio
async def test_list_my_files_supports_limited_recursive_pages(monkeypatch) -> None:
    user = SimpleNamespace(id=1)
    session = SimpleNamespace()
    response = Response()
    captured: dict[str, object] = {}

    monkeypatch.setattr(files_route, "bucket_for_user", lambda _user_id: "user-1-files")

    def fake_list_files_page(bucket, *, limit, cursor):
        captured["bucket"] = bucket
        captured["limit"] = limit
        captured["cursor"] = cursor
        return (
            [
                {
                    "path": "data/input.csv",
                    "size": 10,
                    "modified_at": "2026-06-01T00:00:00+00:00",
                    "content_type": "text/csv",
                }
            ],
            "next-token",
        )

    monkeypatch.setattr(files_route, "list_files_page", fake_list_files_page)

    rows = await files_route.list_my_files(
        response=response,
        user=user,
        session=session,
        prefix=None,
        limit=1,
        cursor="start-token",
    )

    assert captured == {
        "bucket": "user-1-files",
        "limit": 1,
        "cursor": "start-token",
    }
    assert rows == [
        {
            "path": "data/input.csv",
            "size": 10,
            "modified_at": "2026-06-01T00:00:00+00:00",
            "content_type": "text/csv",
        }
    ]
    assert response.headers["X-A2A-Next-Cursor"] == "next-token"
    assert response.headers["X-A2A-Has-More"] == "true"


@pytest.mark.asyncio
async def test_list_my_files_defaults_to_bounded_recursive_page(monkeypatch) -> None:
    user = SimpleNamespace(id=1)
    session = SimpleNamespace()
    response = Response()
    captured: dict[str, object] = {}

    monkeypatch.setattr(files_route, "bucket_for_user", lambda _user_id: "user-1-files")

    def fake_list_files_page(bucket, *, limit, cursor):
        captured["bucket"] = bucket
        captured["limit"] = limit
        captured["cursor"] = cursor
        return ([], None)

    monkeypatch.setattr(files_route, "list_files_page", fake_list_files_page)

    rows = await files_route.list_my_files(
        response=response,
        user=user,
        session=session,
        prefix=None,
        limit=None,
        cursor=None,
    )

    assert rows == []
    assert captured == {
        "bucket": "user-1-files",
        "limit": files_route._DEFAULT_FILE_LIST_PAGE_LIMIT,
        "cursor": None,
    }
    assert response.headers["X-A2A-Has-More"] == "false"


@pytest.mark.asyncio
async def test_gitea_children_lists_requested_directory_without_recursive_tree(monkeypatch) -> None:
    calls: list[tuple[str, str, str]] = []

    async def fake_contents(owner: str, repo: str, prefix: str):
        calls.append((owner, repo, prefix))
        return [
            {"path": "src/main.py", "type": "file", "size": 12, "sha": "file-v1"},
            {"path": "src/lib", "type": "dir", "size": 0, "sha": "tree-v1"},
        ]

    monkeypatch.setattr(files_route, "_gitea_contents", fake_contents)

    rows = await files_route._gitea_children("agents", "demo", "src")

    assert calls == [("agents", "demo", "src")]
    assert rows == [
        {
            "path": "agents/demo/src/lib",
            "size": 0,
            "modified_at": "",
            "content_type": "",
            "etag": "tree-v1",
            "is_dir": True,
            "writable": False,
        },
        {
            "path": "agents/demo/src/main.py",
            "size": 12,
            "modified_at": "",
            "content_type": "",
            "etag": "file-v1",
            "is_dir": False,
            "writable": False,
        },
    ]


@pytest.mark.asyncio
async def test_download_percent_encodes_unicode_file_path_header(monkeypatch) -> None:
    user = SimpleNamespace(id=1)

    monkeypatch.setattr(files_route, "bucket_for_user", lambda _user_id: "user-1-files")
    monkeypatch.setattr(files_route, "stat_file", lambda _bucket, _key: {"modified_at": ""})
    monkeypatch.setattr(files_route, "iter_file", lambda _bucket, _key: (iter([b"snowman\n"]), "text/plain"))

    response = await files_route.download("notes/unicode-\u2603.txt", user=user)

    assert response.headers["X-A2A-File-Path"] == quote("notes/unicode-\u2603.txt", safe="/")
    response.headers["X-A2A-File-Path"].encode("latin-1")
