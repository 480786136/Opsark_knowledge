"""Exercise real legacy rows, not current ORM masquerading as the old schema."""

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import MetaData, create_engine, select
from sqlalchemy.orm import Session

from knowledge.config import settings
from knowledge.lifecycle import apply_source
from knowledge.models import Document, DocumentSource, DocumentVersion, SourceRecord


def migrate(url, target):
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", target],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        check=True,
    )


def test_legacy_duplicates_provenance_and_source_adoption(database_url, monkeypatch):
    monkeypatch.setattr(settings(), "ai_refinement_enabled", False)
    migrate(database_url, "knowledge_0003")
    engine = create_engine(database_url)
    metadata = MetaData()
    metadata.reflect(engine)
    tables = metadata.tables
    old_id, new_id = "a" * 32, "b" * 32
    common = dict(
        knowledge_base_id="kb",
        title="Nginx",
        tags=[],
        environment="test",
        software_names=[],
    )
    with engine.begin() as db:
        db.execute(
            tables["knowledge_bases"]
            .insert()
            .values(
                id="kb",
                name="Legacy",
                description="",
                enabled=True,
                created_at=1,
            )
        )
        for ident, revision in [(old_id, 1), (new_id, 2)]:
            db.execute(
                tables["source_records"]
                .insert()
                .values(
                    id=ident,
                    installation_id="core",
                    source_record_id="task",
                    source_revision=revision,
                    knowledge_base_id="kb",
                    body_hash=ident,
                    status="ready_for_review",
                    created_at=revision,
                    payload={
                        "title": "Nginx",
                        "problem": "check",
                        "steps": [],
                        "outcome": {"status": "partial", "summary": "unverified"},
                        "context": {"environment": f"revision-{revision}"},
                    },
                )
            )
        for ident, source, status, published in [
            ("canonical", old_id, "published", 1),
            ("duplicate", new_id, "draft", None),
        ]:
            db.execute(
                tables["documents"]
                .insert()
                .values(
                    **common,
                    id=ident,
                    source_record_id=source,
                    content="legacy manual content",
                    revision=1,
                    published_version=published,
                    status=status,
                    updated_at=2,
                )
            )
        for ident, number, content in [
            ("original", 1, f"Original source {old_id}"),
            ("unknown", 2, "Manually authored legacy text"),
        ]:
            db.execute(
                tables["document_versions"]
                .insert()
                .values(
                    **{k: v for k, v in common.items() if k != "knowledge_base_id"},
                    id=ident,
                    document_id="canonical",
                    version=number,
                    content=content,
                    index_version="legacy",
                    created_at=3,
                )
            )
        db.execute(
            tables["chunks"]
            .insert()
            .values(
                id="chunk",
                version_id="original",
                content="Nginx check",
                line_start=1,
                line_end=1,
            )
        )
        db.execute(
            tables["jobs"]
            .insert()
            .values(
                id="legacy-job",
                kind="refine",
                target_id="duplicate",
                revision=1,
                status="running",
                attempts=1,
                lease_until=1000,
                lease_token="lease",
                created_at=2,
            )
        )
    migrate(database_url, "head")
    migrate(database_url, "head")
    with Session(engine) as db:
        canonical, duplicate = (
            db.get(Document, "canonical"),
            db.get(Document, "duplicate"),
        )
        assert canonical.logical_source_key and canonical.published_version == 1
        assert duplicate.superseded_by == canonical.id
        assert (
            duplicate.source_record_id is None
            and duplicate.content == "legacy manual content"
        )
        assert duplicate.draft_edited
        assert set(db.scalars(select(DocumentSource.source_record_id))) == {
            old_id,
            new_id,
        }
        version = db.get(DocumentVersion, "original")
        assert (
            version.source_record_ids == [old_id]
            and version.context["environment"] == "revision-1"
        )
        assert version.active_build_id
        unknown = db.get(DocumentVersion, "unknown")
        assert unknown.source_record_ids == [] and unknown.context == {}
        assert unknown.active_build_id is None
        apply_source(db, canonical, db.get(SourceRecord, new_id))
        db.commit()  # The old UNIQUE(source_record_id) must not block explicit adoption.
        assert canonical.source_record_id == new_id
        assert canonical.published_version == 1
        assert version.source_record_ids == [old_id]
    engine.dispose()


def test_legacy_replaced_source_does_not_rewrite_history(database_url):
    migrate(database_url, "knowledge_0003")
    engine = create_engine(database_url)
    metadata = MetaData()
    metadata.reflect(engine)
    tables = metadata.tables
    old_id, new_id = "c" * 32, "d" * 32
    with engine.begin() as db:
        db.execute(
            tables["knowledge_bases"]
            .insert()
            .values(
                id="kb",
                name="Legacy",
                description="",
                enabled=True,
                created_at=1,
            )
        )
        for ident, revision in [(old_id, 1), (new_id, 2)]:
            db.execute(
                tables["source_records"]
                .insert()
                .values(
                    id=ident,
                    installation_id="core",
                    source_record_id="task",
                    source_revision=revision,
                    knowledge_base_id="kb",
                    body_hash=ident,
                    status="processed",
                    created_at=revision,
                    payload={"context": {"environment": f"revision-{revision}"}},
                )
            )
        fields = dict(
            title="Nginx",
            content=f"Source {old_id}",
            tags=[],
            environment="",
            software_names=[],
        )
        db.execute(
            tables["documents"]
            .insert()
            .values(
                **fields,
                id="doc",
                knowledge_base_id="kb",
                source_record_id=new_id,
                revision=3,
                published_version=None,
                status="draft",
                updated_at=3,
            )
        )
        db.execute(
            tables["document_versions"]
            .insert()
            .values(
                **fields,
                id="v1",
                document_id="doc",
                version=1,
                index_version="legacy",
                created_at=1.5,
            )
        )
    migrate(database_url, "head")
    with Session(engine) as db:
        version = db.get(DocumentVersion, "v1")
        assert db.get(Document, "doc").source_record_id == new_id
        assert version.source_record_ids == [old_id]
        assert version.context == {"environment": "revision-1"}
    engine.dispose()
