"""Frozen original business schema, independent of current ORM and settings."""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "knowledge_0001"
down_revision = None


def upgrade():
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    # Keep this definition immutable. Runtime model changes require new migrations.
    metadata = sa.MetaData()

    def table(name, columns, *constraints):
        return sa.Table(name, metadata, *columns, *constraints)

    def col(name, type_, nullable=False, **kwargs):
        return sa.Column(name, type_, nullable=nullable, **kwargs)

    def ident():
        return col("id", sa.String(64), primary_key=True)

    def ref(name, target, nullable=False, **kwargs):
        return sa.Column(
            name, sa.String(64), sa.ForeignKey(target), nullable=nullable, **kwargs
        )

    table(
        "knowledge_bases",
        [
            ident(),
            col("name", sa.String(200)),
            col("description", sa.Text),
            col("enabled", sa.Boolean),
            col("created_at", sa.Float),
        ],
    )
    table(
        "knowledge_keys",
        [
            ident(),
            col("name", sa.String(100)),
            col("installation_id", sa.String(128), index=True),
            col("token_hash", sa.String(64), unique=True),
            col("prefix", sa.String(20)),
            col("scopes", sa.JSON),
            col("knowledge_base_ids", sa.JSON),
            col("revoked", sa.Boolean),
            col("expires", sa.Float),
            col("created_at", sa.Float),
        ],
    )
    table(
        "source_records",
        [
            ident(),
            col("installation_id", sa.String(128)),
            col("source_record_id", sa.String(128)),
            col("source_revision", sa.Integer),
            ref("knowledge_base_id", "knowledge_bases.id"),
            col("body_hash", sa.String(64)),
            col("payload", sa.JSON),
            col("status", sa.String(30)),
            col("created_at", sa.Float),
        ],
        sa.UniqueConstraint("installation_id", "source_record_id", "source_revision"),
    )
    table(
        "idempotency",
        [
            ident(),
            col("installation_id", sa.String(128)),
            col("key", sa.String(128)),
            col("body_hash", sa.String(64)),
            ref("record_id", "source_records.id"),
        ],
        sa.UniqueConstraint("installation_id", "key"),
    )
    table(
        "documents",
        [
            ident(),
            ref("knowledge_base_id", "knowledge_bases.id"),
            ref("source_record_id", "source_records.id", nullable=True, unique=True),
            col("title", sa.String(200)),
            col("content", sa.Text),
            col("tags", sa.JSON),
            col("environment", sa.String(100)),
            col("software_names", sa.JSON),
            col("revision", sa.Integer),
            col("published_version", sa.Integer, nullable=True),
            col("status", sa.String(30)),
            col("updated_at", sa.Float),
        ],
    )
    table(
        "document_versions",
        [
            ident(),
            ref("document_id", "documents.id"),
            col("version", sa.Integer),
            col("title", sa.String(200)),
            col("content", sa.Text),
            col("tags", sa.JSON),
            col("environment", sa.String(100)),
            col("software_names", sa.JSON),
            col("index_version", sa.String(200)),
            col("created_at", sa.Float),
        ],
        sa.UniqueConstraint("document_id", "version"),
    )
    table(
        "chunks",
        [
            ident(),
            ref("version_id", "document_versions.id", index=True),
            col("content", sa.Text),
            col("line_start", sa.Integer),
            col("line_end", sa.Integer),
            col(
                "embedding",
                sa.JSON().with_variant(Vector(1536), "postgresql"),
                nullable=True,
            ),
        ],
    )
    table(
        "jobs",
        [
            ident(),
            col("kind", sa.String(30)),
            col("target_id", sa.String(64)),
            col("revision", sa.Integer, nullable=True),
            col("status", sa.String(30)),
            col("attempts", sa.Integer),
            col("lease_until", sa.Float),
            col("lease_token", sa.String(64), nullable=True),
            col("error", sa.String(100), nullable=True),
            col("created_at", sa.Float),
        ],
    )
    table(
        "audit_events",
        [
            ident(),
            col("actor", sa.String(128)),
            col("action", sa.String(100)),
            col("target", sa.String(128)),
            col("created_at", sa.Float),
        ],
    )
    metadata.create_all(connection)


def downgrade():
    raise RuntimeError("Restore a verified backup instead of destructive downgrade.")
