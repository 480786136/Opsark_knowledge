import hashlib
import math
import time

import httpx
from sqlalchemy import or_, select, update

from .config import settings
from .db import SessionLocal
from .diagnostics import log_failure
from .indexing import chunk_content, chunk_id
from .models import (
    Chunk,
    Document,
    DocumentVersion,
    Job,
    RefinementComparison,
    SourceRecord,
    new_id,
)
from .quality import prepare_draft
from .refinement import refine


class IndexConfigurationMismatch(Exception):
    pass


def require_index_config(expected):
    if expected != index_id():
        raise IndexConfigurationMismatch()


def index_id():
    s = settings()
    if not s.embedding_enabled:
        return "keyword-v1"
    return hashlib.sha256(
        f"{s.embedding_base_url}:{s.embedding_model}:{s.embedding_dimensions}:{s.embedding_index_version}".encode()
    ).hexdigest()


def embed(texts):
    s = settings()
    if not s.embedding_enabled:
        return [None] * len(texts)
    response = httpx.post(
        s.embedding_base_url.rstrip("/") + "/embeddings",
        headers={"Authorization": f"Bearer {s.embedding_api_key}"},
        json={"model": s.embedding_model, "input": texts},
        timeout=30,
        follow_redirects=False,
    )
    response.raise_for_status()
    data = sorted(response.json()["data"], key=lambda item: item["index"])
    vectors = [item["embedding"] for item in data]
    if len(vectors) != len(texts) or [item["index"] for item in data] != list(
        range(len(texts))
    ):
        raise ValueError("INVALID_EMBEDDING_COUNT")
    if any(
        len(v) != s.embedding_dimensions or not all(math.isfinite(x) for x in v)
        for v in vectors
    ):
        raise ValueError("INVALID_EMBEDDING_DIMENSIONS")
    return vectors


def chunks(content):
    return chunk_content(content)


def draft(record):
    p = record.payload
    return Document(
        knowledge_base_id=record.knowledge_base_id,
        source_record_id=record.id,
        title=p["title"],
        content=prepare_draft(record),
        tags=p["tags"],
        environment=p["context"]["environment"],
        software_names=[x["name"] for x in p["context"]["software"]],
    )


