"""Real independent DB sessions, exercised only on disposable PostgreSQL."""
# ruff: noqa: F811

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import pytest
from sqlalchemy import func, select
from test_publication_lifecycle import detail, publish, upload
from test_workflow import env, prepare, record  # noqa: F401

from knowledge import main, processing
from knowledge.models import (
    Document,
    DocumentSource,
    DocumentVersion,
    Job,
    SourceRecord,
)


@pytest.fixture(autouse=True)
def require_postgres(env):
    with env[1]() as db:
        if db.bind.dialect.name != "postgresql":
            pytest.skip(
                "Run with OPSARK_TEST_POSTGRES_ADMIN_URL for row-lock concurrency"
            )
    # One heartbeat per worker process; threads here model independent sessions.
    processing.run_once(env[1])


def test_concurrent_publish_uses_revision_lock(env):
    kb, auth, _ = prepare(env)
    upload(env, kb, auth)
    doc = detail(env)

    def submit(_):
        return env[0].post(
            f"/internal/v1/documents/{doc['id']}/publish",
            headers=env[2],
            json={"revision": doc["revision"]},
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(submit, range(4)))
    assert sorted(r.status_code for r in responses) == [202, 409, 409, 409]
    processing.run_once(env[1])
    with env[1]() as db:
        assert db.scalar(select(func.count()).select_from(DocumentVersion)) == 1
        assert db.get(Document, doc["id"]).published_version == 1


def test_concurrent_idempotent_upload_queues_once(env):
    kb, auth, _ = prepare(env)

    def submit(_):
        return env[0].post(
            "/api/v1/records",
            headers={**auth, "Idempotency-Key": "same-body"},
            json=record(kb),
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(submit, range(8)))
    assert all(r.status_code == 202 for r in responses)
    assert len({r.json()["record_id"] for r in responses}) == 1
    with env[1]() as db:
        assert db.scalar(select(func.count()).select_from(SourceRecord)) == 1
        assert db.scalar(select(func.count()).select_from(Job)) == 1


def test_concurrent_source_revisions_converge_to_one_latest_draft(env):
    kb, auth, _ = prepare(env)
    for revision in range(1, 9):
        payload = record(kb)
        payload.update(source_revision=revision, title=f"Nginx rev {revision}")
        response = env[0].post(
            "/api/v1/records",
            headers={**auth, "Idempotency-Key": f"rev-{revision}"},
            json=payload,
        )
        assert response.status_code == 202

    def work(_):
        for _ in range(24):
            processing.run_once(env[1])

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(work, range(4)))
    with env[1]() as db:
        docs = list(db.scalars(select(Document)))
        assert len(docs) == 1
        assert db.get(SourceRecord, docs[0].source_record_id).source_revision == 8
        assert db.scalar(select(func.count()).select_from(DocumentSource)) == 8
        assert {job.status for job in db.scalars(select(Job))} == {"done"}


def test_retry_cannot_reactivate_job_cancelled_while_waiting_for_document(
    env, monkeypatch
):
    kb, auth, _ = prepare(env)
    upload(env, kb, auth)
    doc = detail(env)
    publish(env, doc)
    doc = detail(env)
    result = env[0].post(
        f"/internal/v1/documents/{doc['id']}/reindex",
        headers=env[2],
        json={"revision": doc["revision"]},
    )
    job_id = result.json()["job_id"]
    with env[1]() as db:
        db.get(Job, job_id).status = "failed"
        db.commit()
    entered, release, lock = Event(), Event(), Lock()
    original = main.managed_document
    delayed = False

    def managed(db, ident):
        nonlocal delayed
        with lock:
            pause = not delayed
            delayed = True
        if pause:
            entered.set()
            assert release.wait(10)
        return original(db, ident)

    monkeypatch.setattr(main, "managed_document", managed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            env[0].post, f"/internal/v1/jobs/{job_id}/retry", headers=env[2]
        )
        try:
            assert entered.wait(10)
            response = env[0].post(
                f"/internal/v1/documents/{doc['id']}/unpublish", headers=env[2]
            )
            assert response.status_code == 200
        finally:
            release.set()
        assert future.result(timeout=10).status_code == 409
    with env[1]() as db:
        assert db.get(Job, job_id).status == "cancelled"
        assert db.get(Document, doc["id"]).published_version is None


def test_source_delete_rechecks_use_after_inflight_draft_commits(env, monkeypatch):
    kb, auth, _ = prepare(env)
    result = env[0].post(
        "/api/v1/records", headers={**auth, "Idempotency-Key": "new"}, json=record(kb)
    )
    source_id = result.json()["record_id"]
    from knowledge import lifecycle

    entered, release = Event(), Event()
    original = lifecycle.prepare_draft

    def prepare_draft(source):
        entered.set()
        assert release.wait(10)
        return original(source)

    monkeypatch.setattr(lifecycle, "prepare_draft", prepare_draft)
    with ThreadPoolExecutor(max_workers=2) as pool:
        worker = pool.submit(processing.run_once, env[1])
        try:
            assert entered.wait(10)
            deletion = pool.submit(
                env[0].delete, f"/internal/v1/records/{source_id}", headers=env[2]
            )
        finally:
            release.set()
        assert worker.result(timeout=10)
        assert deletion.result(timeout=10).status_code == 409
    with env[1]() as db:
        assert db.get(SourceRecord, source_id).payload
        assert db.scalar(select(func.count()).select_from(Document)) == 1
