"""Durable jobs: short transactions, lease ownership, immutable builds and CAS."""

import hashlib
import json
import logging
import math
import time
from urllib.parse import urlsplit

import httpx
from sqlalchemy import delete, or_, select, update

from .config import settings
from .db import SessionLocal
from .diagnostics import log_failure
from .indexing import CHUNKER_VERSION, chunk_content, chunk_id
from .lifecycle import logical_source_key, receive_source
from .models import (
    Chunk,
    Document,
    DocumentVersion,
    IndexBuild,
    Job,
    RefinementComparison,
    SourceRecord,
    WorkerHeartbeat,
    new_id,
)
from .quality import prepare_draft
from .refinement import refine
from .retrieval import searchable_text

WORKER_ID = new_id()


class IndexConfigurationMismatch(Exception):
    pass


def require_index_config(expected):
    if expected != index_id():
        raise IndexConfigurationMismatch()


def index_id():
    s = settings()
    if not s.embedding_enabled:
        return "keyword-" + CHUNKER_VERSION
    return hashlib.sha256(
        f"{s.embedding_base_url}:{s.embedding_model}:{s.embedding_dimensions}:{s.embedding_index_version}:{CHUNKER_VERSION}".encode()
    ).hexdigest()


def embed(texts):
    s = settings()
    if not s.embedding_enabled:
        return [None] * len(texts)
    url = urlsplit(s.embedding_base_url)
    if (
        (
            url.scheme != "https"
            and not (
                url.scheme == "http"
                and url.hostname
                in {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
            )
        )
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError("INVALID_EMBEDDING_ENDPOINT")
    if not 1 <= s.embedding_dimensions <= 16000:
        raise ValueError("INVALID_EMBEDDING_DIMENSIONS")
    with httpx.Client(
        timeout=httpx.Timeout(30, connect=8), follow_redirects=False, trust_env=False
    ) as client:
        with client.stream(
            "POST",
            s.embedding_base_url.rstrip("/") + "/embeddings",
            headers={"Authorization": f"Bearer {s.embedding_api_key}"},
            json={"model": s.embedding_model, "input": texts},
        ) as response:
            response.raise_for_status()
            body, started = bytearray(), time.monotonic()
            for part in response.iter_bytes():
                body.extend(part)
                if len(body) > 8 * 1024 * 1024 or time.monotonic() - started > 45:
                    raise ValueError("INVALID_EMBEDDING_RESPONSE")
    data = sorted(json.loads(body)["data"], key=lambda item: item["index"])
    vectors = [item["embedding"] for item in data]
    if len(vectors) != len(texts) or [item["index"] for item in data] != list(
        range(len(texts))
    ):
        raise ValueError("INVALID_EMBEDDING_COUNT")
    if any(
        len(v) != s.embedding_dimensions
        or not all(type(x) in {int, float} and math.isfinite(x) for x in v)
        for v in vectors
    ):
        raise ValueError("INVALID_EMBEDDING_DIMENSIONS")
    return vectors


def chunks(content):
    return chunk_content(content)


def draft(record):
    p = record.payload
    context = p.get("context", {})
    return Document(
        knowledge_base_id=record.knowledge_base_id,
        source_record_id=record.id,
        title=p["title"],
        content=prepare_draft(record),
        tags=p.get("tags", []),
        context=context,
        environment=context.get("environment", ""),
        software_names=[x["name"] for x in context.get("software", [])],
    )


def _renew(factory, job_id, lease):
    with factory() as db:
        alive = db.execute(
            update(Job)
            .where(Job.id == job_id, Job.lease_token == lease, Job.status == "running")
            .values(lease_until=time.time() + 120)
        )
        db.merge(WorkerHeartbeat(id=WORKER_ID, last_seen=time.time()))
        db.commit()
        return alive.rowcount == 1


def _finish_failure(factory, job_id, lease, exc):
    with factory() as db:
        initial = db.get(Job, job_id)
        if not initial:
            return
        target = (
            db.scalar(
                select(Document)
                .where(Document.id == initial.target_id)
                .with_for_update()
            )
            if initial.kind != "draft"
            else None
        )
        source = (
            db.scalar(
                select(SourceRecord)
                .where(SourceRecord.id == initial.target_id)
                .with_for_update()
            )
            if initial.kind == "draft"
            else None
        )
        job = db.scalar(
            select(Job)
            .where(Job.id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if job.lease_token != lease or job.status != "running":
            return
        mismatch = isinstance(exc, IndexConfigurationMismatch)
        terminal = job.kind == "refine" or mismatch or job.attempts >= 3
        job.status = "failed" if terminal else "pending"
        job.finished_at = time.time() if terminal else None
        job.available_at = time.time() + settings().worker_retry_base_seconds * 2 ** (
            job.attempts - 1
        )
        if job.kind == "refine":
            diagnostic = log_failure(exc, job_id, job.target_id, job.revision)
            job.error = diagnostic["code"] + (
                ":" + diagnostic["request_id"] if diagnostic.get("request_id") else ""
            )
        else:
            job.error = "INDEX_CONFIG_MISMATCH" if mismatch else "PROCESSING_FAILED"
            logging.getLogger(__name__).error(
                "knowledge_job_failed id=%s kind=%s error_type=%s",
                job.id,
                job.kind,
                type(exc).__name__,
            )
        if job.index_build_id:
            build = db.get(IndexBuild, job.index_build_id)
            if build and build.status != "ready":
                build.status = "failed" if terminal else "pending"
                build.finished_at = job.finished_at
        if terminal and job.kind == "draft":
            if source and source.status not in {"deleted", "rejected"}:
                source.status = "failed"
        elif (
            terminal
            and job.kind == "publish"
            and target
            and target.status == "indexing"
            and target.revision == job.document_revision
        ):
            target.status = "failed"
        db.commit()


def run_once(factory=SessionLocal):
    now, lease = time.time(), new_id()
    with factory() as db:
        db.merge(WorkerHeartbeat(id=WORKER_ID, last_seen=now))
        db.commit()
        job = db.scalar(
            select(Job)
            .where(
                or_(
                    (Job.status == "pending") & (Job.available_at <= now),
                    (Job.status == "running") & (Job.lease_until < now),
                )
            )
            .order_by(Job.created_at, Job.id)
            .limit(1)
        )
        if not job:
            return False
        attempts = job.attempts
        claimed = db.execute(
            update(Job)
            .where(
                Job.id == job.id,
                Job.attempts == attempts,
                or_(
                    (Job.status == "pending") & (Job.available_at <= now),
                    (Job.status == "running") & (Job.lease_until < now),
                ),
            )
            .values(
                status="running",
                attempts=attempts + 1,
                lease_token=lease,
                lease_until=now + 120,
            )
        )
        if claimed.rowcount != 1:
            db.rollback()
            return True
        job_id, kind, target, revision, build_id = (
            job.id,
            job.kind,
            job.target_id,
            job.revision,
            job.index_build_id,
        )
        db.commit()
    if attempts >= 3:
        _finish_failure(factory, job_id, lease, RuntimeError("LEASE_EXHAUSTED"))
        with factory() as db:
            job = db.get(Job, job_id)
            if job.lease_token == lease and job.status == "failed":
                job.error = "LEASE_EXHAUSTED"
                db.commit()
        return True
    try:
        prepared, record = [], None
        if kind == "refine":
            with factory() as db:
                doc = db.get(Document, target)
                if (
                    not doc
                    or doc.revision != revision
                    or doc.status != "draft"
                    or doc.superseded_by
                ):
                    raise ValueError("AI_STALE_DRAFT")
                record = db.get(SourceRecord, doc.source_record_id)
                if not record or record.status in {"deleted", "rejected"}:
                    raise ValueError("AI_SOURCE_UNAVAILABLE")
            title, content = refine(record)
        elif kind in {"publish", "reindex"}:
            with factory() as db:
                build = db.get(IndexBuild, build_id)
                if not build:
                    raise ValueError("INDEX_BUILD_MISSING")
                version = db.get(DocumentVersion, build.version_id)
                content, version_id, expected_index = (
                    version.content,
                    version.id,
                    build.index_version,
                )
            require_index_config(expected_index)
            parts = chunks(content)
            if not parts:
                raise ValueError("EMPTY_INDEX")
            for offset in range(0, len(parts), 8):
                if not _renew(factory, job_id, lease):
                    return True
                require_index_config(expected_index)
                batch = parts[offset : offset + 8]
                vectors = embed([x[0] for x in batch])
                require_index_config(expected_index)
                prepared.extend(zip(batch, vectors, strict=True))
        elif kind != "draft":
            raise ValueError("UNKNOWN_JOB_KIND")
        with factory() as db:
            if kind == "draft":
                record = db.get(SourceRecord, target)
                doc = (
                    db.scalar(
                        select(Document)
                        .where(
                            Document.logical_source_key == logical_source_key(record)
                        )
                        .with_for_update()
                    )
                    if record
                    else None
                )
                record = db.scalar(
                    select(SourceRecord)
                    .where(SourceRecord.id == target)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            else:
                doc = db.scalar(
                    select(Document).where(Document.id == target).with_for_update()
                )
            job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if not job or job.lease_token != lease or job.status != "running":
                return True
            if kind == "draft":
                if record and record.status not in {"deleted", "rejected"}:
                    receive_source(db, record)
            elif kind == "refine":
                source = db.get(SourceRecord, doc.source_record_id) if doc else None
                if (
                    not doc
                    or doc.revision != revision
                    or doc.status != "draft"
                    or doc.superseded_by
                    or not source
                    or source.id != record.id
                    or source.status in {"deleted", "rejected"}
                ):
                    job.status, job.error, job.finished_at = (
                        "cancelled",
                        "AI_STALE_DRAFT",
                        time.time(),
                    )
                    db.commit()
                    return True
                before_title, before_content = doc.title, doc.content
                doc.title, doc.content, doc.draft_edited = title, content, False
                db.flush()
                comparison = db.get(RefinementComparison, doc.id)
                if comparison is None:
                    comparison = RefinementComparison(document_id=doc.id)
                    db.add(comparison)
                comparison.before_title, comparison.before_content = (
                    before_title,
                    before_content,
                )
                comparison.after_title, comparison.after_content = title, content
                comparison.applied_revision, comparison.created_at = (
                    doc.revision,
                    time.time(),
                )
            else:
                require_index_config(expected_index)
                build = db.get(IndexBuild, build_id)
                version = db.get(DocumentVersion, version_id)
                valid = (
                    doc
                    and not doc.superseded_by
                    and doc.status not in {"deleted", "rejected"}
                )
                valid = valid and (
                    doc.status == "indexing" and doc.revision == job.document_revision
                    if kind == "publish"
                    else doc.published_version == revision
                )
                if not valid:
                    job.status, job.finished_at = "cancelled", time.time()
                    build.status, build.finished_at = "cancelled", time.time()
                    db.commit()
                    return True
                db.execute(delete(Chunk).where(Chunk.build_id == build_id))
                for ordinal, ((text, start, end), vector) in enumerate(prepared):
                    db.add(
                        Chunk(
                            id=chunk_id(f"{version_id}:{build_id}", ordinal, text),
                            version_id=version_id,
                            build_id=build_id,
                            content=text,
                            line_start=start,
                            line_end=end,
                            embedding=vector,
                            search_text=searchable_text(version.title, text),
                        )
                    )
                build.status, build.chunk_count, build.finished_at = (
                    "ready",
                    len(prepared),
                    time.time(),
                )
                version.active_build_id = build_id
                if kind == "publish":
                    doc.published_version, doc.status, doc.draft_edited = (
                        revision,
                        "published",
                        False,
                    )
                    for source_id in version.source_record_ids:
                        source = db.get(SourceRecord, source_id)
                        if source and source.status not in {"deleted", "rejected"}:
                            source.status = "processed"
            job.status, job.error, job.finished_at = "done", None, time.time()
            db.commit()
    except Exception as exc:
        _finish_failure(factory, job_id, lease, exc)
    return True


if __name__ == "__main__":
    while True:
        try:
            run_once()
        except Exception as exc:
            logging.getLogger(__name__).error(
                "worker_cycle_failed error_type=%s", type(exc).__name__
            )
        time.sleep(max(0.1, settings().worker_poll_seconds))
