"""Full chart flow: chat → main-agent sandbox tools → matplotlib → PNG.

Exercises:
  - file discovery / reads in the user's bucket
  - in-house sandbox execution via run_python or run_shell
  - sandbox-runtime workspace mount + pip install
  - file write back into MinIO

The slow first pip install (~30-60s) inside the sandbox is why this test
is marked ``slow``. CI can opt out with ``-m 'not slow'``.
"""
from __future__ import annotations

import io
import secrets

import pytest

from .conftest import ApiClient

CSV = (
    "run_id,region,revenue\n"
    "R001,Pacific NW,91200\n"
    "R002,Mountain,12400\n"
    "R003,Mid-Atlantic,78900\n"
    "R004,Southeast,45300\n"
    "R005,Southwest,33200\n"
    "R006,Northeast,67800\n"
).encode("utf-8")


@pytest.mark.slow
async def test_chart_end_to_end(client: ApiClient) -> None:
    # Unique CSV + output so re-runs don't collide with prior state.
    suffix = secrets.token_hex(4)
    csv_path = f"e2e-data/sales-{suffix}.csv"
    out_name = f"e2e-chart-{suffix}.png"

    r = await client.post_form(
        "/v1/me/files",
        data={"path": csv_path},
        files={"file": ("sales.csv", io.BytesIO(CSV), "text/csv")},
    )
    assert r.status_code == 201, r.text

    prompt = (
        f"Make a sorted bar chart of revenue by region from {csv_path}. "
        f"Save it as outputs/{out_name}."
    )

    events: list[dict] = []
    async for ev in client.stream_sse(
        "/v1/me/chat",
        body={"messages": [{"role": "user", "content": prompt}], "stream": True, "approval_mode": False},
        timeout=420.0,
    ):
        events.append(ev)

    # 1. The deprecated graph-agent path is gone; charting now stays in the
    # main orchestrator and uses sandbox tools directly.
    graph_handoffs = [
        e for e in events
        if e.get("type") == "agent_handoff" and e.get("to") == "graph-agent"
    ]
    assert not graph_handoffs, f"deprecated graph-agent handoff observed: {graph_handoffs}"

    sandbox_calls = [
        e for e in events
        if e.get("type") == "tool_call" and e.get("tool") in {"run_python", "run_shell"}
    ]
    assert sandbox_calls, "no run_python/run_shell tool_call in stream"

    errors = [e for e in events if e.get("type") == "error"]
    assert not errors, f"orchestrator errors: {errors}"

    # 2. PNG actually landed in the user's bucket.
    listing = (await client.get("/v1/me/files")).json()
    out_entry = next(
        (f for f in listing if f["path"] == f"outputs/{out_name}"),
        None,
    )
    assert out_entry is not None, \
        f"PNG missing — files: {[f['path'] for f in listing]}"
    assert out_entry["size"] > 1024, f"PNG suspiciously small: {out_entry}"

    # 3. Content is a real PNG (magic bytes).
    png = await client.get(f"/v1/me/files/outputs/{out_name}")
    assert png.status_code == 200
    assert png.content[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"

    # 4. Final assistant content references the chart path.
    finals = [e for e in events if e.get("type") == "final"]
    assert finals, "no final event"
    assert out_name in finals[-1].get("content", ""), \
        f"final didn't mention {out_name}: {finals[-1]}"
