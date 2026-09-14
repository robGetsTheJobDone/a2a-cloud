"""Gitea repo provisioning for agent source."""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import secrets
import subprocess
import tempfile
import tarfile
from pathlib import Path
from typing import Any

import httpx

from .config import settings

log = logging.getLogger(__name__)

GITEA_PUBLIC = os.environ.get("A2A_CP_GITEA_PUBLIC", f"https://gitea.{settings.platform_domain}")
GITEA_INTERNAL = os.environ.get(
    "A2A_CP_GITEA_INTERNAL",
    "http://gitea-http.gitea.svc.cluster.local:3000",
)
GITEA_USER = os.environ.get("A2A_CP_GITEA_USER", "gitea_admin")
GITEA_PASS = os.environ.get("A2A_CP_GITEA_PASS", "gitea_admin")
GITEA_REPO_DESCRIPTION_MAX = 255

_PLATFORM_SOURCE_PREFIXES = ("deploy/", ".gitea/", ".a2a-platform/", ".git/")
_PLATFORM_SOURCE_FILES = {"Dockerfile", ".dockerignore", ".a2a-builder-state.json"}
_RUNTIME_REPO_SUFFIX = "-runtime"
_GITEA_USERNAME_RE = re.compile(r"[^a-z0-9-]+")


class RepoDriftCheckError(RuntimeError):
    """Raised when the managed source repo cannot be inspected."""


class RepoBaseMissingError(RepoDriftCheckError):
    """Raised when the requested comparison base is absent from the repo."""


def _repo_owner(owner: str | None = None) -> str:
    return (owner or GITEA_USER).strip()


def gitea_username_for_user_email(email: str, *, user_id: int | None = None) -> str:
    local = (email.split("@", 1)[0] or "").lower()
    base = _GITEA_USERNAME_RE.sub("-", local).strip("-") or "user"
    if user_id is not None:
        suffix = f"-{user_id}"
        max_base = max(1, 39 - len(suffix))
        return f"{base[:max_base].rstrip('-') or 'user'}{suffix}"
    if len(base) <= 39:
        return base
    digest = hashlib.sha256(email.lower().encode("utf-8")).hexdigest()[:8]
    return f"{base[:30].rstrip('-')}-{digest}"


def ensure_user_account(
    username: str,
    *,
    email: str,
    full_name: str | None = None,
) -> dict[str, Any]:
    """Create the Gitea user account if missing and return its API object."""
    auth = (GITEA_USER, GITEA_PASS)
    api_base = GITEA_INTERNAL.rstrip("/")
    with httpx.Client(timeout=10.0) as c:
        r = c.get(f"{api_base}/api/v1/users/{username}", auth=auth)
        if r.status_code == 200:
            data = r.json()
            return data if isinstance(data, dict) else {"username": username}
        if r.status_code != 404:
            r.raise_for_status()

        payload = {
            "username": username,
            "email": email,
            "full_name": full_name or username,
            "password": secrets.token_urlsafe(32),
            "must_change_password": False,
            "visibility": "private",
            "login_name": username,
        }
        r = c.post(f"{api_base}/api/v1/admin/users", auth=auth, json=payload)
        if r.status_code == 422:
            existing = c.get(f"{api_base}/api/v1/users/{username}", auth=auth)
            if existing.status_code == 200:
                data = existing.json()
                return data if isinstance(data, dict) else {"username": username}
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, dict) else {"username": username}


