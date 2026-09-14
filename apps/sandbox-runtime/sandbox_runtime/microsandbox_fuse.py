"""FUSE adapter exposing a chatbot ``BackendProtocol`` as a POSIX filesystem.

Mounted on the host. Bind-mounted into a microsandbox guest via ``Volume.bind``,
giving sandboxed code direct read/write access to S3-backed files without any
sync code, host creds in the guest, or a custom OCI image.

Architecture::

    sandbox guest:  /workspace                  (POSIX, virtiofs)
                        ↓ Volume.bind
    host:           /var/run/sb-{id}/mnt        (FUSE mountpoint, fusepy)
                        ↓ vfs ops
                    S3FuseAdapter
                        ↓ BackendProtocol
                    CompositeBackend → S3Backend / UserUploadS3Backend
                        ↓
                    StorageService → S3 / MinIO

VFS → backend mapping
---------------------
``getattr``  → ``ls_info(parent)`` lookup (cached, short TTL)
``readdir``  → ``ls_info(path)``
``read``     → ``download_files([path])`` (cached per fh)
``write``    → buffered per fh, flushed on ``release``
``release``  → ``backend.overwrite(path, buf)`` if dirty
``unlink``   → ``backend.delete(path)``
``rename``   → ``backend.copy(src, dst) + delete(src)``
``mkdir``    → tracked in ``_synthetic_dirs`` (S3 has no real dirs)
``rmdir``    → drop from ``_synthetic_dirs`` if empty
``truncate`` → in-buffer truncate

The adapter expects the wrapped backend to expose ``delete`` and ``overwrite``
methods (S3Backend does; CompositeBackend forwards via duck typing through its
``_get_backend_and_key``).
"""

from __future__ import annotations

import errno
import hashlib
import os
import platform
import re
import stat as stat_mod
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Generator

from .resource_caps import CapsTracker, ResourceCaps

_IS_DARWIN = platform.system() == "Darwin"

# Sandbox OCI image. Override the tag via ARK_SANDBOX_IMAGE to use Ark's
# data-sci stack, pin a SHA in prod, or point at a local dev image.
_DEFAULT_SANDBOX_IMAGE = "python:3.12-slim"


def _default_sandbox_image() -> str:
    """Sandbox image URI, env-overridable. Resolved at call time so tests /
    runtime config changes apply without re-importing."""
    return os.environ.get("ARK_SANDBOX_IMAGE", _DEFAULT_SANDBOX_IMAGE)


# ECR auth cache for microsandbox image pulls. msb's image puller is its own
# keychain — it does NOT inherit IRSA / kubelet / boto creds. We have to mint
# an ECR auth token via STS and hand it to Sandbox.create() as
# RegistryAuth.basic("AWS", token). Tokens are valid 12h; cache shorter to be
# safe.
_ECR_REGISTRY_RE = re.compile(
    r"^(?P<account>\d+)\.dkr\.ecr\.(?P<region>[^.]+)\.amazonaws\.com(?:/|$)"
)
_ECR_AUTH_CACHE: dict[str, tuple[Any, float]] = {}
_ECR_AUTH_TTL_S = 10 * 3600  # 10h, well under the 12h ECR token lifetime


def _ecr_registry_auth(image: str) -> Any | None:
    """Return a microsandbox.RegistryAuth for ECR images, else None.

    Resolves at call time so a fresh ECR token is minted on cache miss.
    Cached per registry (one token serves all repos in that registry).
    """
    m = _ECR_REGISTRY_RE.match(image)
    if not m:
        return None
    registry = f"{m['account']}.dkr.ecr.{m['region']}.amazonaws.com"
    cached = _ECR_AUTH_CACHE.get(registry)
    now = time.time()
    if cached and (now - cached[1]) < _ECR_AUTH_TTL_S:
        return cached[0]
    try:
        import boto3  # type: ignore[import-not-found]
        from microsandbox import RegistryAuth  # type: ignore[import-untyped]
    except ImportError:
        return None
    try:
        ecr = boto3.client("ecr", region_name=m["region"])
        token_resp = ecr.get_authorization_token(registryIds=[m["account"]])
        b64 = token_resp["authorizationData"][0]["authorizationToken"]
        import base64

        username, _, password = base64.b64decode(b64).decode().partition(":")
        auth = RegistryAuth.basic(username, password)
    except Exception as exc:
        print(f"[microsandbox] ECR auth fetch failed for {registry}: {exc}")
        return None
    _ECR_AUTH_CACHE[registry] = (auth, now)
    return auth


async def _create_microsandbox(sandbox_cls: Any, cfg: dict[str, Any]) -> Any:
    """Create a microsandbox across SDK versions.

    Older deployed SDKs require ``Sandbox.create(name, **config)`` while newer
    builds also accept a single config dict. Prefer the stricter call shape so
    both versions work.
    """
    kwargs = dict(cfg)
    name = str(kwargs.pop("name"))
    return await sandbox_cls.create(name, **kwargs)


def _sandbox_create_config(
    *,
    name: str,
    image: str,
    volumes: dict[str, Any],
    memory_mib: int,
    cpus: int,
    network_disabled: bool,
) -> dict[str, Any]:
    from microsandbox import Network  # type: ignore[import-untyped]

    network = Network.none() if network_disabled else Network.public_only()
    return {
        "name": name,
        "image": image,
        "volumes": volumes,
        "replace": True,
        "memory_mib": memory_mib,
        "cpus": cpus,
        "network": network,
    }


def _mac_fuse_kwargs() -> dict[str, Any]:
    """Extra fusepy kwargs needed on macOS so finder/listdir work without TCC
    surprise denials. ``local`` makes the mount appear as a local volume,
    ``defer_permissions`` lets the kernel trust our reported st_mode without
    its own ACL re-check (POSIX-only enforcement)."""
    if not _IS_DARWIN:
        return {}
    return {
        "fsname": "ark-s3fuse",
        "volname": "ark-s3fuse",
        "local": True,
        "defer_permissions": True,
        "noappledouble": True,
        "noapplexattr": True,
    }


try:
    from fuse import FUSE, FuseOSError, Operations  # type: ignore[import-untyped]

    _FUSE_AVAILABLE = True
except ImportError:  # macFUSE not installed locally; fine for non-sandbox dev
    _FUSE_AVAILABLE = False

    class Operations:  # type: ignore[no-redef]
        pass

    class FuseOSError(OSError):  # type: ignore[no-redef]
        pass


_DIR_MODE = stat_mod.S_IFDIR | 0o755
_READONLY_DIR_MODE = stat_mod.S_IFDIR | 0o555
_FILE_MODE = stat_mod.S_IFREG | 0o644
_WRITE_POLICY_OUTPUTS = "outputs"
_WRITE_POLICY_WORKSPACE = "workspace"
_SANDBOX_WRITABLE_PREFIX = "/outputs/"
_SANDBOX_WRITABLE_EXACT_PATHS = frozenset({"/memories/AGENTS.md"})

# Stat cache TTL — short enough to feel live to the agent, long enough to
# absorb the `find` / `ls -l` storms that scripts emit. Backend `ls_info`
# already paginates and caches on the synthetic-provider side, so this is a
# second-tier cache to keep VFS-level chatter off the wire.
_STAT_CACHE_TTL_S = 8.0

# Read-ahead chunk size for streaming reads. Sequential reads (the common
# case for ``pandas.read_csv``, plain ``open().read()``) get a single
# range fetch this large and serve subsequent ``read()`` calls from the
# in-process buffer. Random-access reads (jumping offsets) bypass the
# buffer entirely so we don't waste bandwidth or RAM.
_READAHEAD_CHUNK_BYTES = 4 * 1024 * 1024  # 4 MiB


# Kernel-side cache flags handed to fusepy. Keep these at zero for correctness:
# agent file tools write directly to S3 outside the guest, and the next
# sandbox execute must not reuse a guest-kernel dentry/inode/negative lookup
# from before that write. The adapter still has its own short stat cache, which
# we can explicitly invalidate before execute; the kernel cache cannot be
# invalidated from here.
_FUSE_CACHE_KWARGS: dict[str, Any] = {
    "auto_cache": False,
    "attr_timeout": 0.0,
    "entry_timeout": 0.0,
    "negative_timeout": 0.0,
    # Keep O_TRUNC attached to open(2). Without this, libfuse may split the
    # truncate from the open; across the host FUSE -> guest virtiofs path that
    # intent can be lost, and object-store writes then preserve stale tail
    # bytes when a file is rewritten shorter.
    "atomic_o_trunc": True,
}


def _parse_mtime(value: Any) -> float:
    """Coerce ``FileInfo.modified_at`` (ISO-8601 str) to a POSIX timestamp."""
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            pass
    return time.time()


def _resolve_concrete(backend: Any, path: str) -> tuple[Any, str]:
    """Walk a CompositeBackend down to the concrete backend handling ``path``.

    Returns ``(concrete_backend, path_within_backend)``. Falls back to
    ``(backend, path)`` if the wrapper is unrecognized.
    """
    if hasattr(backend, "_get_backend_and_key"):
        try:
            inner, stripped = backend._get_backend_and_key(path)
            if inner is not backend:
                return _resolve_concrete(inner, stripped)
        except Exception:
            pass
    return backend, path


# Buffered writes spill to a host tempfile after this many bytes so big
# uploads don't hold the whole file in RAM. Below the threshold, the spool
# stays purely in-memory (Python's SpooledTemporaryFile semantics).
_SPILL_THRESHOLD_BYTES = 64 * 1024 * 1024


def _normalize_backend_path(path: str) -> str:
    normalized = path if path.startswith("/") else f"/{path}"
    return normalized.rstrip("/") or "/"


def _normalize_backend_dir(path: str) -> str:
    return _normalize_backend_path(path).rstrip("/") or "/"


def _strip_workspace_prefix(path: str) -> str:
    """Strip a leading ``/workspace`` segment so file-tool paths align with
    the sandbox FUSE mount, which binds the backend root at the guest's
    ``/workspace``. Without this, an LLM that writes ``/workspace/outputs/x``
    via the deepagents file tools lands at S3 key ``…/workspace/outputs/x``
    while the sandbox shell sees the same logical path resolve to S3 key
    ``…/outputs/x`` — a silent path-mismatch that surfaces as ENOENT at
    execute time."""
    if not isinstance(path, str) or not path:
        return path
    norm = path if path.startswith("/") else f"/{path}"
    if norm == "/workspace":
        return "/"
    if norm.startswith("/workspace/"):
        return norm[len("/workspace") :]
    return norm


def _normalize_readonly_dirs(paths: list[str] | tuple[str, ...] | None) -> set[str]:
    readonly: set[str] = set()
    for raw in paths or ():
        norm = _normalize_backend_dir(raw)
        if norm != "/" and not _is_sandbox_writable_path(norm):
            readonly.add(norm)
    return readonly


