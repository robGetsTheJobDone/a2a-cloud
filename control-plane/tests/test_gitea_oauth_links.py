from __future__ import annotations

import sys
from types import SimpleNamespace

from control_plane import gitea
from control_plane.config import settings


class _Cursor:
    def __init__(self, row: tuple[bool, bool, bool]) -> None:
        self.row = row
        self.sql = ""
        self.params = {}

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: dict) -> None:
        self.sql = sql
        self.params = params

    def fetchone(self) -> tuple[bool, bool, bool]:
        return self.row


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def cursor(self) -> _Cursor:
        return self._cursor


def test_ensure_user_oauth_link_upserts_external_login(monkeypatch) -> None:
    cursor = _Cursor((True, True, True))
    calls = []

    def fake_connect(dsn: str, *, connect_timeout: int) -> _Connection:
        calls.append((dsn, connect_timeout))
        return _Connection(cursor)

    monkeypatch.setattr(settings, "gitea_oauth_linking_enabled", True)
    monkeypatch.setattr(settings, "gitea_database_url", "postgresql://gitea:test@gitea/gitea")
    monkeypatch.setattr(settings, "gitea_oauth_auth_source_name", "A2A Cloud")
    monkeypatch.setattr(settings, "gitea_oauth_provider", "openidConnect")
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=fake_connect))

    linked = gitea.ensure_user_oauth_link(
        "owner-2",
        email="owner@example.com",
        keycloak_sub="kc-owner-2",
    )

    assert linked is True
    assert calls == [("postgresql://gitea:test@gitea/gitea", 5)]
    assert "external_login_user" in cursor.sql
    assert cursor.params["username"] == "owner-2"
    assert cursor.params["email"] == "owner@example.com"
    assert cursor.params["keycloak_sub"] == "kc-owner-2"
    assert cursor.params["auth_source_name"] == "A2A Cloud"


def test_ensure_user_oauth_link_skips_without_keycloak_sub(monkeypatch) -> None:
    monkeypatch.setattr(settings, "gitea_oauth_linking_enabled", True)
    monkeypatch.setattr(settings, "gitea_database_url", "postgresql://unused")

    assert (
        gitea.ensure_user_oauth_link(
            "owner-2",
            email="owner@example.com",
            keycloak_sub=None,
        )
        is False
    )
