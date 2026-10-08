"""Offline retrieval baseline in a disposable SQLite database, never the live DB.

Uses the real HTTP handlers, migrations, publication worker and public retrieval.
No sockets, real model providers, uploaded customer logs, or production credentials.
Quality failures are reported, not hidden by changing the question set.
"""

import argparse
import json
import math
import os
import secrets
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.knowledge_pack import ADMIN, checked, import_drafts, load_pack

QUERIES = ROOT / "knowledge_packs/ops_starter_v1/queries.json"


def metrics(rows):
    positives = [row for row in rows if row["expected"]]
    negatives = [row for row in rows if not row["expected"]]
    return {
        "positive_queries": len(positives),
        "hit_at_1": round(statistics.mean(row["rank"] == 1 for row in positives), 4)
        if positives
        else None,
        "hit_at_5": round(
            statistics.mean(row["rank"] is not None for row in positives), 4
        )
        if positives
        else None,
        "mrr_at_5": round(
            statistics.mean(1 / row["rank"] if row["rank"] else 0 for row in positives),
            4,
        )
        if positives
        else None,
        "answer_marker_hit_at_5": round(
            statistics.mean(row["answer_marker_found"] for row in positives), 4
        )
        if positives
        else None,
        "negative_queries": len(negatives),
        "negative_any_hit_rate": round(
            statistics.mean(bool(row["actual"]) for row in negatives), 4
        )
        if negatives
        else None,
        "mean_unique_documents_at_5": round(
            statistics.mean(len(set(row["actual"])) for row in rows), 2
        )
        if rows
        else 0,
    }


