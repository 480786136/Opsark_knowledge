import pytest
from sqlalchemy import func, select
from test_workflow import env as source_env
from test_workflow import prepare, record

from knowledge.config import settings
from knowledge.models import Audit, Document, DocumentVersion, Job, KnowledgeBase
from knowledge.processing import run_once

env = source_env


def draft(client, service, kb, title="Nginx 配置检查"):
    response = client.post(
        "/internal/v1/documents",
        headers=service,
        json={
            "knowledge_base_id": kb,
            "title": title,
            "content": "# 检查\nnginx -t\n核对语法和实际配置路径",
            "tags": ["参考资料", "ops-v1:nginx-config"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def selection(doc, **changes):
    return {"document_id": doc["id"], "revision": doc["revision"], **changes}


def test_batch_partial_outcomes_keep_reviewed_revisions_and_do_not_republish(env):
    client, factory, service = env
    kb, auth, _ = prepare(env)
    first, stale, published = [
        draft(client, service, kb, name) for name in ["first", "stale", "published"]
    ]
    client.post(
        f"/internal/v1/documents/{published['id']}/publish",
        headers=service,
        json={"revision": published["revision"]},
    )
    assert run_once(factory)
    response = client.post(
        "/internal/v1/documents/batch-publish",
        headers=service,
        json={
            "documents": [
                selection(first),
                selection(stale, revision=99),
                selection(published),
                {"document_id": "missing", "revision": 1},
            ]
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert (result["queued"], result["failed"]) == (1, 3)
    assert [item.get("error", {}).get("code") for item in result["results"]] == [
        None,
        "REVISION_CONFLICT",
        "DOCUMENT_NOT_DRAFT",
        "RESOURCE_NOT_FOUND",
    ]
    repeat = client.post(
        "/internal/v1/documents/batch-publish",
        headers=service,
        json={"documents": [selection(first)]},
    ).json()
    assert repeat["queued"] == 0 and repeat["failed"] == 1
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(DocumentVersion)) == 2
        assert (
            db.scalar(
                select(func.count())
                .select_from(Audit)
                .where(Audit.action == "document.publish")
            )
            == 2
        )
        assert db.get(Document, stale["id"]).status == "draft"
    assert run_once(factory)
    search = client.post(
        "/api/v1/knowledge/search",
        headers=auth,
        json={"query": "nginx", "knowledge_base_ids": [kb]},
    ).json()
    assert search["corpus"] == {
        "documents": 3,
        "drafts": 1,
        "indexing": 0,
        "published": 2,
        "searchable": 2,
    }


@pytest.mark.parametrize(
    "items",
    [
        [],
        [{"document_id": "same", "revision": 1}] * 2,
        [{"document_id": str(i), "revision": 1} for i in range(101)],
    ],
)
def test_batch_rejects_invalid_selection_without_creating_jobs(env, items):
    client, factory, service = env
    response = client.post(
        "/internal/v1/documents/batch-publish",
        headers=service,
        json={"documents": items},
    )
    assert response.status_code == 422
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 0


def test_batch_rejects_disabled_base_and_public_client_credentials(env):
    client, factory, service = env
    kb, auth, _ = prepare(env)
    doc = draft(client, service, kb)
    with factory() as db:
        db.get(KnowledgeBase, kb).enabled = False
        db.commit()
    payload = {"documents": [selection(doc)]}
    assert (
        client.post(
            "/internal/v1/documents/batch-publish", headers=auth, json=payload
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/admin/v1/knowledge/documents/batch-publish", json=payload
        ).status_code
        == 401
    )
    result = client.post(
        "/internal/v1/documents/batch-publish", headers=service, json=payload
    ).json()
    assert (
        result["failed"] == 1
        and result["results"][0]["error"]["code"] == "KNOWLEDGE_BASE_DENIED"
    )
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(DocumentVersion)) == 0


def test_creator_is_audited_admin_not_current_viewer_and_batch_requires_csrf(env):
    from test_knowledge_pack import login

    client, factory, service = env
    kb, _, _ = prepare(env)
    login(client, factory)
    response = client.post(
        "/api/admin/v1/knowledge/documents",
        json={
            "knowledge_base_id": kb,
            "title": "manual",
            "content": "review this",
            "tags": ["参考资料", "ops-v1:linux-disk"],
        },
    )
    doc = response.json()
    listed = client.get("/internal/v1/documents", headers=service).json()[0]
    assert listed["knowledge_base_name"] == "运维知识"
    assert listed["source_kind"] == "reference_pack"
    assert listed["created_by"] == "pack-admin" and listed["created_by_kind"] == "admin"
    detail = client.get(f"/internal/v1/documents/{doc['id']}", headers=service).json()
    assert detail["created_by"] == "pack-admin"
    payload = {"documents": [selection(doc)]}
    assert (
        client.post(
            "/api/admin/v1/knowledge/documents/batch-publish",
            headers={"X-CSRF-Token": "invalid"},
            json=payload,
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/admin/v1/knowledge/documents/batch-publish", json=payload
        ).json()["queued"]
        == 1
    )


def test_core_creator_is_client_identity_not_fabricated_admin(env):
    client, factory, service = env
    kb, auth, _ = prepare(env, "core-laptop")
    assert (
        client.post(
            "/api/v1/records",
            headers={**auth, "Idempotency-Key": "console-source"},
            json=record(kb),
        ).status_code
        == 202
    )
    assert run_once(factory)
    doc = client.get("/internal/v1/documents", headers=service).json()[0]
    assert doc["source_kind"] == "core_record"
    assert doc["created_by_kind"] == "client" and "core-laptop" in doc["created_by"]


def test_overview_counts_full_database_and_filters_before_pagination(env):
    client, factory, service = env
    kb, _, _ = prepare(env)
    other = client.post(
        "/internal/v1/knowledge-bases", headers=service, json={"name": "another"}
    ).json()["id"]
    doc = draft(client, service, kb)
    client.post(
        f"/internal/v1/documents/{doc['id']}/publish",
        headers=service,
        json={"revision": doc["revision"]},
    )
    assert run_once(factory)
    with factory() as db:
        db.add_all(
            [
                Document(
                    knowledge_base_id=other,
                    title=str(i),
                    content="draft",
                    draft_edited=True,
                )
                for i in range(205)
            ]
        )
        db.add(
            Document(
                knowledge_base_id=other,
                title="deleted",
                content="gone",
                status="deleted",
            )
        )
        db.add(
            Document(
                knowledge_base_id=other,
                title="archive",
                content="old",
                superseded_by=doc["id"],
            )
        )
        db.commit()
    result = client.get("/internal/v1/overview?limit=1", headers=service).json()
    assert len(result["bases"]) == 1
    assert result["totals"]["documents"] == 206 and result["totals"]["drafts"] == 205
    assert result["totals"]["searchable"] == 1
    rows = client.get(
        "/internal/v1/documents",
        headers=service,
        params={"knowledge_base_id": kb, "limit": 1},
    ).json()
    assert len(rows) == 1 and rows[0]["id"] == doc["id"]
    rows = client.get(
        "/internal/v1/documents",
        headers=service,
        params={"knowledge_base_id": other, "offset": 200, "limit": 10},
    ).json()
    assert len(rows) == 5
    with factory() as db:
        db.get(KnowledgeBase, kb).enabled = False
        db.commit()
    assert (
        client.get("/internal/v1/overview", headers=service).json()["totals"][
            "searchable"
        ]
        == 0
    )


def test_search_disabled_embedding_is_normal_and_counts_only_authorized_bases(env):
    client, _, service = env
    kb, auth, _ = prepare(env)
    other = client.post(
        "/internal/v1/knowledge-bases", headers=service, json={"name": "private"}
    ).json()["id"]
    draft(client, service, kb)
    draft(client, service, other)
    result = client.post(
        "/api/v1/knowledge/search",
        headers=auth,
        json={"query": "nginx", "knowledge_base_ids": [kb]},
    ).json()
    assert result["embedding_status"] == "disabled"
    assert result["warnings"] == [] and result["hits"] == []
    assert result["corpus"]["documents"] == 1 and result["corpus"]["drafts"] == 1
    assert result["corpus"]["searchable"] == 0


def test_search_provider_failure_is_distinct_and_keyword_hits_survive(env, monkeypatch):
    client, factory, service = env
    kb, auth, _ = prepare(env)
    doc = draft(client, service, kb)
    client.post(
        f"/internal/v1/documents/{doc['id']}/publish",
        headers=service,
        json={"revision": doc["revision"]},
    )
    assert run_once(factory)
    for name in ["embedding_base_url", "embedding_api_key", "embedding_model"]:
        monkeypatch.setattr(settings(), name, "test-configured")

    def unavailable(texts):
        raise RuntimeError("sensitive upstream response")

    monkeypatch.setattr("knowledge.main.embed", unavailable)
    response = client.post(
        "/api/v1/knowledge/search",
        headers=auth,
        json={"query": "nginx", "knowledge_base_ids": [kb]},
    )
    result = response.json()
    assert (
        result["embedding_status"] == "unavailable"
        and result["retrieval_mode"] == "keyword_only"
    )
    assert result["hits"] and result["warnings"]
    assert "sensitive upstream response" not in response.text
