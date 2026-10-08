"""Authorization after upstream waits and pre-budget applicability filtering."""

import time

import pytest
from sqlalchemy import insert, select
from test_workflow import env as workflow_env
from test_workflow import prepare

from knowledge import main
from knowledge.models import ApiKey, Chunk, Document, DocumentVersion, IndexBuild
from knowledge.processing import index_id
from knowledge.retrieval import filter_candidates, postgres_candidates, searchable_text
from knowledge.schemas import SearchFilters

env = workflow_env


def seed(factory, kb, entries):
    docs, versions, builds, chunks = [], [], [], []
    for index, entry in enumerate(entries):
        ident = f"retrieval-{index:04d}"
        title, content = (
            entry.get("title", "nginx diagnostics"),
            entry.get("content", "nginx evidence"),
        )
        docs.append(
            {
                "id": ident,
                "knowledge_base_id": kb,
                "title": title,
                "content": "private mutable draft",
                "published_version": 1,
                "status": "published",
                "revision": 1,
            }
        )
        versions.append(
            {
                "id": f"v-{ident}",
                "document_id": ident,
                "version": 1,
                "title": title,
                "content": content,
                "tags": [],
                "environment": "prod",
                "software_names": entry.get("software_names", []),
                "context": {"software": entry.get("software", [])},
                "index_version": index_id(),
                "active_build_id": f"b-{ident}",
            }
        )
        builds.append(
            {
                "id": f"b-{ident}",
                "version_id": f"v-{ident}",
                "index_version": index_id(),
                "status": "ready",
                "config_snapshot": {},
            }
        )
        chunks.append(
            {
                "id": f"c-{ident}",
                "version_id": f"v-{ident}",
                "build_id": f"b-{ident}",
                "content": content,
                "search_text": searchable_text(title, content),
                "line_start": 1,
                "line_end": 1,
            }
        )
    with factory() as db:
        for model, rows in [
            (Document, docs),
            (DocumentVersion, versions),
            (IndexBuild, builds),
            (Chunk, chunks),
        ]:
            db.execute(insert(model), rows)
        db.commit()


@pytest.mark.parametrize(
    "change,status,code",
    [
        ({"revoked": True}, 401, "INVALID_API_KEY"),
        ({"expires": 1}, 401, "INVALID_API_KEY"),
        ({"scopes": ["records:read"]}, 403, "SCOPE_DENIED"),
        ({"knowledge_base_ids": []}, 403, "KNOWLEDGE_BASE_DENIED"),
    ],
)
def test_key_changed_during_embedding_is_rejected(
    env, monkeypatch, change, status, code
):
    client, factory, _ = env
    kb, auth, key_id = prepare(env)
    seed(factory, kb, [{}])

    def external_wait(_):
        with factory() as db:
            key = db.get(ApiKey, key_id)
            for name, value in change.items():
                setattr(key, name, value)
            db.commit()
        return [None]

    monkeypatch.setattr(main, "embed", external_wait)
    response = client.post(
        "/api/v1/knowledge/search",
        headers=auth,
        json={"query": "nginx", "knowledge_base_ids": [kb]},
    )
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    assert "nginx evidence" not in response.text


@pytest.mark.parametrize(
    "query,filters",
    [
        ("nginx /srv/target/config.toml", {}),
        ("nginx", {"software_names": ["nginx"]}),
        ("nginx", {"software": [{"name": "NGINX", "version": "1.26"}]}),
        ("nginx", {"software": [{"name": "nginx"}]}),
    ],
)
def test_hard_filters_precede_candidate_limits(env, monkeypatch, query, filters):
    client, factory, _ = env
    kb, auth, _ = prepare(env)
    seed(
        factory,
        kb,
        [
            {
                "content": "nginx /srv/other/config.toml",
                "software_names": ["other"],
                "software": [{"name": "other", "version": "1.0"}],
            }
            for _ in range(5)
        ]
        + [
            {
                "content": "nginx /srv/target/config.toml",
                "software_names": ["nginx"],
                "software": [{"name": "Nginx", "version": "1.26"}],
            }
        ],
    )
    monkeypatch.setattr(
        main,
        "postgres_candidates",
        lambda *args: postgres_candidates(*args, capacity=2),
    )
    response = client.post(
        "/api/v1/knowledge/search",
        headers=auth,
        json={"query": query, "knowledge_base_ids": [kb], "filters": filters},
    )
    assert response.status_code == 200, response.text
    assert [hit["document_id"] for hit in response.json()["hits"]] == ["retrieval-0005"]
    # The SQLite fallback must apply the same constraints in SQL, before LIMIT.
    with factory() as db:
        statement = select(Chunk.id).join(
            DocumentVersion, DocumentVersion.id == Chunk.version_id
        )
        paths = ["/srv/target/config.toml"] if "/srv/target" in query else []
        statement = filter_candidates(
            statement, SearchFilters(**filters), paths, db.bind.dialect.name
        )
        assert list(db.scalars(statement.order_by(Chunk.id).limit(2))) == [
            "c-retrieval-0005"
        ]


def test_exact_paths_are_case_sensitive_literals_and_versions_are_bound_values(env):
    client, factory, _ = env
    kb, auth, _ = prepare(env)
    unusual = "1.2' OR 1=1 --"
    seed(
        factory,
        kb,
        [
            {
                "content": "nginx /srv/axb/config.toml",
                "software": [{"name": "nginx", "version": "1.2"}],
            },
            {
                "content": "nginx /srv/a_b/config.toml",
                "software": [{"name": "nginx", "version": unusual}],
            },
            {
                "content": "nginx /srv/A_B/config.toml",
                "software": [{"name": "nginx", "version": unusual}],
            },
        ],
    )
    response = client.post(
        "/api/v1/knowledge/search",
        headers=auth,
        json={
            "query": "nginx /srv/a_b/config.toml",
            "knowledge_base_ids": [kb],
            "filters": {"software": [{"name": "nginx", "version": unusual}]},
        },
    )
    assert response.status_code == 200, response.text
    assert [hit["document_id"] for hit in response.json()["hits"]] == ["retrieval-0001"]


def test_revocation_while_ranking_does_not_return_prepared_hits(env, monkeypatch):
    client, factory, _ = env
    kb, auth, key_id = prepare(env)
    seed(factory, kb, [{}])
    original = main.keyword_rank

    def rank(*args):
        with factory() as db:
            db.get(ApiKey, key_id).expires = time.time() - 1
            db.commit()
        return original(*args)

    monkeypatch.setattr(main, "keyword_rank", rank)
    response = client.post(
        "/api/v1/knowledge/search",
        headers=auth,
        json={"query": "nginx", "knowledge_base_ids": [kb]},
    )
    assert response.status_code == 401, response.text


def test_unpublish_during_ranking_backfills_from_current_candidates(env, monkeypatch):
    client, factory, _ = env
    kb, auth, _ = prepare(env)
    seed(factory, kb, [{}, {}])
    original = main.keyword_rank

    def rank(*args):
        with factory() as db:
            db.get(Document, "retrieval-0000").published_version = None
            db.commit()
        return original(*args)

    monkeypatch.setattr(main, "keyword_rank", rank)
    response = client.post(
        "/api/v1/knowledge/search",
        headers=auth,
        json={"query": "nginx", "knowledge_base_ids": [kb], "top_k": 1},
    )
    assert response.status_code == 200, response.text
    assert [hit["document_id"] for hit in response.json()["hits"]] == ["retrieval-0001"]