def _normalize_write_policy(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    if raw in {_WRITE_POLICY_WORKSPACE, "all", "read_write", "read-write"}:
        return _WRITE_POLICY_WORKSPACE
    return _WRITE_POLICY_OUTPUTS


def _is_sandbox_writable_path(
    path: str,
    *,
    write_policy: str = _WRITE_POLICY_OUTPUTS,
) -> bool:
    normalized = _normalize_backend_path(path)
    if _normalize_write_policy(write_policy) == _WRITE_POLICY_WORKSPACE:
        return normalized != "/"
    return (
        normalized == "/outputs"
        or normalized.startswith(_SANDBOX_WRITABLE_PREFIX)
        or normalized in _SANDBOX_WRITABLE_EXACT_PATHS
    )


def _sandbox_write_denied(path: str) -> FuseOSError:
    return FuseOSError(errno.EACCES)


def _sandbox_write_denied_message(path: str) -> str:
    return (
        f"Cannot write to {path}. Sandbox file writes are restricted to "
        "/outputs/. Create artifacts under /outputs/<filename>."
    )


class _OpenFile:
    """Per-fh state. Two modes:

    - **Streaming** (``streaming=True``): read-only. No bytes held; ``read``
      calls are forwarded to the backend's ``read_range``. Used for plain
      ``O_RDONLY`` opens — host RAM stays bounded regardless of file size.
    - **Buffered**: backed by a ``SpooledTemporaryFile`` that auto-spills
      to disk above ``_SPILL_THRESHOLD_BYTES``. Flushed back to the
      backend on dirty release via ``overwrite_stream`` (multipart) so
      big writes never round-trip through a single bytes blob.
    """

    __slots__ = (
        "path",
        "size",
        "dirty",
        "buf",
        "backend",
        "stripped_path",
        "lock",
        # Streaming read-ahead. ``rahead_offset`` is the absolute file
        # offset where ``rahead_buf`` begins; -1 means no buffer.
        # ``rahead_next_offset`` tracks the predicted next sequential
        # read so we can detect linear scans vs random access.
        "rahead_buf",
        "rahead_offset",
        "rahead_next_offset",
        "replace_on_first_write",
        "saw_write",
    )

    def __init__(
        self,
        path: str,
        *,
        size: int,
        backend: Any,
        stripped_path: str,
        prefill: bytes | None = None,
        streaming: bool = False,
        dirty: bool = False,
        replace_on_first_write: bool = False,
    ) -> None:
        self.path = path
        self.size = size
        self.dirty = dirty
        self.backend = backend
        self.stripped_path = stripped_path
        # Guards the spool's seek+read/write pair against concurrent
        # kernel threads dispatching ops on the same fh. Streaming
        # handles are stateless reads — the lock is unused in that mode
        # but is cheap so we keep one slot.
        self.lock = threading.Lock()
        self.rahead_buf: bytes = b""
        self.rahead_offset: int = -1
        self.rahead_next_offset: int = 0
        self.replace_on_first_write = replace_on_first_write
        self.saw_write = False
        if streaming:
            self.buf = None
        else:
            self.buf = tempfile.SpooledTemporaryFile(max_size=_SPILL_THRESHOLD_BYTES)
            if prefill:
                self.buf.write(prefill)
                self.size = len(prefill)

    @property
    def streaming(self) -> bool:
        return self.buf is None

    def close(self) -> None:
        if self.buf is not None:
            try:
                self.buf.close()
            except Exception:
                pass
            self.buf = None


class _NonClosingFile:
    """Proxy a file object while ignoring close() calls from upload clients."""

    def __init__(self, wrapped: Any) -> None:
        self._wrapped = wrapped

    def close(self) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)


