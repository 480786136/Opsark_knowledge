# ruff: noqa: F811 -- pytest injects the imported shared fixture by name.
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from test_workflow import env, prepare, record  # noqa: F401

from knowledge.config import settings
from knowledge.models import Document, Job, RefinementComparison
from knowledge.processing import run_once
from knowledge.refinement import refine


def enable(monkeypatch):
    for key, value in {
        "ai_refinement_enabled": True,
        "ai_api_key": "test-key",
        "ai_model": "test-model",
        "ai_base_url": "http://127.0.0.1:8001/v1",
    }.items():
        monkeypatch.setattr(settings(), key, value)


def response(refs):
    return {
        "title": "仓库验收经验",
        "claims": [
            {
                "section": "验收标准",
                "kind": "事实",
                "text": "已运行配置检查",
                "refs": refs,
            }
        ],
    }


@pytest.mark.parametrize(
    "refs,valid", [(["s/e"], True), (["invented"], False), ([], False)]
)
def test_model_contract_and_references(monkeypatch, refs, valid):
    enable(monkeypatch)
    source = record("kb")
    source["steps"] = [
        {
            "step_id": "s",
            "description": "检查",
            "command": "",
            "execution_status": "succeeded",
            "validation_status": "unknown",
            "evidence": [
                {
                    "evidence_id": "e",
                    "kind": "command_result",
                    "summary": "已检查",
                    "excerpt": "OK",
                }
            ],
        }
    ]
    source["context"] = {}
    source["tags"] = []
    original = httpx.Client

    def handle(request):
        body = json.loads(request.content)
        assert body["model"] == "test-model"
        assert "max_tokens" not in body
        assert "max_completion_tokens" not in body
        assert "test-key" not in body["messages"][1]["content"]
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(response(refs))},
                    }
                ]
            },
        )

    monkeypatch.setattr(
        "knowledge.refinement.httpx.Client",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )
    row = SimpleNamespace(
        payload=source, id="rec", source_record_id="task-1", source_revision=1
    )
    if valid:
        title, content = refine(row)
        assert title == "仓库验收经验"
        assert "s/e" in content and "experience-v2" in content
        assert "## 原始证据整理" not in content
    else:
        with pytest.raises(ValueError, match="AI_INVALID_REFERENCE"):
            refine(row)


@pytest.mark.parametrize(
    "mode", ["timeout", "http", "json", "truncated", "sensitive", "oversized"]
)
def test_invalid_model_responses_are_rejected(monkeypatch, mode):
    enable(monkeypatch)
    source = record("kb")
    original = httpx.Client

    def handle(request):
        if mode == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        if mode == "http":
            return httpx.Response(429, text="upstream details")
        if mode == "oversized":
            return httpx.Response(200, content=b"x" * (256 * 1024 + 1))
        data = {
            "title": "经验",
            "claims": [
                {
                    "section": "注意事项",
                    "kind": "建议",
                    "text": "password=unacceptable",
                    "refs": [],
                }
            ],
        }
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "length" if mode == "truncated" else "stop",
                        "message": {
                            "content": "not json"
                            if mode == "json"
                            else json.dumps(data)
                        },
                    }
                ]
            },
        )

    monkeypatch.setattr(
        "knowledge.refinement.httpx.Client",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )
    from knowledge.security import ApiError

    with pytest.raises((ValueError, httpx.HTTPError, ApiError)):
        refine(SimpleNamespace(payload=source))


def upload_and_draft(env, monkeypatch):
    enable(monkeypatch)
    client, factory, _service = env
    kb, auth, _ = prepare(env)
    assert (
        client.post(
            "/api/v1/records",
            headers={**auth, "Idempotency-Key": "ai-1"},
            json=record(kb),
        ).status_code
        == 202
    )
    assert run_once(factory)
    with factory() as db:
        doc = db.scalar(select(Document))
        return doc.id, doc.content, doc.revision