def run_once(factory=SessionLocal):
    """Durable lease/CAS. External embedding never holds the database transaction."""
    now, lease = time.time(), new_id()
    with factory() as db:
        job = db.scalar(
            select(Job)
            .where(
                or_(
                    Job.status == "pending",
                    (Job.status == "running") & (Job.lease_until < now),
                )
            )
            .order_by(Job.created_at)
            .limit(1)
        )
        if not job:
            return False
        if job.attempts >= 3:
            job.status, job.error = "failed", "LEASE_EXHAUSTED"
            target_row = db.get(
                SourceRecord if job.kind == "draft" else Document, job.target_id
            )
            if (
                job.kind != "refine"
                and target_row
                and target_row.status not in {"deleted", "rejected"}
            ):
                target_row.status = "failed"
            db.commit()
            return True
        claimed = db.execute(
            update(Job)
            .where(
                Job.id == job.id,
                Job.attempts == job.attempts,
                or_(
                    Job.status == "pending",
                    (Job.status == "running") & (Job.lease_until < now),
                ),
            )
            .values(
                status="running",
                attempts=job.attempts + 1,
                lease_token=lease,
                lease_until=now + 120,
            )
        )
        if claimed.rowcount != 1:
            db.rollback()
            return True
        job_id, kind, target, revision = job.id, job.kind, job.target_id, job.revision
        db.commit()
    try:
        record = None
        prepared = []
        if kind == "refine":
            with factory() as db:
                doc = db.get(Document, target)
                if (
                    not doc
                    or doc.revision != revision
                    or doc.status != "draft"
                    or doc.published_version is not None
                ):
                    raise ValueError("AI_STALE_DRAFT")
                record = db.get(SourceRecord, doc.source_record_id)
                if not record or record.status in {"deleted", "rejected"}:
                    raise ValueError("AI_SOURCE_UNAVAILABLE")
                title, content = None, None
            title, content = refine(record)
        if kind == "publish":
            with factory() as db:
                version = db.scalar(
                    select(DocumentVersion).where(
                        DocumentVersion.document_id == target,
                        DocumentVersion.version == revision,
                    )
                )
                content, version_id, expected_index = (
                    version.content,
                    version.id,
                    version.index_version,
                )
            require_index_config(expected_index)
            parts = chunks(content)
            for offset in range(0, len(parts), 8):
                require_index_config(expected_index)
                batch = parts[offset : offset + 8]
                vectors = embed([x[0] for x in batch])
                require_index_config(expected_index)
                prepared.extend(zip(batch, vectors))
                with factory() as db:
                    alive = db.execute(
                        update(Job)
                        .where(
                            Job.id == job_id,
                            Job.lease_token == lease,
                            Job.status == "running",
                        )
                        .values(lease_until=time.time() + 120)
                    )
                    db.commit()
                    if alive.rowcount != 1:
                        return True
        with factory() as db:
            job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if job.lease_token != lease or job.status != "running":
                return True
            if kind == "draft":
                record = db.get(SourceRecord, target)
                if record.status not in {"deleted", "rejected"}:
                    document = db.scalar(
                        select(Document.id).where(Document.source_record_id == target)
                    )
                    if not document:
                        doc = db.scalar(
                            select(Document)
                            .join(
                                SourceRecord,
                                Document.source_record_id == SourceRecord.id,
                            )
                            .where(
                                SourceRecord.installation_id
                                == record.installation_id,
                                SourceRecord.source_record_id
                                == record.source_record_id,
                                SourceRecord.source_revision
                                < record.source_revision,
                                Document.knowledge_base_id
                                == record.knowledge_base_id,
                                Document.status.notin_(["deleted", "rejected"]),
                                Document.published_version.is_(None),
                            )
                            .order_by(SourceRecord.source_revision.desc())
                        )
                        generated = draft(record)
                        if doc:
                            previous = db.get(SourceRecord, doc.source_record_id)
                            doc.source_record_id = record.id
                            doc.title = generated.title
                            doc.content = generated.content
                            doc.tags = generated.tags
                            doc.environment = generated.environment
                            doc.software_names = generated.software_names
                            doc.status = "draft"
                            if previous and previous.status == "ready_for_review":
                                previous.status = "processed"
                            comparison = db.get(RefinementComparison, doc.id)
                            if comparison:
                                db.delete(comparison)
                        else:
                            doc = generated
                            db.add(doc)
                        db.flush()
                        if settings().ai_refinement_enabled:
                            db.add(
                                Job(
                                    kind="refine",
                                    target_id=doc.id,
                                    revision=doc.revision,
                                )
                            )
                    record.status = "ready_for_review"
            elif kind == "refine":
                doc = db.get(Document, target)
                record = db.get(SourceRecord, doc.source_record_id) if doc else None
                if (
                    not doc
                    or doc.revision != revision
                    or doc.status != "draft"
                    or doc.published_version is not None
                    or not record
                    or record.status in {"deleted", "rejected"}
                ):
                    job.status, job.error = "cancelled", "AI_STALE_DRAFT"
                    db.commit()
                    return True
                before_title, before_content = doc.title, doc.content
                doc.title, doc.content = title, content
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
                doc = db.get(Document, target)
                if doc.status != "indexing":
                    job.status = "cancelled"
                    db.commit()
                    return True
                for ordinal, ((text, start, end), vector) in enumerate(prepared):
                    db.add(
                        Chunk(
                            id=chunk_id(version_id, ordinal, text),
                            version_id=version_id,
                            content=text,
                            line_start=start,
                            line_end=end,
                            embedding=vector,
                        )
                    )
                doc.published_version, doc.status = revision, "published"
                if doc.source_record_id:
                    db.get(SourceRecord, doc.source_record_id).status = "processed"
            job.status, job.error = "done", None
            db.commit()
    except Exception as exc:  # noqa: BLE001 -- persist a sanitized failure at the worker boundary.
        if (
            kind == "refine"
            and not hasattr(exc, "ai_diagnostic")
            and record is not None
        ):
            exc.ai_diagnostic = {
                **getattr(record, "_ai_diagnostic", {}),
                "stage": "save_result",
            }
        diagnostic = (
            log_failure(exc, job_id, target, revision) if kind == "refine" else None
        )
        with factory() as db:
            job = db.get(Job, job_id)
            if job.lease_token == lease and job.status == "running":
                mismatch = isinstance(exc, IndexConfigurationMismatch)
                job.status = (
                    "failed"
                    if kind == "refine" or mismatch or job.attempts >= 3
                    else "pending"
                )
                job.error = (
                    diagnostic["code"]
                    + (
                        ":" + diagnostic["request_id"]
                        if diagnostic.get("request_id")
                        else ""
                    )
                    if kind == "refine"
                    else "INDEX_CONFIG_MISMATCH"
                    if mismatch
                    else "PROCESSING_FAILED"
                )
                if job.status == "failed" and kind != "refine":
                    obj = db.get(SourceRecord if kind == "draft" else Document, target)
                    obj.status = "failed"
                db.commit()
    return True


if __name__ == "__main__":
    while True:
        run_once()
        time.sleep(1)
