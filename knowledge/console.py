"""Read-only, administrator-only console projections; no inferred human owners."""

import time

from sqlalchemy import func, select

from .models import (
    Audit,
    Document,
    DocumentVersion,
    IndexBuild,
    Job,
    KnowledgeAdmin,
    KnowledgeBase,
    SourceRecord,
    WorkerHeartbeat,
)


def document_metadata(db, rows):
    if not rows:
        return {}
    bases = {
        row.id: row
        for row in db.scalars(
            select(KnowledgeBase).where(
                KnowledgeBase.id.in_({row.knowledge_base_id for row in rows})
            )
        )
    }
    sources = {
        row.id: row
        for row in db.scalars(
            select(SourceRecord).where(
                SourceRecord.id.in_(
                    {row.source_record_id for row in rows if row.source_record_id}
                )
            )
        )
    }
    creations = {}
    for event, username in db.execute(
        select(Audit, KnowledgeAdmin.username)
        .outerjoin(KnowledgeAdmin, KnowledgeAdmin.id == Audit.actor)
        .where(
            Audit.action == "document.create",
            Audit.target.in_({row.id for row in rows}),
        )
        .order_by(Audit.created_at, Audit.id)
    ):
        creations.setdefault(event.target, (event, username))
    result = {}
    for row in rows:
        base, source = (
            bases.get(row.knowledge_base_id),
            sources.get(row.source_record_id),
        )
        item = {
            "knowledge_base_name": base.name if base else "知识库不可用",
            "knowledge_base_enabled": bool(base and base.enabled),
            "source_kind": "manual",
            "created_by": "历史记录未留存",
            "created_by_kind": "unknown",
        }
        if source:
            item.update(
                source_kind="core_record",
                created_by=f"Core 客户端 · {source.installation_id}",
                created_by_kind="client",
                source_title=source.payload.get("title", ""),
                source_revision=source.source_revision,
                source_external_id=source.source_record_id,
                source_installation_id=source.installation_id,
            )
        else:
            if "参考资料" in row.tags and any(
                tag.startswith("ops-v1:") for tag in row.tags
            ):
                item["source_kind"] = "reference_pack"
            if row.id in creations:
                event, username = creations[row.id]
                item["created_by"] = username or f"服务集成 · {event.actor}"
                item["created_by_kind"] = "admin" if username else "service"
        result[row.id] = item
    return result


def overview_data(db, offset=0, limit=200):
    canonical = (Document.superseded_by.is_(None), Document.status != "deleted")
    grouped = list(
        db.execute(
            select(Document.knowledge_base_id, Document.status, func.count(Document.id))
            .where(*canonical)
            .group_by(Document.knowledge_base_id, Document.status)
        )
    )
    public_counts = dict(
        db.execute(
            select(Document.knowledge_base_id, func.count(Document.id))
            .where(
                *canonical,
                Document.status != "rejected",
                Document.published_version.is_not(None),
            )
            .group_by(Document.knowledge_base_id)
        ).all()
    )
    searchable_counts = dict(
        db.execute(
            select(Document.knowledge_base_id, func.count(Document.id))
            .join(KnowledgeBase, KnowledgeBase.id == Document.knowledge_base_id)
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
                *canonical,
                Document.status != "rejected",
                KnowledgeBase.enabled.is_(True),
                IndexBuild.status == "ready",
                IndexBuild.chunk_count > 0,
            )
            .group_by(Document.knowledge_base_id)
        ).all()
    )
    totals = {
        "documents": 0,
        "drafts": 0,
        "indexing": 0,
        "published": sum(public_counts.values()),
        "searchable": sum(searchable_counts.values()),
    }
    by_base = {}
    for base_id, status, count in grouped:
        counts = by_base.setdefault(
            base_id, {"documents": 0, "drafts": 0, "indexing": 0}
        )
        counts["documents"] += count
        totals["documents"] += count
        key = {"draft": "drafts", "indexing": "indexing"}.get(status)
        if key:
            counts[key] += count
            totals[key] += count
    totals["knowledge_bases"] = db.scalar(
        select(func.count()).select_from(KnowledgeBase)
    )
    totals["pending_sources"] = db.scalar(
        select(func.count())
        .select_from(SourceRecord)
        .where(SourceRecord.status == "ready_for_review")
    )
    jobs = dict(db.execute(select(Job.status, func.count()).group_by(Job.status)).all())
    heartbeat = db.scalar(select(func.max(WorkerHeartbeat.last_seen)))
    return {
        "totals": totals,
        "bases": [
            {
                "id": base.id,
                "name": base.name,
                "description": base.description,
                "enabled": base.enabled,
                **by_base.get(base.id, {"documents": 0, "drafts": 0, "indexing": 0}),
                "published": public_counts.get(base.id, 0),
                "searchable": searchable_counts.get(base.id, 0),
            }
            for base in db.scalars(
                select(KnowledgeBase)
                .order_by(KnowledgeBase.created_at.desc(), KnowledgeBase.id)
                .offset(offset)
                .limit(limit)
            )
        ],
        "worker": {
            "healthy": heartbeat is not None and time.time() - heartbeat < 180,
            "pending": jobs.get("pending", 0),
            "running": jobs.get("running", 0),
            "failed": jobs.get("failed", 0),
        },
        "generated_at": time.time(),
    }
