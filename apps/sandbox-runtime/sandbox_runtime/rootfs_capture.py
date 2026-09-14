"""Capture guest rootfs writes after sandbox commands.

The workspace mount remains the fast path for intentional artifacts, but it is
not enough: user code often writes to /tmp, /app, /root, or library-specific
output directories. This module snapshots the sandbox filesystem before and
after a command, copies changed regular files out of the guest, and stores them
in the caller's bucket under outputs/rootfs-captures/<capture_id>/...
"""
from __future__ import annotations

import json
import os
import posixpath
import tempfile
from dataclasses import dataclass
from typing import Any


_DEFAULT_EXCLUDE_PREFIXES = (
    "/dev",
    "/proc",
    "/sys",
    "/run",
    "/var/run",
    "/var/lock",
    "/workspace",
)
_DEFAULT_EXCLUDE_EXACT = (
    "/etc/hostname",
    "/etc/hosts",
    "/etc/resolv.conf",
)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_prefixes(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if not raw:
        return default
    out = []
    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        out.append(_norm_path(value))
    return tuple(out)


def _norm_path(path: str) -> str:
    if not path:
        return "/"
    normalized = posixpath.normpath(path if path.startswith("/") else f"/{path}")
    return normalized if normalized != "." else "/"


def _entry_value(entry: Any, name: str, default: Any = None) -> Any:
    if isinstance(entry, dict):
        return entry.get(name, default)
    return getattr(entry, name, default)


def _entry_kind(entry: Any) -> str:
    return str(_entry_value(entry, "kind", "") or "").lower()


def _entry_path(entry: Any) -> str:
    return _norm_path(str(_entry_value(entry, "path", "") or ""))


def _entry_size(entry: Any) -> int:
    try:
        return int(_entry_value(entry, "size", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _entry_modified(entry: Any) -> float:
    try:
        return float(_entry_value(entry, "modified", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _safe_relpath(original_path: str) -> str:
    parts = []
    for part in _norm_path(original_path).strip("/").split("/"):
        if not part or part in {".", ".."}:
            continue
        parts.append(part)
    return "/".join(parts) or "root"


@dataclass(frozen=True)
class RootfsEntry:
    path: str
    size: int
    modified: float


class RootfsCapture:
    def __init__(
        self,
        backend: Any,
        *,
        session_id: str,
        enabled: bool | None = None,
        max_files: int | None = None,
        max_total_bytes: int | None = None,
        max_file_bytes: int | None = None,
        max_scan_entries: int | None = None,
        exclude_prefixes: tuple[str, ...] | None = None,
        exclude_exact: tuple[str, ...] | None = None,
    ) -> None:
        self._backend = backend
        self._session_id = session_id
        self.enabled = (
            _env_bool("A2A_SB_CAPTURE_ROOTFS_WRITES", True)
            if enabled is None
            else enabled
        )
        self.max_files = (
            max_files
            if max_files is not None
            else _env_int("A2A_SB_CAPTURE_MAX_FILES", 256)
        )
        self.max_total_bytes = (
            max_total_bytes
            if max_total_bytes is not None
            else _env_int("A2A_SB_CAPTURE_MAX_TOTAL_BYTES", 512 * 1024 * 1024)
        )
        self.max_file_bytes = (
            max_file_bytes
            if max_file_bytes is not None
            else _env_int("A2A_SB_CAPTURE_MAX_FILE_BYTES", 256 * 1024 * 1024)
        )
        self.max_scan_entries = (
            max_scan_entries
            if max_scan_entries is not None
            else _env_int("A2A_SB_CAPTURE_MAX_SCAN_ENTRIES", 1_000_000)
        )
        self.exclude_prefixes = exclude_prefixes or _env_prefixes(
            "A2A_SB_CAPTURE_EXCLUDE_PREFIXES",
            _DEFAULT_EXCLUDE_PREFIXES,
        )
        self.exclude_exact = exclude_exact or _DEFAULT_EXCLUDE_EXACT

    def _excluded(self, path: str) -> bool:
        normalized = _norm_path(path)
        if normalized in self.exclude_exact:
            return True
        for prefix in self.exclude_prefixes:
            p = _norm_path(prefix)
            if normalized == p or normalized.startswith(p.rstrip("/") + "/"):
                return True
        return False

    async def snapshot(self, sandbox: Any) -> dict[str, RootfsEntry]:
        if not self.enabled:
            return {}
        fs = getattr(sandbox, "fs", None)
        if fs is None:
            return {}
        out: dict[str, RootfsEntry] = {}
        queue = ["/"]
        scanned = 0
        while queue and scanned < self.max_scan_entries:
            current = queue.pop(0)
            if self._excluded(current) and current != "/":
                continue
            try:
                entries = await fs.list(current)
            except Exception:  # noqa: BLE001
                continue
            for entry in entries or []:
                scanned += 1
                path = _entry_path(entry)
                if not path or self._excluded(path):
                    continue
                kind = _entry_kind(entry)
                if kind in {"dir", "directory"}:
                    queue.append(path)
                elif kind in {"file", "regular"}:
                    out[path] = RootfsEntry(
                        path=path,
                        size=_entry_size(entry),
                        modified=_entry_modified(entry),
                    )
                if scanned >= self.max_scan_entries:
                    break
        return out

    def changed_paths(
        self,
        before: dict[str, RootfsEntry],
        after: dict[str, RootfsEntry],
    ) -> list[RootfsEntry]:
        changed: list[RootfsEntry] = []
        for path, entry in sorted(after.items()):
            old = before.get(path)
            if (
                old is None
                or old.size != entry.size
                or old.modified != entry.modified
            ):
                changed.append(entry)
        return changed

    async def persist_delta(
        self,
        sandbox: Any,
        *,
        before: dict[str, RootfsEntry],
        capture_id: str,
    ) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        fs = getattr(sandbox, "fs", None)
        if fs is None:
            return []
        after = await self.snapshot(sandbox)
        candidates = self.changed_paths(before, after)
        files: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        total_bytes = 0
        capture_root = f"outputs/rootfs-captures/{capture_id}"

        for entry in candidates:
            if len(files) >= self.max_files:
                skipped.append({"path": entry.path, "reason": "max_files"})
                continue
            if entry.size > self.max_file_bytes:
                skipped.append({
                    "path": entry.path,
                    "reason": "max_file_bytes",
                    "size": entry.size,
                })
                continue
            if total_bytes + entry.size > self.max_total_bytes:
                skipped.append({
                    "path": entry.path,
                    "reason": "max_total_bytes",
                    "size": entry.size,
                })
                continue
            rel = _safe_relpath(entry.path)
            dest = f"{capture_root}/fs/{rel}"
            try:
                await self._copy_file(fs, entry.path, dest)
            except Exception as exc:  # noqa: BLE001
                skipped.append({
                    "path": entry.path,
                    "reason": f"copy_failed:{type(exc).__name__}",
                })
                continue
            total_bytes += entry.size
            files.append({
                "original_path": entry.path,
                "workspace_path": dest,
                "size": entry.size,
                "source": "rootfs_capture",
            })

        if files or skipped:
            manifest_path = f"{capture_root}/manifest.json"
            manifest = {
                "capture_id": capture_id,
                "session_id": self._session_id,
                "files": files,
                "skipped": skipped,
                "limits": {
                    "max_files": self.max_files,
                    "max_total_bytes": self.max_total_bytes,
                    "max_file_bytes": self.max_file_bytes,
                    "max_scan_entries": self.max_scan_entries,
                },
            }
            manifest_data = json.dumps(manifest, indent=2).encode("utf-8")
            try:
                self._overwrite(manifest_path, manifest_data)
            except PermissionError:
                # Rootfs capture is opportunistic. Some workspace grants allow
                # only a project-specific write prefix, so capture metadata under
                # outputs/rootfs-captures is not writable. Do not turn an
                # otherwise successful sandbox command into HTTP 500.
                return files
            files.append({
                "original_path": "/",
                "workspace_path": manifest_path,
                "size": len(manifest_data),
                "source": "rootfs_capture_manifest",
            })
        return files

    async def _copy_file(self, fs: Any, guest_path: str, dest_path: str) -> None:
        with tempfile.TemporaryDirectory(prefix="a2a-rootfs-capture-") as tmpdir:
            host_path = os.path.join(tmpdir, "file")
            copied = False
            copy_to_host = getattr(fs, "copy_to_host", None)
            if copy_to_host is not None:
                try:
                    await copy_to_host(guest_path, host_path)
                    copied = True
                except Exception:  # noqa: BLE001
                    copied = False
            if copied:
                with open(host_path, "rb") as f:
                    self._overwrite_stream(dest_path, f)
                return
            read = getattr(fs, "read", None)
            if read is None:
                raise RuntimeError("sandbox fs does not support read")
            data = await read(guest_path)
            self._overwrite(dest_path, data)

    def _overwrite(self, path: str, data: bytes) -> None:
        overwrite = getattr(self._backend, "overwrite", None)
        if overwrite is None:
            raise RuntimeError("backend does not support overwrite")
        overwrite("/" + path.strip("/"), data)

    def _overwrite_stream(self, path: str, fileobj: Any) -> None:
        overwrite_stream = getattr(self._backend, "overwrite_stream", None)
        if overwrite_stream is None:
            fileobj.seek(0)
            self._overwrite(path, fileobj.read())
            return
        fileobj.seek(0)
        overwrite_stream("/" + path.strip("/"), fileobj)
