import pytest
from sqlalchemy import select, func
from knowledge.models import Chunk, Document, Job
from knowledge import processing
from test_workflow import env as source_env, prepare, record

env = source_env


def test_status_respects_key_base_scope_even_on_same_installation(env):
    client, factory, service = env
    kb, auth, _ = prepare(env)
    uploaded = client.post(
        "/api/v1/records",
        headers={**auth, "Idempotency-Key": "status-scope"},
        json=record(kb),
    ).json()
    other = client.post(
        "/internal/v1/knowledge-bases",
        headers=service,
        json={"name": "Other private base"},
    ).json()["id"]
    key = client.post(
        "/internal/v1/knowledge-keys",
        headers=service,
        json={
            "name": "limited",
            "installation_id": "device-a",
            "knowledge_base_ids": [other],
            "scopes": ["records:read"],
        },
    ).json()["api_key"]
    assert (
        client.get(
            "/api/v1/records/" + uploaded["record_id"],
            headers={"Authorization": "Bearer " + key},
        ).status_code
        == 404
    )


@pytest.mark.parametrize("process_first", [False, True])
def test_reject_cancels_draft_and_blocks_publish(env, process_first):
    client, factory, service = env
    kb, auth, _ = prepare(env)
    uploaded = client.post(
        "/api/v1/records",
        headers={**auth, "Idempotency-Key": "review"},
        json=record(kb),
    ).json()
    if process_first:
        processing.run_once(factory)
    response = client.post(
        "/internal/v1/records/" + uploaded["record_id"] + "/reject", headers=service
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rejected"
    assert processing.run_once(factory) is False
    with factory() as db:
        docs = list(db.scalars(select(Document)))
        assert len(docs) == int(process_first)
        if docs:
            assert docs[0].status == "rejected"
            path = "/internal/v1/documents/" + docs[0].id
            assert (
                client.post(
                    path + "/publish",
                    headers=service,
                    json={"revision": docs[0].revision},
                ).status_code
                == 409
            )
            assert client.post(path + "/unpublish", headers=service).status_code == 409
    assert (
        client.get("/api/v1/records/" + uploaded["record_id"], headers=auth).json()[
            "status"
        ]
        == "rejected"
    )


def test_list_pagination_preserves_default_contract(env):
    client, factory, service = env
    for index in range(3):
        client.post(
            "/internal/v1/knowledge-bases", headers=service, json={"name": str(index)}
        )
    first = client.get("/internal/v1/knowledge-bases?limit=2", headers=service).json()
    last = client.get(
        "/internal/v1/knowledge-bases?limit=2&offset=2", headers=service
    ).json()
    assert len(first) == 2 and len(last) == 1
    assert first[0]["id"] != last[0]["id"]
    assert len(client.get("/internal/v1/knowledge-bases", headers=service).json()) == 3
    assert (
        client.get("/internal/v1/records?offset=-1", headers=service).status_code == 422
    )
    assert (
        client.get("/internal/v1/documents?limit=201", headers=service).status_code
        == 422
    )


def test_index_config_mismatch_keeps_old_publication_and_can_retry(env, monkeypatch):
    client, factory, service = env
    kb, auth, _ = prepare(env)
    doc = client.post(
        "/internal/v1/documents",
        headers=service,
        json={
            "knowledge_base_id": kb,
            "title": "configuration",
            "content": "Nginx configuration",
        },
    ).json()
    path = "/internal/v1/documents/" + doc["id"]
    assert (
        client.post(
            path + "/publish", headers=service, json={"revision": doc["revision"]}
        ).status_code
        == 202
    )
    processing.run_once(factory)
    current = client.get("/internal/v1/documents", headers=service).json()[0]
    published = client.post(
        path + "/publish", headers=service, json={"revision": current["revision"]}
    ).json()
    original = processing.index_id
    monkeypatch.setattr(processing, "index_id", lambda: "different-worker-config")
    processing.run_once(factory)
    with factory() as db:
        job = db.get(Job, published["job_id"])
        assert job.status == "failed" and job.error == "INDEX_CONFIG_MISMATCH"
        assert db.get(Document, doc["id"]).published_version == 1
        assert db.scalar(select(func.count()).select_from(Chunk)) == 1
    monkeypatch.setattr(processing, "index_id", original)
    assert (
        client.post(
            "/internal/v1/jobs/" + published["job_id"] + "/retry", headers=service
        ).status_code
        == 200
    )
    processing.run_once(factory)
    with factory() as db:
        assert db.get(Document, doc["id"]).published_version == 2


def test_config_change_during_embedding_does_not_publish(env, monkeypatch):
    client, factory, service = env
    kb, auth, _ = prepare(env)
    doc = client.post(
        "/internal/v1/documents",
        headers=service,
        json={
            "knowledge_base_id": kb,
            "title": "configuration",
            "content": "Nginx configuration",
        },
    ).json()
    client.post(
        "/internal/v1/documents/" + doc["id"] + "/publish",
        headers=service,
        json={"revision": doc["revision"]},
    )

    def changed(texts):
        monkeypatch.setattr(processing, "index_id", lambda: "changed-mid-build")
        return [None] * len(texts)

    monkeypatch.setattr(processing, "embed", changed)
    processing.run_once(factory)
    with factory() as db:
        assert db.scalar(select(Job)).error == "INDEX_CONFIG_MISMATCH"
        assert db.get(Document, doc["id"]).published_version is None
        assert db.scalar(select(func.count()).select_from(Chunk)) == 0
