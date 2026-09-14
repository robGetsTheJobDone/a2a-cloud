"""File CRUD: upload (with + without path), list, read, move, delete."""

from __future__ import annotations

import io
import secrets
import time

import httpx

from .conftest import API_URL, ApiClient, E2EUser


async def test_upload_to_root_lists_at_root(client: ApiClient) -> None:
    r = await client.post_form(
        "/v1/me/files",
        files={"file": ("hello.txt", io.BytesIO(b"hi"), "text/plain")},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["path"] == "hello.txt"
    assert body["size"] == 2

    listing = (await client.get("/v1/me/files")).json()
    paths = {f["path"] for f in listing}
    assert "hello.txt" in paths


async def test_upload_to_prefix_lands_in_subfolder(client: ApiClient) -> None:
    r = await client.post_form(
        "/v1/me/files",
        data={"path": "data/nested/x.csv"},
        files={"file": ("ignored.txt", io.BytesIO(b"col1,col2\n1,2\n"), "text/csv")},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    # `path` form overrides the multipart filename.
    assert body["path"] == "data/nested/x.csv"

    listing = (await client.get("/v1/me/files")).json()
    paths = {f["path"] for f in listing}
    assert "data/nested/x.csv" in paths


async def test_read_file_returns_content(client: ApiClient) -> None:
    payload = b"hello world\n"
    await client.post_form(
        "/v1/me/files",
        data={"path": "read-test.txt"},
        files={"file": ("read-test.txt", io.BytesIO(payload), "text/plain")},
    )
    r = await client.get("/v1/me/files/read-test.txt")
    assert r.status_code == 200
    assert r.content == payload


async def test_move_renames_object_within_bucket(client: ApiClient) -> None:
    suffix = secrets.token_hex(4)
    source = f"tmp/move-me-{suffix}.txt"
    destination = f"archive/moved-{suffix}.txt"
    await client.post_form(
        "/v1/me/files",
        data={"path": source},
        files={"file": ("move-me.txt", io.BytesIO(b"data"), "text/plain")},
    )
    r = await client.post_json(
        "/v1/me/files/move",
        body={"from": source, "to": destination},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["moved"] is True
    assert body["from"] == source
    assert body["path"] == destination

    paths = {f["path"] for f in (await client.get("/v1/me/files")).json()}
    assert destination in paths
    assert source not in paths


async def test_move_rejects_dotdot_traversal(client: ApiClient) -> None:
    await client.post_form(
        "/v1/me/files",
        data={"path": "trav/test.txt"},
        files={"file": ("test.txt", io.BytesIO(b"x"), "text/plain")},
    )
    r = await client.post_json(
        "/v1/me/files/move",
        body={"from": "trav/test.txt", "to": "../escape.txt"},
    )
    assert r.status_code == 400, r.text


async def test_delete_removes_file(client: ApiClient) -> None:
    await client.post_form(
        "/v1/me/files",
        data={"path": "delete-me.txt"},
        files={"file": ("delete-me.txt", io.BytesIO(b"bye"), "text/plain")},
    )
    r = await client.delete("/v1/me/files/delete-me.txt")
    assert r.status_code == 204

    paths = {f["path"] for f in (await client.get("/v1/me/files")).json()}
    assert "delete-me.txt" not in paths


async def test_upload_rejected_without_filename_or_path(client: ApiClient) -> None:
    # multipart with no filename — server should 400 or pick something safe.
    r = await client.post_form(
        "/v1/me/files",
        files={"file": ("", io.BytesIO(b"x"), "application/octet-stream")},
    )
    # Either path is fine — either it refuses (400) or it sanitizes; assert
    # we didn't accidentally end up with an empty-keyed object.
    if r.status_code == 201:
        assert r.json()["path"]
    else:
        assert r.status_code >= 400


async def test_isolation_between_users(
    test_user: E2EUser,
    client: ApiClient,
    other_user: E2EUser,
) -> None:
    """A second user gets a disjoint bucket — neither sees the other's files."""
    await client.post_form(
        "/v1/me/files",
        data={"path": "isolation/secret.txt"},
        files={"file": ("secret.txt", io.BytesIO(b"top secret"), "text/plain")},
    )

    suffix = f"{int(time.time())}-{secrets.token_hex(3)}"
    async with httpx.AsyncClient(base_url=API_URL, timeout=20.0, verify=False) as c:
        other_listing = await c.get(
            "/v1/me/files",
            headers={"Authorization": f"bearer {other_user.token}"},
            params={"nonce": suffix},
        )
    assert other_listing.status_code == 200
    other_paths = {f["path"] for f in other_listing.json()}
    assert "isolation/secret.txt" not in other_paths
