import math
import re
import secrets
import time
import uuid
from pathlib import Path
import jieba
from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, update, inspect, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError
from .db import get_db
from .models import (
    ApiKey,
    Audit,
    Chunk,
    Document,
    DocumentVersion,
    Idempotency,
    Job,
    KnowledgeBase,
    SourceRecord,
)
from .schemas import (
    BaseInput,
    DocumentInput,
    DraftInput,
    KeyInput,
    RecordInput,
    RevisionInput,
    SearchInput,
)
from .security import (
    ApiError,
    check_bases,
    check_sensitive,
    digest,
    require_key,
    service_auth,
)
from .processing import embed, index_id

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
    return {"status": "ok", "index_version": index_id()}


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


@internal.get("/documents")
def documents(
    offset: int = Query(0, ge=0, le=1000000),
    limit: int = Query(200, ge=1, le=200),
    db=Depends(get_db),
):
    return [
        data(x)
        for x in db.scalars(
            select(Document)
            .where(Document.status != "deleted")
            .order_by(Document.updated_at.desc(), Document.id)
            .offset(offset)
            .limit(limit)
        )
    ]


@internal.post("/documents", status_code=201)
def create_doc(payload: DocumentInput, request: Request, db=Depends(get_db)):
    check_bases(db, [payload.knowledge_base_id])
    check_sensitive(payload.model_dump())
    row = Document(**payload.model_dump())
    db.add(row)
    db.flush()
    audit(db, request, "document.create", row.id)
    db.commit()
    return data(row)


@internal.post("/records/{ident}/reject")
def reject_record(ident: str, request: Request, db=Depends(get_db)):
    row = get(db, SourceRecord, ident)
    if row.status not in {"received", "ready_for_review", "failed"}:
        raise ApiError(409, "RECORD_NOT_REVIEWABLE")
    doc = db.scalar(select(Document).where(Document.source_record_id == ident))
    if doc and (doc.published_version is not None or doc.status == "indexing"):
        raise ApiError(409, "SOURCE_IN_USE", "已发布或正在发布的来源不能直接拒绝")
    row.status = "rejected"
    if doc:
        doc.status = "rejected"
    targets = [ident] + ([doc.id] if doc else [])
    db.execute(
        update(Job)
        .where(
            Job.target_id.in_(targets), Job.status.in_(["pending", "running", "failed"])
        )
        .values(status="cancelled", lease_token=None)
    )
    audit(db, request, "record.reject", ident)
    db.commit()
    return {"record_id": row.id, "status": row.status}


@internal.patch("/documents/{ident}/draft")
def edit_doc(ident: str, payload: DraftInput, request: Request, db=Depends(get_db)):
    row = get(db, Document, ident)
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
    for k, v in payload.model_dump(exclude={"revision"}).items():
        setattr(row, k, v)
    row.status = "draft"
    audit(db, request, "document.edit", ident)
    db.commit()
    return data(row)


@internal.post("/documents/{ident}/publish", status_code=202)
def publish(ident: str, payload: RevisionInput, request: Request, db=Depends(get_db)):
    row = get(db, Document, ident)
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
    db.add(
        DocumentVersion(
            document_id=ident,
            version=number,
            title=row.title,
            content=row.content,
            tags=row.tags,
            environment=row.environment,
            software_names=row.software_names,
            index_version=index_id(),
        )
    )
    row.status = "indexing"
    job = Job(kind="publish", target_id=ident, revision=number)
    db.add(job)
    db.flush()
    audit(db, request, "document.publish", ident)
    db.commit()
    return {"job_id": job.id, "document": data(row)}


