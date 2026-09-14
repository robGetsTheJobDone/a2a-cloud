"""Unit tests for :class:`a2a_pack.gitea_backend.GiteaBackend`.

These tests stub the Gitea API by overriding ``_request_json`` and ``_client``
on a subclass; they exercise the protocol surface (ls/read/write/edit/glob/grep
plus the branch/PR extras) without making real HTTP calls.
"""
from __future__ import annotations

import base64
from typing import Any

import pytest

from a2a_pack import GiteaBackend, GiteaError


class _FakeGitea(GiteaBackend):
    """Replace HTTP layer with an in-memory dict-of-files."""

    def __init__(
        self,
        files: dict[str, str | bytes],
        *,
        ref: str = "main",
    ) -> None:
        super().__init__(
            gitea_url="http://gitea.test",
            owner="agents",
            repo="hello",
            ref=ref,
            token="fake-token",
        )
        self._files: dict[str, bytes] = {
            p: (c.encode() if isinstance(c, str) else c) for p, c in files.items()
        }
        self.calls: list[tuple[str, str]] = []
        self.commits: list[dict[str, Any]] = []
        self.branches: list[dict[str, Any]] = []
        self.prs: list[dict[str, Any]] = []
        self.comments: list[dict[str, Any]] = []

    async def _request_json(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append((method, url))
        json_body = kwargs.get("json") or {}

        if method == "GET" and "/git/trees/" in url:
            return {
                "tree": [
                    {"path": p, "type": "blob", "size": len(self._files[p]), "sha": "stub"}
                    for p in sorted(self._files)
                ]
            }

        if method == "GET" and "/contents/" in url:
            path = url.split("/contents/", 1)[1]
            if path not in self._files:
                prefix = path.strip("/")
                if prefix:
                    prefix += "/"
                entries: dict[str, dict[str, Any]] = {}
                for file_path, content in self._files.items():
                    if prefix and not file_path.startswith(prefix):
                        continue
                    rest = file_path[len(prefix):]
                    if not rest:
                        continue
                    name = rest.split("/", 1)[0]
                    full = (prefix + name).strip("/")
                    if "/" in rest:
                        entries[full] = {
                            "type": "dir",
                            "path": full,
                            "name": name,
                            "size": 0,
                        }
                    else:
                        entries[full] = {
                            "type": "file",
                            "path": full,
                            "name": name,
                            "size": len(content),
                        }
                return [entries[key] for key in sorted(entries)] if entries else None
            return {
                "type": "file",
                "sha": "stub-sha",
                "encoding": "base64",
                "content": base64.b64encode(self._files[path]).decode(),
            }

        if method in {"POST", "PUT"} and "/contents/" in url:
            path = url.split("/contents/", 1)[1]
            decoded = base64.b64decode(json_body["content"])
            self._files[path] = decoded
            self.commits.append({
                "method": method,
                "path": path,
                "branch": json_body.get("branch"),
                "message": json_body.get("message"),
                "sha": json_body.get("sha"),
            })
            return {"content": {"path": path, "sha": "new-sha"}}

        if method == "DELETE" and "/contents/" in url:
            path = url.split("/contents/", 1)[1]
            self._files.pop(path, None)
            return {}

        if method == "POST" and url.endswith("/branches"):
            self.branches.append(json_body)
            return {"name": json_body["new_branch_name"]}

        if method == "POST" and url.endswith("/pulls"):
            self.prs.append(json_body)
            return {"number": 1, **json_body}

        if method == "POST" and "/commits/" in url and url.endswith("/comments"):
            self.comments.append(json_body)
            return {"id": 1, **json_body}

        raise AssertionError(f"unexpected request: {method} {url}")


@pytest.mark.asyncio
async def test_read_returns_file_content():
    g = _FakeGitea({"agent.py": "print('hi')\n"})
    result = await g.aread("agent.py")
    assert result.error is None
    # splitlines+join semantics (matches WorkspaceBackend) — trailing newline stripped.
    assert result.file_data["content"] == "print('hi')"
    assert result.file_data["encoding"] == "utf-8"


@pytest.mark.asyncio
async def test_read_missing_file_returns_error():
    g = _FakeGitea({"agent.py": "x"})
    result = await g.aread("nope.py")
    assert result.file_data is None
    assert "not found" in result.error


@pytest.mark.asyncio
async def test_write_new_file_creates_commit():
    g = _FakeGitea({})
    result = await g.awrite("new.py", "x = 1\n")
    assert result.error is None
    assert result.path == "/new.py"
    assert g._files["new.py"] == b"x = 1\n"
    assert g.commits and g.commits[0]["path"] == "new.py"
    assert g.commits[0]["branch"] == "main"
    assert g.commits[0]["method"] == "POST"
    assert g.commits[0]["sha"] is None


@pytest.mark.asyncio
async def test_write_existing_file_refused():
    g = _FakeGitea({"agent.py": "old"})
    result = await g.awrite("agent.py", "new")
    assert result.error and "already exists" in result.error


@pytest.mark.asyncio
async def test_edit_replaces_unique_match():
    g = _FakeGitea({"agent.py": "foo = 1\nfoo = 2\n"})
    result = await g.aedit("agent.py", "foo = 1", "foo = 99")
    assert result.error is None
    assert g._files["agent.py"] == b"foo = 99\nfoo = 2\n"
    assert g.commits[0]["method"] == "PUT"
    assert g.commits[0]["sha"] == "stub-sha"


@pytest.mark.asyncio
async def test_edit_refuses_non_unique_without_replace_all():
    g = _FakeGitea({"agent.py": "foo\nfoo\n"})
    result = await g.aedit("agent.py", "foo", "bar")
    assert result.error and "not unique" in result.error


@pytest.mark.asyncio
async def test_edit_replace_all():
    g = _FakeGitea({"agent.py": "foo\nfoo\n"})
    result = await g.aedit("agent.py", "foo", "bar", replace_all=True)
    assert result.error is None
    assert result.occurrences == 2
    assert g._files["agent.py"] == b"bar\nbar\n"


@pytest.mark.asyncio
async def test_ls_lists_top_level_entries():
    g = _FakeGitea({
        "agent.py": "x",
        "skills/one/SKILL.md": "y",
        "skills/two/SKILL.md": "z",
        "README.md": "r",
    })
    result = await g.als("/")
    paths = {e["path"] for e in result.entries}
    assert paths == {"/agent.py", "/README.md", "/skills/"}
    assert not any("/git/trees/" in url for _, url in g.calls)
    assert any("/contents/" in url for _, url in g.calls)


@pytest.mark.asyncio
async def test_glob_filters_by_pattern():
    g = _FakeGitea({"a.py": "x", "b.md": "y", "c.py": "z"})
    result = await g.aglob("*.py")
    paths = {m["path"] for m in result.matches}
    assert paths == {"/a.py", "/c.py"}


@pytest.mark.asyncio
async def test_grep_matches_line_content():
    g = _FakeGitea({"agent.py": "import x\ndef hello():\n    pass\n"})
    result = await g.agrep("hello")
    assert any(m["text"].startswith("def hello") for m in result.matches)


@pytest.mark.asyncio
async def test_grep_searches_exact_file_path():
    g = _FakeGitea({
        "large_tool_results/call_1": "acquisition-swarm\n",
        "large_tool_results/call_2": "no match\n",
    })

    result = await g.agrep("swarm", path="/large_tool_results/call_1")

    assert result.error is None
    assert result.matches == [
        {
            "path": "/large_tool_results/call_1",
            "line": 1,
            "text": "acquisition-swarm",
        }
    ]


@pytest.mark.asyncio
async def test_upload_files_creates_multiple_commits():
    g = _FakeGitea({})
    responses = await g.aupload_files([("a.py", b"1"), ("b.py", b"2")])
    assert [r.error for r in responses] == [None, None]
    assert g._files == {"a.py": b"1", "b.py": b"2"}
    assert len(g.commits) == 2


@pytest.mark.asyncio
async def test_download_files_returns_bytes():
    g = _FakeGitea({"a.py": "hi"})
    responses = await g.adownload_files(["a.py", "missing.py"])
    assert responses[0].content == b"hi"
    assert responses[1].error == "file_not_found"


@pytest.mark.asyncio
async def test_create_branch_records_payload():
    g = _FakeGitea({})
    await g.acreate_branch("feature/x", from_ref="main")
    assert g.branches == [{"new_branch_name": "feature/x", "old_branch_name": "main"}]


@pytest.mark.asyncio
async def test_create_pr_records_payload():
    g = _FakeGitea({})
    pr = await g.acreate_pr("Migrate SDK", head="migrate", base="main", body="why")
    assert g.prs and g.prs[0]["title"] == "Migrate SDK"
    assert pr["title"] == "Migrate SDK"


@pytest.mark.asyncio
async def test_add_commit_comment_records_payload():
    g = _FakeGitea({})
    await g.aadd_commit_comment("abc", "looks wrong", path="agent.py", line=10)
    assert g.comments[0] == {"body": "looks wrong", "path": "agent.py", "line": 10}


@pytest.mark.asyncio
async def test_switch_ref_invalidates_cache():
    g = _FakeGitea({"a.py": "x"})
    await g.aglob("*.py")  # warms the recursive tree cache
    cached = g._tree_cache
    assert cached is not None and "main" in cached
    g.switch_ref("feature/y")
    assert g._tree_cache is None
    assert g.ref == "feature/y"


@pytest.mark.asyncio
async def test_write_invalidates_tree_cache():
    g = _FakeGitea({"a.py": "x"})
    await g.aglob("*.py")
    assert g._tree_cache is not None
    await g.awrite("b.py", "y")
    assert g._tree_cache is None


def test_id_includes_owner_repo_ref():
    g = _FakeGitea({})
    assert g.id == "gitea-agents-hello-main"


def test_gitea_error_is_runtime_error():
    assert issubclass(GiteaError, RuntimeError)


@pytest.mark.asyncio
async def test_client_uses_token_authorization_header():
    g = GiteaBackend(
        gitea_url="http://gitea.test",
        owner="agents",
        repo="hello",
        token="secret-token",
    )
    client = await g._client()
    async with client:
        assert client.headers["Authorization"] == "token secret-token"
        assert client.headers["Accept"] == "application/json"
