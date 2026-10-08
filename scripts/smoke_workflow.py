"""Real HTTP/API/worker acceptance in an isolated database, using loopback AI mocks.

No service .env, user database, remote model or paid provider is used. PostgreSQL
mode creates uniquely named test databases; it never migrates the admin database.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from sqlalchemy.engine import make_url

from replay_core_logs import collect_records, default_core_data

ROOT = Path(__file__).resolve().parents[1]


def require(condition, label):
    if not condition:
        raise RuntimeError(label)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def serve_mock(port):
    class Handler(BaseHTTPRequestHandler):
        embedding_failure = False
        ai_failure = False

        def log_message(self, *args):
            pass

        def do_POST(self):  # noqa: N802
            size = int(self.headers.get("Content-Length", 0))
            if size > 2 * 1024 * 1024:
                self.send_error(413)
                return
            body = json.loads(self.rfile.read(size))
            if self.path == "/control":
                Handler.embedding_failure = body.get("embedding_failure", False)
                Handler.ai_failure = body.get("ai_failure", False)
                result = {"ok": True}
            elif self.path == "/v1/embeddings":
                if Handler.embedding_failure:
                    self.send_error(503)
                    return
                # Deterministic valid vectors, not a semantic-quality benchmark.
                result = {
                    "data": [
                        {"index": i, "embedding": [1.0] + [0.0] * 1535}
                        for i, _ in enumerate(body["input"])
                    ]
                }
            elif self.path == "/v1/chat/completions":
                if Handler.ai_failure:
                    self.send_error(503)
                    return
                experience = {
                    "title": "独立进程回放验收",
                    "claims": [
                        {
                            "section": "结论边界",
                            "kind": "未确认",
                            "text": "本机 mock 仅验证提炼协议与流程；历史证据和实际知识质量仍需人工审核。",
                            "refs": [],
                        }
                    ],
                }
                result = {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": json.dumps(experience, ensure_ascii=False)
                            },
                        }
                    ]
                }
            else:
                self.send_error(404)
                return
            raw = json.dumps(result, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def request(client, method, path, *, expected=200, **kwargs):
    response = client.request(method, path, **kwargs)
    require(
        response.status_code == expected,
        f"{method} {path.split('?')[0]}: expected {expected}, got {response.status_code}",
    )
    return response.json()


def until(callback, label, timeout=40):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = callback()
        if result:
            return result
        time.sleep(0.2)
    raise RuntimeError("Timed out: " + label)


def wait_ready(url, process):
    def ready():
        require(process.poll() is None, "API process exited before becoming healthy")
        try:
            return (
                httpx.get(url + "/health/live", timeout=1, trust_env=False).status_code
                == 200
            )
        except httpx.HTTPError:
            return False

    until(ready, "API health")


@contextmanager
def processes(environment, directory):
    children = []
    handles = []

    def start(name, args, overrides=None):
        log = (directory / (name + ".log")).open("ab")
        handles.append(log)
        process = subprocess.Popen(
            args,
            cwd=directory,
            env={**environment, **(overrides or {})},
            stdout=log,
            stderr=log,
        )
        children.append(process)
        return process

    try:
        yield start
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
        for child in reversed(children):
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        for handle in handles:
            handle.close()


@contextmanager
def database_urls(args, directory):
    if not args.postgres_admin_url_env:
        yield (
            "sqlite:///" + str(directory / "knowledge.db"),
            "sqlite:///" + str(directory / "restored.db"),
        )
        return
    import psycopg
    from psycopg import sql

    value = os.environ.get(args.postgres_admin_url_env, "")
    admin = make_url(value)
    require(
        admin.get_backend_name() == "postgresql"
        and admin.host in {"127.0.0.1", "localhost", "::1"},
        "PostgreSQL smoke requires a loopback administrative URL",
    )
    names = [
        "opsark_smoke_" + secrets.token_hex(8),
        "opsark_restore_" + secrets.token_hex(8),
    ]
    created = []
    with psycopg.connect(
        admin.set(drivername="postgresql").render_as_string(hide_password=False),
        autocommit=True,
    ) as connection:
        try:
            for name in names:
                connection.execute(
                    sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name))
                )
                created.append(name)
            yield tuple(
                admin.set(
                    drivername="postgresql+psycopg", database=name
                ).render_as_string(hide_password=False)
                for name in names
            )
        finally:
            for name in reversed(created):
                connection.execute(
                    sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                        sql.Identifier(name)
                    )
                )


def backup_restore(source_url, restore_url, directory):
    source, target = make_url(source_url), make_url(restore_url)
    if source.get_backend_name() == "sqlite":
        # The online backup API includes WAL contents, unlike copying the .db file.
        with sqlite3.connect(f"file:{source.database}?mode=ro", uri=True) as connection:
            with sqlite3.connect(target.database) as restored:
                connection.backup(restored)
                require(
                    restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
                    "SQLite backup integrity",
                )
        return
    pg_dump, pg_restore = shutil.which("pg_dump"), shutil.which("pg_restore")
    require(
        pg_dump and pg_restore,
        "PostgreSQL backup verification requires pg_dump and pg_restore on PATH",
    )
    backup = directory / "knowledge.dump"
    environment = {**os.environ, "PGPASSWORD": source.password or ""}

    def connection_args(url):
        return [
            "--host",
            url.host,
            "--port",
            str(url.port or 5432),
            "--username",
            url.username,
            "--dbname",
            url.database,
        ]

    subprocess.run(
        [
            pg_dump,
            *connection_args(source),
            "--format=custom",
            "--no-owner",
            "--file",
            str(backup),
        ],
        env=environment,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            pg_restore,
            *connection_args(target),
            "--no-owner",
            "--exit-on-error",
            str(backup),
        ],
        env=environment,
        check=True,
        capture_output=True,
    )


def synthetic_record():
    return {
        "schema_version": "1.0",
        "source_record_id": "synthetic-smoke-task",
        "source_revision": 1,
        "knowledge_base_id": "pending",
        "record_type": "task_result",
        "title": "本机流程验收",
        "occurred_at": "2026-09-23T00:00:00Z",
        "problem": "验证上传发布和检索链路",
        "steps": [
            {
                "step_id": "step-1",
                "description": "已知合成测试输出",
                "execution_status": "succeeded",
                "validation_status": "not_run",
                "evidence": [
                    {
                        "evidence_id": "main",
                        "kind": "command_result",
                        "summary": "合成测试退出码=0",
                        "excerpt": "SMOKE_SYNTHETIC_OK",
                    }
                ],
            }
        ],
        "outcome": {"status": "partial", "summary": "合成协议测试，不代表生产任务成功"},
        "redaction": {"client_applied": True, "ruleset_version": "synthetic-smoke-v1"},
    }


def run_workflow(args):
    directory = Path(tempfile.mkdtemp(prefix="opsark-private-smoke-"))
    report = {
        "artifact_directory": str(directory),
        "checks": [],
        "providers": "loopback mocks only",
    }
    records = [synthetic_record()]
    if args.with_core_logs:
        records, inventory = collect_records(args.core_data, args.limit)
        require(records, "No linked Core evidence records found")
        report["core_inventory"] = inventory
    report["record_count"] = len(records)
    port, mock_port = free_port(), free_port()
    token = secrets.token_urlsafe(40)
    environment = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "KNOWLEDGE_SERVICE_TOKEN": token,
        "COOKIE_SECURE": "false",
        "ALLOWED_ORIGINS": f"http://127.0.0.1:{port}",
        "AI_REFINEMENT_ENABLED": "true",
        "AI_BASE_URL": f"http://127.0.0.1:{mock_port}/v1",
        "AI_API_KEY": "local-smoke-placeholder",
        "AI_MODEL": "local-protocol-mock",
        "EMBEDDING_BASE_URL": f"http://127.0.0.1:{mock_port}/v1",
        "EMBEDDING_API_KEY": "local-smoke-placeholder",
        "EMBEDDING_MODEL": "local-vector-mock",
        "EMBEDDING_DIMENSIONS": "1536",
        "EMBEDDING_INDEX_VERSION": "smoke-v1",
        "WORKER_POLL_SECONDS": "0.1",
        "WORKER_RETRY_BASE_SECONDS": "0.1",
        "WORKER_LOG_PATH": str(directory / "worker-diagnostics.log"),
    }
    try:
        with database_urls(args, directory) as (database, restored):
            environment["DATABASE_URL"] = database
            report["database"] = make_url(database).get_backend_name()
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
            report["checks"].append("empty_database_migration")
            with processes(environment, directory) as start:
                start(
                    "mock",
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--serve-mock",
                        str(mock_port),
                    ],
                )
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
                with httpx.Client(
                    base_url=base_url,
                    headers={"Authorization": "Bearer " + token},
                    trust_env=False,
                    timeout=20,
                ) as admin:
                    bases = [
                        request(
                            admin,
                            "POST",
                            "/internal/v1/knowledge-bases",
                            expected=201,
                            json={"name": f"Isolated replay {i}"},
                        )["id"]
                        for i in range(len(records))
                    ]
                    key = request(
                        admin,
                        "POST",
                        "/internal/v1/knowledge-keys",
                        expected=201,
                        json={
                            "name": "isolated-smoke",
                            "installation_id": "isolated-smoke",
                            "knowledge_base_ids": bases,
                            "scopes": [
                                "records:write",
                                "records:read",
                                "knowledge:read",
                            ],
                        },
                    )["api_key"]
                    with httpx.Client(
                        base_url=base_url,
                        headers={"Authorization": "Bearer " + key},
                        trust_env=False,
                        timeout=20,
                    ) as client:
                        uploaded = []
                        for record, kb in zip(records, bases):
                            record["knowledge_base_id"] = kb
                            encoded = json.dumps(
                                record, ensure_ascii=False, separators=(",", ":")
                            ).encode()
                            headers = {
                                "Content-Type": "application/json",
                                "Idempotency-Key": hashlib.sha256(encoded).hexdigest(),
                            }
                            first = request(
                                client,
                                "POST",
                                "/api/v1/records",
                                expected=202,
                                content=encoded,
                                headers=headers,
                            )
                            again = request(
                                client,
                                "POST",
                                "/api/v1/records",
                                expected=202,
                                content=encoded,
                                headers=headers,
                            )
                            require(
                                first["record_id"] == again["record_id"]
                                and again["duplicate"],
                                "Upload idempotency",
                            )
                            uploaded.append(first["record_id"])
                        report["checks"].append("http_upload_idempotency")
                        # Start the worker after durable uploads and restart it after draft completion.
                        worker = start(
                            "worker", [sys.executable, "-m", "knowledge.processing"]
                        )
                        expected_failed_jobs = set()

                        def settled():
                            jobs = request(admin, "GET", "/internal/v1/jobs")
                            failed = [
                                job
                                for job in jobs
                                if job["status"] == "failed"
                                and job["id"] not in expected_failed_jobs
                            ]
                            require(
                                not failed,
                                "Worker job failed: "
                                + ",".join(str(job.get("error")) for job in failed),
                            )
                            return jobs and all(
                                job["status"]
                                in {"done", "completed", "succeeded", "cancelled"}
                                or job["id"] in expected_failed_jobs
                                for job in jobs
                            )

                        until(settled, "draft and refinement jobs")
                        docs = request(admin, "GET", "/internal/v1/documents")
                        require(
                            len(docs) == len(records), "One draft per uploaded task"
                        )
                        for doc in docs:
                            require(
                                request(
                                    admin,
                                    "GET",
                                    f"/internal/v1/documents/{doc['id']}/comparison",
                                )["comparison"],
                                "AI comparison persisted",
                            )
                        worker.terminate()
                        worker.wait(timeout=5)
                        start(
                            "worker-restarted",
                            [sys.executable, "-m", "knowledge.processing"],
                        )
                        report["checks"].extend(
                            ["real_worker_mock_ai_refinement", "worker_restart"]
                        )

                        def search(kb):
                            return request(
                                client,
                                "POST",
                                "/api/v1/knowledge/search",
                                json={
                                    "query": "独立进程回放验收",
                                    "knowledge_base_ids": [kb],
                                },
                            )

                        for doc in docs:
                            require(
                                not search(doc["knowledge_base_id"])["hits"],
                                "Draft must not be searchable",
                            )
                            request(
                                admin,
                                "POST",
                                f"/internal/v1/documents/{doc['id']}/publish",
                                expected=202,
                                json={"revision": doc["revision"]},
                            )
                        until(settled, "publish jobs")
                        citations = []
                        for doc in docs:
                            found = search(doc["knowledge_base_id"])
                            require(
                                found["retrieval_mode"] == "hybrid" and found["hits"],
                                "Published hybrid retrieval",
                            )
                            hit = found["hits"][0]
                            require(
                                hit["document_id"] == doc["id"],
                                "Citation document identity",
                            )
                            require(
                                hit["citation"]["line_start"]
                                <= hit["citation"]["line_end"],
                                "Citation line order",
                            )
                            citation = request(
                                client, "GET", hit["citation"]["document_url"]
                            )
                            lines = citation["content"].splitlines()
                            quote = "\n".join(
                                lines[
                                    hit["citation"]["line_start"] - 1 : hit["citation"][
                                        "line_end"
                                    ]
                                ]
                            )
                            require(
                                hit["content"] in quote,
                                "Citation content matches immutable version lines",
                            )
                            citations.append(hit)
                        report["checks"].extend(
                            [
                                "draft_search_isolation",
                                "publish_hybrid_search",
                                "citation_line_integrity",
                            ]
                        )

                        # Back up the live published service, restore into a distinct DB, then read via another API.
                        backup_restore(database, restored, directory)
                        restore_port = free_port()
                        restore_api = start(
                            "restored-api",
                            [
                                sys.executable,
                                "-m",
                                "uvicorn",
                                "knowledge.main:app",
                                "--host",
                                "127.0.0.1",
                                "--port",
                                str(restore_port),
                            ],
                            {"DATABASE_URL": restored},
                        )
                        restored_url = f"http://127.0.0.1:{restore_port}"
                        wait_ready(restored_url, restore_api)
                        with httpx.Client(
                            base_url=restored_url,
                            headers={"Authorization": "Bearer " + key},
                            trust_env=False,
                            timeout=20,
                        ) as restored_client:
                            original = request(
                                client, "GET", citations[0]["citation"]["document_url"]
                            )
                            copy_doc = request(
                                restored_client,
                                "GET",
                                citations[0]["citation"]["document_url"],
                            )
                            require(
                                original == copy_doc, "Restored citation is identical"
                            )
                            restored_hits = request(
                                restored_client,
                                "POST",
                                "/api/v1/knowledge/search",
                                json={
                                    "query": "独立进程回放验收",
                                    "knowledge_base_ids": [bases[0]],
                                },
                            )
                            require(
                                restored_hits["hits"],
                                "Restored search retains index and key",
                            )
                        report["checks"].append("live_backup_restore_http_verification")

                        first_doc = next(
                            doc for doc in docs if doc["knowledge_base_id"] == bases[0]
                        )
                        original_hit = next(
                            hit
                            for hit in citations
                            if hit["document_id"] == first_doc["id"]
                        )
                        revision = copy.deepcopy(records[0])
                        revision["source_revision"] = 2
                        revision["outcome"]["summary"] += "；追加第二版回放审核。"
                        request(
                            client,
                            "POST",
                            "/api/v1/records",
                            expected=202,
                            json=revision,
                            headers={"Idempotency-Key": "second-source-revision"},
                        )
                        until(settled, "revised draft and refinement")
                        revised_docs = request(admin, "GET", "/internal/v1/documents")
                        require(
                            len(revised_docs) == len(docs),
                            "New source revision must keep stable document identity",
                        )
                        current = next(
                            doc for doc in revised_docs if doc["id"] == first_doc["id"]
                        )
                        require(
                            current["source_revision"] == 2,
                            "Latest source revision attached",
                        )
                        require(
                            search(bases[0])["hits"][0]["document_version"]
                            == original_hit["document_version"],
                            "Old publication remains active during new draft",
                        )
                        request(
                            admin,
                            "POST",
                            f"/internal/v1/documents/{current['id']}/publish",
                            expected=202,
                            json={"revision": current["revision"]},
                        )
                        until(settled, "new publication")
                        new_hit = search(bases[0])["hits"][0]
                        require(
                            new_hit["document_version"]
                            > original_hit["document_version"],
                            "Publication version advanced",
                        )
                        request(
                            client,
                            "GET",
                            original_hit["citation"]["document_url"],
                            expected=410,
                        )
                        report["checks"].append("source_revision_atomic_publication")

                        with httpx.Client(
                            base_url=f"http://127.0.0.1:{mock_port}", trust_env=False
                        ) as control:
                            request(
                                control,
                                "POST",
                                "/control",
                                json={"embedding_failure": True},
                            )
                            degraded = search(bases[0])
                            require(
                                degraded["retrieval_mode"] == "keyword_only"
                                and degraded["hits"]
                                and degraded["warnings"],
                                "Embedding outage must visibly degrade to keyword retrieval",
                            )
                            current = request(
                                admin, "GET", f"/internal/v1/documents/{current['id']}"
                            )
                            version_path = f"/internal/v1/documents/{current['id']}/versions/{new_hit['document_version']}"
                            before_rebuild = request(admin, "GET", version_path)
                            rebuild = request(
                                admin,
                                "POST",
                                f"/internal/v1/documents/{current['id']}/reindex",
                                expected=202,
                                json={"revision": current["revision"]},
                            )

                            def failed_job(job_id):
                                jobs = request(admin, "GET", "/internal/v1/jobs")
                                return next(
                                    (
                                        job
                                        for job in jobs
                                        if job["id"] == job_id
                                        and job["status"] == "failed"
                                    ),
                                    None,
                                )

                            until(
                                lambda: failed_job(rebuild["job_id"]),
                                "failed reindex retry exhaustion",
                            )
                            require(
                                request(admin, "GET", version_path)["active_build_id"]
                                == before_rebuild["active_build_id"],
                                "Failed reindex must retain the original active generation",
                            )
                            request(control, "POST", "/control", json={})
                            request(
                                admin,
                                "POST",
                                f"/internal/v1/jobs/{rebuild['job_id']}/retry",
                            )
                            until(settled, "reindex retry")
                            after_rebuild = request(admin, "GET", version_path)
                            require(
                                after_rebuild["version"] == before_rebuild["version"]
                                and after_rebuild["content"]
                                == before_rebuild["content"]
                                and after_rebuild["active_build_id"]
                                != before_rebuild["active_build_id"],
                                "Reindex must switch only the active index generation",
                            )

                            current = request(
                                admin, "GET", f"/internal/v1/documents/{current['id']}"
                            )
                            edit = {
                                field: current[field]
                                for field in (
                                    "knowledge_base_id",
                                    "title",
                                    "content",
                                    "revision",
                                )
                            }
                            edit["content"] += "\n\n人工验收草稿；此修改尚未发布。"
                            current = request(
                                admin,
                                "PATCH",
                                f"/internal/v1/documents/{current['id']}/draft",
                                json=edit,
                            )
                            request(
                                control, "POST", "/control", json={"ai_failure": True}
                            )
                            failed_refine = request(
                                admin,
                                "POST",
                                f"/internal/v1/documents/{current['id']}/refine",
                                expected=202,
                                json={"revision": current["revision"]},
                            )
                            until(lambda: failed_job(failed_refine["id"]), "AI failure")
                            expected_failed_jobs.add(failed_refine["id"])
                            require(
                                request(
                                    admin,
                                    "GET",
                                    f"/internal/v1/documents/{current['id']}",
                                )["content"]
                                == current["content"],
                                "Failed AI refinement must preserve the manual draft",
                            )
                            require(
                                request(
                                    client, "GET", new_hit["citation"]["document_url"]
                                )["content"]
                                == before_rebuild["content"],
                                "Failed AI refinement must preserve the active publication",
                            )
                            request(control, "POST", "/control", json={})
                            current = request(
                                admin, "GET", f"/internal/v1/documents/{current['id']}"
                            )
                            request(
                                admin,
                                "POST",
                                f"/internal/v1/documents/{current['id']}/refine",
                                expected=202,
                                json={"revision": current["revision"]},
                            )
                            # A new request starts from the latest draft; failure history remains auditable.
                            until(settled, "AI explicit regeneration")
                        report["checks"].append("embedding_outage_keyword_fallback")
                        report["checks"].extend(
                            [
                                "reindex_failure_keeps_active_generation",
                                "reindex_explicit_retry_atomic_switch",
                                "ai_failure_keeps_draft_and_publication",
                                "ai_explicit_regeneration",
                            ]
                        )
                        request(
                            admin,
                            "POST",
                            f"/internal/v1/documents/{current['id']}/unpublish",
                        )
                        require(
                            not search(bases[0])["hits"],
                            "Unpublished document remains hidden",
                        )
                        request(
                            client,
                            "GET",
                            new_hit["citation"]["document_url"],
                            expected=410,
                        )
                        request(
                            admin, "DELETE", f"/internal/v1/documents/{current['id']}"
                        )
                        sources = request(admin, "GET", "/internal/v1/records")
                        for source in sources:
                            if source["knowledge_base_id"] == bases[0]:
                                request(
                                    admin,
                                    "DELETE",
                                    f"/internal/v1/records/{source['id']}",
                                )
                        require(
                            not search(bases[0])["hits"],
                            "Deleted source/document remains hidden",
                        )
                        report["checks"].append("unpublish_citation_revoke_delete")
        report["status"] = "passed"
    except Exception as exc:
        report["status"] = "failed"
        # Do not print provider bodies, database credentials or source content.
        report["failure"] = (
            str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        )
    (directory / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--with-core-logs",
        action="store_true",
        help="Explicitly read local archived Core task evidence (never remote AI)",
    )
    parser.add_argument("--core-data", type=Path, default=default_core_data())
    parser.add_argument(
        "--limit", type=int, choices=range(1, 11), default=8, metavar="1..10"
    )
    parser.add_argument(
        "--postgres-admin-url-env",
        help="Environment variable containing a LOCAL PG admin URL; creates/drops only generated test DBs",
    )
    parser.add_argument("--serve-mock", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve_mock:
        serve_mock(args.serve_mock)
        return 0
    return run_workflow(args)


if __name__ == "__main__":
    raise SystemExit(main())
