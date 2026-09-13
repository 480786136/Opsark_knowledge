from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from knowledge import db
from knowledge.models import RefinementComparison


def test_comparison_migration_preserves_existing_data(tmp_path, monkeypatch):
    engine = db.make_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    monkeypatch.setattr(db, "engine", engine)
    config = Config("alembic.ini")
    command.upgrade(config, "knowledge_0002")
    # Initial migration imports current metadata; emulate an older deployed schema.
    RefinementComparison.__table__.drop(engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE preservation_check (value TEXT)"))
        connection.execute(text("INSERT INTO preservation_check VALUES ('keep')"))
    command.upgrade(config, "head")
    assert "refinement_comparisons" in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert (
            connection.execute(text("SELECT value FROM preservation_check")).scalar()
            == "keep"
        )
    command.upgrade(config, "head")
    engine.dispose()


def test_fresh_database_migrates_to_head(tmp_path, monkeypatch):
    engine = db.make_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    monkeypatch.setattr(db, "engine", engine)
    command.upgrade(Config("alembic.ini"), "head")
    assert "refinement_comparisons" in inspect(engine).get_table_names()
    engine.dispose()
