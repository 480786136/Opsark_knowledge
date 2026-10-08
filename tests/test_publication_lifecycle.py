"""Regression tests for serving immutable knowledge through edits and failures."""
# ruff: noqa: F811

from sqlalchemy import func, select
from test_workflow import env, prepare, record  # noqa: F401

from knowledge import processing
from knowledge.models import (
    Chunk,
    Document,
    DocumentSource,
    DocumentVersion,
    IndexBuild,
    Job,
    SourceRecord,
)


def upload(env, kb, auth, revision=1):
    payload = record(kb)
    payload.update(source_revision=revision, title=f"Nginx revision {revision}")
    payload["context"] = {
        "environment": "test",
        "software": [{"name": "nginx", "version": str(revision)}],
    }
    result = env[0].post(
        "/api/v1/records",
        headers={**auth, "Idempotency-Key": f"revision-{revision}"},
        json=payload,
    )
    assert result.status_code == 202, result.text
    processing.run_once(env[1])
    return result.json()["record_id"]


def detail(env, ident=None):
    client, _, service = env
    if ident:
        response = client.get(f"/internal/v1/documents/{ident}", headers=service)
        assert response.status_code == 200, response.text
        return response.json()
    return client.get("/internal/v1/documents", headers=service).json()[0]


def publish(env, doc):
    result = env[0].post(
        f"/internal/v1/documents/{doc['id']}/publish",
        headers=env[2],
        json={"revision": doc["revision"]},
    )
    assert result.status_code == 202, result.text
    processing.run_once(env[1])
    return result.json()["job_id"]


def search(env, kb, auth, **extra):
    result = env[0].post(
        "/api/v1/knowledge/search",
        headers=auth,
        json={"query": "Nginx", "knowledge_base_ids": [kb], **extra},
    )
    assert result.status_code == 200, result.text
    return result.json()


def test_published_source_revision_keeps_identity_and_original_provenance(env):
    kb, auth, _ = prepare(env)
    first = upload(env, kb, auth)
    doc = detail(env)
    publish(env, doc)
    old = search(env, kb, auth)["hits"][0]
    second = upload(env, kb, auth, 2)
    current = detail(env, doc["id"])
    assert current["status"] == "draft" and current["published_version"] == 1
    assert current["source_record_id"] == second
    before = search(env, kb, auth)["hits"]
    assert {h["document_id"] for h in before} == {doc["id"]}
    assert all(h["citation"]["source_record_ids"] == [first] for h in before)
    publish(env, current)
    after = search(env, kb, auth)["hits"]
    assert {h["document_version"] for h in after} == {2}
    assert all(h["citation"]["source_record_ids"] == [second] for h in after)
    assert env[0].get(old["citation"]["document_url"], headers=auth).status_code == 410
    history = (
        env[0]
        .get(f"/internal/v1/documents/{doc['id']}/versions/1", headers=env[2])
        .json()
    )
    assert (
        history["source_record_ids"] == [first]
        and history["context"]["software"][0]["version"] == "1"
    )
    with env[1]() as db:
        assert db.scalar(select(func.count()).select_from(Document)) == 1
        assert db.scalar(select(func.count()).select_from(DocumentSource)) == 2


def test_citation_reads_body_and_current_publication_in_same_snapshot(env, monkeypatch):
    from knowledge import main

    kb, auth, _ = prepare(env)
    upload(env, kb, auth)
    doc = detail(env)
    publish(env, doc)
    original = main.check_bases

    def unpublish_after_base_check(db, ids, key=None):
        original(db, ids, key)
        with env[1]() as other:
            other.get(Document, doc["id"]).published_version = None
            other.commit()

    monkeypatch.setattr(main, "check_bases", unpublish_after_base_check)
    response = env[0].get(f"/api/v1/knowledge/documents/{doc['id']}/versions/1", headers=auth)
    assert response.status_code == 410


