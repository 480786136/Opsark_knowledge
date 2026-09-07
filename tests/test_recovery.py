from test_workflow import env, prepare, record  # noqa: F401
from sqlalchemy import select
from knowledge.models import Job, SourceRecord
from knowledge.processing import run_once


def test_exhausted_worker_lease_becomes_failed(env):  # noqa: F811 - imported pytest fixture
    client, factory, service = env
    kb, auth, _ = prepare(env)
    response = client.post(
        "/api/v1/records",
        headers={**auth, "Idempotency-Key": "recover"},
        json=record(kb),
    )
    with factory() as db:
        job = db.scalar(select(Job))
        job.status = "running"
        job.attempts = 3
        job.lease_until = 0
        db.commit()
    assert run_once(factory)
    with factory() as db:
        assert db.scalar(select(Job)).status == "failed"
        assert db.get(SourceRecord, response.json()["record_id"]).status == "failed"


def test_delete_retains_deduplication_marker(env):  # noqa: F811 - imported pytest fixture
    client, factory, service = env
    kb, auth, _ = prepare(env)
    payload = record(kb)
    headers = {**auth, "Idempotency-Key": "delete"}
    r = client.post("/api/v1/records", headers=headers, json=payload).json()
    run_once(factory)
    assert (
        client.delete(
            "/internal/v1/records/" + r["record_id"], headers=service
        ).status_code
        == 409
    )
    doc = client.get("/internal/v1/documents", headers=service).json()[0]
    assert (
        client.delete(
            "/internal/v1/documents/" + doc["id"], headers=service
        ).status_code
        == 200
    )
    assert (
        client.delete(
            "/internal/v1/records/" + r["record_id"], headers=service
        ).status_code
        == 200
    )
    assert (
        client.post("/api/v1/records", headers=headers, json=payload).status_code == 410
    )
    with factory() as db:
        assert db.get(SourceRecord, r["record_id"]).payload == {}
