import json
from types import SimpleNamespace

import httpx
import pytest
from test_workflow import record

from knowledge.config import settings
from knowledge.diagnostics import failure_code, log_failure, log_prepared
from knowledge.refinement import refine


def test_truncated_response_logs_request_id_without_source(
    monkeypatch, tmp_path, caplog
):
    monkeypatch.setattr(settings(), "ai_refinement_enabled", True)
    monkeypatch.setattr(settings(), "ai_api_key", "private-key")
    monkeypatch.setattr(settings(), "ai_model", "test-model")
    monkeypatch.setattr(settings(), "ai_base_url", "http://127.0.0.1:8001/v1")
    monkeypatch.setattr(settings(), "worker_log_path", str(tmp_path / "worker.log"))
    original = httpx.Client
    transport = httpx.MockTransport(
        lambda r: httpx.Response(
            200,
            headers={"X-Request-ID": "request123"},
            json={
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": "private-output"},
                    }
                ],
                "usage": {"prompt_tokens": 2081, "completion_tokens": 5000},
            },
        )
    )
    monkeypatch.setattr(
        "knowledge.refinement.httpx.Client",
        lambda **kwargs: original(transport=transport, **kwargs),
    )
    with pytest.raises(ValueError) as caught:
        refine(SimpleNamespace(payload=record("kb")))
    info = log_failure(caught.value, "job123", "doc123", 7)
    assert info["code"] == "AI_INCOMPLETE_OUTPUT"
    assert info["request_id"] == "request123"
    assert info["finish_reason"] == "length"
    assert info["completion_tokens"] == 5000
    persisted = (tmp_path / "worker.log").read_text()
    assert json.loads(persisted)["revision"] == 7
    assert "private-key" not in caplog.text + persisted
    assert "private-output" not in caplog.text + persisted


def test_unknown_exception_message_not_logged(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(settings(), "worker_log_path", str(tmp_path / "safe.log"))
    try:
        raise RuntimeError("password=private-value and SQL parameters")
    except RuntimeError as exc:
        log_failure(exc, "job", "doc", 1)
    assert "private-value" not in caplog.text
    assert "RuntimeError" in caplog.text
    assert failure_code(ValueError("AI_INVALID_REFERENCE")) == "AI_INVALID_REFERENCE"


def test_diagnostics_allow_only_safe_structured_metadata(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(settings(), "worker_log_path", str(tmp_path / "safe.log"))
    exc = RuntimeError("private-response")
    exc.ai_diagnostic = {
        "event": "forged-success",
        "code": "forged-code",
        "source_text": "private-source",
        "model": "sk-" + "a" * 30,
        "request_id": "password=private-password",
        "stage": "evidence_validation",
        "quality_metrics": {
            "step_count": 3,
            "source_text": "private-source",
            "warning_count": True,
        },
        "claim_count": 2,
        "prompt_tokens": -1,
    }
    info = log_failure(exc, "job", "doc", 1)
    assert info["event"] == "knowledge_refinement_failed"
    assert info["code"] == "AI_REFINEMENT_FAILED"
    assert info["quality_metrics"] == {"step_count": 3}
    assert (
        "model" not in info and "request_id" not in info and "prompt_tokens" not in info
    )
    assert "private" not in caplog.text


def test_prepared_diagnostic_does_not_claim_publication_or_semantic_verification(
    monkeypatch, tmp_path
):
    path = tmp_path / "prepared.log"
    monkeypatch.setattr(settings(), "worker_log_path", str(path))
    info = log_prepared(
        {
            "stage": "prepared_for_review",
            "model": "test-model",
            "quality_metrics": {"evidence_count": 2},
            "claim_count": 1,
        },
        "source",
        4,
    )
    assert info["event"] == "knowledge_refinement_prepared"
    assert info["model"] == "test-model"
    assert info["quality_metrics"]["evidence_count"] == 2
    assert json.loads(path.read_text())["source_revision"] == 4
