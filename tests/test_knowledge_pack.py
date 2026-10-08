import copy
import json

import httpx
import pytest
from argon2 import PasswordHasher
from sqlalchemy import func, select
from test_workflow import env as source_env

from knowledge.models import Document, Job, KnowledgeAdmin
from scripts.evaluate_knowledge_pack import QUERIES, evaluate, metrics
from scripts.knowledge_pack import (
    ADMIN,
    ROOT,
    checked,
    import_drafts,
    load_pack,
    local_url,
    main,
    pages,
    render,
)

env = source_env


def login(client, factory):
    with factory() as db:
        db.add(
            KnowledgeAdmin(
                username="pack-admin",
                password_hash=PasswordHasher().hash("test-only-password"),
            )
        )
        db.commit()
    result = checked(
        client.post(
            "/api/admin/v1/session",
            json={"username": "pack-admin", "password": "test-only-password"},
        )
    )
    client.headers["X-CSRF-Token"] = result["csrf"]


def test_pack_is_reviewable_and_render_matches_catalog():
    pack = load_pack()
    assert len(pack["documents"]) == 20
    assert len({item["id"] for item in pack["documents"]}) == 20
    rendered = "# 运维参考知识包 v1\n\n" + "\n---\n\n".join(
        render(pack, item) for item in pack["documents"]
    )
    assert (ROOT / "knowledge_packs/ops_starter_v1/KNOWLEDGE.md").read_text(
        encoding="utf-8"
    ) == rendered


def test_default_cli_never_connects(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline validation must not create an HTTP client")

    monkeypatch.setattr(httpx, "Client", forbidden)
    main([])
    report = json.loads(capsys.readouterr().out)
    assert report["writes"] == 0 and report["documents"] == 20


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://localhost:8002",
        "http://127.0.0.1.example.com",
        "http://user:pass@127.0.0.1",
        "http://127.0.0.1/path",
        "http://127.0.0.1?secret=value",
        "file:///tmp/knowledge",
        "http://127.0.0.1:bad",
    ],
)
def test_import_rejects_unsafe_endpoints(url):
    with pytest.raises(ValueError):
        local_url(url)


@pytest.mark.parametrize("url", ["http://127.0.0.1:8002", "https://[::1]:8002/"])
def test_explicit_loopback_is_allowed(url):
    assert local_url(url) == url.rstrip("/")


def test_all_conflicts_preflight_before_writing(env):
    client, factory, _ = env
    login(client, factory)
    kb = checked(
        client.post(ADMIN + "/knowledge-bases", json={"name": "test-only"}), 201
    )["id"]
    pack = load_pack()
    first = copy.deepcopy(pack)
    first["documents"] = [pack["documents"][-1]]
    row = import_drafts(client, first, kb)[0]
    with factory() as db:
        db.get(Document, row["document_id"]).content += "\n用户自己补充的内容"
        db.commit()
    with pytest.raises(ValueError, match="保护人工编辑"):
        import_drafts(client, pack, kb)
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Document)) == 1
        assert db.scalar(select(func.count()).select_from(Job)) == 0


def test_disabled_or_missing_base_cannot_import(env):
    client, factory, _ = env
    login(client, factory)
    kb = checked(
        client.post(
            ADMIN + "/knowledge-bases", json={"name": "disabled", "enabled": False}
        ),
        201,
    )["id"]
    for ident in [kb, "missing"]:
        with pytest.raises(ValueError, match="不存在或未启用"):
            import_drafts(client, load_pack(), ident)
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Document)) == 0


def test_duplicate_markers_are_not_silently_skipped(env):
    from scripts.knowledge_pack import document_payload

    client, factory, _ = env
    login(client, factory)
    kb = checked(
        client.post(ADMIN + "/knowledge-bases", json={"name": "duplicates"}), 201
    )["id"]
    pack = load_pack()
    payload = document_payload(pack, pack["documents"][0], kb)
    for _ in range(2):
        checked(client.post(ADMIN + "/documents", json=payload), 201)
    with pytest.raises(ValueError, match="重复标记"):
        import_drafts(client, pack, kb)


def test_document_pagination_does_not_stop_at_first_page():
    offsets = []

    def handle(request):
        offset = int(request.url.params["offset"])
        offsets.append(offset)
        return httpx.Response(
            200, json=[{"id": str(i)} for i in range(offset, min(offset + 200, 201))]
        )

    with httpx.Client(
        base_url="http://127.0.0.1", transport=httpx.MockTransport(handle)
    ) as client:
        assert len(pages(client, "/documents")) == 201
    assert offsets == [0, 200]


def test_http_error_does_not_expose_response_secrets():
    with pytest.raises(RuntimeError) as error:
        checked(httpx.Response(500, text="private-customer-payload"))
    assert "private-customer-payload" not in str(error.value)


def test_metrics_use_original_chunk_rank_not_unique_document_rank():
    result = metrics(
        [
            {
                "expected": ["a"],
                "actual": ["b", "b", "a"],
                "rank": 3,
                "answer_marker_found": False,
            },
            {
                "expected": [],
                "actual": ["b"],
                "rank": None,
                "answer_marker_found": False,
            },
        ]
    )
    assert result["hit_at_1"] == 0 and result["hit_at_5"] == 1
    assert result["mrr_at_5"] == 0.3333
    assert result["answer_marker_hit_at_5"] == 0
    assert result["negative_any_hit_rate"] == 1


def test_pack_publishes_only_in_isolated_evaluation_and_citations_match(env):
    client, factory, service = env
    report = evaluate(
        client,
        factory,
        service,
        load_pack(),
        json.loads(QUERIES.read_text(encoding="utf-8"))["cases"],
    )
    assert report["document_count"] == 20 and report["query_count"] == 50
    assert report["metrics"]["positive_queries"] == 42
    assert report["metrics"]["negative_queries"] == 8
    assert len(report["checks"]) == 7
    # Quality is intentionally reported separately; passing mechanics is not a
    # claim that retrieval relevance, answerability or rejection is sufficient.
