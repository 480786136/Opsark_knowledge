import logging
import math
import re
import secrets
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import case, delete, func, inspect, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm.exc import StaleDataError

from .config import settings
from .console import document_metadata, overview_data
from .db import get_db
from .lifecycle import apply_source, cancel_jobs, enqueue_index, logical_source_key
from .models import (
    ApiKey,
    Audit,
    Chunk,
    Document,
    DocumentSource,
    DocumentVersion,
    Idempotency,
    IndexBuild,
    Job,
    KnowledgeBase,
    RefinementComparison,
    SourceRecord,
    WorkerHeartbeat,
)
from .processing import embed, index_id
from .retrieval import (
    keyword_rank,
    postgres_candidates,
    query_terms,
    snapshot_still_public,
)
from .schemas import (
    BaseInput,
    BatchPublishInput,
    DocumentInput,
    DraftInput,
    KeyInput,
    RecordInput,
    RevisionInput,
    SearchInput,
    SourceRevisionInput,
)
from .security import (
    ApiError,
    check_bases,
    check_sensitive,
    digest,
    require_key,
    service_auth,
)

app = FastAPI(
    title="Opsark Knowledge",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
internal = APIRouter(prefix="/internal/v1", dependencies=[Depends(service_auth)])


def data(row):
    return {
        c.key: getattr(row, c.key)
        for c in inspect(row).mapper.column_attrs
        if c.key not in {"token_hash", "lease_token"}
    }


def audit(db, request, action, target):
    db.add(
        Audit(
            actor=getattr(
                request.state,
                "knowledge_actor",
                request.headers.get("x-opsark-actor", "service"),
            )[:128],
            action=action,
            target=target,
        )
    )


def get(db, model, ident):
    row = db.get(model, ident)
    if not row:
        raise ApiError(404, "RESOURCE_NOT_FOUND")
    return row


def managed_document(db, ident):
    row = db.scalar(select(Document).where(Document.id == ident).with_for_update())
    if not row or row.status == "deleted":
        raise ApiError(404, "RESOURCE_NOT_FOUND")
    if row.superseded_by:
        raise ApiError(
            409, "DOCUMENT_SUPERSEDED", "该文档已归档，请在当前文档的来源历史中操作"
        )
    return row


def document_data(db, row, metadata=None):
    item = data(row)
    item.update(
        metadata if metadata is not None else document_metadata(db, [row])[row.id]
    )
    return item


@app.exception_handler(ApiError)
async def error(request, exc):
    return JSONResponse(
        {
            "request_id": getattr(request.state, "request_id", ""),
            "error": {
                "code": exc.code,
                "message": exc.message,
                "fields": exc.fields,
                "retryable": exc.status in {429, 503},
            },
        },
        status_code=exc.status,
        headers={"Retry-After": "60"} if exc.status == 429 else {},
    )


@app.exception_handler(RequestValidationError)
async def validation(request, exc):
    return await error(
        request,
        ApiError(
            422,
            "VALIDATION_ERROR",
            "请求字段不符合契约",
            [".".join(map(str, e["loc"])) for e in exc.errors()],
        ),
    )


@app.exception_handler(StaleDataError)
async def stale(request, exc):
    return await error(
        request, ApiError(409, "REVISION_CONFLICT", "内容已变化，请刷新")
    )


@app.middleware("http")
async def bounds(request, call_next):
    request.state.request_id = uuid.uuid4().hex
    if request.method in {"POST", "PATCH", "PUT"}:
        limit = (
            2 * 1024 * 1024
            if request.url.path.startswith(("/internal/", "/api/admin/"))
            else 256 * 1024
        )
        if request.headers.get("content-encoding", "identity") != "identity":
            return await error(request, ApiError(415, "UNSUPPORTED_ENCODING"))
        body = bytearray()
        async for part in request.stream():
            body.extend(part)
            if len(body) > limit:
                return await error(request, ApiError(413, "PAYLOAD_TOO_LARGE"))
        request._body = bytes(body)
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


@app.get("/health/live")
def live():
    return {"status": "ok", "service": "knowledge"}


@internal.get("/health")
def health(db=Depends(get_db)):
    db.execute(select(KnowledgeBase.id).limit(1))
    heartbeat = db.scalar(select(func.max(WorkerHeartbeat.last_seen)))
    return {
        "status": "ok",
        "index_version": index_id(),
        "worker": {
            "last_seen": heartbeat,
            "healthy": heartbeat is not None and time.time() - heartbeat < 180,
            "pending": db.scalar(
                select(func.count()).select_from(Job).where(Job.status == "pending")
            ),
            "failed": db.scalar(
                select(func.count()).select_from(Job).where(Job.status == "failed")
            ),
        },
    }


@app.get("/health/ready")
def ready(db=Depends(get_db)):
    # Liveness alone cannot detect a missing migration or an unavailable database.
    db.execute(select(Document.logical_source_key).limit(1))
    db.execute(select(IndexBuild.id).limit(1))
    return {"status": "ready"}


@internal.get("/openapi.json")
def openapi():
    return app.openapi()


@internal.get("/knowledge-bases")
def bases(
    offset: int = Query(0, ge=0, le=1000000),
    limit: int = Query(200, ge=1, le=200),
    db=Depends(get_db),
):
    return [
        data(x)
        for x in db.scalars(
            select(KnowledgeBase)
            .order_by(KnowledgeBase.created_at.desc(), KnowledgeBase.id)
            .offset(offset)
            .limit(limit)
        )
    ]


@internal.get("/overview")
def overview(
    offset: int = Query(0, ge=0, le=1000000),
    limit: int = Query(200, ge=1, le=200),
    db=Depends(get_db),
):
    return overview_data(db, offset, limit)


@internal.post("/knowledge-bases", status_code=201)
def create_base(payload: BaseInput, request: Request, db=Depends(get_db)):
    row = KnowledgeBase(**payload.model_dump())
    db.add(row)
    db.flush()
    audit(db, request, "base.create", row.id)
    db.commit()
    return data(row)


@internal.patch("/knowledge-bases/{ident}")
def update_base(ident: str, payload: BaseInput, request: Request, db=Depends(get_db)):
    row = get(db, KnowledgeBase, ident)
    for k, v in payload.model_dump().items():
        setattr(row, k, v)
    audit(db, request, "base.update", ident)
    db.commit()
    return data(row)


@internal.get("/knowledge-keys")
def keys(
    offset: int = Query(0, ge=0, le=1000000),
    limit: int = Query(200, ge=1, le=200),
    db=Depends(get_db),
):
    return [
        data(x)
        for x in db.scalars(
            select(ApiKey)
            .order_by(ApiKey.created_at.desc(), ApiKey.id)
            .offset(offset)
            .limit(limit)
        )
    ]


@internal.post("/knowledge-keys", status_code=201)
def create_key(payload: KeyInput, request: Request, db=Depends(get_db)):
    check_bases(db, payload.knowledge_base_ids)
    raw = "okk_" + secrets.token_urlsafe(32)
    row = ApiKey(
        name=payload.name,
        installation_id=payload.installation_id,
        scopes=payload.scopes,
        knowledge_base_ids=payload.knowledge_base_ids,
        token_hash=digest(raw),
        prefix=raw[:12],
        expires=time.time() + payload.expires_days * 86400,
    )
    db.add(row)
    db.flush()
    audit(db, request, "key.create", row.id)
    db.commit()
    return {**data(row), "api_key": raw}


@internal.delete("/knowledge-keys/{ident}")
def revoke_key(ident: str, request: Request, db=Depends(get_db)):
    get(db, ApiKey, ident).revoked = True
    audit(db, request, "key.revoke", ident)
    db.commit()
    return {"revoked": True}


@app.post("/api/v1/records", status_code=202)
async def upload(
    payload: RecordInput,
    request: Request,
    key=Depends(require_key("records:write")),
    db=Depends(get_db),
):
    check_bases(db, [payload.knowledge_base_id], key)
    body = payload.model_dump(mode="json")
    check_sensitive(body)
    # Check the permanent lineage tombstone before accepting a later revision.
    identity = SourceRecord(
        installation_id=key.installation_id,
        knowledge_base_id=payload.knowledge_base_id,
        source_record_id=payload.source_record_id,
    )
    lineage = db.scalar(
        select(Document).where(
            Document.logical_source_key == logical_source_key(identity)
        )
    )
    if lineage and lineage.status == "deleted":
        raise ApiError(410, "DOCUMENT_DELETED", "该来源的知识文档已删除")
    idem = request.headers.get("idempotency-key", "")
    if not re.fullmatch(r"[\x21-\x7e]{1,128}", idem):
        raise ApiError(422, "INVALID_IDEMPOTENCY_KEY")
    body_hash = digest(await request.body())
    duplicate = False
    for attempt in range(2):
        try:
            prior = db.scalar(
                select(Idempotency).where(
                    Idempotency.installation_id == key.installation_id,
                    Idempotency.key == idem,
                )
            )
            record = (
                db.get(SourceRecord, prior.record_id)
                if prior
                else db.scalar(
                    select(SourceRecord).where(
                        SourceRecord.installation_id == key.installation_id,
                        SourceRecord.source_record_id == payload.source_record_id,
                        SourceRecord.source_revision == payload.source_revision,
                    )
                )
            )
            if prior and prior.body_hash != body_hash:
                raise ApiError(409, "IDEMPOTENCY_CONFLICT")
            if record:
                if record.body_hash != body_hash:
                    raise ApiError(409, "SOURCE_REVISION_CONFLICT")
                if record.status == "deleted":
                    raise ApiError(410, "RECORD_DELETED")
                duplicate = True
            else:
                pending = db.scalar(
                    select(func.count())
                    .select_from(SourceRecord)
                    .where(
                        SourceRecord.installation_id == key.installation_id,
                        SourceRecord.status.in_(["received", "ready_for_review"]),
                    )
                )
                if pending >= 1000:
                    raise ApiError(429, "QUEUE_FULL")
                record = SourceRecord(
                    installation_id=key.installation_id,
                    source_record_id=payload.source_record_id,
                    source_revision=payload.source_revision,
                    knowledge_base_id=payload.knowledge_base_id,
                    body_hash=body_hash,
                    payload=body,
                )
                db.add(record)
                db.flush()
                db.add(Job(kind="draft", target_id=record.id))
            if not prior:
                db.add(
                    Idempotency(
                        installation_id=key.installation_id,
                        key=idem,
                        body_hash=body_hash,
                        record_id=record.id,
                    )
                )
            db.commit()
            return {
                "request_id": request.state.request_id,
                "record_id": record.id,
                "status": record.status,
                "duplicate": duplicate,
                "status_url": f"/api/v1/records/{record.id}",
            }
        except IntegrityError:
            db.rollback()
            if attempt:
                raise ApiError(409, "UPLOAD_CONFLICT")


@app.get("/api/v1/records/{ident}")
def record_status(
    ident: str, key=Depends(require_key("records:read")), db=Depends(get_db)
):
    row = get(db, SourceRecord, ident)
    if (
        row.installation_id != key.installation_id
        or row.knowledge_base_id not in key.knowledge_base_ids
    ):
        raise ApiError(404, "RESOURCE_NOT_FOUND")
    check_bases(db, [row.knowledge_base_id], key)
    return {
        "record_id": row.id,
        "status": row.status,
        "source_record_id": row.source_record_id,
        "source_revision": row.source_revision,
    }


@internal.get("/records")
def records(
    offset: int = Query(0, ge=0, le=1000000),
    limit: int = Query(200, ge=1, le=200),
    db=Depends(get_db),
):
    return [
        data(x)
        for x in db.scalars(
            select(SourceRecord)
            .order_by(SourceRecord.created_at.desc(), SourceRecord.id)
            .offset(offset)
            .limit(limit)
        )
    ]


@internal.get("/records/{ident}")
def record_detail(ident: str, db=Depends(get_db)):
    row = get(db, SourceRecord, ident)
    if row.status == "deleted":
        raise ApiError(410, "RECORD_DELETED")
    return data(row)


@internal.get("/documents")
def documents(
    offset: int = Query(0, ge=0, le=1000000),
    limit: int = Query(200, ge=1, le=200),
    knowledge_base_id: str | None = Query(None, max_length=128),
    db=Depends(get_db),
):
    statement = select(Document).where(
        Document.status != "deleted", Document.superseded_by.is_(None)
    )
    if knowledge_base_id:
        statement = statement.where(Document.knowledge_base_id == knowledge_base_id)
    rows = list(
        db.scalars(
            statement.order_by(Document.updated_at.desc(), Document.id)
            .offset(offset)
            .limit(limit)
        )
    )
    metadata = document_metadata(db, rows)
    return [document_data(db, row, metadata[row.id]) for row in rows]


@internal.get("/documents/{ident}")
def document_detail(ident: str, db=Depends(get_db)):
    row = get(db, Document, ident)
    if row.status == "deleted":
        raise ApiError(404, "RESOURCE_NOT_FOUND")
    item = document_data(db, row)
    item["pending_source_record_ids"] = list(
        db.scalars(
            select(SourceRecord.id)
            .join(DocumentSource, DocumentSource.source_record_id == SourceRecord.id)
            .where(
                DocumentSource.document_id == ident,
                SourceRecord.status == "ready_for_review",
                SourceRecord.id != (row.source_record_id or ""),
            )
            .order_by(SourceRecord.source_revision.desc())
        )
    )
    item["latest_jobs"] = [
        data(j)
        for j in db.scalars(
            select(Job)
            .where(Job.target_id == ident)
            .order_by(Job.created_at.desc(), Job.id)
            .limit(10)
        )
    ]
    item["legacy_documents"] = [
        document_data(db, d)
        for d in db.scalars(
            select(Document).where(
                Document.superseded_by == ident, Document.status != "deleted"
            )
        )
    ]
    return item


@internal.get("/documents/{ident}/sources")
def document_sources(
    ident: str,
    offset: int = Query(0, ge=0, le=1000000),
    limit: int = Query(50, ge=1, le=200),
    db=Depends(get_db),
):
    get(db, Document, ident)
    return [
        dict(row)
        for row in db.execute(
            select(
                SourceRecord.id,
                SourceRecord.source_record_id,
                SourceRecord.source_revision,
                SourceRecord.installation_id,
                SourceRecord.knowledge_base_id,
                SourceRecord.status,
                SourceRecord.created_at,
                SourceRecord.payload["title"].as_string().label("title"),
            )
            .join(DocumentSource, DocumentSource.source_record_id == SourceRecord.id)
            .where(DocumentSource.document_id == ident)
            .order_by(
                SourceRecord.source_revision.desc(),
                SourceRecord.created_at.desc(),
                SourceRecord.id,
            )
            .offset(offset)
            .limit(limit)
        ).mappings()
    ]


@internal.post("/documents/{ident}/source")
def select_source(
    ident: str, payload: SourceRevisionInput, request: Request, db=Depends(get_db)
):
    row = managed_document(db, ident)
    if row.revision != payload.revision or row.status in {"indexing", "rejected"}:
        raise ApiError(409, "REVISION_CONFLICT")
    link = db.get(DocumentSource, payload.source_record_id)
    source = get(db, SourceRecord, payload.source_record_id)
    if (
        not link
        or link.document_id != ident
        or source.status in {"deleted", "rejected"}
    ):
        raise ApiError(409, "AI_SOURCE_UNAVAILABLE")
    check_bases(db, [row.knowledge_base_id])
    apply_source(db, row, source)
    audit(db, request, "document.source", ident)
    db.commit()
    return document_data(db, row)


@internal.get("/documents/{ident}/versions")
def document_versions(
    ident: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db=Depends(get_db),
):
    doc = get(db, Document, ident)
    return [
        {
            **data(v),
            "is_current": doc.published_version == v.version and not doc.superseded_by,
            "published": doc.published_version == v.version and not doc.superseded_by,
        }
        for v in db.scalars(
            select(DocumentVersion)
            .where(DocumentVersion.document_id == ident)
            .order_by(DocumentVersion.version.desc())
            .offset(offset)
            .limit(limit)
        )
    ]


@internal.get("/documents/{ident}/versions/{version}")
def version_detail(ident: str, version: int, db=Depends(get_db)):
    doc = get(db, Document, ident)
    row = db.scalar(
        select(DocumentVersion).where(
            DocumentVersion.document_id == ident, DocumentVersion.version == version
        )
    )
    if not row or doc.status == "deleted":
        raise ApiError(404, "RESOURCE_NOT_FOUND")
    return {
        **data(row),
        "is_current": doc.published_version == version and not doc.superseded_by,
    }


@internal.post("/documents", status_code=201)
def create_doc(payload: DocumentInput, request: Request, db=Depends(get_db)):
    check_bases(db, [payload.knowledge_base_id])
    check_sensitive(payload.model_dump())
    row = Document(**payload.model_dump(), draft_edited=True)
    db.add(row)
    db.flush()
    audit(db, request, "document.create", row.id)
    db.commit()
    return data(row)


@internal.get("/documents/{ident}/comparison")
def document_comparison(ident: str, db=Depends(get_db)):
    doc = get(db, Document, ident)
    if doc.status in {"deleted", "rejected"}:
        raise ApiError(404, "RESOURCE_NOT_FOUND")
    row = db.get(RefinementComparison, ident)
    return {"comparison": data(row) if row else None, "current_revision": doc.revision}


@internal.post("/documents/{ident}/refine", status_code=202)
def refine_document(
    ident: str, payload: RevisionInput, request: Request, db=Depends(get_db)
):
    row = managed_document(db, ident)
    if (
        not settings().ai_refinement_enabled
        or not settings().ai_model
        or not settings().ai_api_key
    ):
        raise ApiError(
            409, "AI_NOT_CONFIGURED", "请配置 Knowledge 的 AI 提炼模型并启用"
        )
    if row.revision != payload.revision or row.status != "draft":
        raise ApiError(409, "AI_STALE_DRAFT", "请先保存当前草稿；旧发布版本可继续使用")
    source = (
        get(db, SourceRecord, row.source_record_id) if row.source_record_id else None
    )
    if not source or source.status in {"deleted", "rejected"}:
        raise ApiError(409, "AI_SOURCE_UNAVAILABLE", "没有可用任务来源")
    # Touch the versioned document so concurrent enqueue/edit requests conflict.
    row.updated_at = time.time()
    db.flush()
    db.execute(
        update(Job)
        .where(
            Job.target_id == ident,
            Job.kind == "refine",
            Job.status.in_(["pending", "running"]),
        )
        .values(status="cancelled", lease_token=None)
    )
    job = Job(kind="refine", target_id=ident, revision=row.revision)
    db.add(job)
    audit(db, request, "document.refine", ident)
    db.commit()
    return data(job)


@internal.post("/records/{ident}/reject")
def reject_record(ident: str, request: Request, db=Depends(get_db)):
    row = get(db, SourceRecord, ident)
    # All lifecycle writers lock document -> source -> job. The source lock also
    # serializes rejection with a first draft that has not created a document yet.
    locked_doc = db.scalar(
        select(Document)
        .where(
            or_(
                Document.logical_source_key == logical_source_key(row),
                Document.source_record_id == ident,
            )
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    row = db.scalar(
        select(SourceRecord)
        .where(SourceRecord.id == ident)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row.status not in {"received", "ready_for_review", "failed"}:
        raise ApiError(409, "RECORD_NOT_REVIEWABLE")
    doc = db.scalar(
        select(Document)
        .where(Document.source_record_id == ident)
        .execution_options(populate_existing=True)
    )
    if doc and (locked_doc is None or doc.id != locked_doc.id):
        raise ApiError(409, "SOURCE_STATE_CHANGED", "来源已生成草稿，请刷新后重新审核")
    if doc and (doc.published_version is not None or doc.status == "indexing"):
        raise ApiError(409, "SOURCE_IN_USE", "已发布或正在发布的来源不能直接拒绝")
    row.status = "rejected"
    if doc:
        doc.status = "rejected"
    targets = [ident] + ([doc.id] if doc else [])
    for target in targets:
        cancel_jobs(db, target)
    audit(db, request, "record.reject", ident)
    db.commit()
    return {"record_id": row.id, "status": row.status}


@internal.patch("/documents/{ident}/draft")
def edit_doc(ident: str, payload: DraftInput, request: Request, db=Depends(get_db)):
    row = managed_document(db, ident)
    if row.revision != payload.revision or row.status in {
        "indexing",
        "deleted",
        "rejected",
    }:
        raise ApiError(409, "REVISION_CONFLICT")
    check_bases(db, [payload.knowledge_base_id])
    check_sensitive(payload.model_dump())
    if row.knowledge_base_id != payload.knowledge_base_id:
        raise ApiError(409, "KNOWLEDGE_BASE_IMMUTABLE")
    for k, v in payload.model_dump(exclude={"revision"}, exclude_unset=True).items():
        setattr(row, k, v)
    row.status = "draft"
    row.draft_edited = True
    cancel_jobs(db, ident, ["refine", "publish"])
    audit(db, request, "document.edit", ident)
    db.commit()
    return data(row)


@internal.post("/documents/{ident}/publish", status_code=202)
def publish(ident: str, payload: RevisionInput, request: Request, db=Depends(get_db)):
    row = managed_document(db, ident)
    if row.revision != payload.revision or row.status in {
        "indexing",
        "deleted",
        "rejected",
    }:
        raise ApiError(409, "REVISION_CONFLICT")
    check_bases(db, [row.knowledge_base_id])
    number = (
        db.scalar(
            select(func.max(DocumentVersion.version)).where(
                DocumentVersion.document_id == ident
            )
        )
        or 0
    ) + 1
    version = DocumentVersion(
        document_id=ident,
        version=number,
        title=row.title,
        content=row.content,
        tags=row.tags,
        environment=row.environment,
        software_names=row.software_names,
        index_version=index_id(),
        source_record_ids=[row.source_record_id] if row.source_record_id else [],
        context=row.context,
        reviewed_by=getattr(
            request.state,
            "knowledge_actor",
            request.headers.get("x-opsark-actor", "service"),
        )[:128],
    )
    db.add(version)
    cancel_jobs(db, ident, ["publish", "refine", "reindex"])
    row.status = "indexing"
    db.flush()
    job = enqueue_index(db, row, version, index_id())
    audit(db, request, "document.publish", ident)
    db.commit()
    return {"job_id": job.id, "document": data(row)}


@internal.post("/documents/batch-publish")
def batch_publish(payload: BatchPublishInput, request: Request, db=Depends(get_db)):
    # Each item commits independently. A stale item must neither publish an
    # unreviewed revision nor conceal the outcomes of the other selected items.
    results = []
    for item in payload.documents:
        try:
            row = managed_document(db, item.document_id)
            if row.status != "draft":
                raise ApiError(
                    409, "DOCUMENT_NOT_DRAFT", "仅可批量发布待审核草稿，请刷新状态"
                )
            result = publish(
                item.document_id, RevisionInput(revision=item.revision), request, db
            )
            results.append(
                {
                    "document_id": item.document_id,
                    "status": "queued",
                    "job_id": result["job_id"],
                }
            )
        except (ApiError, StaleDataError, SQLAlchemyError) as exc:
            db.rollback()
            if isinstance(exc, ApiError):
                code, message = exc.code, exc.message
            elif isinstance(exc, (StaleDataError, IntegrityError)):
                code, message = "REVISION_CONFLICT", "文档已变化，请重新审核后发布"
            else:
                code, message = "DATABASE_ERROR", "发布提交未确认，请刷新状态后重试"
            results.append(
                {
                    "document_id": item.document_id,
                    "status": "failed",
                    "error": {"code": code, "message": message},
                }
            )
    queued = sum(item["status"] == "queued" for item in results)
    return {"queued": queued, "failed": len(results) - queued, "results": results}


@internal.post("/documents/{ident}/unpublish")
def unpublish(ident: str, request: Request, db=Depends(get_db)):
    row = managed_document(db, ident)
    if row.status == "rejected":
        raise ApiError(409, "RECORD_REJECTED", "拒绝的草稿不能通过下架恢复")
    row.published_version, row.status = None, "draft"
    cancel_jobs(db, ident)
    audit(db, request, "document.unpublish", ident)
    db.commit()
    return data(row)


@internal.post("/documents/{ident}/reindex", status_code=202)
def reindex(ident: str, payload: RevisionInput, request: Request, db=Depends(get_db)):
    doc = managed_document(db, ident)
    if (
        doc.revision != payload.revision
        or doc.published_version is None
        or doc.status == "indexing"
    ):
        raise ApiError(409, "REVISION_CONFLICT")
    check_bases(db, [doc.knowledge_base_id])
    cancel_jobs(db, ident, ["reindex"])
    version = db.scalar(
        select(DocumentVersion).where(
            DocumentVersion.document_id == ident,
            DocumentVersion.version == doc.published_version,
        )
    )
    job = enqueue_index(db, doc, version, index_id(), "reindex")
    audit(db, request, "document.reindex", ident)
    db.commit()
    return {"job_id": job.id, "document": data(doc)}


@internal.get("/documents/{ident}/index-builds")
def index_builds(ident: str, db=Depends(get_db)):
    get(db, Document, ident)
    return [
        data(b)
        for b in db.scalars(
            select(IndexBuild)
            .join(DocumentVersion)
            .where(DocumentVersion.document_id == ident)
            .order_by(IndexBuild.created_at.desc())
            .limit(200)
        )
    ]


@internal.get("/jobs")
def jobs(
    offset: int = Query(0, ge=0, le=1000000),
    limit: int = Query(200, ge=1, le=200),
    db=Depends(get_db),
):
    return [
        data(x)
        for x in db.scalars(
            select(Job)
            .order_by(Job.created_at.desc(), Job.id)
            .offset(offset)
            .limit(limit)
        )
    ]


@internal.delete("/documents/{ident}")
def delete_document(ident: str, request: Request, db=Depends(get_db)):
    row = get(db, Document, ident)
    # Include archived pre-migration duplicates, without resurrecting hidden data.
    targets = [
        ident,
        *db.scalars(select(Document.id).where(Document.superseded_by == ident)),
    ]
    locked = list(
        db.scalars(
            select(Document)
            .where(Document.id.in_(targets))
            .order_by(Document.id)
            .with_for_update()
        )
    )
    for target in targets:
        cancel_jobs(db, target)
    for document in locked:
        source = (
            db.get(SourceRecord, document.source_record_id)
            if document.source_record_id
            else None
        )
        if source:
            source_ids = list(
                db.scalars(
                    select(SourceRecord.id).where(
                        SourceRecord.installation_id == source.installation_id,
                        SourceRecord.source_record_id == source.source_record_id,
                        SourceRecord.knowledge_base_id == document.knowledge_base_id,
                    )
                )
            )
            for source_id in source_ids:
                cancel_jobs(db, source_id, ["draft"])
    versions = select(DocumentVersion.id).where(
        DocumentVersion.document_id.in_(targets)
    )
    db.execute(delete(Chunk).where(Chunk.version_id.in_(versions)))
    db.execute(delete(IndexBuild).where(IndexBuild.version_id.in_(versions)))
    db.execute(delete(DocumentVersion).where(DocumentVersion.document_id.in_(targets)))
    db.execute(
        delete(RefinementComparison).where(
            RefinementComparison.document_id.in_(targets)
        )
    )
    db.execute(
        update(Job)
        .where(Job.target_id == ident)
        .values(status="cancelled", lease_token=None)
    )
    for row in locked:
        row.content, row.title, row.tags, row.software_names = "", "已删除", [], []
        row.published_version, row.status, row.environment, row.context = (
            None,
            "deleted",
            "",
            {},
        )
    audit(db, request, "document.delete", ident)
    db.commit()
    return {"deleted": True, "sources_retained": True}


@internal.delete("/records/{ident}")
def delete_record(ident: str, request: Request, db=Depends(get_db)):
    row = db.scalar(
        select(SourceRecord).where(SourceRecord.id == ident).with_for_update()
    )
    if row is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND")
    if db.scalar(
        select(Document.id).where(
            or_(
                Document.source_record_id == ident,
                Document.id.in_(
                    select(DocumentSource.document_id).where(
                        DocumentSource.source_record_id == ident
                    )
                ),
            ),
            Document.status != "deleted",
        )
    ):
        raise ApiError(409, "SOURCE_IN_USE", "请先删除关联知识文档")
    row.payload, row.status = {}, "deleted"
    db.execute(
        update(Job)
        .where(Job.target_id == ident)
        .values(status="cancelled", lease_token=None)
    )
    audit(db, request, "record.delete", ident)
    db.commit()
    return {"deleted": True, "deduplication_marker_retained": True}


@internal.post("/jobs/{ident}/retry")
def retry(ident: str, request: Request, db=Depends(get_db)):
    row = get(db, Job, ident)
    if row.status != "failed":
        raise ApiError(409, "JOB_NOT_FAILED")
    if row.kind == "refine":
        raise ApiError(
            409,
            "AI_REGENERATE_REQUIRED",
            "请在知识文档中点击 AI 重新提炼，基于当前草稿重试",
        )
    target = (
        managed_document(db, row.target_id)
        if row.kind in {"publish", "reindex"}
        else get(db, SourceRecord, row.target_id)
    )
    if row.kind == "draft":
        target = db.scalar(
            select(SourceRecord)
            .where(SourceRecord.id == row.target_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    row = db.scalar(
        select(Job)
        .where(Job.id == ident)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row.status != "failed":
        raise ApiError(409, "JOB_NOT_FAILED")
    if target.status in {"deleted", "rejected"}:
        raise ApiError(409, "RESOURCE_UNAVAILABLE")
    if row.kind in {"publish", "reindex"}:
        latest = db.scalar(
            select(Job.id)
            .where(Job.target_id == row.target_id, Job.kind == row.kind)
            .order_by(Job.created_at.desc(), Job.id)
            .limit(1)
        )
        if latest != row.id or (
            row.kind == "reindex" and target.published_version != row.revision
        ):
            raise ApiError(409, "JOB_SUPERSEDED", "请基于当前文档重新发布或重建")
        if row.kind == "publish":
            target.status = "indexing"
            db.flush()
        row.document_revision = target.revision
        build = get(db, IndexBuild, row.index_build_id)
        build.status, build.finished_at = "pending", None
    else:
        target.status = "received"
    row.status, row.attempts, row.error = "pending", 0, None
    row.available_at, row.finished_at, row.lease_token = 0, None, None
    audit(db, request, "job.retry", ident)
    db.commit()
    return data(row)


@internal.get("/audit-events")
def audits(db=Depends(get_db)):
    return [
        data(x)
        for x in db.scalars(select(Audit).order_by(Audit.created_at.desc()).limit(200))
    ]


@app.get("/api/v1/knowledge/bases")
def public_bases(key=Depends(require_key("knowledge:read")), db=Depends(get_db)):
    return [
        data(x)
        for x in db.scalars(
            select(KnowledgeBase).where(
                KnowledgeBase.id.in_(key.knowledge_base_ids),
                KnowledgeBase.enabled.is_(True),
            )
        )
    ]


def search_results(payload, db, key=None):
    from sqlalchemy.orm import load_only

    from .retrieval import filter_candidates

    check_bases(db, payload.knowledge_base_ids, key)
    key_id = key.id if key is not None else None

    def current_key():
        if key_id is None:
            return None
        current = db.scalar(
            select(ApiKey)
            .where(ApiKey.id == key_id)
            .execution_options(populate_existing=True)
        )
        if current is None or current.revoked or current.expires <= time.time():
            raise ApiError(401, "INVALID_API_KEY")
        if "knowledge:read" not in current.scopes:
            raise ApiError(403, "SCOPE_DENIED")
        if not set(payload.knowledge_base_ids).issubset(current.knowledge_base_ids):
            raise ApiError(403, "KNOWLEDGE_BASE_DENIED")
        return current

    statement = (
        select(Chunk, DocumentVersion, Document)
        .join(DocumentVersion, Chunk.version_id == DocumentVersion.id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(
            Document.knowledge_base_id.in_(payload.knowledge_base_ids),
            Document.published_version == DocumentVersion.version,
            Document.superseded_by.is_(None),
            Document.status.notin_(["deleted", "rejected"]),
            Chunk.build_id == DocumentVersion.active_build_id,
        )
    )
    words = query_terms(payload.query)
    exact_paths = sorted(set(re.findall(r"(?:/[A-Za-z0-9_.-]+)+", payload.query)))
    fingerprint = index_id()
    statement = filter_candidates(
        statement, payload.filters, exact_paths, db.bind.dialect.name
    )
    query_vector, mode = None, "keyword_only"
    embedding_state = "unavailable" if settings().embedding_enabled else "disabled"
    try:
        query_vector = embed([payload.query])[0]
        if query_vector is not None:
            mode = "hybrid"
            embedding_state = "ready"
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "retrieval_embedding_degraded error_type=%s", type(exc).__name__
        )
    # Finish external work before reading the current publication snapshot.
    db.expire_all()
    check_bases(db, payload.knowledge_base_ids, current_key())
    chunk_columns = [
        Chunk.id,
        Chunk.version_id,
        Chunk.build_id,
        Chunk.content,
        Chunk.line_start,
        Chunk.line_end,
    ]
    if query_vector is not None:
        chunk_columns.append(Chunk.embedding)
    # Candidate ranking needs chunk bodies, not another full document/draft per
    # chunk. Avoid transferring repeated content and unused vectors from SQL.
    statement = statement.options(
        load_only(*chunk_columns),
        load_only(
            DocumentVersion.id,
            DocumentVersion.version,
            DocumentVersion.title,
            DocumentVersion.active_build_id,
            DocumentVersion.index_version,
            DocumentVersion.source_record_ids,
        ),
        load_only(Document.id, Document.knowledge_base_id),
    )
    candidate_limited = False
    if db.bind.dialect.name == "postgresql":
        rows, candidate_limited = postgres_candidates(
            db, statement, words, query_vector, fingerprint
        )
    else:
        rows = db.execute(statement.limit(50001)).all()
        if len(rows) > 50000:
            raise ApiError(503, "SEARCH_CAPACITY_EXCEEDED", "请缩小检索库范围")
    build_configs = (
        dict(
            db.execute(
                select(IndexBuild.id, IndexBuild.index_version).where(
                    IndexBuild.id.in_(
                        {version.active_build_id for _, version, _ in rows}
                    )
                )
            ).all()
        )
        if query_vector is not None
        else {}
    )
    lexical, semantic = [], []
    eligible_vectors, considered = 0, 0
    for chunk, version, doc in rows:
        considered += 1
        score = keyword_rank(payload.query, words, version.title, chunk.content)
        if score:
            lexical.append((score, chunk.id))
        if (
            query_vector is not None
            and build_configs.get(version.active_build_id, version.index_version)
            == fingerprint
            and chunk.embedding is not None
            and len(chunk.embedding) == len(query_vector)
        ):
            eligible_vectors += 1
            v = list(chunk.embedding)
            denom = math.sqrt(sum(x * x for x in v) * sum(x * x for x in query_vector))
            cosine = sum(x * y for x, y in zip(v, query_vector)) / denom if denom else 0
            if cosine > 0.25:
                semantic.append((cosine, chunk.id))
    scores = {}
    for channel in (lexical, semantic):
        for rank, (_, ident) in enumerate(sorted(channel, reverse=True)[:100], 1):
            scores[ident] = scores.get(ident, 0) + 1 / (60 + rank)
    lookup = {chunk.id: (chunk, version, doc) for chunk, version, doc in rows}
    hits, remaining = [], payload.max_content_chars
    ordered = sorted(scores, key=lambda ident: (-scores[ident], ident))
    was_truncated = False
    for ident in ordered:
        if len(hits) >= payload.top_k:
            break
        chunk, version, doc = lookup[ident]
        if not snapshot_still_public(
            db, doc.id, version.id, chunk.build_id, payload.knowledge_base_ids
        ):
            continue
        if remaining <= 0:
            break
        snippet = chunk.content[:remaining]
        was_truncated = was_truncated or len(snippet) < len(chunk.content)
        remaining -= len(snippet)
        hits.append(
            {
                "chunk_id": ident,
                "document_id": doc.id,
                "document_version": version.version,
                "knowledge_base_id": doc.knowledge_base_id,
                "title": version.title,
                "content": snippet,
                "rank": len(hits) + 1,
                "citation": {
                    "label": f"K{len(hits) + 1}",
                    "line_start": chunk.line_start,
                    "line_end": min(
                        chunk.line_end, chunk.line_start + snippet.count("\n")
                    ),
                    "source_record_ids": version.source_record_ids,
                    "document_url": f"/api/v1/knowledge/documents/{doc.id}/versions/{version.version}",
                },
            }
        )
    warnings = []
    if candidate_limited:
        warnings.append("候选结果达到检索预算，请增加关键词缩小范围")
    if query_vector is None and embedding_state == "unavailable":
        warnings.append(
            "向量服务暂时不可用，已回退到关键词检索；已有关键词结果仍可使用"
        )
    elif eligible_vectors == 0:
        if query_vector is not None:
            mode = "keyword_only"
            embedding_state = "index_missing"
            warnings.append(
                "当前候选无匹配索引配置的向量，使用关键词检索；请检查发布索引"
            )
    elif eligible_vectors < considered:
        warnings.append("部分候选缺少匹配的向量索引，混合检索覆盖不完整")
    # Revocation/expiry during ranking also wins over a response prepared from an
    # earlier snapshot; knowledge snippets never grant continued key validity.
    current_key()
    corpus_counts = db.execute(
        select(
            func.count(Document.id),
            func.coalesce(func.sum(case((Document.status == "draft", 1), else_=0)), 0),
            func.coalesce(
                func.sum(case((Document.status == "indexing", 1), else_=0)), 0
            ),
            func.coalesce(
                func.sum(case((Document.published_version.is_not(None), 1), else_=0)), 0
            ),
        ).where(
            Document.knowledge_base_id.in_(payload.knowledge_base_ids),
            Document.superseded_by.is_(None),
            Document.status.notin_(["deleted", "rejected"]),
        )
    ).one()
    corpus = dict(zip(("documents", "drafts", "indexing", "published"), corpus_counts))
    corpus["searchable"] = db.scalar(
        select(func.count(Document.id))
        .join(
            DocumentVersion,
            (DocumentVersion.document_id == Document.id)
            & (DocumentVersion.version == Document.published_version),
        )
        .join(
            IndexBuild,
            (IndexBuild.id == DocumentVersion.active_build_id)
            & (IndexBuild.version_id == DocumentVersion.id),
        )
        .where(
            Document.knowledge_base_id.in_(payload.knowledge_base_ids),
            Document.superseded_by.is_(None),
            Document.status.notin_(["deleted", "rejected"]),
            IndexBuild.status == "ready",
            IndexBuild.chunk_count > 0,
        )
    )
    check_bases(db, payload.knowledge_base_ids, current_key())
    return {
        "retrieval_mode": mode,
        "embedding_status": embedding_state,
        "corpus": corpus,
        "index_version": fingerprint,
        "hits": hits,
        "truncated": was_truncated
        or (remaining <= 0 and len(hits) < min(len(ordered), payload.top_k)),
        "warnings": warnings,
    }


@app.post("/api/v1/knowledge/search")
def search(
    payload: SearchInput, key=Depends(require_key("knowledge:read")), db=Depends(get_db)
):
    return search_results(payload, db, key)


@internal.post("/search")
def admin_search(payload: SearchInput, db=Depends(get_db)):
    return search_results(payload, db)


@app.get("/api/v1/knowledge/documents/{ident}/versions/{version}")
def citation(
    ident: str,
    version: int,
    key=Depends(require_key("knowledge:read")),
    db=Depends(get_db),
):
    doc = get(db, Document, ident)
    if doc.knowledge_base_id not in key.knowledge_base_ids:
        raise ApiError(404, "RESOURCE_NOT_FOUND")
    check_bases(db, [doc.knowledge_base_id], key)
    if doc.published_version != version:
        raise ApiError(410, "DOCUMENT_VERSION_UNAVAILABLE")
    if doc.superseded_by or doc.status in {"deleted", "rejected"}:
        raise ApiError(410, "DOCUMENT_VERSION_UNAVAILABLE")
    row = db.scalar(
        select(DocumentVersion)
        .join(Document, Document.id == DocumentVersion.document_id)
        .join(KnowledgeBase, KnowledgeBase.id == Document.knowledge_base_id)
        .where(
            Document.id == ident,
            DocumentVersion.version == version,
            Document.published_version == DocumentVersion.version,
            Document.superseded_by.is_(None),
            Document.status.notin_(["deleted", "rejected"]),
            KnowledgeBase.enabled.is_(True),
        )
    )
    if row is None:
        raise ApiError(410, "DOCUMENT_VERSION_UNAVAILABLE")
    # Public citations do not expose reviewer/worker/internal build metadata.
    return {
        **{
            k: v
            for k, v in data(row).items()
            if k not in {"reviewed_by", "active_build_id"}
        },
        "knowledge_base_id": doc.knowledge_base_id,
    }


app.include_router(internal)

# Separate local management surface; no service Token is exposed to the browser.
from .admin import require_admin
from .admin import router as admin_router  # noqa: E402

management = APIRouter(
    prefix="/api/admin/v1/knowledge", dependencies=[Depends(require_admin)]
)
for route in internal.routes:
    management.add_api_route(
        route.path.removeprefix("/internal/v1"),
        route.endpoint,
        methods=list(route.methods),
        status_code=route.status_code,
        response_model=route.response_model,
    )
app.include_router(admin_router)
app.include_router(management)
web = Path(__file__).resolve().parent.parent / "web" / "dist"
if web.is_dir():
    app.mount("/", StaticFiles(directory=web, html=True), name="knowledge-web")
