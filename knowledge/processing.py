import hashlib
import math
import time
import httpx
from sqlalchemy import select, update, or_
from .config import settings
from .db import SessionLocal
from .models import Chunk, Document, DocumentVersion, Job, SourceRecord, new_id


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
    result, buffer, start = [], [], 1
    for line_no, line in enumerate(content.splitlines(), 1):
        if buffer and sum(len(x) for x in buffer) + len(line) > 1800:
            result.append(("\n".join(buffer), start, line_no - 1))
            buffer, start = [], line_no
        if len(line) > 1800:
            for offset in range(0, len(line), 1800):
                result.append((line[offset : offset + 1800], line_no, line_no))
            start = line_no + 1
        else:
            buffer.append(line)
    if buffer:
        result.append(("\n".join(buffer), start, len(content.splitlines())))
    return result


def draft(record):
    p = record.payload
    sections = [f"# {p['title']}", "## 问题", p["problem"], "## 操作与证据"]
    for step in p["steps"]:
        sections += [
            f"### {step['description']}",
            f"执行：{step['execution_status']}；验证：{step['validation_status']}",
        ]
        if step["command"]:
            sections += ["```sh", step["command"], "```"]
        sections += [e["summary"] + "\n" + e["excerpt"] for e in step["evidence"]]
    sections += [
        "## 结果",
        p["outcome"]["status"],
        p["outcome"]["summary"],
        "## 来源",
        record.id,
    ]
    return Document(
        knowledge_base_id=record.knowledge_base_id,
        source_record_id=record.id,
        title=p["title"],
        content="\n\n".join(sections),
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
            if target_row and target_row.status not in {"deleted", "rejected"}:
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
        prepared = []
        if kind == "publish":
            with factory() as db:
                version = db.scalar(
                    select(DocumentVersion).where(
                        DocumentVersion.document_id == target,
                        DocumentVersion.version == revision,
                    )
                )
                content, version_id = version.content, version.id
            parts = chunks(content)
            for offset in range(0, len(parts), 8):
                batch = parts[offset : offset + 8]
                vectors = embed([x[0] for x in batch])
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
                    if not db.scalar(
                        select(Document.id).where(Document.source_record_id == target)
                    ):
                        db.add(draft(record))
                    record.status = "ready_for_review"
            else:
                doc = db.get(Document, target)
                if doc.status != "indexing":
                    job.status = "cancelled"
                    db.commit()
                    return True
                for (text, start, end), vector in prepared:
                    db.add(
                        Chunk(
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
    except Exception:
        with factory() as db:
            job = db.get(Job, job_id)
            if job.lease_token == lease and job.status == "running":
                job.status = "failed" if job.attempts >= 3 else "pending"
                job.error = "PROCESSING_FAILED"  # never persist provider exception bodies / keys
                if job.status == "failed":
                    obj = db.get(SourceRecord if kind == "draft" else Document, target)
                    obj.status = "failed"
                db.commit()
    return True


if __name__ == "__main__":
    while True:
        run_once()
        time.sleep(1)