class S3FuseAdapter(Operations):
    """fusepy ``Operations`` impl backed by a chatbot ``BackendProtocol``.

    Thread-safe: fusepy invokes each VFS op on a fresh kernel thread, and the
    underlying backend is sync (boto3-driven). A single ``threading.Lock``
    guards the open-file table and synthetic-dir set; per-call work is held
    outside the lock so concurrent reads of different files don't serialize.
    """

    def __init__(
        self,
        backend: Any,
        *,
        caps: CapsTracker | None = None,
        readonly_prefixes: list[str] | tuple[str, ...] | None = None,
        write_policy: str = _WRITE_POLICY_OUTPUTS,
    ) -> None:
        if not _FUSE_AVAILABLE:
            raise RuntimeError(
                "fusepy not importable — install `fusepy` and the OS FUSE library "
                "(libfuse2/libfuse3 on Linux, macFUSE on macOS)."
            )
        self._backend = backend
        self._caps = caps or CapsTracker()
        self._write_policy = _normalize_write_policy(write_policy)
        self._files: dict[int, _OpenFile] = {}
        self._next_fh = 1
        self._readonly_dirs = _normalize_readonly_dirs(readonly_prefixes)
        self._synthetic_dirs: set[str] = {"/", "/outputs"} | self._readonly_dirs
        self._stat_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        # Files newly created via ``create``/``open(O_CREAT)`` but not yet
        # flushed to S3. Without this, the kernel's post-create stat returns
        # ENOENT (because ls_info doesn't see the object yet), and the open
        # call appears to fail even though the fd is live.
        self._inflight: dict[str, int] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_writable_path(self, path: str) -> bool:
        return _is_sandbox_writable_path(path, write_policy=self._write_policy)

    def _list_dir(self, path: str) -> list[dict[str, Any]]:
        """Return ``ls_info`` entries for *path*. Single source of truth for
        directory contents — getattr/readdir both feed off this, so a single
        TTL cache covers stat-storm patterns like ``find /workspace``."""
        normalized = path if path.startswith("/") else f"/{path}"
        if not normalized.endswith("/"):
            normalized = f"{normalized}/"
        now = time.monotonic()
        cached = self._stat_cache.get(normalized)
        if cached and now - cached[0] < _STAT_CACHE_TTL_S:
            return cached[1]["entries"]
        try:
            entries = list(self._backend.ls_info(normalized) or [])
        except Exception:
            entries = []
        self._stat_cache[normalized] = (now, {"entries": entries})
        return entries

    def _invalidate(self, path: str) -> None:
        """Drop stat cache for *path*'s parent (and root) so the next stat sees
        the mutation."""
        parent = os.path.dirname(path) or "/"
        if not parent.endswith("/"):
            parent = f"{parent}/"
        self._stat_cache.pop(parent, None)
        self._stat_cache.pop("/", None)

    def invalidate_subtree(self, path: str = "/") -> None:
        """Drop cached directory listings under *path*.

        Used before sandbox execution so S3-direct writes made by the agent
        file tools are visible to a warm live-FUSE VM before the command does
        its first path lookup.
        """
        norm = path if path.startswith("/") else f"/{path}"
        if norm != "/" and not norm.endswith("/"):
            norm = f"{norm}/"
        if norm == "/":
            self._stat_cache.clear()
            return
        for cached_path in list(self._stat_cache):
            if cached_path == norm or cached_path.startswith(norm):
                self._stat_cache.pop(cached_path, None)

    def _stat_file(self, path: str) -> dict[str, Any] | None:
        """Look up ``path`` in its parent's listing. Returns the FileInfo dict
        or None if missing."""
        parent = os.path.dirname(path) or "/"
        for entry in self._list_dir(parent):
            entry_path = entry.get("path") or ""
            if entry_path.rstrip("/") == path.rstrip("/"):
                return entry
        return None

    def _is_dir(self, path: str) -> bool:
        norm = path.rstrip("/") or "/"
        with self._lock:
            if norm in self._synthetic_dirs:
                return True
        # Treat any path that has children as a directory, even if the bucket
        # only stores object keys — S3 has no first-class dir entries.
        prefix_listing = self._list_dir(norm)
        if any(e.get("is_dir") for e in prefix_listing) or prefix_listing:
            return True
        return False

    def _alloc_fh(self, opened: _OpenFile) -> int:
        with self._lock:
            fh = self._next_fh
            self._next_fh += 1
            self._files[fh] = opened
        try:
            self._caps.reserve_open(
                fh,
                opened.path,
                initial_size=0 if opened.streaming else opened.size,
            )
        except OSError as exc:
            with self._lock:
                self._files.pop(fh, None)
            opened.close()
            raise FuseOSError(exc.errno or errno.EIO) from exc
        return fh

    def _get_fh(self, fh: int) -> _OpenFile:
        with self._lock:
            try:
                return self._files[fh]
            except KeyError:
                raise FuseOSError(errno.EBADF) from None

    def _drop_fh(self, fh: int) -> _OpenFile | None:
        with self._lock:
            opened = self._files.pop(fh, None)
        self._caps.release(fh)
        return opened

    def _download(self, path: str) -> bytes:
        """Raw bytes read via ``download_files``. We deliberately bypass
        ``read()`` because that returns formatted, line-numbered text intended
        for LLM consumption — not a faithful POSIX read."""
        try:
            responses = self._backend.download_files([path])
        except Exception as exc:
            raise FuseOSError(errno.EIO) from exc
        if not responses:
            raise FuseOSError(errno.ENOENT)
        resp = responses[0]
        content = getattr(resp, "content", None)
        err = getattr(resp, "error", None)
        if content is None:
            if err == "permission_denied":
                raise FuseOSError(errno.EACCES)
            raise FuseOSError(errno.ENOENT)
        return content

    # ------------------------------------------------------------------
    # FUSE: metadata
    # ------------------------------------------------------------------

    def getattr(self, path: str, fh: int | None = None) -> dict[str, Any]:
        now = time.time()
        norm = path.rstrip("/") or "/"

        if norm == "/":
            return {
                "st_mode": _DIR_MODE,
                "st_nlink": 2,
                "st_size": 0,
                "st_ctime": now,
                "st_mtime": now,
                "st_atime": now,
            }

        with self._lock:
            if norm in self._synthetic_dirs:
                return {
                    "st_mode": _READONLY_DIR_MODE
                    if norm in self._readonly_dirs
                    else _DIR_MODE,
                    "st_nlink": 2,
                    "st_size": 0,
                    "st_ctime": now,
                    "st_mtime": now,
                    "st_atime": now,
                }
            if norm in self._inflight:
                return {
                    "st_mode": _FILE_MODE,
                    "st_nlink": 1,
                    "st_size": self._inflight[norm],
                    "st_ctime": now,
                    "st_mtime": now,
                    "st_atime": now,
                }

        entry = self._stat_file(norm)
        if entry is None:
            if self._is_dir(norm):
                return {
                    "st_mode": _DIR_MODE,
                    "st_nlink": 2,
                    "st_size": 0,
                    "st_ctime": now,
                    "st_mtime": now,
                    "st_atime": now,
                }
            raise FuseOSError(errno.ENOENT)

        if entry.get("is_dir"):
            return {
                "st_mode": _DIR_MODE,
                "st_nlink": 2,
                "st_size": 0,
                "st_ctime": now,
                "st_mtime": now,
                "st_atime": now,
            }

        size = int(entry.get("size") or 0)
        mtime = _parse_mtime(entry.get("modified_at"))
        return {
            "st_mode": _FILE_MODE,
            "st_nlink": 1,
            "st_size": size,
            "st_ctime": mtime,
            "st_mtime": mtime,
            "st_atime": mtime,
        }

    def readdir(self, path: str, fh: int) -> list[str]:
        norm = path.rstrip("/") or "/"
        names: set[str] = {".", ".."}
        for entry in self._list_dir(norm):
            entry_path = entry.get("path") or ""
            base = entry_path.rstrip("/").rsplit("/", 1)[-1]
            if base:
                names.add(base)
        # Emit synthetic subdirs the agent created via mkdir even if S3 hasn't
        # observed any objects in them yet.
        prefix = norm if norm.endswith("/") else f"{norm}/"
        with self._lock:
            for syn in self._synthetic_dirs:
                if syn == norm:
                    continue
                if syn.startswith(prefix):
                    rel = syn[len(prefix) :].split("/", 1)[0]
                    if rel:
                        names.add(rel)
        return sorted(names)

    # ------------------------------------------------------------------
    # FUSE: file IO
    # ------------------------------------------------------------------

    _WRITE_FLAG_MASK = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_TRUNC

    def open(self, path: str, flags: int) -> int:
        """Two strategies:

        - Pure read (``O_RDONLY`` only): streaming handle. No bytes pulled.
          ``read`` calls go to the backend's ``read_range`` so big files
          don't balloon host RAM.
        - Anything writable (``O_WRONLY``/``O_RDWR``/``O_APPEND``/``O_TRUNC``):
          buffered handle backed by a SpooledTemporaryFile. Existing bytes
          prefetched (unless ``O_TRUNC``) so partial overwrites work; the
          spool spills to disk past 64 MB so we don't OOM.
        """
        norm = path.rstrip("/") or "/"
        backend, stripped = _resolve_concrete(self._backend, path)

        is_writable = bool(flags & self._WRITE_FLAG_MASK)
        if is_writable and not self._is_writable_path(path):
            raise _sandbox_write_denied(path)
        if not is_writable:
            try:
                size = self._size_of(path)
            except FuseOSError:
                if flags & os.O_CREAT:
                    # Read-side creat with no write flag is degenerate but
                    # treat as empty-streaming.
                    size = 0
                else:
                    raise
            return self._alloc_fh(
                _OpenFile(
                    path=path,
                    size=size,
                    backend=backend,
                    stripped_path=stripped,
                    streaming=True,
                )
            )

        # Buffered (writable) handle. Prefill unless O_TRUNC asked for empty.
        access_mode = flags & os.O_ACCMODE
        explicit_truncate = bool(flags & os.O_TRUNC)
        append = bool(flags & os.O_APPEND)
        prefill: bytes | None
        if explicit_truncate:
            prefill = b""
        else:
            try:
                prefill = self._download(path)
            except FuseOSError:
                if flags & os.O_CREAT:
                    prefill = b""
                else:
                    raise

        of = _OpenFile(
            path=path,
            size=len(prefill or b""),
            backend=backend,
            stripped_path=stripped,
            prefill=prefill,
            streaming=False,
            dirty=explicit_truncate,
            replace_on_first_write=(
                access_mode == os.O_WRONLY and not explicit_truncate and not append
            ),
        )
        with self._lock:
            self._inflight[norm] = of.size
        return self._alloc_fh(of)

    def create(self, path: str, mode: int, fi: Any | None = None) -> int:
        if not self._is_writable_path(path):
            raise _sandbox_write_denied(path)
        norm = path.rstrip("/") or "/"
        backend, stripped = _resolve_concrete(self._backend, path)
        with self._lock:
            self._inflight[norm] = 0
        return self._alloc_fh(
            _OpenFile(
                path=path,
                size=0,
                backend=backend,
                stripped_path=stripped,
                prefill=b"",
                streaming=False,
                dirty=True,
            )
        )

    def read(self, path: str, size: int, offset: int, fh: int) -> bytes:
        of = self._get_fh(fh)
        if of.streaming:
            read_range = getattr(of.backend, "read_range", None)
            if read_range is None:
                # Backend doesn't support range reads. Fall back to
                # whole-file download (legacy behavior). Should be rare —
                # all our concrete backends implement it.
                blob = self._download(of.path)
                return blob[offset : offset + size]
            return self._streaming_read(of, read_range, offset, size)

        # Buffered: serve from the spool.
        buf = of.buf
        assert buf is not None
        with of.lock:
            buf.seek(offset)
            return buf.read(size)

    def _streaming_read(
        self,
        of: _OpenFile,
        read_range: Any,
        offset: int,
        size: int,
    ) -> bytes:
        """Serve a streaming ``read`` with sequential read-ahead.

        Linear-scan readers (the common case — ``pandas.read_csv``,
        ``open().read()``, ``shutil.copy``) issue page-sized ``read`` calls
        at monotonically increasing offsets. We coalesce them into single
        ``_READAHEAD_CHUNK_BYTES``-sized range fetches and serve from the
        in-process buffer. Random-access readers (seek-heavy formats,
        partial reads) skip the buffer to avoid wasted bandwidth.
        """
        if size <= 0:
            return b""

        with of.lock:
            buf = of.rahead_buf
            buf_start = of.rahead_offset
            buf_end = buf_start + len(buf) if buf_start >= 0 else -1

            # Buffer hit: requested window lies entirely inside cached chunk.
            if buf_start >= 0 and offset >= buf_start and offset + size <= buf_end:
                rel = offset - buf_start
                of.rahead_next_offset = offset + size
                return buf[rel : rel + size]

            sequential = offset == of.rahead_next_offset or (
                buf_start >= 0 and offset == buf_end
            )

        if sequential:
            chunk_size = max(size, _READAHEAD_CHUNK_BYTES)
            try:
                chunk = read_range(of.stripped_path, offset, chunk_size)
            except Exception as exc:
                raise FuseOSError(errno.EIO) from exc
            with of.lock:
                of.rahead_buf = chunk
                of.rahead_offset = offset
                of.rahead_next_offset = offset + size
            return chunk[:size]

        # Random access — bypass buffer, single range fetch.
        try:
            data = read_range(of.stripped_path, offset, size)
        except Exception as exc:
            raise FuseOSError(errno.EIO) from exc
        with of.lock:
            of.rahead_next_offset = offset + size
        return data

    def write(self, path: str, data: bytes, offset: int, fh: int) -> int:
        of = self._get_fh(fh)
        if of.streaming:
            # Read-only handle. Shouldn't happen — kernel respects the
            # flags we returned from open() — but guard explicitly.
            raise FuseOSError(errno.EBADF)
        buf = of.buf
        assert buf is not None
        with of.lock:
            if of.replace_on_first_write and not of.saw_write and offset == 0:
                # Defensive repair for stacks that lose O_TRUNC between the
                # sandbox guest and host FUSE process. Normal open("w") writes
                # arrive as write-only fd + first write at offset 0; if the
                # truncate flag disappeared, replacing here prevents stale
                # object-store tail bytes from surviving shorter rewrites.
                buf.seek(0)
                buf.truncate(0)
                of.size = 0
            of.saw_write = True
            # POSIX: writes past EOF zero-extend the gap.
            if offset > of.size:
                buf.seek(of.size)
                buf.write(b"\0" * (offset - of.size))
            buf.seek(offset)
            buf.write(data)
            end = offset + len(data)
            if end > of.size:
                of.size = end
            of.dirty = True
            new_size = of.size
        try:
            self._caps.resize(fh, new_size)
        except OSError as exc:
            raise FuseOSError(exc.errno or errno.EIO) from exc
        norm = of.path.rstrip("/") or "/"
        with self._lock:
            if norm in self._inflight:
                self._inflight[norm] = new_size
        return len(data)

    def truncate(self, path: str, length: int, fh: int | None = None) -> None:
        if not self._is_writable_path(path):
            raise _sandbox_write_denied(path)
        if fh is not None:
            of = self._get_fh(fh)
            owned = False
        else:
            backend, stripped = _resolve_concrete(self._backend, path)
            try:
                data = self._download(path)
            except FuseOSError:
                data = b""
            of = _OpenFile(
                path=path,
                size=len(data),
                backend=backend,
                stripped_path=stripped,
                prefill=data,
                streaming=False,
                dirty=True,
            )
            owned = True

        if of.streaming:
            raise FuseOSError(errno.EBADF)
        buf = of.buf
        assert buf is not None
        with of.lock:
            buf.truncate(length)
            if length > of.size:
                buf.seek(of.size)
                buf.write(b"\0" * (length - of.size))
            of.size = length
            of.dirty = True
        if not owned and fh is not None:
            try:
                self._caps.resize(fh, length)
            except OSError as exc:
                raise FuseOSError(exc.errno or errno.EIO) from exc

        if owned:
            try:
                self._flush_buffer(of, clear_inflight=True)
            finally:
                of.close()

    def flush(self, path: str, fh: int) -> None:
        of = self._get_fh(fh)
        if not of.streaming and of.dirty:
            self._flush_buffer(of, clear_inflight=False)
        return None

    def fsync(self, path: str, fdatasync: int, fh: int) -> int:
        self.flush(path, fh)
        return 0

    def release(self, path: str, fh: int) -> int:
        of = self._drop_fh(fh)
        if of is None:
            return 0
        try:
            if of.dirty:
                self._flush_buffer(of, clear_inflight=True)
            else:
                norm = of.path.rstrip("/") or "/"
                with self._lock:
                    self._inflight.pop(norm, None)
        finally:
            of.close()
        return 0

    def _flush_buffer(self, of: _OpenFile, *, clear_inflight: bool) -> None:
        if of.streaming:
            return  # nothing to flush
        backend = of.backend
        stripped = of.stripped_path
        buf = of.buf
        assert buf is not None

        # Prefer the streaming overwrite so big spools don't have to be
        # re-read into a contiguous bytes object before upload.
        overwrite_stream = getattr(backend, "overwrite_stream", None)
        try:
            with of.lock:
                buf.truncate(of.size)
                buf.seek(0)
                if overwrite_stream is not None:
                    overwrite_stream(stripped, _NonClosingFile(buf))
                else:
                    overwrite = getattr(backend, "overwrite", None)
                    if overwrite is None:
                        raise FuseOSError(errno.EROFS)
                    overwrite(stripped, buf.read())
        except FuseOSError:
            raise
        except Exception as exc:
            raise FuseOSError(errno.EIO) from exc

        of.dirty = False
        self._invalidate(of.path)
        norm = of.path.rstrip("/") or "/"
        if clear_inflight:
            with self._lock:
                self._inflight.pop(norm, None)

    def _size_of(self, path: str) -> int:
        """Return file size for *path* using the cached parent listing.
        Raises FuseOSError(ENOENT) if missing."""
        entry = self._stat_file(path)
        if entry is None or entry.get("is_dir"):
            raise FuseOSError(errno.ENOENT)
        return int(entry.get("size") or 0)

    # ------------------------------------------------------------------
    # FUSE: namespace mutation
    # ------------------------------------------------------------------

    def mkdir(self, path: str, mode: int) -> None:
        if not self._is_writable_path(path):
            raise _sandbox_write_denied(path)
        norm = path.rstrip("/") or "/"
        with self._lock:
            self._synthetic_dirs.add(norm)
        self._invalidate(norm)

    def rmdir(self, path: str) -> None:
        if not self._is_writable_path(path):
            raise _sandbox_write_denied(path)
        norm = path.rstrip("/") or "/"
        # Reject if S3 has objects under this prefix
        if any(self._list_dir(norm)):
            raise FuseOSError(errno.ENOTEMPTY)
        with self._lock:
            self._synthetic_dirs.discard(norm)
        self._invalidate(norm)

    def unlink(self, path: str) -> None:
        if not self._is_writable_path(path):
            raise _sandbox_write_denied(path)
        backend, stripped = _resolve_concrete(self._backend, path)
        delete = getattr(backend, "delete", None)
        if delete is None:
            raise FuseOSError(errno.EROFS)
        try:
            delete(stripped)
        except Exception as exc:
            raise FuseOSError(errno.EIO) from exc
        norm = path.rstrip("/") or "/"
        with self._lock:
            self._inflight.pop(norm, None)
        self._invalidate(path)

    def rename(self, old: str, new: str) -> None:
        if not self._is_writable_path(old) or not self._is_writable_path(new):
            raise _sandbox_write_denied(new)
        src_backend, src_stripped = _resolve_concrete(self._backend, old)
        dst_backend, dst_stripped = _resolve_concrete(self._backend, new)
        if src_backend is dst_backend and hasattr(src_backend, "copy"):
            try:
                src_backend.copy(src_stripped, dst_stripped)
            except Exception as exc:
                raise FuseOSError(errno.EIO) from exc
        else:
            # Cross-route rename — fall back to read + write + delete.
            try:
                data = self._download(old)
            except FuseOSError:
                raise
            overwrite = getattr(dst_backend, "overwrite", None)
            if overwrite is None:
                raise FuseOSError(errno.EROFS)
            try:
                overwrite(dst_stripped, data)
            except Exception as exc:
                raise FuseOSError(errno.EIO) from exc
        delete = getattr(src_backend, "delete", None)
        if delete is not None:
            try:
                delete(src_stripped)
            except Exception:
                pass  # destination already written — ENOENT on src is benign
        self._invalidate(old)
        self._invalidate(new)

    # ------------------------------------------------------------------
    # FUSE: stubs
    # ------------------------------------------------------------------

    def chmod(self, path: str, mode: int) -> int:
        if not self._is_writable_path(path):
            raise _sandbox_write_denied(path)
        return 0

    def chown(self, path: str, uid: int, gid: int) -> int:
        if not self._is_writable_path(path):
            raise _sandbox_write_denied(path)
        return 0

    def utimens(self, path: str, times: tuple[float, float] | None = None) -> int:
        if not self._is_writable_path(path):
            raise _sandbox_write_denied(path)
        return 0

    def statfs(self, path: str) -> dict[str, int]:
        # Pretend the bucket is huge so agents don't trip on disk-full heuristics.
        return {
            "f_bsize": 4096,
            "f_frsize": 4096,
            "f_blocks": 1 << 32,
            "f_bfree": 1 << 32,
            "f_bavail": 1 << 32,
            "f_files": 1 << 24,
            "f_ffree": 1 << 24,
            "f_namemax": 255,
        }

    # ---- xattr stubs --------------------------------------------------
    # microsandbox/virtiofs bind-mount in strict mode probes xattr support
    # by calling listxattr; if it gets ENOTSUP it refuses the mount with
    # "xattr not supported on root filesystem and strict mode is enabled".
    # S3 has no xattr concept so we just report "supported but empty":
    # listxattr returns [], getxattr/removexattr raise ENODATA, setxattr
    # is a no-op success. Enough to satisfy the strict probe without
    # actually persisting anything.
    def listxattr(self, path: str) -> list[str]:
        return []

    def getxattr(self, path: str, name: str, position: int = 0) -> bytes:
        # ENODATA / ENOATTR — "the named attribute does not exist", which
        # is the right answer for "we don't store xattrs."
        raise FuseOSError(errno.ENODATA)

    def setxattr(
        self,
        path: str,
        name: str,
        value: bytes,
        options: int,
        position: int = 0,
    ) -> None:
        # No-op success. Could persist to S3 user-defined-metadata in
        # future; not needed for sandbox bind to work.
        return None

    def removexattr(self, path: str, name: str) -> None:
        raise FuseOSError(errno.ENODATA)