@internal.post("/documents/{ident}/unpublish")
def unpublish(ident: str, request: Request, db=Depends(get_db)):
    row = get(db, Document, ident)
    if row.status == "rejected":
        raise ApiError(409, "RECORD_REJECTED", "拒绝的草稿不能通过下架恢复")
    row.published_version, row.status = None, "draft"
    db.execute(
        update(Job)
        .where(Job.target_id == ident, Job.status.in_(["pending", "running"]))
        .values(status="cancelled", lease_token=None)
    )
    audit(db, request, "document.unpublish", ident)
    db.commit()
    return data(row)


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
    versions = select(DocumentVersion.id).where(DocumentVersion.document_id == ident)
    db.execute(delete(Chunk).where(Chunk.version_id.in_(versions)))
    db.execute(delete(DocumentVersion).where(DocumentVersion.document_id == ident))
    db.execute(
        update(Job)
        .where(Job.target_id == ident)
        .values(status="cancelled", lease_token=None)
    )
    row.content, row.title, row.tags, row.software_names = "", "已删除", [], []
    row.published_version, row.status, row.environment = None, "deleted", ""
    audit(db, request, "document.delete", ident)
    db.commit()
    return {"deleted": True}


@internal.delete("/records/{ident}")
def delete_record(ident: str, request: Request, db=Depends(get_db)):
    row = get(db, SourceRecord, ident)
    if db.scalar(
        select(Document.id).where(
            Document.source_record_id == ident, Document.status != "deleted"
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
    row.status, row.attempts, row.error = "pending", 0, None
    target = get(db, Document if row.kind == "publish" else SourceRecord, row.target_id)
    target.status = "indexing" if row.kind == "publish" else "received"
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
    check_bases(db, payload.knowledge_base_ids, key)
    statement = (
        select(Chunk, DocumentVersion, Document)
        .join(DocumentVersion, Chunk.version_id == DocumentVersion.id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(
            Document.knowledge_base_id.in_(payload.knowledge_base_ids),
            Document.published_version == DocumentVersion.version,
        )
    )
    if payload.filters.environment:
        statement = statement.where(
            DocumentVersion.environment == payload.filters.environment
        )
    rows = db.execute(statement.limit(50001)).all()
    if len(rows) > 50000:
        raise ApiError(503, "SEARCH_CAPACITY_EXCEEDED", "请缩小检索库范围")
    words = set(x.lower() for x in jieba.cut_for_search(payload.query) if x.strip())
    query_vector, mode = None, "keyword_only"
    try:
        query_vector = embed([payload.query])[0]
        if query_vector is not None:
            mode = "hybrid"
    except Exception:
        pass
    lexical, semantic = [], []
    eligible_vectors, considered = 0, 0
    for chunk, version, doc in rows:
        if payload.filters.software_names and not set(
            payload.filters.software_names
        ).issubset(version.software_names):
            continue
        original = version.title + " " + chunk.content
        exact_paths = re.findall(r"(?:/[A-Za-z0-9_.-]+)+", payload.query)
        if exact_paths and not all(path in original for path in exact_paths):
            continue
        considered += 1
        corpus = original.lower()
        score = sum(len(w) for w in words if w in corpus)
        if score:
            lexical.append((score, chunk.id))
        if (
            query_vector is not None
            and version.index_version == index_id()
            and chunk.embedding is not None
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
    for ident in sorted(scores, key=scores.get, reverse=True)[: payload.top_k]:
        chunk, version, doc = lookup[ident]
        if remaining <= 0:
            break
        snippet = chunk.content[:remaining]
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
                    "line_end": chunk.line_end,
                    "source_record_ids": [doc.source_record_id]
                    if doc.source_record_id
                    else [],
                    "document_url": f"/api/v1/knowledge/documents/{doc.id}/versions/{version.version}",
                },
            }
        )
    warnings = []
    if query_vector is None:
        warnings.append("未启用或无法访问 Embedding，当前使用关键词检索")
    elif eligible_vectors == 0:
        mode = "keyword_only"
        warnings.append("当前候选无匹配索引配置的向量，使用关键词检索；请检查发布索引")
    elif eligible_vectors < considered:
        warnings.append("部分候选缺少匹配的向量索引，混合检索覆盖不完整")
    return {
        "retrieval_mode": mode,
        "index_version": index_id(),
        "hits": hits,
        "truncated": remaining <= 0,
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
    return data(
        db.scalar(
            select(DocumentVersion).where(
                DocumentVersion.document_id == ident, DocumentVersion.version == version
            )
        )
    )


app.include_router(internal)

# Separate local management surface; no service Token is exposed to the browser.
from .admin import router as admin_router, require_admin  # noqa: E402

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
