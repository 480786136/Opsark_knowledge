import json
from types import SimpleNamespace

import httpx
import pytest

from knowledge.config import settings
from knowledge.diagnostics import failure_code, log_failure
from knowledge.refinement import refine
from test_workflow import record


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