# ----------------------------------------------------------------------
# Mount lifecycle
# ----------------------------------------------------------------------


class _MountHandle:
    """Owns the FUSE thread + mountpoint. Use via ``mount_backend``."""

    def __init__(
        self,
        mountpoint: str,
        thread: threading.Thread,
        adapter: S3FuseAdapter,
    ) -> None:
        self.mountpoint = mountpoint
        self._thread = thread
        self._adapter = adapter

    def invalidate_fuse_cache(self, path: str = "/") -> None:
        self._adapter.invalidate_subtree(path)

    def unmount(self, *, timeout: float = 5.0) -> None:
        # `fusermount -u` (Linux) / `umount` (macOS) tells the kernel to detach
        # the mount, which causes the blocking FUSE() call in our worker thread
        # to return cleanly.
        for cmd in (
            ["fusermount", "-u", self.mountpoint],
            ["fusermount3", "-u", self.mountpoint],
            ["umount", self.mountpoint],
        ):
            try:
                subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)
                break
            except (
                FileNotFoundError,
                subprocess.CalledProcessError,
                subprocess.TimeoutExpired,
            ):
                continue
        self._thread.join(timeout=timeout)


@contextmanager
def mount_backend(
    backend: Any,
    mountpoint: str,
    *,
    foreground_log: bool = False,
    caps: CapsTracker | None = None,
    readonly_prefixes: list[str] | tuple[str, ...] | None = None,
    write_policy: str = _WRITE_POLICY_OUTPUTS,
) -> Generator[_MountHandle, None, None]:
    """Mount *backend* as a FUSE filesystem at *mountpoint* for the duration of
    the ``with`` block. Creates the mountpoint dir if missing.

    The FUSE main loop runs on a dedicated daemon thread; the contextmanager
    body sees a populated mountpoint and is free to bind it into a sandbox
    (``Volume.bind(mountpoint)``)."""
    if not _FUSE_AVAILABLE:
        raise RuntimeError("fusepy unavailable — cannot mount FUSE backend.")

    os.makedirs(mountpoint, exist_ok=True)
    adapter = S3FuseAdapter(
        backend,
        caps=caps,
        readonly_prefixes=readonly_prefixes,
        write_policy=write_policy,
    )
    ready = threading.Event()
    error: list[BaseException] = []

    def _run() -> None:
        try:
            ready.set()
            FUSE(
                adapter,
                mountpoint,
                foreground=True,
                nothreads=False,
                allow_other=False,
                debug=foreground_log,
                **_FUSE_CACHE_KWARGS,
                **_mac_fuse_kwargs(),
            )
        except BaseException as exc:  # noqa: BLE001 — surface any failure
            error.append(exc)
            ready.set()

    thread = threading.Thread(
        target=_run, name=f"fuse-{os.path.basename(mountpoint)}", daemon=True
    )
    thread.start()
    ready.wait(timeout=5.0)

    # Poll briefly for the mountpoint to be ready — `FUSE()` blocks before
    # the kernel finishes the handshake on some platforms, so the `ready`
    # event firing only proves the thread reached the call.
    for _ in range(20):
        if os.path.ismount(mountpoint):
            break
        if error:
            raise error[0]
        time.sleep(0.05)

    handle = _MountHandle(
        mountpoint=mountpoint,
        thread=thread,
        adapter=adapter,
    )
    try:
        yield handle
    finally:
        handle.unmount()
        if error:
            # Surface mount-time failures only after teardown so the unmount
            # always runs.
            raise error[0]


# ----------------------------------------------------------------------
# microsandbox session
# ----------------------------------------------------------------------


def _default_mountpoint(session_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in {"-", "_"} else "_" for c in session_id)
    # /var/run is root-only on macOS and immutable on some Linux distros.
    # Pick a user-writable base: $XDG_RUNTIME_DIR if set (Linux session bus),
    # else /tmp (works everywhere we care about). Avoid $TMPDIR on macOS
    # because it lives under /var/folders which TCC can lock down for FUSE.
    base = (
        os.environ.get("ARK_SANDBOX_RUNTIME_DIR")
        or os.environ.get("XDG_RUNTIME_DIR")
        or "/tmp"
    )
    return os.path.join(base, f"ark-sb-{safe}", "mnt")


def _unique_sandbox_name(session_id: str) -> str:
    return f"ark-{_short_session_key(session_id)}-{uuid.uuid4().hex[:8]}"


def _short_session_key(session_id: str) -> str:
    return hashlib.blake2b(session_id.encode("utf-8"), digest_size=5).hexdigest()


class _AsyncSandboxSession:
    """Bundle the FUSE mount + Sandbox so callers don't manage both manually."""

    def __init__(self, sandbox: Any, mountpoint: str) -> None:
        self.sandbox = sandbox
        self.mountpoint = mountpoint