@pytest.mark.parametrize("code", ["AI_REFINEMENT_FAILED", "AI_INCOMPLETE_OUTPUT"])
def test_ai_failure_preserves_rule_draft_and_requires_explicit_retry(
    env, monkeypatch, code
):
    ident, original, revision = upload_and_draft(env, monkeypatch)

    def fail(_):
        if code == "AI_INCOMPLETE_OUTPUT":
            exc = ValueError(code)
            exc.ai_diagnostic = {
                "stage": "response_validation",
                "request_id": "test-request",
            }
            raise exc
        raise RuntimeError("private upstream response and key")

    monkeypatch.setattr("knowledge.processing.refine", fail)
    assert run_once(env[1])
    with env[1]() as db:
        doc = db.get(Document, ident)
        job = db.scalar(select(Job).where(Job.kind == "refine"))
        assert doc.content == original and doc.status == "draft"
        assert db.get(RefinementComparison, ident) is None
        assert job.status == "failed"
        assert job.error == (
            code + ":test-request" if code == "AI_INCOMPLETE_OUTPUT" else code
        )
    client, _, service = env
    result = client.post(
        f"/internal/v1/documents/{ident}/refine",
        headers=service,
        json={"revision": revision},
    )
    assert result.status_code == 202


def test_ai_result_cannot_overwrite_concurrent_edit(env, monkeypatch):
    ident, _, _ = upload_and_draft(env, monkeypatch)

    def edit_during_call(_):
        with env[1]() as db:
            db.get(Document, ident).content = "人工编辑"
            db.commit()
        return "AI title", "AI content"

    monkeypatch.setattr("knowledge.processing.refine", edit_during_call)
    assert run_once(env[1])
    with env[1]() as db:
        assert db.get(Document, ident).content == "人工编辑"
        assert db.scalar(select(Job).where(Job.kind == "refine")).status == "cancelled"


def test_success_stays_unpublished_and_rejects_stale_revision(env, monkeypatch):
    ident, before_content, old_revision = upload_and_draft(env, monkeypatch)
    monkeypatch.setattr(
        "knowledge.processing.refine", lambda _: ("AI经验", "AI正文\n来源证据")
    )
    assert run_once(env[1])
    with env[1]() as db:
        doc = db.get(Document, ident)
        assert (
            doc.title == "AI经验"
            and doc.published_version is None
            and doc.status == "draft"
        )
    client, _, service = env
    assert client.get(f"/internal/v1/documents/{ident}/comparison").status_code == 401
    comparison = client.get(
        f"/internal/v1/documents/{ident}/comparison", headers=service
    ).json()
    assert comparison["comparison"]["before_content"] == before_content
    assert comparison["comparison"]["after_content"] == "AI正文\n来源证据"
    assert (
        comparison["comparison"]["applied_revision"] == comparison["current_revision"]
    )
    assert (
        client.post(
            f"/internal/v1/documents/{ident}/refine",
            headers=service,
            json={"revision": old_revision},
        ).status_code
        == 409
    )


def test_regeneration_requires_management_auth_and_configuration(env, monkeypatch):
    ident, _, revision = upload_and_draft(env, monkeypatch)
    client, _, service = env
    assert (
        client.post(
            f"/internal/v1/documents/{ident}/refine", json={"revision": revision}
        ).status_code
        == 401
    )
    assert (
        client.post(
            f"/api/admin/v1/knowledge/documents/{ident}/refine",
            json={"revision": revision},
        ).status_code
        == 401
    )
    monkeypatch.setattr(settings(), "ai_refinement_enabled", False)
    assert (
        client.post(
            f"/internal/v1/documents/{ident}/refine",
            headers=service,
            json={"revision": revision},
        ).status_code
        == 409
    )


def test_comparison_preserves_snapshot_after_edit_and_clears_on_delete(
    env, monkeypatch
):
    ident, original, _ = upload_and_draft(env, monkeypatch)
    monkeypatch.setattr("knowledge.processing.refine", lambda _: ("AI经验", "AI快照"))
    assert run_once(env[1])
    with env[1]() as db:
        db.get(Document, ident).content = "后续人工修改"
        db.commit()
    client, _, service = env
    result = client.get(
        f"/internal/v1/documents/{ident}/comparison", headers=service
    ).json()
    assert result["comparison"]["before_content"] == original
    assert result["comparison"]["after_content"] == "AI快照"
    assert result["current_revision"] != result["comparison"]["applied_revision"]
    assert (
        client.delete(f"/internal/v1/documents/{ident}", headers=service).status_code
        == 200
    )
    with env[1]() as db:
        assert db.get(RefinementComparison, ident) is None
    assert (
        client.get(
            f"/internal/v1/documents/{ident}/comparison", headers=service
        ).status_code
        == 404
    )
