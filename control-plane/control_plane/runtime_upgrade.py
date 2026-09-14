from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
import re
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import yaml

from .config import settings
from .scaffold import commit_and_push_runtime_from_repo

log = logging.getLogger(__name__)

A2A_PACK_PACKAGE = "a2a-pack"
# Fallback only — used when the registry can't be reached and nothing is cached.
DEFAULT_A2A_PACK_VERSION = "0.1.94"
_SEMVER_TAG_RE = re.compile(r"^\d+\.\d+\.\d+$")
_DOCKER_TAG_UNSAFE_RE = re.compile(r"[^a-z0-9_.-]+")
_LATEST_CACHE_TTL_SECONDS = 300.0
# Module-level memo so repeated runtime_upgrade_status() calls (every agent
# detail/list render) don't hit the registry each time.
_latest_cache: dict[str, Any] = {"version": None, "at": 0.0}


def latest_a2a_pack_version() -> str:
    """Resolve the latest published a2a-pack version.

    Precedence: explicit ``A2A_CP_A2A_PACK_VERSION`` override → highest semver
    tag of the ``a2a-pack-base`` image in the registry (cached) → last known
    value → ``DEFAULT_A2A_PACK_VERSION``. Auto-detection means publishing a new
    a2a-pack build is enough to offer the runtime upgrade; no env bump needed.
    """
    pinned = os.environ.get("A2A_CP_A2A_PACK_VERSION")
    if pinned:
        return pinned
    now = time.monotonic()
    cached = _latest_cache["version"]
    if cached and (now - _latest_cache["at"]) < _LATEST_CACHE_TTL_SECONDS:
        return cached
    detected = (
        _detect_latest_from_registry()
        if settings.a2a_pack_registry_auto_detect
        else None
    )
    if detected:
        _latest_cache["version"] = detected
        _latest_cache["at"] = now
        return detected
    return cached or DEFAULT_A2A_PACK_VERSION


def _detect_latest_from_registry() -> str | None:
    """Return the highest semver tag of the a2a-pack-base image, or None."""
    url = (
        f"{settings.a2a_pack_registry_url.rstrip('/')}"
        f"/v2/{settings.a2a_pack_image_repo.strip('/')}/tags/list"
    )
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - trusted internal registry
            data = json.load(resp)
    except Exception as exc:  # noqa: BLE001
        log.debug("a2a-pack latest-version registry lookup failed: %s", exc)
        return None
    tags = data.get("tags") if isinstance(data, dict) else None
    versions = [t for t in (tags or []) if isinstance(t, str) and _SEMVER_TAG_RE.match(t)]
    if not versions:
        return None
    return max(versions, key=_version_key)


def current_a2a_pack_version(card: dict[str, Any] | None) -> str | None:
    if not isinstance(card, dict):
        return None
    capabilities = card.get("capabilities")
    if isinstance(capabilities, dict):
        for key in ("a2a_pack", "a2a-pack", "a2aPack"):
            value = _version_from_mapping(capabilities.get(key))
            if value:
                return value
    for key in ("sdk", "a2a_pack", "a2a-pack", "platform"):
        value = _version_from_mapping(card.get(key))
        if value:
            return value
    runtime = card.get("runtime")
    if isinstance(runtime, dict):
        value = runtime.get("a2a_pack_version") or runtime.get("sdk_version")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def runtime_upgrade_status(
    *,
    name: str,
    image: str,
    card: dict[str, Any] | None,
    latest_version: str | None = None,
) -> dict[str, Any]:
    latest = latest_version or latest_a2a_pack_version()
    current = current_a2a_pack_version(card)
    update_available = current is None or _compare_versions(current, latest) < 0
    managed_image = image.startswith(f"registry.a2acloud.io/agents/{name}:")
    if current is None:
        message = "Agent does not report an a2a-pack version yet."
    elif update_available:
        message = f"Agent is on a2a-pack {current}; latest is {latest}."
    else:
        message = f"Agent is on the latest a2a-pack runtime ({latest})."
    if update_available and not managed_image:
        message = "Agent image is external; redeploy it from source to upgrade."
    return {
        "package": A2A_PACK_PACKAGE,
        "current_version": current,
        "latest_version": latest,
        "update_available": update_available,
        "can_redeploy": update_available and managed_image,
        "message": message,
    }


