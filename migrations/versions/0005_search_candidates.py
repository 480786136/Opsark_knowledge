"""Pre-tokenized searchable text and PostgreSQL GIN candidate index."""

import jieba
import sqlalchemy as sa
from alembic import op

revision = "knowledge_0005"
down_revision = "knowledge_0004"


def upgrade():
    op.add_column(
        "chunks", sa.Column("search_text", sa.Text(), nullable=False, server_default="")
    )
    bind = op.get_bind()
    chunks = sa.table(
        "chunks",
        sa.column("id"),
        sa.column("version_id"),
        sa.column("content"),
        sa.column("search_text"),
    )
    versions = sa.table("document_versions", sa.column("id"), sa.column("title"))
    last = ""
    while True:
        rows = bind.execute(
            sa.select(chunks.c.id, chunks.c.content, versions.c.title)
            .join(versions, chunks.c.version_id == versions.c.id)
            .where(chunks.c.id > last)
            .order_by(chunks.c.id)
            .limit(200)
        ).all()
        if not rows:
            break
        for row in rows:
            # Frozen tokenization input; no import of the current retrieval module.
            tokenized = " ".join(jieba.cut_for_search(row.title + "\n" + row.content))
            bind.execute(
                chunks.update()
                .where(chunks.c.id == row.id)
                .values(search_text=tokenized)
            )
        last = rows[-1].id
    if bind.dialect.name == "postgresql":
        op.execute(
            "CREATE INDEX ix_chunks_search_gin ON chunks USING gin (to_tsvector('simple', search_text))"
        )


def downgrade():
    raise RuntimeError("Restore a verified backup instead of removing search data.")
