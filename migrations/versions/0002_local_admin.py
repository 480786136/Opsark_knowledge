"""Add local administration without modifying knowledge records."""

from alembic import op
import sqlalchemy as sa

revision = "knowledge_0002"
down_revision = "knowledge_0001"
branch_labels = None
depends_on = None


def upgrade():
    # The legacy initial migration uses current metadata; support fresh and existing DBs.
    names = set(sa.inspect(op.get_bind()).get_table_names())
    if "knowledge_admins" not in names:
        op.create_table(
            "knowledge_admins",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("username", sa.String(100), nullable=False, unique=True),
            sa.Column("password_hash", sa.Text(), nullable=False),
        )
    if "knowledge_sessions" not in names:
        op.create_table(
            "knowledge_sessions",
            sa.Column("token_hash", sa.String(64), primary_key=True),
            sa.Column(
                "admin_id",
                sa.String(64),
                sa.ForeignKey("knowledge_admins.id"),
                nullable=False,
            ),
            sa.Column("csrf", sa.String(100), nullable=False),
            sa.Column("expires", sa.Float(), nullable=False),
        )


def downgrade():
    raise RuntimeError(
        "Restore a verified backup instead of deleting administrator data."
    )
