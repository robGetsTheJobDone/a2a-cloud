"""Configured first-party Gitea repository mounts."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RepoMount:
    repo: str
    owner: str
    mount_path: str


def parse_repo_mounts(raw: str | None, *, default_owner: str) -> tuple[RepoMount, ...]:
    """Parse ``owner/repo`` or ``repo`` entries into ``repos/<repo>/`` mounts."""
    mounts: list[RepoMount] = []
    seen: set[tuple[str, str]] = set()
    for item in (raw or "").split(","):
        value = item.strip().strip("/")
        if not value:
            continue
        owner = default_owner
        repo = value
        if "/" in value:
            owner_part, repo_part = value.rsplit("/", 1)
            owner = owner_part.strip() or default_owner
            repo = repo_part.strip()
        if not repo or any(char in repo for char in "*?[]\\"):
            continue
        key = (owner, repo)
        if key in seen:
            continue
        seen.add(key)
        mounts.append(RepoMount(repo=repo, owner=owner, mount_path=f"repos/{repo}/"))
    return tuple(mounts)
