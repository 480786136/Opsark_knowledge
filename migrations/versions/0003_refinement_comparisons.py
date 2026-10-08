"""Persist the latest AI before/after comparison without changing documents."""

import sqlalchemy as sa
from alembic import op

revision = "knowledge_0003"
down_revision = "knowledge_0002"
branch_labels = None
depends_on = None


def upgrade():
    if "refinement_comparisons" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "refinement_comparisons",
            sa.Column(
                "document_id",
                sa.String(64),
                sa.ForeignKey("documents.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("before_title", sa.String(200), nullable=False),
            sa.Column("before_content", sa.Text(), nullable=False),
            sa.Column("after_title", sa.String(200), nullable=False),
            sa.Column("after_content", sa.Text(), nullable=False),
            sa.Column("applied_revision", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.Float(), nullable=False),
        )


def downgrade():
    raise RuntimeError(
        "Restore a verified backup instead of deleting comparison history."
    )
