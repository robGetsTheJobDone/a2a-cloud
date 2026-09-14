from __future__ import annotations

from pathlib import Path

from sandbox_runtime.microsandbox_fuse import (
    S3SandboxBackend,
    _flush_changes,
    _is_sandbox_writable_path,
    _snapshot_dir,
)


class _FakeBackend:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.written: list[tuple[str, object]] = []

    def delete(self, path: str) -> None:
        self.deleted.append(path)

    def write(self, path: str, content: object) -> str:
        self.written.append((path, content))
        return "ok"


def test_default_policy_keeps_workspace_prefixes_readonly() -> None:
    assert _is_sandbox_writable_path("/outputs/report.txt")
    assert _is_sandbox_writable_path("/memories/AGENTS.md")
    assert not _is_sandbox_writable_path("/agents/research/agent.py")


def test_workspace_policy_allows_user_paths_but_not_root() -> None:
    assert _is_sandbox_writable_path(
        "/agents/research/agent.py",
        write_policy="workspace",
    )
    assert _is_sandbox_writable_path("/data/input.csv", write_policy="read-write")
    assert not _is_sandbox_writable_path("/", write_policy="workspace")


def test_bridge_flush_deletes_removed_files_when_policy_allows(tmp_path: Path) -> None:
    agent_file = tmp_path / "agents" / "research" / "agent.py"
    agent_file.parent.mkdir(parents=True)
    agent_file.write_text("print('old')\n")
    snapshot = _snapshot_dir(str(tmp_path))
    agent_file.unlink()

    backend = _FakeBackend()

    assert _flush_changes(
        backend,
        str(tmp_path),
        snapshot,
        write_policy="workspace",
    ) == 1
    assert backend.deleted == ["/agents/research/agent.py"]


def test_bridge_flush_keeps_default_outputs_only_policy(tmp_path: Path) -> None:
    agent_file = tmp_path / "agents" / "research" / "agent.py"
    agent_file.parent.mkdir(parents=True)
    agent_file.write_text("print('old')\n")
    snapshot = _snapshot_dir(str(tmp_path))
    agent_file.unlink()

    backend = _FakeBackend()

    assert _flush_changes(backend, str(tmp_path), snapshot) == 0
    assert backend.deleted == []


def test_s3_sandbox_backend_honors_workspace_policy_for_file_tools() -> None:
    wrapped = _FakeBackend()
    backend = S3SandboxBackend(
        wrapped,
        session_id="test-workspace-policy",
        write_policy="workspace",
    )

    assert backend.write("/workspace/agents/demo.py", "print('ok')") == "ok"
    assert wrapped.written == [("/agents/demo.py", "print('ok')")]