@contextmanager
def microsandbox_session_sync(
    backend: Any,
    *,
    session_id: str,
    image: str | None = None,
    guest_mount: str = "/workspace",
    mountpoint: str | None = None,
    readonly_prefixes: list[str] | tuple[str, ...] | None = None,
    write_policy: str = _WRITE_POLICY_OUTPUTS,
    network_disabled: bool = False,
    resource_caps: ResourceCaps | None = None,
    caps_tracker: CapsTracker | None = None,
) -> Generator[_AsyncSandboxSession, None, None]:
    """Synchronous version. The async chatbot loop should prefer
    :func:`microsandbox_session` which does the same thing without blocking.

    Useful for tests + scripts that already run in a thread. Spins a FUSE
    mount, brings up a microsandbox that bind-mounts it into the guest,
    yields, then tears both down.
    """
    try:
        import asyncio

        from microsandbox import Sandbox, Volume  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError(
            "microsandbox not importable — install the `microsandbox` package."
        ) from exc

    mp = mountpoint or _default_mountpoint(session_id)
    resource_caps = resource_caps or ResourceCaps.from_env()
    caps_tracker = caps_tracker or CapsTracker(resource_caps)
    sandbox_name = _unique_sandbox_name(session_id)
    resolved_image = image or _default_sandbox_image()
    with mount_backend(
        backend,
        mp,
        caps=caps_tracker,
        readonly_prefixes=readonly_prefixes,
        write_policy=write_policy,
    ):

        async def _spin() -> Any:
            cfg = _sandbox_create_config(
                name=sandbox_name,
                image=resolved_image,
                volumes={guest_mount: Volume.bind(mp)},
                memory_mib=resource_caps.vm_memory_mib,
                cpus=resource_caps.vm_cpu_count,
                network_disabled=network_disabled,
            )
            auth = _ecr_registry_auth(resolved_image)
            if auth is not None:
                cfg["registry_auth"] = auth
            return await _create_microsandbox(Sandbox, cfg)

        sandbox = asyncio.run(_spin())
        try:
            yield _AsyncSandboxSession(sandbox=sandbox, mountpoint=mp)
        finally:
            try:
                asyncio.run(sandbox.stop_and_wait())
            except Exception:
                pass


async def microsandbox_session(
    backend: Any,
    *,
    session_id: str,
    image: str | None = None,
    guest_mount: str = "/workspace",
    mountpoint: str | None = None,
    seed_paths: list[str] | None = None,
    readonly_prefixes: list[str] | tuple[str, ...] | None = None,
    write_policy: str = _WRITE_POLICY_OUTPUTS,
    network_disabled: bool = False,
    bridge_mode: bool | None = None,
    resource_caps: ResourceCaps | None = None,
    caps_tracker: CapsTracker | None = None,
) -> "_SessionCtx":
    """Yield a running ``microsandbox.Sandbox`` with *backend* exposed at
    *guest_mount*. Two impls picked at runtime:

    - **Live FUSE mode** (Linux, future M3+ macOS): host FUSE mounts the
      backend, sandbox bind-mounts it into the guest. Live, no sync, no
      creds in guest.
    - **Bridge mode** (M1/M2 macOS — forced because Apple silicon < M3 lacks
      hardware nested virt and macOS libkrun virtiofs cannot re-export FUSE):
      pre-hydrates a host tmpdir from the backend, binds it raw, walks
      changes back to the backend on exit. Loses live concurrent reads from
      the host; gains Mac local dev parity with prod.

    *seed_paths* (bridge mode only): paths to materialize before sandbox
    boot. ``None`` means "everything under /". Pass a list when the caller
    knows exactly what files the agent will touch. Saves walks on big buckets.

    *bridge_mode* forces a mode regardless of platform — useful for tests.
    """
    if bridge_mode is None:
        bridge_mode = _IS_DARWIN

    resolved_image = image or _default_sandbox_image()
    if bridge_mode:
        return _BridgeSessionCtx(
            backend=backend,
            session_id=session_id,
            image=resolved_image,
            guest_mount=guest_mount,
            mountpoint=mountpoint,
            seed_paths=seed_paths,
            readonly_prefixes=readonly_prefixes,
            write_policy=write_policy,
            network_disabled=network_disabled,
            resource_caps=resource_caps or ResourceCaps.from_env(),
        )
    return _AsyncSessionCtx(
        backend=backend,
        session_id=session_id,
        image=resolved_image,
        guest_mount=guest_mount,
        mountpoint=mountpoint,
        readonly_prefixes=readonly_prefixes,
        write_policy=write_policy,
        network_disabled=network_disabled,
        resource_caps=resource_caps or ResourceCaps.from_env(),
        caps_tracker=caps_tracker,
    )


# Type alias shared by both session impls so callers can annotate.
_SessionCtx = "Any"


class _AsyncSessionCtx:
    """Async ctx mgr around the FUSE + sandbox bundle. The FUSE mount runs in
    a worker thread (because fusepy is sync); the sandbox itself uses the
    native microsandbox async API."""

    def __init__(
        self,
        *,
        backend: Any,
        session_id: str,
        image: str,
        guest_mount: str,
        mountpoint: str | None,
        readonly_prefixes: list[str] | tuple[str, ...] | None,
        write_policy: str,
        network_disabled: bool,
        resource_caps: ResourceCaps,
        caps_tracker: CapsTracker | None,
    ) -> None:
        self._backend = backend
        self._session_id = session_id
        self._image = image
        self._guest_mount = guest_mount
        self._mountpoint = mountpoint or _default_mountpoint(session_id)
        self._readonly_prefixes = readonly_prefixes
        self._write_policy = _normalize_write_policy(write_policy)
        self._network_disabled = network_disabled
        self._resource_caps = resource_caps
        self._caps_tracker = caps_tracker or CapsTracker(resource_caps)
        self._sandbox_name = _unique_sandbox_name(session_id)
        self._handle: _MountHandle | None = None
        self._sandbox: Any = None

    async def __aenter__(self) -> _AsyncSandboxSession:
        import asyncio

        from microsandbox import Sandbox, Volume  # type: ignore[import-untyped]

        # Run the synchronous mount in a worker thread so the asyncio loop
        # stays responsive. The thread holds the daemon FUSE thread, plus
        # the unmount call we'll make on teardown.
        loop = asyncio.get_running_loop()

        def _mount_blocking() -> _MountHandle:
            os.makedirs(self._mountpoint, exist_ok=True)
            adapter = S3FuseAdapter(
                self._backend,
                caps=self._caps_tracker,
                readonly_prefixes=self._readonly_prefixes,
                write_policy=self._write_policy,
            )
            ready = threading.Event()
            error: list[BaseException] = []

            def _run() -> None:
                try:
                    ready.set()
                    FUSE(
                        adapter,
                        self._mountpoint,
                        foreground=True,
                        nothreads=False,
                        allow_other=False,
                        **_FUSE_CACHE_KWARGS,
                        **_mac_fuse_kwargs(),
                    )
                except BaseException as exc:  # noqa: BLE001
                    error.append(exc)
                    ready.set()

            t = threading.Thread(
                target=_run,
                name=f"fuse-{self._session_id}",
                daemon=True,
            )
            t.start()
            ready.wait(timeout=5.0)
            for _ in range(20):
                if os.path.ismount(self._mountpoint):
                    break
                if error:
                    raise error[0]
                time.sleep(0.05)
            return _MountHandle(
                mountpoint=self._mountpoint,
                thread=t,
                adapter=adapter,
            )

        self._handle = await loop.run_in_executor(None, _mount_blocking)
        cfg = _sandbox_create_config(
            name=self._sandbox_name,
            image=self._image,
            volumes={self._guest_mount: Volume.bind(self._mountpoint)},
            memory_mib=self._resource_caps.vm_memory_mib,
            cpus=self._resource_caps.vm_cpu_count,
            network_disabled=self._network_disabled,
        )
        auth = _ecr_registry_auth(self._image)
        if auth is not None:
            cfg["registry_auth"] = auth
        self._sandbox = await _create_microsandbox(Sandbox, cfg)
        return _AsyncSandboxSession(sandbox=self._sandbox, mountpoint=self._mountpoint)

    def invalidate_fuse_cache(self, path: str = "/") -> None:
        if self._handle is not None:
            self._handle.invalidate_fuse_cache(path)

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        import asyncio

        if self._sandbox is not None:
            try:
                await self._sandbox.stop_and_wait()
            except Exception:
                pass
        if self._handle is not None:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._handle.unmount)
        return False


# ----------------------------------------------------------------------
# Bridge mode (macOS M1/M2 fallback)
# ----------------------------------------------------------------------
#
# Apple Silicon below M3 lacks hardware nested virtualization (FEAT_NV2),
# and macOS libkrun's virtiofs refuses to re-export FUSE-backed mountpoints.
# Both block the live FUSE-into-sandbox architecture on M1/M2 Macs. This
# mode bypasses both: we pre-materialize the relevant slice of the backend
# into a regular host tmpdir (no FUSE), bind-mount it into the sandbox, run
# the workload, and walk the tmpdir post-exec to push changes back.
#
# Tradeoffs vs. live FUSE:
#   - No live concurrent reads from host during sandbox run
#   - Initial hydrate cost; small if `seed_paths` scoped, big otherwise
#   - Deletes don't propagate by default (would require a tombstone walk)
#   - mtime resolution dictates change detection — fine for human-paced edits
#
# Tradeoffs vs. nothing:
#   - Mac dev iteration on the chatbot stack works without a Linux VM
#   - Exercises the same Sandbox + bind code path as prod, just w/ a non-FUSE
#     source — the part that's macOS-broken is the FUSE source, not the bind


def _hydrate_path(backend: Any, src_path: str, host_root: str) -> int:
    """Download *src_path* from *backend* into *host_root*. Returns bytes
    written. Path semantics mirror the backend's ``ls_info`` view: src_path
    is rooted at "/", and everything under it lands at the same relative
    path inside *host_root*."""
    written = 0
    try:
        responses = backend.download_files([src_path])
    except Exception as exc:  # noqa: BLE001
        logger = __import__("logging").getLogger(__name__)
        logger.warning("bridge hydrate failed for %s: %s", src_path, exc)
        return 0
    for resp in responses or []:
        content = getattr(resp, "content", None)
        if content is None:
            continue
        rel = (getattr(resp, "path", src_path) or src_path).lstrip("/")
        host_path = os.path.join(host_root, rel)
        parent = os.path.dirname(host_path) or host_root
        os.makedirs(parent, exist_ok=True)
        parent_mode: int | None = None
        file_mode: int | None = None
        try:
            parent_mode = stat_mod.S_IMODE(os.stat(parent).st_mode)
            os.chmod(parent, 0o755)
        except OSError:
            parent_mode = None
        if os.path.exists(host_path):
            try:
                file_mode = stat_mod.S_IMODE(os.stat(host_path).st_mode)
                os.chmod(host_path, 0o644)
            except OSError:
                file_mode = None
        with open(host_path, "wb") as f:
            f.write(content)
        if file_mode is not None:
            try:
                os.chmod(host_path, file_mode)
            except OSError:
                pass
        if parent_mode is not None:
            try:
                os.chmod(parent, parent_mode)
            except OSError:
                pass
        written += len(content)
    return written


def _hydrate_walk(backend: Any, root: str, host_root: str) -> int:
    """Recursive hydrate: walk every file under *root* in the backend and
    materialize each. Falls back to "/" when *root* is unset."""
    if not root.startswith("/"):
        root = f"/{root}"
    written = 0
    queue: list[str] = [root]
    seen: set[str] = set()
    while queue:
        path = queue.pop(0)
        if path in seen:
            continue
        seen.add(path)
        try:
            entries = backend.ls_info(path) or []
        except Exception:
            entries = []
        for entry in entries:
            entry_path = entry.get("path") or ""
            if not entry_path:
                continue
            if entry.get("is_dir"):
                queue.append(entry_path)
            else:
                written += _hydrate_path(backend, entry_path, host_root)
    return written


