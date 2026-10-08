# ruff: noqa: F811 -- pytest injects the imported shared fixture by name.
import pytest
from test_workflow import env, prepare  # noqa: F401

from knowledge.models import Document, DocumentSource, SourceRecord


def seed_sources(env, count=207):
    client, factory, service = env
    base, _, _ = prepare(env)
    document = client.post(
        "/internal/v1/documents",
        headers=service,
        json={
            "knowledge_base_id": base,
            "title": "分页验收",
            "content": "人工草稿",
        },
    ).json()
    with factory() as db:
        for index in range(count):
            source = SourceRecord(
                id=f"source-page-{index:04d}",
                installation_id="source-pagination",
                source_record_id="task-pages",
                source_revision=index + 1,
                knowledge_base_id=base,
                body_hash=f"hash-{index}",
                status="processed" if index == 0 else "ready_for_review",
                payload={
                    "title": f"来源 {index + 1}",
                    "private_body": "large-body-marker" * 300,
                },
            )
            db.add(source)
            db.flush()
            db.add(
                DocumentSource(source_record_id=source.id, document_id=document["id"])
            )
        db.get(Document, document["id"]).source_record_id = "source-page-0000"
        db.commit()
    return document["id"]


def test_linked_source_summaries_are_bounded_ordered_and_payload_free(env):
    ident = seed_sources(env)
    client, _, service = env
    path = f"/internal/v1/documents/{ident}/sources"
    first = client.get(path, headers=service)
    assert first.status_code == 200
    rows = first.json()
    assert len(rows) == 50
    assert [row["source_revision"] for row in rows] == list(range(207, 157, -1))
    assert rows[0]["title"] == "来源 207"
    assert all("payload" not in row and "body_hash" not in row for row in rows)
    assert "large-body-marker" not in first.text
    assert len(client.get(path + "?limit=200", headers=service).json()) == 200
    last = client.get(path + "?offset=200&limit=50", headers=service).json()
    assert len(last) == 7
    assert last[-1]["id"] == "source-page-0000"
    assert client.get(path + "?offset=207", headers=service).json() == []


def test_current_source_and_pending_ids_remain_readable_outside_summary_page(env):
    ident = seed_sources(env, count=52)
    client, _, service = env
    detail = client.get(f"/internal/v1/documents/{ident}", headers=service).json()
    summaries = client.get(
        f"/internal/v1/documents/{ident}/sources", headers=service
    ).json()
    assert detail["source_record_id"] not in {row["id"] for row in summaries}
    assert "source-page-0001" in detail["pending_source_record_ids"]
    assert "source-page-0001" not in {row["id"] for row in summaries}
    source = client.get(
        f"/internal/v1/records/{detail['source_record_id']}", headers=service
    )
    assert source.status_code == 200
    assert source.json()["payload"]["title"] == "来源 1"
    assert "large-body-marker" in source.json()["payload"]["private_body"]


@pytest.mark.parametrize(
    "query", ["offset=-1", "offset=1000001", "limit=0", "limit=201"]
)
def test_source_pagination_rejects_invalid_bounds(env, query):
    ident = seed_sources(env, count=1)
    response = env[0].get(
        f"/internal/v1/documents/{ident}/sources?{query}", headers=env[2]
    )
    assert response.status_code == 422


def test_source_summary_endpoint_requires_authentication(env):
    ident = seed_sources(env, count=1)
    assert env[0].get(f"/internal/v1/documents/{ident}/sources").status_code == 401
    assert (
        env[0].get(f"/api/admin/v1/knowledge/documents/{ident}/sources").status_code
        == 401
    )