def ensure_user_oauth_link(
    username: str,
    *,
    email: str,
    keycloak_sub: str | None,
) -> bool:
    """Ensure a personal Gitea user can sign in through the Keycloak auth source.

    Gitea's REST API does not expose external account links, so this writes the
    same ``external_login_user`` row that Gitea creates after a successful OAuth
    account-link flow.
    """
    sub = (keycloak_sub or "").strip()
    if not settings.gitea_oauth_linking_enabled or not sub:
        return False
    if not settings.gitea_database_url:
        log.warning("Gitea OAuth link skipped for %s: A2A_CP_GITEA_DATABASE_URL is unset", username)
        return False

    import psycopg

    raw_data = json.dumps(
        {
            "linked_by": "control-plane",
            "control_plane_email": email,
            "gitea_owner": username,
        },
        separators=(",", ":"),
    )
    sql = """
WITH auth_source AS (
  SELECT id
  FROM login_source
  WHERE name = %(auth_source_name)s
    AND type = 6
    AND is_active = true
  LIMIT 1
),
gitea_user AS (
  SELECT id, name
  FROM public."user"
  WHERE lower_name = lower(%(username)s)
    AND type = 0
  LIMIT 1
),
upsert AS (
  INSERT INTO external_login_user (
    external_id,
    user_id,
    login_source_id,
    provider,
    email,
    name,
    nick_name,
    raw_data
  )
  SELECT
    %(keycloak_sub)s,
    gitea_user.id,
    auth_source.id,
    %(provider)s,
    %(email)s,
    gitea_user.name,
    gitea_user.name,
    %(raw_data)s::json
  FROM auth_source
  CROSS JOIN gitea_user
  ON CONFLICT (external_id, login_source_id) DO UPDATE
  SET
    user_id = EXCLUDED.user_id,
    provider = EXCLUDED.provider,
    email = EXCLUDED.email,
    name = EXCLUDED.name,
    nick_name = EXCLUDED.nick_name,
    raw_data = EXCLUDED.raw_data
  RETURNING user_id
)
SELECT
  EXISTS (SELECT 1 FROM auth_source) AS has_auth_source,
  EXISTS (SELECT 1 FROM gitea_user) AS has_gitea_user,
  EXISTS (SELECT 1 FROM upsert) AS linked;
"""
    params = {
        "auth_source_name": settings.gitea_oauth_auth_source_name,
        "provider": settings.gitea_oauth_provider,
        "username": username,
        "email": email,
        "keycloak_sub": sub,
        "raw_data": raw_data,
    }
    with psycopg.connect(settings.gitea_database_url, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
    if row is None:
        return False
    has_auth_source, has_gitea_user, linked = row
    if not has_auth_source:
        raise RuntimeError(
            f"Gitea OAuth source {settings.gitea_oauth_auth_source_name!r} was not found"
        )
    if not has_gitea_user:
        raise RuntimeError(f"Gitea user {username!r} was not found")
    return bool(linked)


def runtime_repo_name(name: str) -> str:
    return f"{name}{_RUNTIME_REPO_SUFFIX}"


def is_runtime_repo_name(name: str) -> bool:
    return name.endswith(_RUNTIME_REPO_SUFFIX)


def agent_name_from_runtime_repo(name: str) -> str | None:
    if not is_runtime_repo_name(name):
        return None
    agent_name = name[: -len(_RUNTIME_REPO_SUFFIX)]
    return agent_name or None


def repo_urls(name: str, *, owner: str | None = None) -> tuple[str, str]:
    """Return user-safe public and Argo/internal repository URLs."""
    repo_owner = _repo_owner(owner)
    public_url = f"{GITEA_PUBLIC.rstrip('/')}/{repo_owner}/{name}.git"
    internal_url = f"{GITEA_INTERNAL}/{repo_owner}/{name}.git"
    return public_url, internal_url


def repo_web_url(name: str, *, owner: str | None = None) -> str:
    repo_owner = _repo_owner(owner)
    return f"{GITEA_PUBLIC.rstrip('/')}/{repo_owner}/{name}"


def authenticated_repo_url(name: str, *, owner: str | None = None) -> str:
    """Return a credentialed internal URL for trusted server-side git calls."""
    repo_owner = _repo_owner(owner)
    internal_host = GITEA_INTERNAL.removeprefix("http://").removeprefix("https://")
    return f"http://{GITEA_USER}:{GITEA_PASS}@{internal_host}/{repo_owner}/{name}.git"


def create_repo_clone_token(username: str, *, name_hint: str) -> tuple[str, str]:
    """Mint a fresh, read-scoped Gitea access token for ``username`` via the
    admin API, so a dev box can clone the agent repo without ever seeing the
    admin password. Treat as short-lived; revoke on teardown (``revoke_token``).

    Returns ``(token, token_name)``.
    """
    auth = (GITEA_USER, GITEA_PASS)
    api_base = GITEA_INTERNAL.rstrip("/")
    token_name = f"devbox-{name_hint}-{secrets.token_hex(4)}"
    payload = {"name": token_name, "scopes": ["read:repository"]}
    with httpx.Client(timeout=10.0) as c:
        r = c.post(f"{api_base}/api/v1/users/{username}/tokens", auth=auth, json=payload)
        r.raise_for_status()
        data = r.json()
    token = str(data.get("sha1") or "")
    if not token:
        raise RuntimeError("gitea token response missing sha1")
    return token, token_name


def revoke_token(username: str, token_name: str) -> None:
    """Best-effort revoke of a token minted by ``create_repo_clone_token``."""
    auth = (GITEA_USER, GITEA_PASS)
    api_base = GITEA_INTERNAL.rstrip("/")
    try:
        with httpx.Client(timeout=10.0) as c:
            c.delete(f"{api_base}/api/v1/users/{username}/tokens/{token_name}", auth=auth)
    except Exception as exc:  # noqa: BLE001
        log.warning("gitea: token revoke failed for %s/%s: %s", username, token_name, exc)


def repo_exists(name: str, *, owner: str | None = None) -> bool:
    repo_owner = _repo_owner(owner)
    auth = (GITEA_USER, GITEA_PASS)
    with httpx.Client(timeout=10.0) as c:
        r = c.get(f"{GITEA_INTERNAL}/api/v1/repos/{repo_owner}/{name}", auth=auth)
    if r.status_code == 404:
        return False
    r.raise_for_status()
    return True


def repo_head_sha(name: str, *, owner: str | None = None) -> str | None:
    repo_owner = _repo_owner(owner)
    auth = (GITEA_USER, GITEA_PASS)
    with httpx.Client(timeout=10.0) as c:
        r = c.get(
            f"{GITEA_INTERNAL}/api/v1/repos/{repo_owner}/{name}/branches/main",
            auth=auth,
        )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    data = r.json()
    commit = data.get("commit") if isinstance(data, dict) else None
    if not isinstance(commit, dict):
        return None
    sha = commit.get("id") or commit.get("sha")
    return sha if isinstance(sha, str) and sha else None


def ensure_runtime_repo_actions_secrets(name: str) -> None:
    """Install registry and source-clone credentials on a hidden runtime repo."""
    if not is_runtime_repo_name(name):
        raise ValueError("Actions registry credentials are restricted to runtime repos")
    username = (settings.registry_actions_username or "").strip()
    password = settings.registry_actions_password or ""
    if username != "registry-push" or not password:
        raise RuntimeError("runtime repository registry Actions credentials are unavailable")
    if not GITEA_USER or not GITEA_PASS:
        raise RuntimeError("runtime repository source clone credentials are unavailable")
    auth = (GITEA_USER, GITEA_PASS)
    api_base = GITEA_INTERNAL.rstrip("/")
    owner = _repo_owner()
    with httpx.Client(timeout=10.0) as client:
        for secret_name, value in (
            ("REGISTRY_USERNAME", username),
            ("REGISTRY_PASSWORD", password),
            ("SOURCE_REPO_USERNAME", GITEA_USER),
            ("SOURCE_REPO_PASSWORD", GITEA_PASS),
        ):
            response = client.put(
                f"{api_base}/api/v1/repos/{owner}/{name}/actions/secrets/{secret_name}",
                auth=auth,
                json={"data": value},
            )
            response.raise_for_status()


def action_build_status(
    name: str,
    *,
    owner: str | None = None,
    head_sha: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Best-effort fetch of Gitea Actions build status/logs for a repo.

    Gitea's Actions API surface varies by version and job logs are not always
    exposed over the stable REST API. This returns whatever is available and
    always degrades gracefully:

        {
          "available": bool,        # tasks endpoint responded
          "reason": str | None,     # why unavailable
          "web_url": str,           # deep link to the Actions tab
          "run": {status, conclusion, workflow, run_number, sha} | None,
          "log": str | None,        # raw log text when the API exposes it
        }
    """
    repo_owner = _repo_owner(owner)
    auth = (GITEA_USER, GITEA_PASS)
    web_url = f"{repo_web_url(name, owner=owner)}/actions"
    result: dict[str, Any] = {
        "available": False,
        "reason": None,
        "web_url": web_url,
        "run": None,
        "log": None,
    }
    try:
        with httpx.Client(timeout=10.0) as c:
            r = c.get(
                f"{GITEA_INTERNAL}/api/v1/repos/{repo_owner}/{name}/actions/tasks",
                params={"limit": limit},
                auth=auth,
            )
        if r.status_code in (403, 404, 501):
            result["reason"] = "gitea actions API not available"
            return result
        r.raise_for_status()
        payload = r.json() if r.content else {}
    except Exception as exc:  # noqa: BLE001
        result["reason"] = f"gitea actions lookup failed: {str(exc)[:120]}"
        return result

    tasks = payload.get("workflow_runs") or payload.get("tasks") or []
    if not isinstance(tasks, list):
        tasks = []
    result["available"] = True

    run = _match_action_task(tasks, head_sha)
    if run is None:
        result["reason"] = "no matching Actions run yet"
        return result
    result["run"] = {
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "workflow": run.get("name") or run.get("workflow_id"),
        "run_number": run.get("run_number") or run.get("index"),
        "sha": run.get("head_sha") or run.get("sha"),
    }
    return result


def _match_action_task(
    tasks: list[dict[str, Any]],
    head_sha: str | None,
) -> dict[str, Any] | None:
    if not tasks:
        return None
    if head_sha:
        for task in tasks:
            if not isinstance(task, dict):
                continue
            sha = task.get("head_sha") or task.get("sha")
            if isinstance(sha, str) and sha.startswith(head_sha[:12]):
                return task
    # Fall back to the most recent task.
    for task in tasks:
        if isinstance(task, dict):
            return task
    return None


def is_user_source_path(path: str) -> bool:
    rel = path.strip()
    while rel.startswith("./"):
        rel = rel[2:]
    if not rel:
        return False
    if rel in _PLATFORM_SOURCE_FILES:
        return False
    return not rel.startswith(_PLATFORM_SOURCE_PREFIXES)


def source_changed_paths_since(
    name: str,
    base_sha: str,
    *,
    owner: str | None = None,
) -> list[str]:
    """Return user-authored paths that changed since ``base_sha``.

    Gitea Actions mutates platform-owned paths after source commits
    (notably ``deploy/20-deployment.yaml``). Those commits should not make
    agent-builder workspaces look stale, but edits to ``agent.py``,
    ``requirements.txt``, ``a2a.yaml``, or user modules must block a stale
    MinIO workspace from force-overwriting the repo.
    """
    push_url = (
        authenticated_repo_url(name)
        if owner is None
        else authenticated_repo_url(name, owner=owner)
    )
    try:
        with tempfile.TemporaryDirectory(prefix="a2a-repo-drift-") as tmp:
            workdir = Path(tmp) / "repo"
            _git(None, "clone", "--quiet", "--no-checkout", push_url, str(workdir))
            try:
                _git(workdir, "cat-file", "-e", f"{base_sha}^{{commit}}")
            except subprocess.CalledProcessError as exc:
                raise RepoBaseMissingError(
                    f"base commit {base_sha} is not present in source repo {name!r}: "
                    f"{_format_git_error(exc)}"
                ) from exc
            out = _git(workdir, "diff", "--name-only", base_sha, "origin/main")
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RepoDriftCheckError(_format_git_error(exc)) from exc
    return [path for path in out.splitlines() if is_user_source_path(path)]


def source_tarball_from_repo(
    name: str,
    *,
    owner: str | None = None,
) -> tuple[bytes, str]:
    """Export current user-authored source files from the managed repo."""
    push_url = (
        authenticated_repo_url(name)
        if owner is None
        else authenticated_repo_url(name, owner=owner)
    )
    try:
        with tempfile.TemporaryDirectory(prefix="a2a-repo-source-") as tmp:
            workdir = Path(tmp) / "repo"
            _git(None, "clone", "--quiet", "--depth", "1", push_url, str(workdir))
            head_sha = _git(workdir, "rev-parse", "HEAD").strip()
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tf:
                for path in sorted(workdir.rglob("*")):
                    if not path.is_file():
                        continue
                    rel = path.relative_to(workdir).as_posix()
                    if not is_user_source_path(rel):
                        continue
                    body = path.read_bytes()
                    info = tarfile.TarInfo(rel)
                    info.size = len(body)
                    info.mode = 0o644
                    tf.addfile(info, io.BytesIO(body))
            return buf.getvalue(), head_sha
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RepoDriftCheckError(_format_git_error(exc)) from exc


def delete_repo(name: str, *, owner: str | None = None) -> None:
    """Delete the managed Gitea repo if it still exists."""
    repo_owner = _repo_owner(owner)
    auth = (GITEA_USER, GITEA_PASS)
    with httpx.Client(timeout=10.0) as c:
        r = c.delete(f"{GITEA_INTERNAL}/api/v1/repos/{repo_owner}/{name}", auth=auth)
    if r.status_code in (204, 404):
        return
    r.raise_for_status()


def transfer_repo(
    name: str,
    *,
    from_owner: str | None,
    to_owner: str,
) -> None:
    """Transfer one managed source repo between provisioned tenant owners."""
    source_owner = _repo_owner(from_owner)
    target_owner = _repo_owner(to_owner)
    if source_owner == target_owner:
        return
    auth = (GITEA_USER, GITEA_PASS)
    api_base = GITEA_INTERNAL.rstrip("/")
    with httpx.Client(timeout=20.0) as client:
        response = client.post(
            f"{api_base}/api/v1/repos/{source_owner}/{name}/transfer",
            auth=auth,
            json={"new_owner": target_owner},
        )
        if response.status_code in {200, 201, 202, 204}:
            return
        # An idempotent retry may observe the repository at its destination.
        destination = client.get(
            f"{api_base}/api/v1/repos/{target_owner}/{name}",
            auth=auth,
        )
        if destination.status_code == 200:
            return
        response.raise_for_status()


def _git(cwd: Path | None, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd is not None else None,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _format_git_error(exc: BaseException) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        parts = [str(exc)]
        stderr = (exc.stderr or "").strip()
        stdout = (exc.stdout or "").strip()
        if stderr:
            parts.append(f"stderr: {stderr}")
        if stdout:
            parts.append(f"stdout: {stdout}")
        return "; ".join(parts)
    return str(exc)


def gitea_org_url(name: str) -> str:
    return f"{GITEA_PUBLIC.rstrip('/')}/{name}"


def ensure_organization(
    name: str,
    *,
    full_name: str | None = None,
    description: str = "",
) -> dict[str, Any]:
    """Create the Gitea organization if missing and return its API object."""
    auth = (GITEA_USER, GITEA_PASS)
    api_base = GITEA_INTERNAL.rstrip("/")
    payload = {
        "username": name,
        "full_name": full_name or name,
        "description": description,
        "visibility": "private",
        "repo_admin_change_team_access": False,
    }
    with httpx.Client(timeout=10.0) as c:
        r = c.get(f"{api_base}/api/v1/orgs/{name}", auth=auth)
        if r.status_code == 200:
            data = r.json()
            return data if isinstance(data, dict) else {"username": name}
        if r.status_code != 404:
            r.raise_for_status()

        r = c.post(f"{api_base}/api/v1/orgs", auth=auth, json=payload)
        if r.status_code in (404, 405):
            r = c.post(
                f"{api_base}/api/v1/admin/users/{GITEA_USER}/orgs",
                auth=auth,
                json=payload,
            )
        if r.status_code == 422:
            existing = c.get(f"{api_base}/api/v1/orgs/{name}", auth=auth)
            if existing.status_code == 200:
                data = existing.json()
                return data if isinstance(data, dict) else {"username": name}
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, dict) else {"username": name}


def ensure_repo(
    name: str,
    description: str = "",
    *,
    owner: str | None = None,
    public: bool | None = None,
) -> tuple[str, str]:
    """Create the repo if missing and optionally enforce its visibility.

    ``public=None`` preserves the visibility of an existing repository and
    creates a new repository privately. Source-deployment callers pass the
    agent's public flag; runtime and workspace repositories remain private.
    """
    repo_owner = _repo_owner(owner)
    auth = (GITEA_USER, GITEA_PASS)
    api_base = GITEA_INTERNAL.rstrip("/")
    repo_url = f"{api_base}/api/v1/repos/{repo_owner}/{name}"
    with httpx.Client(timeout=10.0) as c:
        r = c.get(repo_url, auth=auth)
        if r.status_code == 404:
            payload = {
                "name": name,
                "description": _repo_description(description or f"agent {name}"),
                "auto_init": False,
                "private": not public if public is not None else True,
                "default_branch": "main",
            }
            if repo_owner == GITEA_USER:
                r = c.post(f"{api_base}/api/v1/user/repos", auth=auth, json=payload)
            else:
                r = c.post(
                    f"{api_base}/api/v1/orgs/{repo_owner}/repos",
                    auth=auth,
                    json=payload,
                )
                if r.status_code in (404, 405):
                    r = c.post(
                        f"{api_base}/api/v1/org/{repo_owner}/repos",
                        auth=auth,
                        json=payload,
                    )
                if r.status_code in (404, 405, 422):
                    r = c.post(
                        f"{api_base}/api/v1/admin/users/{repo_owner}/repos",
                        auth=auth,
                        json=payload,
                    )
            if r.status_code >= 400:
                # Gitea can return a server error when concurrent callers race
                # to create the same repo. Treat that as success if the repo is
                # visible after the failed create attempt.
                existing = c.get(repo_url, auth=auth)
                if existing.status_code != 200:
                    r.raise_for_status()
                r = existing
        elif r.status_code >= 400:
            r.raise_for_status()
        if public is not None:
            if public:
                ensure_repo_owner_public(repo_owner)
            data = r.json() if r.content else {}
            current_private = data.get("private") if isinstance(data, dict) else None
            desired_private = not public
            if current_private is None or bool(current_private) != desired_private:
                updated = c.patch(
                    repo_url,
                    auth=auth,
                    json={"private": desired_private},
                )
                updated.raise_for_status()
    return authenticated_repo_url(name, owner=repo_owner), repo_urls(name, owner=repo_owner)[1]


def set_repo_visibility(
    name: str,
    *,
    public: bool,
    owner: str | None = None,
) -> bool:
    """Synchronize one existing managed repository with agent visibility.

    Returns ``False`` when the agent has no managed source repository. This is
    expected for externally imported agents and direct image registrations.
    """
    repo_owner = _repo_owner(owner)
    auth = (GITEA_USER, GITEA_PASS)
    repo_url = f"{GITEA_INTERNAL.rstrip('/')}/api/v1/repos/{repo_owner}/{name}"
    desired_private = not public
    with httpx.Client(timeout=10.0) as c:
        current = c.get(repo_url, auth=auth)
        if current.status_code == 404:
            return False
        current.raise_for_status()
        if public:
            ensure_repo_owner_public(repo_owner)
        data = current.json()
        current_private = data.get("private") if isinstance(data, dict) else None
        if current_private is not None and bool(current_private) == desired_private:
            return True
        updated = c.patch(
            repo_url,
            auth=auth,
            json={"private": desired_private},
        )
        updated.raise_for_status()
    return True


def ensure_repo_owner_public(owner: str | None = None) -> None:
    """Make a managed repository owner visible to anonymous Gitea users.

    Gitea hides even a public repository when its owning user or organization
    has private visibility. Private repositories beneath the same owner remain
    private; this only makes the owner profile and public repo paths resolvable.
    """
    repo_owner = _repo_owner(owner)
    auth = (GITEA_USER, GITEA_PASS)
    api_base = GITEA_INTERNAL.rstrip("/")
    with httpx.Client(timeout=10.0) as c:
        organization = c.get(
            f"{api_base}/api/v1/orgs/{repo_owner}",
            auth=auth,
        )
        if organization.status_code == 200:
            data = organization.json()
            if isinstance(data, dict) and data.get("visibility") == "public":
                return
            updated = c.patch(
                f"{api_base}/api/v1/orgs/{repo_owner}",
                auth=auth,
                json={"visibility": "public"},
            )
            updated.raise_for_status()
            return
        if organization.status_code != 404:
            organization.raise_for_status()

        user = c.get(
            f"{api_base}/api/v1/users/{repo_owner}",
            auth=auth,
        )
        user.raise_for_status()
        data = user.json()
        if isinstance(data, dict) and data.get("visibility") == "public":
            return
        updated = c.patch(
            f"{api_base}/api/v1/admin/users/{repo_owner}",
            auth=auth,
            json={"login_name": repo_owner, "visibility": "public"},
        )
        updated.raise_for_status()


def _repo_description(value: str) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= GITEA_REPO_DESCRIPTION_MAX:
        return text
    return text[: GITEA_REPO_DESCRIPTION_MAX - 3].rstrip() + "..."


def ensure_repo_push_webhook(
    name: str,
    *,
    owner: str | None = None,
    url: str | None,
    secret: str | None,
    allow_runtime_repo: bool = False,
) -> dict[str, Any] | None:
    """Ensure a managed source repo has one active push webhook."""
    if (is_runtime_repo_name(name) and not allow_runtime_repo) or not url or not secret:
        return None
    repo_owner = _repo_owner(owner)
    auth = (GITEA_USER, GITEA_PASS)
    api_base = GITEA_INTERNAL.rstrip("/")
    target_url = url.strip()
    payload = {
        "type": "gitea",
        "active": True,
        "events": ["push"],
        "branch_filter": "*",
        "config": {
            "url": target_url,
            "content_type": "json",
            "secret": secret,
        },
    }
    with httpx.Client(timeout=10.0) as c:
        hooks = c.get(f"{api_base}/api/v1/repos/{repo_owner}/{name}/hooks", auth=auth)
        hooks.raise_for_status()
        for hook in _hook_items(hooks.json()):
            if _hook_url(hook) != target_url:
                continue
            hook_id = hook.get("id")
            if hook_id is None:
                return hook
            updated = c.patch(
                f"{api_base}/api/v1/repos/{repo_owner}/{name}/hooks/{hook_id}",
                auth=auth,
                json=payload,
            )
            updated.raise_for_status()
            data = updated.json()
            return data if isinstance(data, dict) else hook
        created = c.post(
            f"{api_base}/api/v1/repos/{repo_owner}/{name}/hooks",
            auth=auth,
            json=payload,
        )
        created.raise_for_status()
        data = created.json()
        return data if isinstance(data, dict) else {}


def ensure_runtime_repo_push_webhook(
    name: str,
    *,
    owner: str | None = None,
    url: str | None,
    secret: str | None,
) -> dict[str, Any] | None:
    """Ensure a hidden runtime repo has one active push webhook."""
    if not is_runtime_repo_name(name):
        return None
    return ensure_repo_push_webhook(
        name,
        owner=owner,
        url=url,
        secret=secret,
        allow_runtime_repo=True,
    )


def _hook_items(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict):
        value = raw.get("data") or raw.get("hooks")
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _hook_url(hook: dict[str, Any]) -> str | None:
    config = hook.get("config")
    if not isinstance(config, dict):
        return None
    url = config.get("url")
    return url.strip() if isinstance(url, str) else None