def _snapshot_dir(host_root: str) -> dict[str, tuple[int, float]]:
    """Snapshot every file under *host_root* as ``rel_path -> (size, mtime)``.
    Used pre-exec so post-exec we can detect which files the guest changed."""
    snap: dict[str, tuple[int, float]] = {}
    for dirpath, _dirs, files in os.walk(host_root):
        for fname in files:
            full = os.path.join(dirpath, fname)
            try:
                st = os.stat(full)
            except OSError:
                continue
            rel = os.path.relpath(full, host_root)
            snap[rel] = (st.st_size, st.st_mtime)
    return snap


def _ensure_bridge_policy_dirs(
    host_root: str,
    readonly_prefixes: list[str] | tuple[str, ...] | None,
    *,
    write_policy: str = _WRITE_POLICY_OUTPUTS,
) -> None:
    """Materialize route roots so denied writes fail as permission errors.

    Bridge mode is a plain bind mount, not live FUSE. Creating the known
    backend route roots with read-only permissions gives Python/open() a real
    parent path, so `/uploads/foo` reports authorization failure instead of
    collapsing into ENOENT when there are no uploaded files yet.
    """
    os.makedirs(os.path.join(host_root, "outputs"), exist_ok=True)
    for readonly in _normalize_readonly_dirs(readonly_prefixes):
        os.makedirs(os.path.join(host_root, readonly.lstrip("/")), exist_ok=True)
    _apply_bridge_write_policy(
        host_root,
        readonly_prefixes,
        write_policy=write_policy,
    )


def _materialize_bridge_route_dirs(
    host_root: str,
    readonly_prefixes: list[str] | tuple[str, ...] | None,
) -> None:
    """Create bridge route roots before hydration without making them read-only."""
    os.makedirs(os.path.join(host_root, "outputs"), exist_ok=True)
    for readonly in _normalize_readonly_dirs(readonly_prefixes):
        os.makedirs(os.path.join(host_root, readonly.lstrip("/")), exist_ok=True)


def _apply_bridge_write_policy(
    host_root: str,
    readonly_prefixes: list[str] | tuple[str, ...] | None,
    *,
    write_policy: str = _WRITE_POLICY_OUTPUTS,
) -> None:
    os.chmod(os.path.join(host_root, "outputs"), 0o755)
    for readonly in _normalize_readonly_dirs(readonly_prefixes):
        root = os.path.join(host_root, readonly.lstrip("/"))
        if not os.path.exists(root):
            continue
        for dirpath, dirs, files in os.walk(root):
            for fname in files:
                try:
                    full = os.path.join(dirpath, fname)
                    backend_path = "/" + os.path.relpath(full, host_root).replace(
                        os.sep, "/"
                    )
                    os.chmod(
                        full,
                        0o644
                        if _is_sandbox_writable_path(
                            backend_path,
                            write_policy=write_policy,
                        )
                        else 0o444,
                    )
                except OSError:
                    pass
            for dirname in dirs:
                try:
                    os.chmod(os.path.join(dirpath, dirname), 0o555)
                except OSError:
                    pass
        try:
            os.chmod(root, 0o555)
        except OSError:
            pass


def _changed_backend_paths(
    host_root: str,
    pre_snapshot: dict[str, tuple[int, float]],
) -> list[str]:
    """Return backend paths for files created or modified under *host_root*."""
    changed: list[str] = []
    for dirpath, _dirs, files in os.walk(host_root):
        for fname in files:
            full = os.path.join(dirpath, fname)
            try:
                st = os.stat(full)
            except OSError:
                continue
            rel = os.path.relpath(full, host_root)
            prev = pre_snapshot.get(rel)
            if prev is not None and prev == (st.st_size, st.st_mtime):
                continue
            changed.append("/" + rel.replace(os.sep, "/"))
    return changed


def _deleted_backend_paths(
    host_root: str,
    pre_snapshot: dict[str, tuple[int, float]],
) -> list[str]:
    """Return backend paths for files present before the run and missing now."""
    deleted: list[str] = []
    for rel in pre_snapshot:
        if os.path.exists(os.path.join(host_root, rel)):
            continue
        deleted.append("/" + rel.replace(os.sep, "/"))
    return deleted


def _flush_changes(
    backend: Any,
    host_root: str,
    pre_snapshot: dict[str, tuple[int, float]],
    *,
    write_policy: str = _WRITE_POLICY_OUTPUTS,
) -> int:
    """Push creates/modifications/deletions from the sandbox run to *backend*.

    Returns the number of object-store mutations applied.
    """
    flushed = 0
    for backend_path in _deleted_backend_paths(host_root, pre_snapshot):
        if not _is_sandbox_writable_path(
            backend_path,
            write_policy=write_policy,
        ):
            continue
        backend_concrete, stripped = _resolve_concrete(backend, backend_path)
        delete = getattr(backend_concrete, "delete", None)
        if delete is None:
            continue
        try:
            delete(stripped)
            flushed += 1
        except Exception as exc:  # noqa: BLE001
            logger = __import__("logging").getLogger(__name__)
            logger.warning("bridge delete flush failed for %s: %s", backend_path, exc)
    for dirpath, _dirs, files in os.walk(host_root):
        for fname in files:
            full = os.path.join(dirpath, fname)
            try:
                st = os.stat(full)
            except OSError:
                continue
            rel = os.path.relpath(full, host_root)
            prev = pre_snapshot.get(rel)
            if prev is not None and prev == (st.st_size, st.st_mtime):
                continue  # unchanged
            backend_path = "/" + rel.replace(os.sep, "/")
            if not _is_sandbox_writable_path(
                backend_path,
                write_policy=write_policy,
            ):
                continue
            try:
                with open(full, "rb") as f:
                    content = f.read()
            except OSError:
                continue
            backend_concrete, stripped = _resolve_concrete(backend, backend_path)
            overwrite = getattr(backend_concrete, "overwrite", None)
            if overwrite is None:
                continue
            try:
                overwrite(stripped, content)
                flushed += 1
            except Exception as exc:  # noqa: BLE001
                logger = __import__("logging").getLogger(__name__)
                logger.warning("bridge flush failed for %s: %s", backend_path, exc)
    return flushed


class _BridgeSessionCtx:
    """Async ctx mgr that fakes live FUSE on Macs that can't run nested VMs.

    Pre-exec: materialize *seed_paths* (or full tree) into a regular host
    tmpdir. Sandbox.create binds tmpdir into the guest at *guest_mount*.
    Post-exec: walk tmpdir, diff vs. snapshot, push changed files back to
    the backend via the same ``overwrite`` hook live FUSE uses on flush.

    This keeps the chatbot's call sites identical between live and bridge
    modes — only the propagation timing differs.
    """

    def __init__(
        self,
        *,
        backend: Any,
        session_id: str,
        image: str,
        guest_mount: str,
        mountpoint: str | None,
        seed_paths: list[str] | None,
        readonly_prefixes: list[str] | tuple[str, ...] | None,
        write_policy: str,
        network_disabled: bool,
        resource_caps: ResourceCaps,
    ) -> None:
        self._backend = backend
        self._session_id = session_id
        self._image = image
        self._guest_mount = guest_mount
        self._host_root = mountpoint or _default_mountpoint(session_id)
        self._seed_paths = seed_paths
        self._readonly_prefixes = readonly_prefixes
        self._write_policy = _normalize_write_policy(write_policy)
        self._network_disabled = network_disabled
        self._resource_caps = resource_caps
        self._sandbox_name = _unique_sandbox_name(session_id)
        self._sandbox: Any = None
        self._snapshot: dict[str, tuple[int, float]] = {}

    async def __aenter__(self) -> _AsyncSandboxSession:
        import asyncio

        from microsandbox import Sandbox, Volume  # type: ignore[import-untyped]

        loop = asyncio.get_running_loop()

        def _hydrate_blocking() -> None:
            os.makedirs(self._host_root, exist_ok=True)
            _materialize_bridge_route_dirs(self._host_root, self._readonly_prefixes)
            if self._seed_paths is None:
                _hydrate_walk(self._backend, "/", self._host_root)
            else:
                for path in self._seed_paths:
                    if path.endswith("/"):
                        _hydrate_walk(self._backend, path, self._host_root)
                    else:
                        _hydrate_path(self._backend, path, self._host_root)
            _ensure_bridge_policy_dirs(
                self._host_root,
                self._readonly_prefixes,
                write_policy=self._write_policy,
            )
            self._snapshot = _snapshot_dir(self._host_root)

        await loop.run_in_executor(None, _hydrate_blocking)

        cfg = _sandbox_create_config(
            name=self._sandbox_name,
            image=self._image,
            volumes={self._guest_mount: Volume.bind(self._host_root)},
            memory_mib=self._resource_caps.vm_memory_mib,
            cpus=self._resource_caps.vm_cpu_count,
            network_disabled=self._network_disabled,
        )
        auth = _ecr_registry_auth(self._image)
        if auth is not None:
            cfg["registry_auth"] = auth
        self._sandbox = await _create_microsandbox(Sandbox, cfg)
        return _AsyncSandboxSession(sandbox=self._sandbox, mountpoint=self._host_root)

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        import asyncio

        if self._sandbox is not None:
            try:
                await self._sandbox.stop_and_wait()
            except Exception:
                pass

        loop = asyncio.get_running_loop()

        def _flush_blocking() -> int:
            changed = _changed_backend_paths(
                self._host_root,
                self._snapshot,
            )
            deleted = _deleted_backend_paths(
                self._host_root,
                self._snapshot,
            )
            denied = [
                path
                for path in changed + deleted
                if not _is_sandbox_writable_path(
                    path,
                    write_policy=self._write_policy,
                )
            ]
            if denied:
                preview = ", ".join(denied[:10])
                suffix = "" if len(denied) <= 10 else ", ..."
                raise RuntimeError(
                    "sandbox write denied: write artifacts only under "
                    "/workspace/outputs (backend path /outputs/). "
                    f"Disallowed changed path(s): {preview}{suffix}"
                )
            return _flush_changes(
                self._backend,
                self._host_root,
                self._snapshot,
                write_policy=self._write_policy,
            )

        await loop.run_in_executor(None, _flush_blocking)
        return False


# ----------------------------------------------------------------------
# SandboxBackendProtocol-compatible S3 backend
# ----------------------------------------------------------------------


try:
    from deepagents.backends.protocol import (  # type: ignore[import-untyped]
        ExecuteResponse as _ExecuteResponse,
    )
    from deepagents.backends.protocol import (
        SandboxBackendProtocol as _SandboxBackendProtocol,
    )

    _HAS_DEEPAGENTS = True
