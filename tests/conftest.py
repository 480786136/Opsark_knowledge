"""Isolated SQLite by default; opt-in disposable loopback PostgreSQL databases."""

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


@pytest.fixture
def database_url(tmp_path):
    admin_url = os.environ.get("OPSARK_TEST_POSTGRES_ADMIN_URL")
    if not admin_url:
        yield f"sqlite:///{tmp_path / 'test.db'}"
        return
    url = make_url(admin_url)
    if url.get_backend_name() != "postgresql" or url.host not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError("Tests require an explicit loopback PostgreSQL admin URL")
    url = url.set(drivername="postgresql+psycopg")
    name = "opsark_knowledge_test_" + uuid.uuid4().hex
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    test_url = url.set(database=name).render_as_string(hide_password=False)
    try:
        engine = create_engine(test_url)
        with engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        engine.dispose()
        yield test_url
    finally:
        # Only this fixture's exact randomly generated database is removed.
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()
