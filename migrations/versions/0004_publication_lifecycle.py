"""Stable source lineage, immutable provenance, and independent index builds.

Run with API/Worker stopped. Legacy duplicate documents are retained as history;
only the newest already-published document remains publicly active per lineage.
"""

import hashlib
import json
import uuid

import sqlalchemy as sa
from alembic import op

revision = "knowledge_0004"
down_revision = "knowledge_0003"


def upgrade():
    bind = op.get_bind()
    op.add_column("documents", sa.Column("logical_source_key", sa.String(64)))
    op.create_index(
        "uq_documents_logical_source_key",
        "documents",
        ["logical_source_key"],
        unique=True,
    )
    op.add_column("documents", sa.Column("superseded_by", sa.String(64)))
    op.create_index("ix_documents_superseded_by", "documents", ["superseded_by"])
    op.add_column(
        "documents",
        sa.Column(
            "draft_edited", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "documents",
        sa.Column("context", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.create_table(
        "document_sources",
        sa.Column(
            "source_record_id",
            sa.String(64),
            sa.ForeignKey("source_records.id"),
            primary_key=True,
        ),
        sa.Column(
            "document_id", sa.String(64), sa.ForeignKey("documents.id"), nullable=False
        ),
    )
    op.create_index(
        "ix_document_sources_document_id", "document_sources", ["document_id"]
    )
    op.add_column(
        "document_versions",
        sa.Column("source_record_ids", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "document_versions",
        sa.Column("context", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.add_column(
        "document_versions",
        sa.Column("reviewed_by", sa.String(128), nullable=False, server_default=""),
    )
    op.add_column("document_versions", sa.Column("active_build_id", sa.String(64)))
    op.create_table(
        "index_builds",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "version_id",
            sa.String(64),
            sa.ForeignKey("document_versions.id"),
            nullable=False,
        ),
        sa.Column("index_version", sa.String(200), nullable=False),
        sa.Column("config_snapshot", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("finished_at", sa.Float()),
    )
    op.create_index("ix_index_builds_version_id", "index_builds", ["version_id"])
    # The build owns dimension/configuration. A generic vector column permits
    # rebuilding into another dimension without changing or dropping old vectors.
    op.add_column("chunks", sa.Column("build_id", sa.String(64)))
    op.create_index("ix_chunks_build_id", "chunks", ["build_id"])
    with op.batch_alter_table("chunks") as batch:
        batch.create_foreign_key(
            "fk_chunks_build_id", "index_builds", ["build_id"], ["id"]
        )
    if bind.dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE chunks ALTER COLUMN embedding TYPE vector USING embedding::vector"
        )
    for column in [
        sa.Column("document_revision", sa.Integer()),
        sa.Column("index_build_id", sa.String(64)),
        sa.Column("available_at", sa.Float(), nullable=False, server_default="0"),
        sa.Column("finished_at", sa.Float()),
    ]:
        op.add_column("jobs", column)
    op.create_index("ix_jobs_available_at", "jobs", ["available_at"])
    op.create_table(
        "worker_heartbeats",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("last_seen", sa.Float(), nullable=False),
    )
    metadata = sa.MetaData()
    metadata.reflect(bind=bind)
    docs, sources, links, versions, builds, chunks, jobs = (
        metadata.tables[name]
        for name in (
            "documents",
            "source_records",
            "document_sources",
            "document_versions",
            "index_builds",
            "chunks",
            "jobs",
        )
    )
    source_map = {
        row.id: dict(row) for row in bind.execute(sa.select(sources)).mappings()
    }
    document_rows = list(bind.execute(sa.select(docs)).mappings())
    groups = {}
    for row in document_rows:
        source = source_map.get(row.source_record_id)
        if source:
            key = (
                source["installation_id"],
                row.knowledge_base_id,
                source["source_record_id"],
            )
            groups.setdefault(key, []).append(row)
            context = source["payload"].get("context", {})
            bind.execute(
                docs.update().where(docs.c.id == row.id).values(context=context)
            )
        # Existing manual edits cannot be distinguished from generated drafts.
        if row.status == "draft":
            bind.execute(
                docs.update().where(docs.c.id == row.id).values(draft_edited=True)
            )
    for key, rows in groups.items():
        live = [r for r in rows if r.status not in {"deleted", "rejected"}]
        published = [r for r in live if r.published_version is not None]
        canonical = max(
            published or live or rows,
            key=lambda r: (
                source_map[r.source_record_id]["source_revision"],
                r.updated_at,
                r.id,
            ),
        )
        logical = hashlib.sha256(
            json.dumps(key, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
        bind.execute(
            docs.update()
            .where(docs.c.id == canonical.id)
            .values(logical_source_key=logical)
        )
        for row in rows:
            if row.id != canonical.id:
                bind.execute(
                    docs.update()
                    .where(docs.c.id == row.id)
                    .values(superseded_by=canonical.id, source_record_id=None)
                )
                bind.execute(
                    jobs.update()
                    .where(
                        jobs.c.target_id == row.id,
                        jobs.c.status.in_(["pending", "running", "failed"]),
                    )
                    .values(status="cancelled", lease_token=None)
                )
        # Include older source records whose document link was replaced in v0.3.
        for source in source_map.values():
            if (
                source["installation_id"],
                source["knowledge_base_id"],
                source["source_record_id"],
            ) == key:
                bind.execute(
                    links.insert().values(
                        source_record_id=source["id"], document_id=canonical.id
                    )
                )
    doc_map = {row.id: row for row in document_rows}
    for version in bind.execute(sa.select(versions)).mappings():
        doc = doc_map[version.document_id]
        source = source_map.get(doc.source_record_id)
        # A source may have changed after unpublishing in v0.3. Only the preserved
        # body's explicit source ID can establish historical provenance; never
        # pretend the latest source was the basis of all older versions.
        proven = [
            candidate
            for candidate in source_map.values()
            if source
            and candidate["installation_id"] == source["installation_id"]
            and candidate["knowledge_base_id"] == doc.knowledge_base_id
            and candidate["source_record_id"] == source["source_record_id"]
            and candidate["created_at"] <= version.created_at
            and candidate["id"] in version.content
        ]
        bind.execute(
            versions.update()
            .where(versions.c.id == version.id)
            .values(
                source_record_ids=[candidate["id"] for candidate in proven],
                context=proven[0]["payload"].get("context", {})
                if len(proven) == 1
                else {},
            )
        )
        count = bind.scalar(
            sa.select(sa.func.count())
            .select_from(chunks)
            .where(chunks.c.version_id == version.id)
        )
        build_id = uuid.uuid4().hex
        bind.execute(
            builds.insert().values(
                id=build_id,
                version_id=version.id,
                index_version=version.index_version,
                config_snapshot={"legacy": True},
                status="ready" if count else "pending",
                chunk_count=count,
                created_at=version.created_at,
                finished_at=version.created_at if count else None,
            )
        )
        bind.execute(
            chunks.update()
            .where(chunks.c.version_id == version.id)
            .values(build_id=build_id)
        )
        if count:
            bind.execute(
                versions.update()
                .where(versions.c.id == version.id)
                .values(active_build_id=build_id)
            )
        bind.execute(
            jobs.update()
            .where(
                jobs.c.target_id == doc.id,
                jobs.c.kind == "publish",
                jobs.c.revision == version.version,
            )
            .values(index_build_id=build_id, document_revision=doc.revision)
        )


def downgrade():
    raise RuntimeError(
        "Restore a verified backup; this migration preserves source and publication history."
    )
