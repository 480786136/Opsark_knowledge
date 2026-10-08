"""Bounded PostgreSQL keyword retrieval benchmark using only new HTTP-seeded test data.

This is a reproducible engineering baseline, not a production SLA or semantic-model
quality evaluation. Creates 500-1000 synthetic documents in fresh temporary DBs.
"""

import argparse
import asyncio
import json
import math
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from smoke_workflow import (
    ROOT,
    database_urls,
    free_port,
    processes,
    request,
    require,
    wait_ready,
)

TOPICS = [
    "数据库连接池等待排查",
    "慢查询与索引检查",
    "磁盘空间不足处理",
    "备份恢复校验",
    "服务端口连接检查",
    "日志轮转保留策略",
    "进程内存增长排查",
    "配置变更回滚验证",
    "复制延迟排查",
    "证书有效期检查",
]


def document(index, kb):
    version = "16.4" if index % 2 == 0 else "15.9"
    environment = "staging" if index % 2 == 0 else "production"
    topic = TOPICS[index % len(TOPICS)]
    return {
        "knowledge_base_id": kb,
        "title": f"{topic}：案例 {index:04d}",
        "content": (
            f"# {topic}\n\n## 适用条件\n本篇为合成验收资料，适用环境 {environment}，PostgreSQL {version}。\n"
            f"诊断编号 BENCHCASE{index:04d}，配置路径 /srv/opsark-bench/{index:04d}/config.toml。\n"
            "## 操作方法\n先核对环境、问题范围、采集时间和备份状态，再执行只读检查；修复方案须经过管理员审批。\n"
            + (
                "观察实际输出并保留失败记录；历史知识只作为参考，不能替代实时状态检查或操作授权。\n"
                * 18
            )
            + "## 验收标准\n需要独立校验关键指标，缺少校验时标记未确认；本段没有宣称真实服务器完成任何操作。\n"
        ),
        "environment": environment,
        "software_names": ["PostgreSQL"],
        "tags": ["合成压测"],
        "context": {
            "environment": environment,
            "software": [{"name": "PostgreSQL", "version": version}],
        },
    }


async def seed(base_url, token, kb, count):
    semaphore = asyncio.Semaphore(8)
    async with httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": "Bearer " + token},
        trust_env=False,
        timeout=60,
    ) as client:

        async def create(index):
            async with semaphore:
                response = await client.post(
                    "/internal/v1/documents", json=document(index, kb)
                )
                require(
                    response.status_code == 201,
                    f"Seed create HTTP {response.status_code}",
                )
                doc = response.json()
                response = await client.post(
                    f"/internal/v1/documents/{doc['id']}/publish",
                    json={"revision": doc["revision"]},
                )
                require(
                    response.status_code == 202,
                    f"Seed publish HTTP {response.status_code}",
                )
                return doc

        return await asyncio.gather(*(create(index) for index in range(count)))


