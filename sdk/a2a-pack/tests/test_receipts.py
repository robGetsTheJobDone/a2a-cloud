from __future__ import annotations

import time

import pytest

from a2a_pack.receipts import (
    ArtifactRef,
    ExecutionReceipt,
    FileOps,
    Handoff,
    ReceiptInvalid,
    ToolCall,
    hash_input,
    seal_receipt,
    sign_receipt,
    verify_receipt,
)


def test_seal_and_verify_roundtrip_under_ed25519() -> None:
    started = int(time.time()) - 4
    receipt, token = seal_receipt(
        agent_name="finance-recon",
        agent_version="1.4.2",
        skill_name="reconcile",
        caller="controller@acme",
        task_id="task-abc",
        started_at=started,
        ended_at=started + 4,
        inputs={"period": "2026-Q1"},
        result={"period": "2026-Q1", "exceptions": 7},
        grant_ids=("grant-1", "grant-2"),
        file_ops=FileOps(reads=1250, writes=3, bytes_read=42_000_000),
        tool_calls=(ToolCall(name="workspace.list", elapsed_ms=12),),
        artifacts=(ArtifactRef(path="reports/2026-Q1.xlsx", bytes=89_213),),
        handoffs=(Handoff(callee="chart-agent", skill="render", grant_id="g3"),),
    )

    assert receipt.elapsed_ms == 4000
    assert receipt.input_hash == hash_input({"period": "2026-Q1"})
    assert "2026-Q1" in receipt.input_preview
    assert receipt.status == "ok"

    verified = verify_receipt(token)
    assert verified.receipt_id == receipt.receipt_id
    assert verified.file_ops.reads == 1250
    assert verified.handoffs[0].callee == "chart-agent"


def test_verify_detects_tampering() -> None:
    _, token = seal_receipt(
        agent_name="x",
        skill_name="y",
        started_at=1,
        ended_at=2,
        inputs={"a": 1},
    )
    payload, sig = token.rsplit(".", 1)
    # Flip one byte in the signature.
    tampered_sig = sig[:-1] + ("A" if sig[-1] != "A" else "B")
    with pytest.raises(ReceiptInvalid):
        verify_receipt(f"{payload}.{tampered_sig}")


def test_verify_rejects_malformed() -> None:
    with pytest.raises(ReceiptInvalid):
        verify_receipt("not-a-token")
    with pytest.raises(ReceiptInvalid):
        verify_receipt("")


def test_ed25519_roundtrip(monkeypatch) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    priv_bytes = key.private_bytes_raw()
    pub_bytes = key.public_key().public_bytes_raw()

    import base64

    monkeypatch.setenv(
        "A2A_RECEIPT_SIGNING_KEY",
        base64.b64encode(priv_bytes).decode(),
    )
    monkeypatch.setenv(
        "A2A_RECEIPT_VERIFYING_KEY",
        base64.b64encode(pub_bytes).decode(),
    )

    _, token = seal_receipt(
        agent_name="x",
        skill_name="y",
        started_at=1,
        ended_at=2,
        inputs={"a": 1},
    )
    verified = verify_receipt(token)
    assert verified.agent_name == "x"


def test_hash_input_is_order_stable() -> None:
    a = hash_input({"x": 1, "y": [1, 2, 3]})
    b = hash_input({"y": [1, 2, 3], "x": 1})
    assert a == b


def test_immutability() -> None:
    receipt = ExecutionReceipt(
        receipt_id="r1",
        agent_name="x",
        skill_name="y",
    )
    with pytest.raises(Exception):  # noqa: BLE001 — frozen model raises ValidationError or similar
        receipt.agent_name = "changed"  # type: ignore[misc]


def test_result_preview_truncates() -> None:
    big = {"data": "z" * 10_000}
    receipt, _ = seal_receipt(
        agent_name="x",
        skill_name="y",
        started_at=1,
        ended_at=2,
        result=big,
    )
    assert len(receipt.result_preview) <= 240
    assert receipt.result_preview.endswith("…")