except ImportError:
    _HAS_DEEPAGENTS = False

    @dataclass
    class _ExecuteResponse:  # type: ignore[no-redef]
        """Minimal fallback so :class:`S3SandboxBackend` keeps working without
        the upstream deepagents protocol installed.

        Only the fields :meth:`S3SandboxBackend.aexecute` actually constructs
        are modeled — extend if more callers appear.
        """

        output: str = ""
        exit_code: int = 0
        truncated: bool = False

    _SandboxBackendProtocol = None  # type: ignore[assignment]


# Module-level registry of live ``S3SandboxBackend`` instances keyed by
# ``session_id``. Lets the chatbot lifecycle (e.g. ``ChatbotManager.delete_thread``)
# look up + tear down the right backend without needing a handle to the
# closure-scoped factory cache. Cleared by ``aclose()``.
_LIVE_SANDBOX_BACKENDS: dict[str, "S3SandboxBackend"] = {}


async def aclose_session(session_id: str) -> bool:
    """Close + remove the ``S3SandboxBackend`` for *session_id* if registered.

    Returns True if a backend was found and closed, False if no backend
    matched (e.g. thread never executed any code, or already closed).

    Wire this into chatbot session-end hooks so each thread's microVM and
    bridge tmpdir get reclaimed instead of leaking onto disk.
    """
    backend = _LIVE_SANDBOX_BACKENDS.get(session_id)
    if backend is None:
        return False
    await backend.aclose()
    return True