def bump_agent_runtime_repo(
    *,
    name: str,
    source_repo_url: str,
    source_sha: str,
    runtime_repo_url: str,
    latest_version: str | None = None,
    image_tag: str | None = None,
) -> str:
    latest = latest_version or latest_a2a_pack_version()
    base_image_tag = _docker_tag_component(latest, default="latest")
    deploy_image_tag = image_tag or runtime_upgrade_image_tag(source_sha, latest)
    entrypoint = _read_entrypoint_from_repo(source_repo_url, source_sha)
    runtime_sha = commit_and_push_runtime_from_repo(
        name=name,
        entrypoint=entrypoint,
        source_repo_url=source_repo_url,
        source_sha=source_sha,
        push_url=runtime_repo_url,
        image_tag=deploy_image_tag,
        base_image_tag=base_image_tag,
        extra_files={
            ".a2acloud/runtime-upgrade.json": json.dumps(
                {
                    "package": A2A_PACK_PACKAGE,
                    "target_version": latest,
                    "base_image_tag": base_image_tag,
                    "source_sha": source_sha,
                    "image_tag": deploy_image_tag,
                    "requested_at": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        },
    )
    return runtime_sha


def runtime_upgrade_image_tag(
    source_sha: str,
    latest_version: str,
    *,
    nonce: str | None = None,
) -> str:
    """Return a unique immutable image tag for a runtime-only rebuild."""
    source = _docker_tag_component(source_sha, default="source")
    version = _docker_tag_component(latest_version, default="unknown")
    unique = _docker_tag_component(nonce or uuid.uuid4().hex[:12], default="rebuild")
    tag = f"{source}-runtime-{version}-{unique}"
    if len(tag) <= 128:
        return tag
    suffix = f"-runtime-{version}-{unique}"
    return f"{source[: max(1, 128 - len(suffix))]}{suffix}"


def _version_from_mapping(value: Any) -> str | None:
    if isinstance(value, dict):
        version = value.get("version") or value.get("sdk_version")
        if isinstance(version, str) and version.strip():
            return version.strip()
    return None


def _docker_tag_component(value: str, *, default: str) -> str:
    cleaned = _DOCKER_TAG_UNSAFE_RE.sub("-", str(value).strip().lower()).strip(".-")
    return cleaned or default


def _compare_versions(left: str, right: str) -> int:
    left_key = _version_key(left)
    right_key = _version_key(right)
    if left_key < right_key:
        return -1
    if left_key > right_key:
        return 1
    return 0


def _version_key(value: str) -> tuple[int, int, int, str]:
    match = re.match(r"^\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?([^\s]*)?", value)
    if not match:
        return (0, 0, 0, value)
    major, minor, patch, suffix = match.groups()
    return (int(major), int(minor or 0), int(patch or 0), suffix or "")


def _read_entrypoint(workdir: Path) -> str:
    yaml_path = workdir / "a2a.yaml"
    if yaml_path.exists():
        data = yaml.safe_load(yaml_path.read_text()) or {}
        entrypoint = data.get("entrypoint")
        if isinstance(entrypoint, str) and entrypoint.strip():
            return entrypoint.strip()

    dockerfile = workdir / "Dockerfile"
    if dockerfile.exists():
        match = re.search(
            r"^ENV\s+A2A_ENTRYPOINT=(?P<value>[^\n]+)$",
            dockerfile.read_text(),
            flags=re.MULTILINE,
        )
        if match:
            return match.group("value").strip().strip('"').strip("'")
    raise RuntimeError("agent repo is missing a2a.yaml entrypoint")


def _read_entrypoint_from_repo(repo_url: str, source_sha: str) -> str:
    import tempfile

    with tempfile.TemporaryDirectory(prefix="a2a-runtime-source-") as tmp:
        parent = Path(tmp)
        workdir = parent / "source"
        _run(parent, "clone", "--branch", "main", repo_url, str(workdir))
        _git(workdir, "checkout", "--quiet", source_sha)
        return _read_entrypoint(workdir)


def _run(workdir: Path, *args: str) -> str:
    import subprocess

    result = subprocess.run(
        ["git", *args],
        cwd=workdir,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _git(workdir: Path, *args: str) -> str:
    import subprocess

    result = subprocess.run(
        ["git", "-C", str(workdir), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout
