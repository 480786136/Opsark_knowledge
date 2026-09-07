from alembic import op
from knowledge.db import Base
from knowledge import models  # noqa: F401

revision = "knowledge_0001"
down_revision = None


def upgrade():
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    Base.metadata.create_all(connection)


def downgrade():
    raise RuntimeError("Restore a verified backup instead of destructive downgrade.")
