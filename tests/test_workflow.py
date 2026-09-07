import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from knowledge.main import app
from knowledge.db import Base, get_db, make_engine
from knowledge.config import settings
from knowledge.models import SourceRecord
from knowledge.processing import run_once
from knowledge.security import _calls


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)

    def db():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = db
    monkeypatch.setattr(settings(), "knowledge_service_token", "s" * 40)
    monkeypatch.setattr(settings(), "embedding_base_url", "")
    _calls.clear()
    with TestClient(app) as client:
        yield client, factory, {"Authorization": "Bearer " + "s" * 40}
    app.dependency_overrides.clear()
    engine.dispose()


def prepare(env, device="device-a"):
    client, factory, service = env
    response = client.post(
        "/internal/v1/knowledge-bases", headers=service, json={"name": "运维知识"}
    )
    assert response.status_code == 201, response.text
    kb = response.json()["id"]
    response = client.post(
        "/internal/v1/knowledge-keys",
        headers=service,
        json={
            "name": "开发者",
            "installation_id": device,
            "knowledge_base_ids": [kb],
            "scopes": ["records:write", "records:read", "knowledge:read"],
        },
    )
    assert response.status_code == 201, response.text
    return (
        kb,
        {"Authorization": "Bearer " + response.json()["api_key"]},
        response.json()["id"],
    )


def record(kb):
    return {
        "schema_version": "1.0",
        "source_record_id": "task-1",
        "source_revision": 1,
        "knowledge_base_id": kb,
        "record_type": "task_result",
        "title": "Nginx 配置检查",
        "occurred_at": "2026-09-07T00:00:00Z",
        "problem": "Nginx 配置重载前检查",
        "steps": [],
        "outcome": {"status": "partial", "summary": "仅完成 nginx -t 检查"},
        "redaction": {"client_applied": True, "ruleset_version": "v1"},
    }


def test_upload_publish_search_and_unpublish(env):
    client, factory, service = env
    kb, auth, _ = prepare(env)
    body = json.dumps(record(kb)).encode()
    headers = {
        **auth,
        "Idempotency-Key": "task-1-v1",
        "Content-Type": "application/json",
    }
    first = client.post("/api/v1/records", headers=headers, content=body)
    assert first.status_code == 202, first.text
    again = client.post("/api/v1/records", headers=headers, content=body)
    assert again.json()["record_id"] == first.json()["record_id"]
    assert again.json()["duplicate"]
    conflict = client.post("/api/v1/records", headers=headers, content=body + b" ")
    assert conflict.status_code == 409
    assert run_once(factory)
    docs = client.get("/internal/v1/documents", headers=service).json()
    assert len(docs) == 1 and docs[0]["status"] == "draft"
    query = {"query": "Nginx 配置", "knowledge_base_ids": [kb]}
    assert (
        client.post("/api/v1/knowledge/search", headers=auth, json=query).json()["hits"]
        == []
    )
    doc = docs[0]
    published = client.post(
        f"/internal/v1/documents/{doc['id']}/publish",
        headers=service,
        json={"revision": doc["revision"]},
    )
    assert published.status_code == 202, published.text
    assert run_once(factory)
    result = client.post("/api/v1/knowledge/search", headers=auth, json=query)
    assert result.status_code == 200, result.text
    assert result.json()["retrieval_mode"] == "keyword_only"
    hit = result.json()["hits"][0]
    assert client.get(hit["citation"]["document_url"], headers=auth).status_code == 200
    assert (
        client.post(
            f"/internal/v1/documents/{doc['id']}/unpublish", headers=service
        ).status_code
        == 200
    )
    assert (
        client.post("/api/v1/knowledge/search", headers=auth, json=query).json()["hits"]
        == []
    )
    assert client.get(hit["citation"]["document_url"], headers=auth).status_code == 410


