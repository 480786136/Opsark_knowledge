"""Source lineage and publication operations shared by API and durable worker."""

import hashlib
import json
import time

from sqlalchemy import select

from .config import settings
from .indexing import CHUNKER_VERSION
from .models import (
    Document,
    DocumentSource,
    IndexBuild,
    Job,
    RefinementComparison,
    SourceRecord,
)
from .quality import prepare_draft


def logical_source_key(record):
    value = (record.installation_id, record.knowledge_base_id, record.source_record_id)
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def index_config():
    s = settings()
    return {
        "mode": "hybrid" if s.embedding_enabled else "keyword_only",
        "base_url": s.embedding_base_url if s.embedding_enabled else "",
        "model": s.embedding_model if s.embedding_enabled else "",
        "dimensions": s.embedding_dimensions if s.embedding_enabled else 0,
        "index_version": s.embedding_index_version,
        "chunker": CHUNKER_VERSION,
    }


def cancel_jobs(db, ident, kinds=None):
    query = select(Job).where(
        Job.target_id == ident, Job.status.in_(["pending", "running", "failed"])
    )
    if kinds is not None:
        query = query.where(Job.kind.in_(kinds))
    for job in db.scalars(query):
        job.status, job.lease_token, job.finished_at = "cancelled", None, time.time()
        if job.index_build_id:
            build = db.get(IndexBuild, job.index_build_id)
            if build and build.status != "ready":
                build.status, build.finished_at = "cancelled", time.time()


def apply_source(db, document, record):
    """Caller locks the document and decides whether replacing its draft is allowed."""
    previous = (
        db.get(SourceRecord, document.source_record_id)
        if document.source_record_id
        else None
    )
    cancel_jobs(db, document.id, ["refine", "publish"])
    p = record.payload
    context = p.get("context", {})
    document.source_record_id = record.id
    document.title, document.content = p["title"], prepare_draft(record)
    document.tags = p.get("tags", [])
    document.context = context
    document.environment = context.get("environment", "")
    document.software_names = [x["name"] for x in context.get("software", [])]
    document.status, document.draft_edited = "draft", False
    document.updated_at = time.time()
    if previous and previous.id != record.id and previous.status == "ready_for_review":
        previous.status = "superseded"
    record.status = "ready_for_review"
    comparison = db.get(RefinementComparison, document.id)
    if comparison:
        db.delete(comparison)
    db.flush()
    if settings().ai_refinement_enabled:
        db.add(Job(kind="refine", target_id=document.id, revision=document.revision))


def receive_source(db, record):
    """Never resurrect deleted lineages or overwrite a newer/manually edited draft."""
    logical = logical_source_key(record)
    document = db.scalar(
        select(Document).where(Document.logical_source_key == logical).with_for_update()
    )
    if document is None:
        document = Document(
            knowledge_base_id=record.knowledge_base_id,
            logical_source_key=logical,
            title=record.payload["title"],
            content="",
        )
        db.add(document)
        db.flush()
    if not db.get(DocumentSource, record.id):
        db.add(DocumentSource(source_record_id=record.id, document_id=document.id))
    if document.status in {"deleted", "rejected"}:
        record.status = "rejected"
        return
    current = (
        db.get(SourceRecord, document.source_record_id)
        if document.source_record_id
        else None
    )
    if current and current.source_revision >= record.source_revision:
        if current.id != record.id:
            record.status = "superseded"
        return
    if document.draft_edited or document.status == "indexing":
        record.status = "ready_for_review"
        return
    apply_source(db, document, record)


def enqueue_index(db, document, version, fingerprint, kind="publish"):
    build = IndexBuild(
        version_id=version.id, index_version=fingerprint, config_snapshot=index_config()
    )
    db.add(build)
    db.flush()
    job = Job(
        kind=kind,
        target_id=document.id,
        revision=version.version,
        document_revision=document.revision,
        index_build_id=build.id,
    )
    db.add(job)
    db.flush()
    return job