def evaluate(client, factory, service, pack, cases):
    from argon2 import PasswordHasher
    from sqlalchemy import func, select

    from knowledge.models import Document, Job, KnowledgeAdmin, SourceRecord
    from knowledge.processing import run_once

    item_ids = {item["id"] for item in pack["documents"]}
    if len({case["id"] for case in cases}) != len(cases):
        raise ValueError("验收问题编号重复")
    if len(cases) > 55 or any(
        not set(case["expected"]).issubset(item_ids) for case in cases
    ):
        raise ValueError("验收集超出预算或引用不存在的知识")
    # This function is only called with the isolated database below or a pytest fixture.
    password = secrets.token_urlsafe(24)
    with factory() as db:
        db.add(
            KnowledgeAdmin(
                username="pack-evaluation",
                password_hash=PasswordHasher().hash(password),
            )
        )
        db.commit()
    login = checked(
        client.post(
            "/api/admin/v1/session",
            json={"username": "pack-evaluation", "password": password},
        )
    )
    client.headers["X-CSRF-Token"] = login["csrf"]
    kb = checked(
        client.post(
            ADMIN + "/knowledge-bases", json={"name": "隔离验收知识库：不连接真实数据"}
        ),
        201,
    )["id"]
    results = import_drafts(client, pack, kb)
    assert len(results) == len(item_ids) and all(
        row["action"] == "created_draft" for row in results
    )
    repeated = import_drafts(client, pack, kb)
    assert all(row["action"] == "skipped" for row in repeated)
    identity = {row["document_id"]: row["pack_item"] for row in results}
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(SourceRecord)) == 0
        assert db.scalar(select(func.count()).select_from(Job)) == 0
        assert all(
            row.status == "draft" and row.published_version is None
            for row in db.scalars(select(Document))
        )

    def public_key(device):
        key = checked(
            client.post(
                "/internal/v1/knowledge-keys",
                headers=service,
                json={
                    "name": "isolated-evaluation",
                    "installation_id": device,
                    "knowledge_base_ids": [kb],
                    "scopes": ["knowledge:read"],
                },
            ),
            201,
        )
        return {"Authorization": "Bearer " + key["api_key"]}

    auth, citation_auth = public_key("pack-search"), public_key("pack-citations")
    request = {
        "query": "Nginx 配置",
        "knowledge_base_ids": [kb],
        "top_k": 5,
        "max_content_chars": 6000,
    }
    assert not checked(
        client.post("/api/v1/knowledge/search", headers=auth, json=request)
    )["hits"]
    for row in checked(client.get(ADMIN + "/documents")):
        checked(
            client.post(
                ADMIN + f"/documents/{row['id']}/publish",
                json={"revision": row["revision"]},
            ),
            202,
        )
    for _ in range(len(item_ids) + 2):
        if not run_once(factory):
            break
    with factory() as db:
        assert all(
            row.status == "published" and row.published_version == 1
            for row in db.scalars(select(Document))
        )
        assert all(
            row.kind == "publish" and row.status == "done"
            for row in db.scalars(select(Job))
        )

    rows, latencies, citation_urls = [], [], {}
    for case in cases:
        start = time.perf_counter()
        result = checked(
            client.post(
                "/api/v1/knowledge/search",
                headers=auth,
                json={
                    **request,
                    "query": case["query"],
                    "filters": case.get("filters", {}),
                },
            )
        )
        latencies.append((time.perf_counter() - start) * 1000)
        hits = result["hits"]
        assert result["retrieval_mode"] == "keyword_only"
        assert len(hits) <= 5 and sum(len(hit["content"]) for hit in hits) <= 6000
        actual = [identity[hit["document_id"]] for hit in hits]
        rank = next(
            (i for i, ident in enumerate(actual, 1) if ident in case["expected"]), None
        )
        answer_found = any(
            identity[hit["document_id"]] in case["expected"]
            and any(
                marker.lower() in hit["content"].lower()
                for marker in case["answer_markers"]
            )
            for hit in hits
        )
        for hit in hits:
            url = hit["citation"]["document_url"]
            if url not in citation_urls:
                citation_urls[url] = checked(client.get(url, headers=citation_auth))
            version = citation_urls[url]
            lines = version["content"].splitlines()
            first, last = hit["citation"]["line_start"], hit["citation"]["line_end"]
            assert 1 <= first <= last <= len(lines)
            assert "\n".join(lines[first - 1 : last]).startswith(
                hit["content"].rstrip("\n")
            )
        rows.append(
            {
                **case,
                "actual": actual,
                "rank": rank,
                "answer_marker_found": answer_found,
            }
        )

    first_doc = next(iter(identity))
    checked(client.post(ADMIN + f"/documents/{first_doc}/unpublish"))
    withdrawn = client.get(
        f"/api/v1/knowledge/documents/{first_doc}/versions/1", headers=citation_auth
    )
    assert withdrawn.status_code == 410
    result = checked(
        client.post(
            "/api/v1/knowledge/search",
            headers=auth,
            json={
                **request,
                "query": next(
                    item["title"]
                    for item in pack["documents"]
                    if item["id"] == identity[first_doc]
                ),
            },
        )
    )
    assert all(hit["document_id"] != first_doc for hit in result["hits"])
    checked(client.delete("/api/admin/v1/session"))
    return {
        "pack_id": pack["pack_id"],
        "document_count": len(item_ids),
        "query_count": len(cases),
        "backend": "sqlite",
        "providers": "disabled",
        "scope": "isolated functional/keyword baseline; not a production SLA or real AI evaluation",
        "rank_unit": "original chunk position, without deduplicating or reranking",
        "answer_marker_note": "Proxy only: marker present in an expected document snippet, not a semantic correctness judgment.",
        "checks": [
            "drafts_invisible",
            "repeat_import_skips_identical",
            "manual_import_no_source_or_ai_jobs",
            "worker_publish",
            "top_k_and_character_budget",
            "citation_line_provenance",
            "unpublish_removes_search_and_citation",
        ],
        "metrics": metrics(rows),
        "by_category": {
            category: metrics([row for row in rows if row["category"] == category])
            for category in sorted({row["category"] for row in rows})
        },
        "latency_ms": {
            "p50": round(statistics.median(latencies), 2),
            "p95": round(sorted(latencies)[math.ceil(len(latencies) * 0.95) - 1], 2),
        },
        "cases": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, help="可选 JSON 报告文件；拒绝覆盖已有文件"
    )
    args = parser.parse_args()
    if args.output and args.output.exists():
        parser.error("输出文件已存在；请换一个文件名，保留旧基线")
    with tempfile.TemporaryDirectory(prefix="opsark-pack-eval-") as directory:
        token = secrets.token_urlsafe(40)
        os.environ.update(
            {
                "DATABASE_URL": f"sqlite:///{directory}/evaluation.sqlite",
                "KNOWLEDGE_SERVICE_TOKEN": token,
                "AI_REFINEMENT_ENABLED": "false",
                "AI_BASE_URL": "",
                "AI_API_KEY": "",
                "AI_MODEL": "",
                "EMBEDDING_BASE_URL": "",
                "EMBEDDING_API_KEY": "",
                "EMBEDDING_MODEL": "",
                "WORKER_LOG_PATH": f"{directory}/worker.log",
                "COOKIE_SECURE": "false",
            }
        )
        from alembic import command
        from alembic.config import Config
        from fastapi.testclient import TestClient

        from knowledge.config import settings

        settings.cache_clear()
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "migrations"))
        command.upgrade(config, "head")
        from knowledge.db import SessionLocal, engine
        from knowledge.main import app

        try:
            with TestClient(app) as client:
                report = evaluate(
                    client,
                    SessionLocal,
                    {"Authorization": "Bearer " + token},
                    load_pack(),
                    json.loads(QUERIES.read_text(encoding="utf-8"))["cases"],
                )
            if args.output:
                with args.output.open("x", encoding="utf-8") as stream:
                    json.dump(report, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
            print(
                json.dumps(
                    {key: value for key, value in report.items() if key != "cases"},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        finally:
            engine.dispose()


if __name__ == "__main__":
    main()
