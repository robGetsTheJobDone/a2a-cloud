from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

import httpx
import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")
Error = playwright_sync.Error
expect = playwright_sync.expect
sync_playwright = playwright_sync.sync_playwright


pytestmark = pytest.mark.e2e


def _write_e2e_project(project: Path) -> None:
    project.mkdir()
    (project / "a2a.yaml").write_text(
        "name: e2e-dev\nversion: 0.1.0\nentrypoint: agent:E2EDevAgent\n"
    )
    (project / "agent.py").write_text(
        """
from a2a_pack import A2AAgent, NoAuth, RunContext, WorkspaceAccess, WorkspaceMode, skill


class E2EDevAgent(A2AAgent[None, NoAuth]):
    name = "e2e-dev"
    description = "Browser E2E dev console agent"
    version = "0.1.0"
    auth_model = NoAuth
    workspace_access = WorkspaceAccess.dynamic(
        allowed_modes=(WorkspaceMode.READ_WRITE_OVERLAY,),
        require_reason=False,
    )

    @skill(description="Echo the prompt into a workspace output file.")
    async def ask(self, ctx: RunContext[NoAuth], prompt: str) -> str:
        await ctx.emit_progress("writing e2e output")
        result = ctx.workspace_backend().write(
            "/workspace/outputs/e2e-result.txt",
            prompt,
        )
        if result.error:
            raise RuntimeError(result.error)
        return result.path or ""
"""
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_server(proc: subprocess.Popen[str], port: int) -> None:
    deadline = time.monotonic() + 20
    url = f"http://127.0.0.1:{port}/healthz"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out, err = proc.communicate(timeout=1)
            raise AssertionError(
                f"a2a dev exited before startup\nstdout:\n{out}\nstderr:\n{err}"
            )
        try:
            if httpx.get(url, timeout=0.5).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.1)
    out, err = proc.communicate(timeout=1) if proc.poll() is not None else ("", "")
    raise AssertionError(f"a2a dev did not start\nstdout:\n{out}\nstderr:\n{err}")


@pytest.fixture
def dev_server(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    project = tmp_path / "e2e-dev"
    _write_e2e_project(project)
    port = _free_port()
    env = os.environ.copy()
    env.pop("A2A_PROJECT_DIR", None)
    env.pop("A2A_ENTRYPOINT", None)
    env.pop("A2A_ENV_FILE", None)
    env.pop("A2A_LOCAL_DEV", None)
    env.pop("A2A_LOCAL_WORKSPACE_DIR", None)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "a2a_pack.cli.main",
            "dev",
            "--local",
            "--project",
            str(project),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-reload",
            "--host-runtime",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_server(proc, port)
        yield f"http://127.0.0.1:{port}", project
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=5)


def test_dev_console_uploads_runs_skill_and_shows_output_file(
    dev_server: tuple[str, Path],
    tmp_path: Path,
) -> None:
    base_url, project = dev_server
    upload = tmp_path / "source.txt"
    upload.write_text("hello from browser e2e")

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Error as exc:
            pytest.skip(f"Playwright Chromium is not available: {exc}")
        page = browser.new_page(viewport={"width": 1440, "height": 950})
        try:
            page.goto(f"{base_url}/_dev", wait_until="networkidle")

            expect(page.get_by_text("e2e-dev").first).to_be_visible()
            expect(page.get_by_text("local dev")).to_be_visible()

            page.set_input_files('input[type="file"]', str(upload))
            expect(
                page.locator(".file-row", has_text="inputs/source.txt").first
            ).to_be_visible(timeout=5000)

            page.locator("textarea").first.fill("Use the uploaded file and write an output.")
            page.get_by_role("button", name="Run").click()

            expect(page.get_by_text("[progress] writing e2e output")).to_be_visible(
                timeout=5000
            )
            expect(page.get_by_text("/outputs/e2e-result.txt")).to_be_visible(
                timeout=5000
            )
            expect(
                page.locator(".file-row", has_text="outputs/e2e-result.txt").first
            ).to_be_visible(timeout=5000)

            output = project / ".a2a" / "workspace" / "outputs" / "e2e-result.txt"
            assert output.read_text() == "\n".join(
                [
                    "Uploaded files:",
                    "- inputs/source.txt",
                    "",
                    "User request:",
                    "Use the uploaded file and write an output.",
                ]
            )
        finally:
            browser.close()