def test_manual_draft_is_preserved_until_explicit_source_selection(env):
    kb, auth, _ = prepare(env)
    first = upload(env, kb, auth)
    doc = detail(env)
    edit = env[0].patch(
        f"/internal/v1/documents/{doc['id']}/draft",
        headers=env[2],
        json={
            "knowledge_base_id": kb,
            "revision": doc["revision"],
            "title": "Manual",
            "content": "Manual correction",
        },
    )
    assert edit.status_code == 200
    second = upload(env, kb, auth, 2)
    doc = detail(env, doc["id"])
    assert doc["content"] == "Manual correction" and doc["source_record_id"] == first
    assert doc["pending_source_record_ids"] == [second]
    response = env[0].post(
        f"/internal/v1/documents/{doc['id']}/source",
        headers=env[2],
        json={"revision": doc["revision"], "source_record_id": second},
    )
    assert response.status_code == 200, response.text
    assert response.json()["source_record_id"] == second
    assert "Manual correction" not in response.json()["content"]


def test_late_older_source_cannot_replace_latest_draft(env):
    kb, auth, _ = prepare(env)
    newest = upload(env, kb, auth, 3)
    old = upload(env, kb, auth, 1)
    assert detail(env)["source_record_id"] == newest
    with env[1]() as db:
        assert db.get(SourceRecord, old).status == "superseded"
        assert db.scalar(select(func.count()).select_from(Document)) == 1


def test_refine_new_draft_keeps_old_publication(env, monkeypatch):
    kb, auth, _ = prepare(env)
    upload(env, kb, auth)
    doc = detail(env)
    publish(env, doc)
    upload(env, kb, auth, 2)
    doc = detail(env)
    monkeypatch.setattr(processing.settings(), "ai_refinement_enabled", True)
    monkeypatch.setattr(processing.settings(), "ai_model", "mock")
    monkeypatch.setattr(processing.settings(), "ai_api_key", "mock")
    monkeypatch.setattr(
        processing, "refine", lambda _: ("AI revised", "Nginx revised draft")
    )
    response = env[0].post(
        f"/internal/v1/documents/{doc['id']}/refine",
        headers=env[2],
        json={"revision": doc["revision"]},
    )
    assert response.status_code == 202, response.text
    processing.run_once(env[1])
    doc = detail(env)
    assert doc["title"] == "AI revised" and doc["published_version"] == 1
    assert all(h["document_version"] == 1 for h in search(env, kb, auth)["hits"])


def test_reindex_switches_build_without_new_content_version(env, monkeypatch):
    kb, auth, _ = prepare(env)
    upload(env, kb, auth)
    doc = detail(env)
    publish(env, doc)
    doc = detail(env)
    with env[1]() as db:
        version = db.scalar(select(DocumentVersion))
        old_build, original_content, version_id = (
            version.active_build_id,
            version.content,
            version.id,
        )
    response = env[0].post(
        f"/internal/v1/documents/{doc['id']}/reindex",
        headers=env[2],
        json={"revision": doc["revision"]},
    )
    assert response.status_code == 202, response.text
    processing.run_once(env[1])
    with env[1]() as db:
        version = db.get(DocumentVersion, version_id)
        assert (
            version.active_build_id != old_build and version.content == original_content
        )
        assert db.scalar(select(func.count()).select_from(DocumentVersion)) == 1
        assert db.get(IndexBuild, version.active_build_id).status == "ready"
        active_ids = set(
            db.scalars(
                select(Chunk.id).where(Chunk.build_id == version.active_build_id)
            )
        )
    assert all(h["chunk_id"] in active_ids for h in search(env, kb, auth)["hits"])


