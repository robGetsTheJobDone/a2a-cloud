from __future__ import annotations

from fastapi.testclient import TestClient

from a2a_pack import A2AAgent, NoAuth, RunContext, skill
from a2a_pack.serve import build_app


class _SseErrorAgent(A2AAgent):
    name = "sse-error-agent"
    description = "Exercises invoke SSE errors"
    auth_model = NoAuth

    @skill(description="No-op")
    async def ping(self, ctx: RunContext[NoAuth]) -> str:
        return "pong"


def test_invoke_sse_preparation_error_returns_error_event(monkeypatch) -> None:
    client = TestClient(build_app(_SseErrorAgent()))

    response = client.post(
        "/invoke/ping",
        headers={"Accept": "text/event-stream"},
        json={"arguments": {}, "grant": "invalid.invalid"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"type": "error"' in response.text
    assert '"status": 403' in response.text
    assert "data: [DONE]" in response.text