class S3SandboxBackend:
    """``S3Backend`` + microsandbox ``execute`` — registered as a virtual
    subclass of ``deepagents.backends.SandboxBackendProtocol`` so the agent
    gets a ``bash`` tool that runs inside a microVM bound to the same tree.

    NB: virtual subclass (via ``ABCMeta.register``) — *not* concrete
    inheritance — because the protocol's base class defines every file op
    as ``raise NotImplementedError``. Concrete inheritance would shadow our
    ``__getattr__`` delegation to the wrapped backend (Python's MRO finds
    the abstract method first and never falls back to ``__getattr__``).
    Virtual registration keeps ``isinstance(b, SandboxBackendProtocol)``
    truthy while leaving the class hierarchy clean.

    Lifecycle: the microVM is created lazily on the first ``aexecute`` call
    and stays warm for the lifetime of this backend (which is the chat
    thread). Call ``aclose()`` on session teardown to stop the VM and
    propagate any final unflushed changes.

    File ops (``ls``/``read``/``write``/``edit``/``upload_files``/``download_files``)
    delegate to the wrapped ``S3Backend`` — S3 stays the source of truth.
    Code exec and file flush happen in the microVM's bind-mounted view.

    Why subclass-by-composition instead of ``BaseSandbox``: ``BaseSandbox``
    treats the sandbox FS as authoritative and shells everything through
    ``execute``. Our authority is S3; we want sandbox writes to *propagate
    back* to S3, not the other way around. Composition keeps both worlds:

    - ``ls``/``read``/``write``/etc → fast S3 ops (no VM round-trip)
    - ``execute`` → microVM with bridge-mounted S3 view, post-flush sync
    """

    def __init__(
        self,
        wrapped: Any,
        *,
        session_id: str,
        image: str | None = None,
        guest_mount: str = "/workspace",
        seed_paths: list[str] | None = None,
        readonly_prefixes: list[str] | tuple[str, ...] | None = None,
        flush_after_execute: bool = True,
        write_policy: str = _WRITE_POLICY_OUTPUTS,
        resource_caps: ResourceCaps | None = None,
    ) -> None:
        self._wrapped = wrapped
        self._session_id = session_id
        self._runtime_id = f"{_short_session_key(session_id)}-{uuid.uuid4().hex[:8]}"
        self._image = image or _default_sandbox_image()
        self._guest_mount = guest_mount
        self._write_policy = _normalize_write_policy(write_policy)
        self._resource_caps = resource_caps or ResourceCaps.from_env()
        self._caps_tracker = CapsTracker(self._resource_caps)
        # Register in the module-level lookup so chatbot lifecycle hooks
        # can find + aclose this backend without holding a direct handle.
        # Last-write-wins: re-creating a backend for the same thread (e.g.
        # after a hot reload) overwrites the prior registration.
        _LIVE_SANDBOX_BACKENDS[session_id] = self
        try:
            from simulation_web_app.chatbot.actor_supervisor import (
                get_actor_supervisor,
            )

            get_actor_supervisor().register_existing(
                session_id,
                self,
                caps=self._caps_tracker,
            )
        except Exception:
            pass
        self._seed_paths = seed_paths
        self._readonly_prefixes = readonly_prefixes
        self._flush_after_execute = flush_after_execute
        self._ctx: Any = None
        self._session: _AsyncSandboxSession | None = None
        self._init_lock: Any = None  # asyncio.Lock created lazily

    # ------------------------------------------------------------------
    # SandboxBackendProtocol surface
    # ------------------------------------------------------------------

    @property
    def id(self) -> str:
        """Stable id used by deepagents middleware to scope this backend."""
        safe = "".join(
            c if c.isalnum() or c in {"-", "_"} else "_" for c in self._session_id
        )
        return f"ark-msb-{safe}"

    async def aexecute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> Any:
        """Run *command* in the warm microVM. Returns ``ExecuteResponse``.

        On Mac (M1/M2) this is bridge-mode: pre-flushed snapshot diff
        propagates guest writes back to S3 after the command finishes (when
        ``flush_after_execute`` is set). On Linux/M3+ it's live FUSE — flush
        is a no-op because writes hit S3 the moment the guest's ``write()``
        syscall returns.
        """
        ExecuteResponse = _ExecuteResponse  # captured at import; raises NameError if deepagents missing

        await self._ensure_session()
        sess = self._session
        assert sess is not None  # _ensure_session post-condition

        invalidate_fuse_cache = getattr(self._ctx, "invalidate_fuse_cache", None)
        if invalidate_fuse_cache is not None:
            invalidate_fuse_cache("/")

        # Pre-execute delta-hydrate (bridge mode only). The agent may have
        # written new files via deepagents file tools since the last run,
        # which land in S3 directly — the bridge tmpdir doesn't see those
        # without us re-pulling. Snapshot the wrapped backend, diff against
        # tmpdir, hydrate anything new or changed. No-op for live FUSE mode.
        if isinstance(self._ctx, _BridgeSessionCtx):
            import asyncio as _asyncio

            loop = _asyncio.get_running_loop()
            ctx = self._ctx

            def _delta_hydrate() -> None:
                _hydrate_walk(self._wrapped, "/", ctx._host_root)
                _ensure_bridge_policy_dirs(
                    ctx._host_root,
                    self._readonly_prefixes,
                    write_policy=ctx._write_policy,
                )
                # Re-snapshot so the post-execute flush only catches
                # changes the *guest* makes, not files we just hydrated.
                ctx._snapshot = _snapshot_dir(ctx._host_root)

            try:
                await loop.run_in_executor(None, _delta_hydrate)
            except Exception:
                pass

        # Refresh the symlink farm so any new top-level dir we just hydrated
        # (or any new dir from a prior flush) is reachable at the bare path
        # the agent's file tools use. Cheap one-pass shell loop; failures
        # don't break the user's command.
        await self._sync_path_aliases()

        import asyncio as _asyncio

        wall_timeout = timeout or self._resource_caps.max_aexecute_wall_s
        try:
            out = await _asyncio.wait_for(
                sess.sandbox.shell(command),
                timeout=wall_timeout,
            )
        except TimeoutError:
            return ExecuteResponse(
                output=f"sandbox timeout after {wall_timeout}s",
                exit_code=124,
                truncated=False,
            )
        except Exception as exc:
            return ExecuteResponse(
                output=f"sandbox error: {exc}",
                exit_code=-1,
                truncated=False,
            )

        text = (getattr(out, "stdout_text", "") or "") + (
            getattr(out, "stderr_text", "") or ""
        )

        # Bridge mode: walk the host tmpdir + push changed files back to S3
        # so subsequent S3-direct reads see the guest's writes. No-op for
        # live FUSE mode (which is already write-through).
        if self._flush_after_execute and isinstance(self._ctx, _BridgeSessionCtx):
            import asyncio as _asyncio

            loop = _asyncio.get_running_loop()
            ctx = self._ctx

            def _flush_blocking() -> list[str]:
                changed = _changed_backend_paths(ctx._host_root, ctx._snapshot)
                deleted = _deleted_backend_paths(ctx._host_root, ctx._snapshot)
                denied = [
                    path
                    for path in changed + deleted
                    if not _is_sandbox_writable_path(
                        path,
                        write_policy=ctx._write_policy,
                    )
                ]
                if denied:
                    return denied
                _flush_changes(
                    self._wrapped,
                    ctx._host_root,
                    ctx._snapshot,
                    write_policy=ctx._write_policy,
                )
                # Re-snapshot so the next execute only sees *new* changes.
                ctx._snapshot = _snapshot_dir(ctx._host_root)
                return []

            try:
                denied_paths = await loop.run_in_executor(None, _flush_blocking)
            except Exception:
                denied_paths = []
            if denied_paths:
                preview = ", ".join(denied_paths[:10])
                suffix = "" if len(denied_paths) <= 10 else ", ..."
                return ExecuteResponse(
                    output=(
                        text
                        + "\n\nsandbox write denied: write artifacts only under "
                        + "/workspace/outputs (backend path /outputs/). "
                        + f"Disallowed changed path(s): {preview}{suffix}"
                    ),
                    exit_code=1,
                    truncated=False,
                )

        return ExecuteResponse(
            output=text,
            exit_code=getattr(out, "exit_code", None),
            truncated=False,
        )

    def execute(self, command: str, *, timeout: int | None = None) -> Any:
        """Sync façade — runs ``aexecute`` on a one-off event loop.

        Real chatbot uses ``aexecute``; this is here so the
        ``SandboxBackendProtocol`` contract is fully satisfied for callers
        that grab the sync method via reflection.
        """
        import asyncio

        return asyncio.run(self.aexecute(command, timeout=timeout))

    async def aclose(self) -> None:
        """Stop the microVM, flush remaining bridge changes, and free disk.

        Three-phase cleanup so the chat thread leaves no garbage:

        1. ``__aexit__`` on the inner ctx stops the VM and runs the final
           bridge-mode flush. After this the guest is gone but the sandbox
           dir (``~/.microsandbox/sandboxes/<name>/``) and our bridge tmpdir
           are still on disk.
        2. ``Sandbox.remove`` deletes the sandbox dir (rootfs + overlay +
           logs ~ 300+ MB per thread). Without this, every chat thread
           leaves a permanent footprint.
        3. ``shutil.rmtree`` on the bridge tmpdir wipes the host-side
           hydrated files. S3 is the source of truth — anything that
           mattered already flushed in step 1.

        Safe to call multiple times. Should be invoked from the chatbot
        session-end hook (``ChatbotManager`` cleanup)."""
        if self._ctx is None:
            return

        ctx = self._ctx
        host_root = getattr(ctx, "_host_root", None) or getattr(
            ctx, "_mountpoint", None
        )
        sandbox_name = getattr(self._ctx, "_sandbox_name", None)

        try:
            await ctx.__aexit__(None, None, None)
        finally:
            self._ctx = None
            self._session = None

        # Disk reclamation. Best-effort; logged-and-swallowed because the
        # VM is already stopped and S3 holds the durable state.
        try:
            from microsandbox import Sandbox  # type: ignore[import-untyped]

            if sandbox_name:
                await Sandbox.remove(sandbox_name)
        except Exception as exc:  # noqa: BLE001
            __import__("logging").getLogger(__name__).debug(
                "Sandbox.remove(%s) failed (ignored): %s", sandbox_name, exc
            )

        if host_root:
            import shutil

            try:
                shutil.rmtree(host_root, ignore_errors=True)
            except Exception:
                pass

        # Drop registry entry only if this instance is still the registered
        # one (avoid evicting a successor created during a thread restart).
        if _LIVE_SANDBOX_BACKENDS.get(self._session_id) is self:
            _LIVE_SANDBOX_BACKENDS.pop(self._session_id, None)
        try:
            from simulation_web_app.chatbot.actor_supervisor import (
                get_actor_supervisor,
            )

            get_actor_supervisor().unregister_if_current(self._session_id, self)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # BackendProtocol delegation
    # ------------------------------------------------------------------

    # Write surface — every method strips a leading ``/workspace`` from the
    # caller-supplied path before policy-checking and delegating, so file
    # tools and sandbox bash resolve to the same S3 key. Async siblings
    # route through the sync method so the policy check fires on both paths
    # (deepagents calls ``awrite`` from its async tools; without these
    # overrides the lookup falls through ``__getattr__`` straight to the
    # wrapped backend, bypassing the writable-path policy).

    def write(self, file_path: str, content: str) -> Any:
        """Write via deepagents file tools, constrained by the mount policy."""
        norm = _strip_workspace_prefix(file_path)
        if not _is_sandbox_writable_path(norm, write_policy=self._write_policy):
            from deepagents.backends.protocol import WriteResult

            return WriteResult(error=_sandbox_write_denied_message(file_path))
        return self._wrapped.write(norm, content)

    async def awrite(self, file_path: str, content: str) -> Any:
        import asyncio as _asyncio

        return await _asyncio.to_thread(self.write, file_path, content)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> Any:
        """Edit via deepagents file tools, constrained by the mount policy."""
        norm = _strip_workspace_prefix(file_path)
        if not _is_sandbox_writable_path(norm, write_policy=self._write_policy):
            from deepagents.backends.protocol import EditResult

            return EditResult(error=_sandbox_write_denied_message(file_path))
        return self._wrapped.edit(norm, old_string, new_string, replace_all)

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> Any:
        import asyncio as _asyncio

        return await _asyncio.to_thread(
            self.edit, file_path, old_string, new_string, replace_all
        )

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[Any]:
        """Upload files via backend API, constrained by the mount policy."""
        from deepagents.backends.protocol import FileUploadResponse

        allowed: list[tuple[int, str, bytes]] = []
        responses: list[Any] = [None] * len(files)
        for idx, (file_path, content) in enumerate(files):
            norm = _strip_workspace_prefix(file_path)
            if not _is_sandbox_writable_path(norm, write_policy=self._write_policy):
                responses[idx] = FileUploadResponse(
                    path=file_path,
                    error=_sandbox_write_denied_message(file_path),
                )
            else:
                allowed.append((idx, norm, content))

        if allowed:
            delegated = self._wrapped.upload_files(
                [(file_path, content) for _idx, file_path, content in allowed]
            )
            for (idx, _file_path, _content), response in zip(allowed, delegated):
                responses[idx] = response

        return responses

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[Any]:
        import asyncio as _asyncio

        return await _asyncio.to_thread(self.upload_files, files)

    def delete(self, file_path: str) -> None:
        norm = _strip_workspace_prefix(file_path)
        if not _is_sandbox_writable_path(norm, write_policy=self._write_policy):
            raise _sandbox_write_denied(file_path)
        self._wrapped.delete(norm)

    async def adelete(self, file_path: str) -> None:
        import asyncio as _asyncio

        await _asyncio.to_thread(self.delete, file_path)

    def copy(self, src_path: str, dst_path: str) -> None:
        norm_dst = _strip_workspace_prefix(dst_path)
        if not _is_sandbox_writable_path(norm_dst, write_policy=self._write_policy):
            raise _sandbox_write_denied(dst_path)
        self._wrapped.copy(_strip_workspace_prefix(src_path), norm_dst)

    async def acopy(self, src_path: str, dst_path: str) -> None:
        import asyncio as _asyncio

        await _asyncio.to_thread(self.copy, src_path, dst_path)

    def overwrite(self, file_path: str, content: bytes) -> None:
        norm = _strip_workspace_prefix(file_path)
        if not _is_sandbox_writable_path(norm, write_policy=self._write_policy):
            raise _sandbox_write_denied(file_path)
        self._wrapped.overwrite(norm, content)

    async def aoverwrite(self, file_path: str, content: bytes) -> None:
        import asyncio as _asyncio

        await _asyncio.to_thread(self.overwrite, file_path, content)

    def overwrite_stream(self, file_path: str, fileobj: Any) -> None:
        norm = _strip_workspace_prefix(file_path)
        if not _is_sandbox_writable_path(norm, write_policy=self._write_policy):
            raise _sandbox_write_denied(file_path)
        self._wrapped.overwrite_stream(norm, fileobj)

    async def aoverwrite_stream(self, file_path: str, fileobj: Any) -> None:
        import asyncio as _asyncio

        await _asyncio.to_thread(self.overwrite_stream, file_path, fileobj)

    # Read surface — strip /workspace prefix on the way in so file-tool
    # reads resolve to the same S3 key the sandbox FUSE mount uses.

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> Any:
        return self._wrapped.read(_strip_workspace_prefix(file_path), offset, limit)

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> Any:
        import asyncio as _asyncio

        return await _asyncio.to_thread(self.read, file_path, offset, limit)

    def ls(self, path: str) -> Any:
        return self._wrapped.ls(_strip_workspace_prefix(path))

    async def als(self, path: str) -> Any:
        import asyncio as _asyncio

        return await _asyncio.to_thread(self.ls, path)

    def ls_info(self, path: str) -> Any:
        return self._wrapped.ls_info(_strip_workspace_prefix(path))

    async def als_info(self, path: str) -> Any:
        import asyncio as _asyncio

        return await _asyncio.to_thread(self.ls_info, path)

    def glob(self, pattern: str, path: str = "/") -> Any:
        return self._wrapped.glob(pattern, _strip_workspace_prefix(path))

    async def aglob(self, pattern: str, path: str = "/") -> Any:
        import asyncio as _asyncio

        return await _asyncio.to_thread(self.glob, pattern, path)

    def glob_info(self, pattern: str, path: str = "/") -> Any:
        return self._wrapped.glob_info(pattern, _strip_workspace_prefix(path))

    async def aglob_info(self, pattern: str, path: str = "/") -> Any:
        import asyncio as _asyncio

        return await _asyncio.to_thread(self.glob_info, pattern, path)

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> Any:
        norm_path = _strip_workspace_prefix(path) if path is not None else path
        return self._wrapped.grep(pattern, norm_path, glob)

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> Any:
        import asyncio as _asyncio

        return await _asyncio.to_thread(self.grep, pattern, path, glob)

    def grep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> Any:
        norm_path = _strip_workspace_prefix(path) if path is not None else path
        return self._wrapped.grep_raw(pattern, norm_path, glob)

    async def agrep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> Any:
        import asyncio as _asyncio

        return await _asyncio.to_thread(self.grep_raw, pattern, path, glob)

    def download_files(self, paths: list[str]) -> list[Any]:
        return self._wrapped.download_files([_strip_workspace_prefix(p) for p in paths])

    async def adownload_files(self, paths: list[str]) -> list[Any]:
        import asyncio as _asyncio

        return await _asyncio.to_thread(self.download_files, paths)

    def __getattr__(self, name: str) -> Any:
        # Forward any unknown attribute (caps tracker, stats, etc.) to the
        # wrapped S3Backend. All path-taking BackendProtocol methods are
        # explicitly overridden above so this never sees a path arg.
        return getattr(self._wrapped, name)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _ensure_session(self) -> None:
        if self._session is not None:
            return
        import asyncio

        if self._init_lock is None:
            self._init_lock = asyncio.Lock()
        async with self._init_lock:
            if self._session is not None:
                return
            self._ctx = await microsandbox_session(
                backend=self._wrapped,
                session_id=self._runtime_id,
                image=self._image,
                guest_mount=self._guest_mount,
                seed_paths=self._seed_paths,
                readonly_prefixes=self._readonly_prefixes,
                write_policy=self._write_policy,
                resource_caps=self._resource_caps,
                caps_tracker=self._caps_tracker,
            )
            self._session = await self._ctx.__aenter__()

    async def _sync_path_aliases(self) -> None:
        """Make every top-level dir in the bridge mount also reachable from
        the guest's root, so a file at ``/uploads/foo.csv`` (file-tool view)
        is also at ``/uploads/foo.csv`` from the shell — not just under
        ``/workspace/uploads/foo.csv``.

        Re-run before each ``aexecute`` because the agent may have created
        new top-level prefixes via file tools between commands.
        """
        if self._session is None:
            return
        guest_mount = self._guest_mount
        # Single shell pass: list children of the bridge mount, symlink each
        # into root if not already present and not a name we must not shadow
        # (everything Linux ships in /). We accept the symlink failing for
        # `/etc`, `/bin`, etc — that's the point of the existence guard.
        script = (
            f"set -e; mkdir -p {guest_mount}/outputs; "
            f"for d in {guest_mount}/*; do "
            f'  name=$(basename "$d"); '
            f'  if [ ! -e "/$name" ]; then ln -s "$d" "/$name"; fi; '
            f"done"
        )
        try:
            await self._session.sandbox.shell(script)
        except Exception:
            # Symlink farm is a convenience — not having it just means the
            # agent must use /workspace-prefixed paths in shell. Don't kill
            # the session over it.
            pass


# Virtual subclass registration — see ``S3SandboxBackend`` docstring.
if _HAS_DEEPAGENTS and _SandboxBackendProtocol is not None:
    _SandboxBackendProtocol.register(S3SandboxBackend)
