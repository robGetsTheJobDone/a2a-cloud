from __future__ import annotations

import json

from a2a_pack.cli import api_client


def test_control_plane_client_refreshes_once_after_401(monkeypatch):
    calls: list[str | None] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def request(self, method, url, headers=None, **kwargs):
            calls.append((headers or {}).get("authorization"))
            if len(calls) == 1:
                return api_client.httpx.Response(401, json={"detail": "expired"})
            return api_client.httpx.Response(200, json={"email": "dev@example.test"})

    monkeypatch.setattr(api_client.httpx, "Client", FakeClient)

    client = api_client.ControlPlaneClient(
        "https://api.example.test",
        token="old-token",
        refresh_token=lambda: "fresh-token",
    )

    assert client.me() == {"email": "dev@example.test"}
    assert calls == ["Bearer old-token", "Bearer fresh-token"]


def test_from_tarball_sends_agent_dsl(monkeypatch):
    posts: list[dict] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, url, headers=None, data=None, files=None):
            posts.append({"url": url, "headers": headers, "data": data, "files": files})
            return api_client.httpx.Response(
                200,
                json={
                    "name": "demo-agent",
                    "version": "0.1.0",
                    "status": "building",
                },
            )

    monkeypatch.setattr(api_client.httpx, "Client", FakeClient)

    client = api_client.ControlPlaneClient("https://api.example.test", token="token")
    out = client.from_tarball(
        name="demo-agent",
        version="0.1.0",
        entrypoint="agent:DemoAgent",
        description="Demo",
        public=True,
        tarball=b"tgz",
        agent_dsl={"schema_version": "2026-06-04", "name": "demo-agent"},
    )

    assert out["status"] == "building"
    assert posts[0]["data"]["agent_dsl"]
    assert json.loads(posts[0]["data"]["agent_dsl"]) == {
        "schema_version": "2026-06-04",
        "name": "demo-agent",
    }
    assert posts[0]["files"]["source"] == (
        "source.tar.gz",
        b"tgz",
        "application/gzip",
    )