def test_failed_reindex_keeps_active_build_and_can_retry(env, monkeypatch):
    kb, auth, _ = prepare(env)
    upload(env, kb, auth)
    doc = detail(env)
    publish(env, doc)
    before = search(env, kb, auth)["hits"]
    doc = detail(env)
    response = env[0].post(
        f"/internal/v1/documents/{doc['id']}/reindex",
        headers=env[2],
        json={"revision": doc["revision"]},
    )
    original = processing.embed
    monkeypatch.setattr(
        processing,
        "embed",
        lambda _: (_ for _ in ()).throw(ValueError("private failure")),
    )
    for _ in range(3):
        processing.run_once(env[1])
    assert search(env, kb, auth)["hits"] == before
    monkeypatch.setattr(processing, "embed", original)
    response = env[0].post(
        f"/internal/v1/jobs/{response.json()['job_id']}/retry", headers=env[2]
    )
    assert response.status_code == 200, response.text
    processing.run_once(env[1])
    assert search(env, kb, auth)["hits"]


def test_unpublish_during_build_cancels_late_result(env, monkeypatch):
    kb, auth, _ = prepare(env)
    upload(env, kb, auth)
    doc = detail(env)
    publish(env, doc)
    doc = detail(env)
    response = env[0].post(
        f"/internal/v1/documents/{doc['id']}/reindex",
        headers=env[2],
        json={"revision": doc["revision"]},
    )

    def unpublish(texts):
        assert (
            env[0]
            .post(f"/internal/v1/documents/{doc['id']}/unpublish", headers=env[2])
            .status_code
            == 200
        )
        return [None] * len(texts)

    monkeypatch.setattr(processing, "embed", unpublish)
    processing.run_once(env[1])
    assert search(env, kb, auth)["hits"] == []
    with env[1]() as db:
        assert db.get(Job, response.json()["job_id"]).status == "cancelled"


def test_deleted_document_cannot_be_restored_by_old_jobs_or_new_upload(env):
    kb, auth, _ = prepare(env)
    first = upload(env, kb, auth)
    doc = detail(env)
    publish(env, doc)
    path = f"/internal/v1/documents/{doc['id']}"
    assert env[0].delete(path, headers=env[2]).status_code == 200
    assert env[0].post(path + "/unpublish", headers=env[2]).status_code == 404
    assert (
        env[0].get(f"/internal/v1/records/{first}", headers=env[2]).status_code == 200
    )
    payload = record(kb)
    payload["source_revision"] = 3
    assert (
        env[0]
        .post(
            "/api/v1/records",
            headers={**auth, "Idempotency-Key": "later"},
            json=payload,
        )
        .status_code
        == 410
    )
    assert search(env, kb, auth)["hits"] == []


def test_source_detail_and_software_version_filter_are_independent_of_pages(env):
    kb, auth, _ = prepare(env)
    first = upload(env, kb, auth)
    assert (
        env[0].get(f"/internal/v1/records/{first}", headers=env[2]).json()["id"]
        == first
    )
    assert env[0].get(f"/internal/v1/records/{first}", headers=auth).status_code == 401
    publish(env, detail(env))
    assert search(
        env, kb, auth, filters={"software": [{"name": "nginx", "version": "1"}]}
    )["hits"]
    assert (
        search(
            env, kb, auth, filters={"software": [{"name": "nginx", "version": "2"}]}
        )["hits"]
        == []
    )


def test_worker_error_backoff_and_readiness(env, monkeypatch):
    kb, auth, _ = prepare(env)
    upload(env, kb, auth)
    doc = detail(env)
    monkeypatch.setattr(processing.settings(), "worker_retry_base_seconds", 30)
    monkeypatch.setattr(
        processing, "embed", lambda _: (_ for _ in ()).throw(ValueError("failure"))
    )
    job_id = publish(env, doc)
    assert processing.run_once(env[1]) is False
    with env[1]() as db:
        job = db.get(Job, job_id)
        assert job.status == "pending" and job.attempts == 1
    assert env[0].get("/health/ready").status_code == 200
    assert env[0].get("/internal/v1/health", headers=env[2]).json()["worker"]["healthy"]
