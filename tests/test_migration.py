import os
from pathlib import Path
import subprocess
import sys
from sqlalchemy import create_engine, inspect, text


def test_additive_local_admin_migration(tmp_path):
    url = "sqlite:///" + (tmp_path / "upgrade.db").as_posix()
    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(
            text(
                "CREATE TABLE source_records (id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
        )
        c.execute(
            text(
                "INSERT INTO source_records VALUES ('existing','retained synthetic source')"
            )
        )
    environment = {**os.environ, "DATABASE_URL": url}
    root = Path(__file__).resolve().parents[1]
    for args in [("stamp", "knowledge_0001"), ("upgrade", "head"), ("upgrade", "head")]:
        subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=root,
            env=environment,
            check=True,
            capture_output=True,
        )
    with engine.connect() as c:
        assert (
            c.execute(text("SELECT payload FROM source_records")).scalar()
            == "retained synthetic source"
        )
    assert {"knowledge_admins", "knowledge_sessions"}.issubset(
        inspect(engine).get_table_names()
    )
    engine.dispose()
