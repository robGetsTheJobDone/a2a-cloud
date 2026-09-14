from __future__ import annotations

import pytest

from control_plane.models import User
from control_plane.repo_mounts import RepoMount, parse_repo_mounts
from control_plane.routes import agents as agent_routes
from control_plane.routes import platform


def test_parse_repo_mounts_defaults_mount_path() -> None:
    assert parse_repo_mounts(
        "control-plane,gitea_admin/dashboard",
        default_owner="gitea_admin",
    ) == (
        RepoMount(repo="control-plane", owner="gitea_admin", mount_path="repos/control-plane/"),
        RepoMount(repo="dashboard", owner="gitea_admin", mount_path="repos/dashboard/"),
    )


@pytest.mark.asyncio
async def test_first_party_repo_mount_token_requires_admin(monkeypatch) -> None:
    monkeypatch.setattr(platform.settings, "repo_mounts", "gitea_admin/control-plane")
    admin = User(id=1, email="admin@example.com", password_hash="x", is_admin=True)
    user = User(id=2, email="user@example.com", password_hash="x", is_admin=False)

    assert await platform._assert_caller_can_access_repo(
        object(),  # type: ignore[arg-type]
        admin,
        owner="gitea_admin",
        repo="control-plane",
        scope="write",
    ) is None

    with pytest.raises(platform.HTTPException) as exc:
        await platform._assert_caller_can_access_repo(
            object(),  # type: ignore[arg-type]
            user,
            owner="gitea_admin",
            repo="control-plane",
            scope="read",
        )

    assert exc.value.status_code == 403


def test_first_party_repo_mount_code_editor_status_requires_admin(monkeypatch) -> None:
    monkeypatch.setattr(agent_routes.settings, "repo_mounts", "gitea_admin/control-plane")
    admin = User(id=1, email="admin@example.com", password_hash="x", is_admin=True)
    user = User(id=2, email="user@example.com", password_hash="x", is_admin=False)

    out = agent_routes._first_party_repo_code_editor_out("control-plane", admin)

    assert out is not None
    assert out.enabled is True
    assert out.target_agent_name == "control-plane"
    assert out.workspace_key == "gitea_admin/control-plane"

    with pytest.raises(agent_routes.HTTPException) as exc:
        agent_routes._first_party_repo_code_editor_out("control-plane", user)

    assert exc.value.status_code == 403
