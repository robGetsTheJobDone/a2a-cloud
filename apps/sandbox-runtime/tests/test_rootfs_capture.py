from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from sandbox_runtime.rootfs_capture import RootfsCapture


class _FakeFs:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = dict(files)
        self.modified = {path: float(i + 1) for i, path in enumerate(self.files)}

    async def list(self, path: str) -> list[dict[str, object]]:
        root = "/" if path == "/" else path.rstrip("/")
        prefix = "" if root == "/" else root
        seen_dirs: set[str] = set()
        out: list[dict[str, object]] = []
        for file_path, data in sorted(self.files.items()):
            if root != "/" and not file_path.startswith(prefix + "/"):
                continue
            rest = (
                file_path.strip("/")
                if root == "/"
                else file_path[len(prefix) + 1 :]
            )
            if not rest:
                continue
            first = rest.split("/", 1)[0]
            child_path = f"/{first}" if root == "/" else f"{prefix}/{first}"
            if "/" in rest:
                if child_path in seen_dirs:
                    continue
                seen_dirs.add(child_path)
                out.append({"path": child_path, "kind": "directory", "size": 0})
            else:
                out.append({
                    "path": child_path,
                    "kind": "file",
                    "size": len(data),
                    "modified": self.modified[file_path],
                })
        return out

    async def copy_to_host(self, guest_path: str, host_path: str) -> None:
        Path(host_path).write_bytes(self.files[guest_path])


class _FakeSandbox:
    def __init__(self, fs: _FakeFs) -> None:
        self.fs = fs


class _FakeBackend:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def overwrite(self, path: str, data: bytes) -> None:
        self.objects[path.lstrip("/")] = data

    def overwrite_stream(self, path: str, fileobj: Any) -> None:
        self.objects[path.lstrip("/")] = fileobj.read()


class _DenyingBackend:
    def overwrite(self, path: str, data: bytes) -> None:
        raise PermissionError(f"workspace path is not writable by grant: {path}")

    def overwrite_stream(self, path: str, fileobj: Any) -> None:
        raise PermissionError(f"workspace path is not writable by grant: {path}")


def test_persist_delta_captures_changed_files_outside_workspace() -> None:
    async def _run() -> None:
        fs = _FakeFs({
            "/etc/config": b"base",
            "/workspace/outputs/already-handled.txt": b"skip",
        })
        sandbox = _FakeSandbox(fs)
        backend = _FakeBackend()
        capture = RootfsCapture(backend, session_id="sb-test")

        before = await capture.snapshot(sandbox)
        fs.files["/tmp/result.txt"] = b"durable"
        fs.modified["/tmp/result.txt"] = 50.0
        fs.files["/workspace/outputs/native.txt"] = b"native"
        fs.modified["/workspace/outputs/native.txt"] = 51.0

        files = await capture.persist_delta(
            sandbox,
            before=before,
            capture_id="cap-1",
        )

        assert {
            "original_path": "/tmp/result.txt",
            "workspace_path": "outputs/rootfs-captures/cap-1/fs/tmp/result.txt",
            "size": 7,
            "source": "rootfs_capture",
        } in files
        assert backend.objects[
            "outputs/rootfs-captures/cap-1/fs/tmp/result.txt"
        ] == b"durable"
        assert all(
            item["original_path"] != "/workspace/outputs/native.txt"
            for item in files
        )

        manifest = json.loads(
            backend.objects["outputs/rootfs-captures/cap-1/manifest.json"]
        )
        assert manifest["session_id"] == "sb-test"
        assert manifest["files"][0]["original_path"] == "/tmp/result.txt"

    asyncio.run(_run())


def test_persist_delta_skips_rootfs_capture_when_grant_denies_outputs() -> None:
    async def _run() -> None:
        fs = _FakeFs({"/etc/config": b"base"})
        sandbox = _FakeSandbox(fs)
        capture = RootfsCapture(_DenyingBackend(), session_id="sb-test")

        before = await capture.snapshot(sandbox)
        fs.files["/tmp/result.txt"] = b"durable"
        fs.modified["/tmp/result.txt"] = 50.0

        files = await capture.persist_delta(
            sandbox,
            before=before,
            capture_id="cap-denied",
        )

        assert files == []

    asyncio.run(_run())


def test_persist_delta_skips_manifest_when_nothing_changed() -> None:
    async def _run() -> None:
        fs = _FakeFs({"/etc/config": b"base"})
        sandbox = _FakeSandbox(fs)
        backend = _FakeBackend()
        capture = RootfsCapture(backend, session_id="sb-test")

        before = await capture.snapshot(sandbox)
        files = await capture.persist_delta(sandbox, before=before, capture_id="cap-2")

        assert files == []
        assert backend.objects == {}

    asyncio.run(_run())
