"""Persist the latest AI before/after comparison without changing documents."""

from alembic import op
import sqlalchemy as sa
from knowledge.models import RefinementComparison

revision = "knowledge_0003"
down_revision = "knowledge_0002"
branch_labels = None
depends_on = None


def upgrade():
    if "refinement_comparisons" not in sa.inspect(op.get_bind()).get_table_names():
        RefinementComparison.__table__.create(op.get_bind())


def downgrade():
    raise RuntimeError(
        "Restore a verified backup instead of deleting comparison history."
    )
