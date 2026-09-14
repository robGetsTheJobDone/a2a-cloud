"""Per-chat sandbox resource caps.

The caps object is intentionally small and synchronous because FUSE callbacks
run on kernel worker threads. It tracks only host-side resources we can
enforce directly: open handles and writable spool sizes.
"""

from __future__ import annotations

import errno
import os
import threading
from dataclasses import dataclass, field

_GB = 1 << 30


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class ResourceCaps:
    vm_memory_mib: int = 512
    vm_cpu_count: int = 1
    max_single_file_bytes: int = 1 * _GB
    max_spool_total_bytes: int = 4 * _GB
    max_open_files: int = 64
    max_aexecute_wall_s: int = 300

    @classmethod
    def from_env(cls) -> "ResourceCaps":
        return cls(
            vm_memory_mib=_env_int("A2A_SB_VM_MEMORY_MIB", 512),
            vm_cpu_count=_env_int("A2A_SB_VM_CPU_COUNT", 1),
            max_single_file_bytes=_env_int("A2A_SB_MAX_SINGLE_FILE_BYTES", 1 * _GB),
            max_spool_total_bytes=_env_int("A2A_SB_MAX_SPOOL_TOTAL_BYTES", 4 * _GB),
            max_open_files=_env_int("A2A_SB_MAX_OPEN_FILES", 64),
            max_aexecute_wall_s=_env_int("A2A_SB_MAX_AEXECUTE_WALL_S", 300),
        )


@dataclass
class CapsTracker:
    caps: ResourceCaps = field(default_factory=ResourceCaps.from_env)
    _open_paths: dict[int, str] = field(default_factory=dict)
    _fh_sizes: dict[int, int] = field(default_factory=dict)
    _spool_total_bytes: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def reserve_open(self, fh: int, path: str, *, initial_size: int = 0) -> None:
        with self._lock:
            if len(self._open_paths) >= self.caps.max_open_files:
                raise OSError(errno.EMFILE, "too many open sandbox files", path)
            if initial_size > self.caps.max_single_file_bytes:
                raise OSError(errno.EFBIG, "sandbox file exceeds cap", path)
            next_total = self._spool_total_bytes + max(initial_size, 0)
            if next_total > self.caps.max_spool_total_bytes:
                raise OSError(errno.EFBIG, "sandbox spool exceeds cap", path)
            self._open_paths[fh] = path
            self._fh_sizes[fh] = max(initial_size, 0)
            self._spool_total_bytes = next_total

    def resize(self, fh: int, size: int) -> None:
        with self._lock:
            path = self._open_paths.get(fh, "<unknown>")
            if size > self.caps.max_single_file_bytes:
                raise OSError(errno.EFBIG, "sandbox file exceeds cap", path)
            previous = self._fh_sizes.get(fh, 0)
            next_total = self._spool_total_bytes - previous + max(size, 0)
            if next_total > self.caps.max_spool_total_bytes:
                raise OSError(errno.EFBIG, "sandbox spool exceeds cap", path)
            self._fh_sizes[fh] = max(size, 0)
            self._spool_total_bytes = next_total

    def release(self, fh: int) -> None:
        with self._lock:
            self._open_paths.pop(fh, None)
            self._spool_total_bytes -= self._fh_sizes.pop(fh, 0)
            if self._spool_total_bytes < 0:
                self._spool_total_bytes = 0

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "open_files": len(self._open_paths),
                "spool_total_bytes": self._spool_total_bytes,
                "vm_memory_mib": self.caps.vm_memory_mib,
                "vm_cpu_count": self.caps.vm_cpu_count,
            }


