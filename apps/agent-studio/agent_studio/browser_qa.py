from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from .models import BrowserJourney


_CHROMIUM_CANDIDATES = (
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
)


class BrowserProofRunner:
    """Drive one bounded packed-frontend journey in a real Chromium page."""

    def __init__(self, ctx: Any, *, timeout_ms: int = 30_000) -> None:
        self._ctx = ctx
        self._timeout_ms = max(5_000, min(int(timeout_ms), 90_000))

    async def run(
        self,
        *,
        base_url: str,
        journey: BrowserJourney,
        authorization: str = "",
    ) -> dict[str, Any]:
        target = urljoin(base_url.rstrip("/") + "/", journey.path.lstrip("/"))
        parsed = urlparse(target)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return {
                "ok": False,
                "error": "browser journey target is not a safe HTTP URL",
            }
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return {"ok": False, "error": "browser QA runtime is missing Playwright"}

        console_errors: list[str] = []
        page_errors: list[str] = []
        completed: list[str] = []
        screenshot_ref: dict[str, Any] | None = None
        executable = _chromium_executable()
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    headless=True,
                    executable_path=executable,
                    args=("--no-sandbox", "--disable-dev-shm-usage"),
                )
                context = await browser.new_context(
                    extra_http_headers={"authorization": authorization}
                    if authorization
                    else None,
                    accept_downloads=True,
                )
                page = await context.new_page()
                page.set_default_timeout(self._timeout_ms)
                page.on(
                    "console",
                    lambda message: (
                        console_errors.append(message.text[:500])
                        if message.type == "error"
                        else None
                    ),
                )
                page.on("pageerror", lambda error: page_errors.append(str(error)[:500]))
                response = await page.goto(target, wait_until="domcontentloaded")
                if response is None or response.status >= 400:
                    status = response.status if response is not None else 0
                    return {
                        "ok": False,
                        "error": f"browser navigation failed with HTTP {status}",
                    }

                for index, step in enumerate(journey.steps):
                    locator = page.locator(step.selector).first
                    if step.action == "fill":
                        await locator.fill(step.value)
                    elif step.action == "upload":
                        filename = step.filename or "browser-proof.txt"
                        await locator.set_input_files(
                            {
                                "name": filename,
                                "mimeType": "text/plain",
                                "buffer": step.value.encode("utf-8"),
                            }
                        )
                    elif step.action == "click":
                        await locator.click()
                    elif step.action == "assert_visible":
                        await locator.wait_for(state="visible")
                    elif step.action == "assert_text":
                        await locator.wait_for(state="visible")
                        actual = (await locator.inner_text()).strip()
                        if step.value not in actual:
                            raise AssertionError(
                                f"{step.selector!r} did not contain expected text {step.value!r}"
                            )
                    elif step.action == "assert_download":
                        async with page.expect_download() as pending:
                            await locator.click()
                        download = await pending.value
                        suggested = download.suggested_filename
                        if step.filename and suggested != step.filename:
                            raise AssertionError(
                                f"download filename {suggested!r} != {step.filename!r}"
                            )
                    completed.append(f"{index + 1}:{step.action}")

                if journey.screenshot:
                    shot = await page.screenshot(full_page=True, type="png")
                    artifact = await self._ctx.write_artifact(
                        f"browser-proof-{_safe_name(journey.name)}.png",
                        shot,
                        "image/png",
                    )
                    await self._ctx.emit_artifact(artifact)
                    screenshot_ref = {
                        "name": artifact.name,
                        "uri": artifact.uri,
                        "mime_type": artifact.mime_type,
                        "size_bytes": artifact.size_bytes,
                    }
                await context.close()
                await browser.close()
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": f"browser journey failed: {type(exc).__name__}: {str(exc)[:500]}",
                "completed_steps": completed,
                "console_errors": console_errors[-10:],
                "page_errors": page_errors[-10:],
            }
        if page_errors:
            return {
                "ok": False,
                "error": "frontend raised an uncaught browser error",
                "completed_steps": completed,
                "console_errors": console_errors[-10:],
                "page_errors": page_errors[-10:],
                "screenshot": screenshot_ref,
            }
        return {
            "ok": True,
            "url": target,
            "completed_steps": completed,
            "console_errors": console_errors[-10:],
            "screenshot": screenshot_ref,
        }


def _chromium_executable() -> str | None:
    configured = os.getenv("A2A_CHROMIUM_EXECUTABLE", "").strip()
    if configured and Path(configured).is_file():
        return configured
    return next((path for path in _CHROMIUM_CANDIDATES if Path(path).is_file()), None)


def _safe_name(value: str) -> str:
    cleaned = "".join(char.lower() if char.isalnum() else "-" for char in value)
    return "-".join(part for part in cleaned.split("-") if part)[:80] or "journey"


__all__ = ["BrowserProofRunner"]
