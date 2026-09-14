from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from a2a_pack import A2AAgent
from a2a_pack.serve import build_app
from a2a_pack.serve.asgi import (
    _A2AJsonRpcError,
    _validate_a2a_send_message_response,
)


class _ProtocolErrorAgent(A2AAgent):
    name = "protocol-error-agent"
    description = "A2A protocol error handling fixture"


@pytest.fixture(autouse=True)
def _clean_protocol_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("A2A_API_KEY", "test-a2a-token")
    for name in (
        "A2A_ENABLE_PUSH_NOTIFICATIONS",
        "A2A_ENABLE_STREAMING",
        "A2A_ENABLE_EXTENDED_AGENT_CARD",
        "A2A_EXTENDED_AGENT_CARD_CONFIGURED",
        "A2A_REQUIRED_EXTENSIONS",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def client() -> TestClient:
    return TestClient(build_app(_ProtocolErrorAgent()))


def _auth(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"Authorization": "Bearer test-a2a-token"}
    if extra:
        headers.update(extra)
    return headers


def _rpc(client: TestClient, method: str, params: dict | None = None, headers: dict[str, str] | None = None) -> dict:
    response = client.post(
        "/",
        headers=_auth(headers),
        json={"jsonrpc": "2.0", "id": "test-id", "method": method, "params": params or {}},
    )
    assert response.status_code == 200
    return response.json()


def _message(message_id: str = "msg-1", **extra: object) -> dict:
    message = {
        "messageId": message_id,
        "role": "ROLE_USER",
        "parts": [{"text": "hello"}],
    }
    message.update(extra)
    return message


def _jsonrpc_reason(payload: dict) -> str:
    return payload["error"]["data"][0]["reason"]


def _rest_reason(payload: dict) -> str:
    return payload["error"]["details"][0]["reason"]


def test_unsupported_a2a_version_returns_32009_jsonrpc(client: TestClient) -> None:
    payload = _rpc(
        client,
        "GetTask",
        {"id": "missing-task"},
        headers={"A2A-Version": "0.5"},
    )

    assert payload["error"]["code"] == -32009
    assert _jsonrpc_reason(payload) == "VERSION_NOT_SUPPORTED"
    assert payload["error"]["data"][0]["metadata"]["requestedVersion"] == "0.5"
    assert payload["error"]["data"][0]["metadata"]["supportedVersions"] == "1.0"


def test_unsupported_a2a_version_returns_status_errorinfo_rest(client: TestClient) -> None:
    response = client.get(
        "/tasks/missing-task",
        headers=_auth({"A2A-Version": "0.5"}),
    )

    assert response.status_code == 400
    payload = response.json()
    assert payload["code"] == -32009
    assert payload["error"]["code"] == 400
    assert payload["error"]["status"] == "FAILED_PRECONDITION"
    assert _rest_reason(payload) == "VERSION_NOT_SUPPORTED"


def test_noauth_protocol_rest_routes_work_without_server_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("A2A_API_KEY", raising=False)
    http_client = TestClient(build_app(_ProtocolErrorAgent()))

    response = http_client.post(
        "/message:send",
        json={"message": _message("noauth-rest-1")},
    )

    assert response.status_code == 200
    assert response.json()["task"]["status"]["state"] == "TASK_STATE_WORKING"


def test_push_notification_disabled_returns_32003(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("A2A_ENABLE_PUSH_NOTIFICATIONS", "false")
    client = TestClient(build_app(_ProtocolErrorAgent()))

    card = client.get("/.well-known/agent-card.json").json()
    assert card["capabilities"]["pushNotifications"] is False

    payload = _rpc(
        client,
        "CreateTaskPushNotificationConfig",
        {"taskId": "task-1", "id": "cfg-1", "url": "https://example.com/hook"},
    )

    assert payload["error"]["code"] == -32003
    assert _jsonrpc_reason(payload) == "PUSH_NOTIFICATION_NOT_SUPPORTED"


def test_extended_agent_card_disabled_and_unconfigured_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("A2A_ENABLE_EXTENDED_AGENT_CARD", "false")
    client = TestClient(build_app(_ProtocolErrorAgent()))
    disabled = _rpc(client, "GetExtendedAgentCard")

    assert disabled["error"]["code"] == -32004
    assert _jsonrpc_reason(disabled) == "UNSUPPORTED_OPERATION"

    monkeypatch.setenv("A2A_ENABLE_EXTENDED_AGENT_CARD", "true")
    monkeypatch.setenv("A2A_EXTENDED_AGENT_CARD_CONFIGURED", "false")
    client = TestClient(build_app(_ProtocolErrorAgent()))
    unconfigured = _rpc(client, "GetExtendedAgentCard")

    assert unconfigured["error"]["code"] == -32007
    assert _jsonrpc_reason(unconfigured) == "EXTENDED_AGENT_CARD_NOT_CONFIGURED"


def test_required_extension_not_declared_returns_32008(monkeypatch: pytest.MonkeyPatch) -> None:
    extension_uri = "https://example.com/a2a/extensions/required/v1"
    monkeypatch.setenv("A2A_REQUIRED_EXTENSIONS", extension_uri)
    client = TestClient(build_app(_ProtocolErrorAgent()))

    card = client.get("/.well-known/agent-card.json").json()
    required = [item for item in card["capabilities"]["extensions"] if item["uri"] == extension_uri]
    assert required and required[0]["required"] is True

    missing = _rpc(client, "SendMessage", {"message": _message("missing-extension")})
    assert missing["error"]["code"] == -32008
    assert _jsonrpc_reason(missing) == "EXTENSION_SUPPORT_REQUIRED"

    accepted = _rpc(
        client,
        "SendMessage",
        {"message": _message("accepted-extension")},
        headers={"A2A-Extensions": extension_uri},
    )
    assert "result" in accepted
    assert accepted["result"]["task"]["status"]["state"] == "TASK_STATE_WORKING"


def test_follow_up_message_to_terminal_task_returns_32004(client: TestClient) -> None:
    created = _rpc(client, "SendMessage", {"message": _message("terminal-create")})
    task_id = created["result"]["task"]["id"]

    canceled = _rpc(client, "CancelTask", {"id": task_id})
    assert canceled["result"]["status"]["state"] == "TASK_STATE_CANCELED"

    follow_up = _rpc(
        client,
        "SendMessage",
        {"message": _message("terminal-follow-up", taskId=task_id)},
    )

    assert follow_up["error"]["code"] == -32004
    assert _jsonrpc_reason(follow_up) == "UNSUPPORTED_OPERATION"
    assert follow_up["error"]["data"][0]["metadata"]["taskId"] == task_id


def test_invalid_agent_response_validator_returns_32006() -> None:
    with pytest.raises(_A2AJsonRpcError) as excinfo:
        _validate_a2a_send_message_response({"task": {}, "message": {}})

    assert excinfo.value.code == -32006
    assert excinfo.value.data[0]["reason"] == "INVALID_AGENT_RESPONSE"
