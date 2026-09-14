from __future__ import annotations

from control_plane.trial_rooms import (
    build_trial_args,
    evaluate_trial_result,
    receipt_id,
    summarize_trial_receipt,
)


def test_evaluate_trial_result_scores_schema_and_artifacts() -> None:
    status, score, notes = evaluate_trial_result(
        result={"invoice_id": "A-1", "total": 42},
        file_ops=[{"op": "create", "path": "trials/out.json", "size": 10}],
        output_schema={"type": "object", "required": ["invoice_id", "total"]},
        acceptance_criteria="return json and write a file",
    )

    assert status == "passed"
    assert score == 100
    assert "required output keys" in notes
    assert "file artifact" in notes


def test_evaluate_trial_result_fails_missing_required_keys() -> None:
    status, score, notes = evaluate_trial_result(
        result={"invoice_id": "A-1"},
        file_ops=[],
        output_schema={"type": "object", "required": ["invoice_id", "total"]},
        acceptance_criteria="return the total",
    )

    assert status == "failed"
    assert score == 60
    assert "missing required output keys: total" in notes


def test_build_trial_args_fills_common_schema_fields() -> None:
    skill = {
        "input_schema": {
            "type": "object",
            "properties": {
                "instructions": {"type": "string"},
                "file_paths": {"type": "array"},
                "output_prefix": {"type": "string"},
                "criteria": {"type": "string"},
                "output_schema": {"type": "object"},
            },
        },
    }

    assert build_trial_args(
        skill=skill,
        goal="Extract invoices",
        input_paths=["invoices/a.pdf"],
        output_prefix="trials/room/bot/run-1/",
        acceptance_criteria="must include total",
        output_schema={"required": ["total"]},
    ) == {
        "instructions": "Extract invoices",
        "file_paths": ["invoices/a.pdf"],
        "output_prefix": "trials/room/bot/run-1/",
        "criteria": "must include total",
        "output_schema": {"required": ["total"]},
    }


def test_receipt_id_ignores_existing_receipt_id() -> None:
    receipt = {"receipt_id": "old", "result": {"ok": True}}

    assert receipt_id(receipt) == receipt_id({"result": {"ok": True}})


def test_summarize_trial_receipt_reports_buyer_evidence() -> None:
    summary = summarize_trial_receipt(
        result={"summary": "ok"},
        input_files=[
            {"path": "invoices/a.pdf", "sha256": "abc"},
            {"path": "invoices/missing.pdf", "error": "not found"},
        ],
        file_ops=[
            {"op": "create", "path": "trials/out.json", "size": 10},
            {"op": "delete", "path": "tmp.txt", "size": 0},
        ],
        output_schema={"required": ["summary", "total"]},
        status="failed",
        score=60,
        elapsed_ms=1234,
    )

    assert summary["status"] == "failed"
    assert summary["score"] == 60
    assert summary["input_count"] == 2
    assert summary["input_error_count"] == 1
    assert summary["artifact_count"] == 1
    assert summary["result_keys"] == ["summary"]
    assert summary["missing_required_keys"] == ["total"]