def test_service_and_client_authority_are_separate(env):
    client, factory, service = env
    kb, auth, key_id = prepare(env)
    assert client.get("/internal/v1/records", headers=auth).status_code == 401
    assert client.get("/api/v1/knowledge/bases", headers=service).status_code == 401
    kb2, auth2, _ = prepare(env, "device-b")
    assert (
        client.post(
            "/api/v1/knowledge/search",
            headers=auth,
            json={"query": "test", "knowledge_base_ids": [kb2]},
        ).status_code
        == 403
    )
    body = record(kb)
    r = client.post(
        "/api/v1/records", headers={**auth, "Idempotency-Key": "a"}, json=body
    )
    assert (
        client.get(
            "/api/v1/records/" + r.json()["record_id"], headers=auth2
        ).status_code
        == 404
    )
    assert (
        client.delete(
            "/internal/v1/knowledge-keys/" + key_id, headers=service
        ).status_code
        == 200
    )
    assert client.get("/api/v1/knowledge/bases", headers=auth).status_code == 401
    with factory() as db:
        assert "api_key" not in str(db.execute(select(SourceRecord.payload)).all())


def test_sensitive_and_oversized_content_is_not_stored(env):
    client, factory, service = env
    kb, auth, _ = prepare(env)
    body = record(kb)
    body["problem"] = "Authorization: Bearer private-value"
    result = client.post(
        "/api/v1/records", headers={**auth, "Idempotency-Key": "s"}, json=body
    )
    assert result.status_code == 422 and "private-value" not in result.text
    body = record(kb)
    body["unexpected_trace"] = "secret"
    assert (
        client.post(
            "/api/v1/records", headers={**auth, "Idempotency-Key": "b"}, json=body
        ).status_code
        == 422
    )
    result = client.post(
        "/api/v1/records",
        headers={**auth, "Content-Type": "application/json"},
        content=b" " * (256 * 1024 + 1),
    )
    assert result.status_code == 413
    with factory() as db:
        assert db.scalar(select(SourceRecord)) is None


def test_index_failure_preserves_old_published_version(env, monkeypatch):
    client, factory, service = env
    kb, auth, _ = prepare(env)
    payload = {
        "knowledge_base_id": kb,
        "title": "Nginx",
        "content": "Nginx old verified content",
    }
    doc = client.post("/internal/v1/documents", headers=service, json=payload).json()
    client.post(
        f"/internal/v1/documents/{doc['id']}/publish",
        headers=service,
        json={"revision": doc["revision"]},
    )
    run_once(factory)
    doc = client.get("/internal/v1/documents", headers=service).json()[0]
    payload.update(content="Nginx replacement", revision=doc["revision"])
    doc = client.patch(
        f"/internal/v1/documents/{doc['id']}/draft", headers=service, json=payload
    ).json()
    assert (
        client.patch(
            f"/internal/v1/documents/{doc['id']}/draft", headers=service, json=payload
        ).status_code
        == 409
    )
    client.post(
        f"/internal/v1/documents/{doc['id']}/publish",
        headers=service,
        json={"revision": doc["revision"]},
    )

    def unavailable(*args):
        raise RuntimeError("provider secret must not escape")

    monkeypatch.setattr("knowledge.processing.embed", unavailable)
    for _ in range(3):
        run_once(factory)
    hit = client.post(
        "/api/v1/knowledge/search",
        headers=auth,
        json={"query": "Nginx", "knowledge_base_ids": [kb]},
    ).json()["hits"][0]
    assert hit["document_version"] == 1 and "old verified" in hit["content"]
    jobs = client.get("/internal/v1/jobs", headers=service)
    assert "provider secret" not in jobs.text and "failed" in jobs.text


def test_internal_openapi_and_health(env):
    client, _, service = env
    assert client.get("/health/live").status_code == 200
    assert client.get("/internal/v1/openapi.json").status_code == 401
    schema = client.get("/internal/v1/openapi.json", headers=service).json()
    assert "/api/v1/records" in schema["paths"]
