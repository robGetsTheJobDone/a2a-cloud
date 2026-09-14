from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import pytest

from sandbox_runtime import microsandbox_fuse as fuse_mod
from sandbox_runtime.microsandbox_fuse import S3FuseAdapter


pytestmark = pytest.mark.skipif(
    not fuse_mod._FUSE_AVAILABLE,
    reason="fusepy is not importable in this environment",
)


@dataclass
class _DownloadResponse:
    path: str
    content: bytes | None
    error: str | None = None


class _FakeBackend:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = {
            self._norm(path): content for path, content in objects.items()
        }

    def _norm(self, path: str) -> str:
        return path if path.startswith("/") else f"/{path}"

    def ls_info(self, path: str) -> list[dict[str, Any]]:
        root = self._norm(path).rstrip("/") or "/"
        prefix = "" if root == "/" else root
        entries: list[dict[str, Any]] = []
        seen_dirs: set[str] = set()

        for object_path, content in sorted(self.objects.items()):
            if root != "/" and not object_path.startswith(prefix + "/"):
                continue
            rest = (
                object_path.strip("/")
                if root == "/"
                else object_path[len(prefix) + 1 :]
            )
            if not rest:
                continue
            name = rest.split("/", 1)[0]
            child_path = f"/{name}" if root == "/" else f"{prefix}/{name}"
            if "/" in rest:
                if child_path in seen_dirs:
                    continue
                seen_dirs.add(child_path)
                entries.append({"path": child_path, "is_dir": True, "size": 0})
            else:
                entries.append({
                    "path": child_path,
                    "is_dir": False,
                    "size": len(content),
                })
        return entries

    def download_files(self, paths: list[str]) -> list[_DownloadResponse]:
        responses: list[_DownloadResponse] = []
        for path in paths:
            norm = self._norm(path)
            responses.append(
                _DownloadResponse(path=norm, content=self.objects.get(norm))
            )
        return responses

    def read_range(self, path: str, offset: int, size: int) -> bytes:
        return self.objects[self._norm(path)][offset : offset + size]

    def overwrite(self, path: str, data: bytes) -> None:
        self.objects[self._norm(path)] = data

    def overwrite_stream(self, path: str, fileobj: Any) -> None:
        self.objects[self._norm(path)] = fileobj.read()


class _ClosingStreamBackend(_FakeBackend):
    def overwrite_stream(self, path: str, fileobj: Any) -> None:
        self.objects[self._norm(path)] = fileobj.read()
        fileobj.close()


def test_write_only_first_write_replaces_when_trunc_flag_is_missing() -> None:
    backend = _FakeBackend({
        "/outputs/text.svg": b"<svg>new</svg>\nSTALE-TAIL",
    })
    fs = S3FuseAdapter(backend)

    fh = fs.open("/outputs/text.svg", os.O_WRONLY)
    fs.write("/outputs/text.svg", b"<svg>ok</svg>\n", 0, fh)
    fs.release("/outputs/text.svg", fh)

    assert backend.objects["/outputs/text.svg"] == b"<svg>ok</svg>\n"


def test_rdwr_partial_overwrite_preserves_existing_tail() -> None:
    backend = _FakeBackend({"/outputs/data.bin": b"abcdef"})
    fs = S3FuseAdapter(backend)

    fh = fs.open("/outputs/data.bin", os.O_RDWR)
    fs.write("/outputs/data.bin", b"XY", 0, fh)
    fs.release("/outputs/data.bin", fh)

    assert backend.objects["/outputs/data.bin"] == b"XYcdef"


def test_truncate_flush_uploads_only_logical_size() -> None:
    backend = _FakeBackend({"/outputs/data.bin": b"abcdef"})
    fs = S3FuseAdapter(backend)

    fh = fs.open("/outputs/data.bin", os.O_RDWR)
    fs.truncate("/outputs/data.bin", 3, fh)
    fs.release("/outputs/data.bin", fh)

    assert backend.objects["/outputs/data.bin"] == b"abc"


def test_open_with_trunc_can_empty_file_without_write() -> None:
    backend = _FakeBackend({"/outputs/data.bin": b"abcdef"})
    fs = S3FuseAdapter(backend)

    fh = fs.open("/outputs/data.bin", os.O_WRONLY | os.O_TRUNC)
    fs.release("/outputs/data.bin", fh)

    assert backend.objects["/outputs/data.bin"] == b""


def test_flush_does_not_close_writable_handle_before_release() -> None:
    backend = _ClosingStreamBackend({})
    fs = S3FuseAdapter(backend)

    fh = fs.open("/outputs/live.txt", os.O_WRONLY | os.O_CREAT)
    fs.write("/outputs/live.txt", b"one", 0, fh)
    fs.flush("/outputs/live.txt", fh)
    fs.write("/outputs/live.txt", b"two", 3, fh)
    fs.release("/outputs/live.txt", fh)

    assert backend.objects["/outputs/live.txt"] == b"onetwo"


def test_fuse_mount_requests_atomic_open_truncation() -> None:
    assert fuse_mod._FUSE_CACHE_KWARGS["atomic_o_trunc"] is True