async def measure(base_url, keys, kb, count, queries):
    latencies, errors, matched = [], [], 0

    async def client_load(client_index, key):
        nonlocal matched
        async with httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": "Bearer " + key},
            trust_env=False,
            timeout=30,
        ) as client:
            for ordinal in range(client_index, queries, len(keys)):
                index = 2 + (ordinal * 37) % (count - 2)
                body = {
                    "query": TOPICS[index % len(TOPICS)]
                    + f" /srv/opsark-bench/{index:04d}/config.toml",
                    "knowledge_base_ids": [kb],
                    "top_k": 3,
                    "max_content_chars": 500,
                }
                start = time.perf_counter()
                response = await client.post("/api/v1/knowledge/search", json=body)
                latencies.append((time.perf_counter() - start) * 1000)
                if response.status_code != 200:
                    errors.append(response.status_code)
                    continue
                hits = response.json()["hits"]
                if hits and all(f"案例 {index:04d}" in hit["title"] for hit in hits):
                    matched += 1
                require(
                    sum(len(hit["content"]) for hit in hits) <= 500 and len(hits) <= 3,
                    "Result budget exceeded",
                )

    started = time.perf_counter()
    await asyncio.gather(*(client_load(index, key) for index, key in enumerate(keys)))
    elapsed = time.perf_counter() - started
    ordered = sorted(latencies)

    def percentile(value):
        return round(ordered[max(0, math.ceil(len(ordered) * value) - 1)], 2)

    return {
        "requests": queries,
        "clients": len(keys),
        "elapsed_seconds": round(elapsed, 2),
        "throughput_per_second": round(queries / elapsed, 2),
        "p50_ms": percentile(0.5),
        "p95_ms": percentile(0.95),
        "p99_ms": percentile(0.99),
        "http_errors": len(errors),
        "error_statuses": sorted(set(errors)),
        "expected_document_matches": matched,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-admin-url-env", required=True)
    parser.add_argument(
        "--documents",
        type=int,
        choices=range(500, 1001),
        default=500,
        metavar="500..1000",
    )
    parser.add_argument(
        "--queries", type=int, choices=range(10, 401), default=200, metavar="10..400"
    )
    args = parser.parse_args()
    directory = Path(tempfile.mkdtemp(prefix="opsark-private-benchmark-"))
    report = {
        "artifact_directory": str(directory),
        "documents": args.documents,
        "providers": "disabled",
        "scope": "synthetic PostgreSQL keyword retrieval; not a production SLA",
        "checks": [],
    }
    token, port = secrets.token_urlsafe(40), free_port()
    environment = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "KNOWLEDGE_SERVICE_TOKEN": token,
        "COOKIE_SECURE": "false",
        "ALLOWED_ORIGINS": f"http://127.0.0.1:{port}",
        "AI_REFINEMENT_ENABLED": "false",
        "AI_BASE_URL": "",
        "AI_MODEL": "",
        "AI_API_KEY": "",
        "EMBEDDING_BASE_URL": "",
        "EMBEDDING_MODEL": "",
        "EMBEDDING_API_KEY": "",
        "EMBEDDING_DIMENSIONS": "1536",
        "WORKER_POLL_SECONDS": "0.1",
        "WORKER_LOG_PATH": str(directory / "worker-diagnostics.log"),
    }
    try:
        with database_urls(args, directory) as (database, _):
            environment["DATABASE_URL"] = database
            migration = (
                "from alembic.config import Config; from alembic import command; c=Config(); c.set_main_option('script_location',"
                + repr(str(ROOT / "migrations"))
                + "); command.upgrade(c,'head')"
            )
            subprocess.run(
                [sys.executable, "-c", migration],
                cwd=directory,
                env=environment,
                check=True,
                capture_output=True,
            )
            with processes(environment, directory) as start:
                api = start(
                    "api",
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "knowledge.main:app",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                    ],
                )
                base_url = f"http://127.0.0.1:{port}"
                wait_ready(base_url, api)
                for index in range(4):
                    start(
                        f"worker-{index}",
                        [sys.executable, "-m", "knowledge.processing"],
                    )
                with httpx.Client(
                    base_url=base_url,
                    headers={"Authorization": "Bearer " + token},
                    trust_env=False,
                    timeout=60,
                ) as admin:
                    kb = request(
                        admin,
                        "POST",
                        "/internal/v1/knowledge-bases",
                        expected=201,
                        json={"name": "合成检索压测库"},
                    )["id"]
                    seed_start = time.perf_counter()
                    docs = asyncio.run(seed(base_url, token, kb, args.documents))
                    deadline = time.monotonic() + 300
                    while True:
                        health = request(admin, "GET", "/internal/v1/health")
                        require(
                            health["worker"]["failed"] == 0,
                            "Index worker reported failed jobs",
                        )
                        if health["worker"]["pending"] == 0:
                            published = []
                            for offset in range(0, args.documents, 200):
                                published.extend(
                                    request(
                                        admin,
                                        "GET",
                                        f"/internal/v1/documents?offset={offset}&limit=200",
                                    )
                                )
                            if len(published) == args.documents and all(
                                doc["published_version"] is not None
                                for doc in published
                            ):
                                break
                        require(
                            time.monotonic() < deadline,
                            "Timed out waiting for benchmark publications",
                        )
                        time.sleep(0.5)
                    report["seed_and_publish_seconds"] = round(
                        time.perf_counter() - seed_start, 2
                    )
                    report["checks"].append(
                        "all_synthetic_documents_published_via_http_and_four_workers"
                    )
                    keys = [
                        request(
                            admin,
                            "POST",
                            "/internal/v1/knowledge-keys",
                            expected=201,
                            json={
                                "name": f"benchmark-client-{index}",
                                "installation_id": f"benchmark-client-{index}",
                                "knowledge_base_ids": [kb],
                                "scopes": ["knowledge:read"],
                            },
                        )["api_key"]
                        for index in range(10)
                    ]
                    with httpx.Client(
                        base_url=base_url,
                        headers={"Authorization": "Bearer " + keys[0]},
                        trust_env=False,
                        timeout=30,
                    ) as client:

                        def search(query, filters=None):
                            return request(
                                client,
                                "POST",
                                "/api/v1/knowledge/search",
                                json={
                                    "query": query,
                                    "knowledge_base_ids": [kb],
                                    "filters": filters or {},
                                    "max_content_chars": 500,
                                },
                            )

                        require(
                            not search("UNRECORDEDZZZZ" + secrets.token_hex(12))[
                                "hits"
                            ],
                            "No-answer query produced a false match",
                        )
                        require(
                            search("BENCHCASE0002")["hits"][0]["document_id"]
                            == docs[2]["id"],
                            "Exact diagnostic identifier ranking",
                        )
                        require(
                            not search(
                                "/srv/opsark-bench/0002/config.toml",
                                {"environment": "production"},
                            )["hits"],
                            "Environment filter leaked a staging version",
                        )
                        require(
                            not search(
                                "/srv/opsark-bench/0002/config.toml",
                                {
                                    "software": [
                                        {"name": "PostgreSQL", "version": "15.9"}
                                    ]
                                },
                            )["hits"],
                            "Software-version filter mismatch",
                        )
                        require(
                            search(
                                "/srv/opsark-bench/0002/config.toml",
                                {
                                    "software": [
                                        {"name": "PostgreSQL", "version": "16.4"}
                                    ]
                                },
                            )["hits"],
                            "Software-version matching filter",
                        )
                        for topic in TOPICS:
                            found = search(topic)["hits"]
                            require(
                                found and all(topic in hit["title"] for hit in found),
                                "Chinese natural-language topic ranking",
                            )
                        current = request(
                            admin, "GET", f"/internal/v1/documents/{docs[0]['id']}"
                        )
                        edited = {
                            field: current[field]
                            for field in (
                                "knowledge_base_id",
                                "title",
                                "content",
                                "revision",
                            )
                        }
                        edited["content"] = (
                            edited["content"]
                            .replace("BENCHCASE0000", "UPDATEDCASE0000")
                            .replace(
                                "/srv/opsark-bench/0000/config.toml",
                                "/srv/opsark-updated/0000/config.toml",
                            )
                        )
                        current = request(
                            admin,
                            "PATCH",
                            f"/internal/v1/documents/{current['id']}/draft",
                            json=edited,
                        )
                        request(
                            admin,
                            "POST",
                            f"/internal/v1/documents/{current['id']}/publish",
                            expected=202,
                            json={"revision": current["revision"]},
                        )
                        deadline = time.monotonic() + 30
                        while (
                            request(
                                admin, "GET", f"/internal/v1/documents/{current['id']}"
                            )["published_version"]
                            != 2
                        ):
                            require(
                                time.monotonic() < deadline,
                                "Updated publication timeout",
                            )
                            time.sleep(0.2)
                        require(
                            not search("/srv/opsark-bench/0000/config.toml")["hits"],
                            "Old version leaked into search",
                        )
                        require(
                            search("/srv/opsark-updated/0000/config.toml")["hits"][0][
                                "document_version"
                            ]
                            == 2,
                            "Updated version missing",
                        )
                        request(
                            admin,
                            "POST",
                            f"/internal/v1/documents/{docs[1]['id']}/unpublish",
                        )
                        require(
                            not search("/srv/opsark-bench/0001/config.toml")["hits"],
                            "Unpublished document leaked into search",
                        )
                    report["checks"].extend(
                        [
                            "no_answer",
                            "exact_identifier_rank",
                            "chinese_natural_language_topics",
                            "environment_filter",
                            "software_version_filter",
                            "active_version_only",
                            "unpublished_hidden",
                        ]
                    )
                    report["performance"] = asyncio.run(
                        measure(base_url, keys, kb, args.documents, args.queries)
                    )
                    require(
                        report["performance"]["http_errors"] == 0,
                        "Retrieval benchmark returned HTTP failures",
                    )
                    require(
                        report["performance"]["expected_document_matches"]
                        == args.queries,
                        "Chinese/path query recall regression",
                    )
                    report["checks"].extend(
                        [
                            "chinese_path_query_expected_document",
                            "top_k_and_content_budget",
                        ]
                    )
        report["status"] = "passed"
    except Exception as exc:
        report["status"] = "failed"
        report["failure"] = (
            str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        )
    (directory / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
