"""Launch an isolated real backend and run the opt-in live browser acceptance test."""

import json
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx

from smoke_workflow import (
    ROOT,
    free_port,
    processes,
    request,
    require,
    synthetic_record,
    until,
    wait_ready,
)


def main():
    directory = Path(tempfile.mkdtemp(prefix="opsark-private-browser-"))
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    password, token = secrets.token_urlsafe(24), secrets.token_urlsafe(40)
    environment = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "DATABASE_URL": "sqlite:///" + str(directory / "browser.db"),
        "KNOWLEDGE_SERVICE_TOKEN": token,
        "COOKIE_SECURE": "false",
        "ALLOWED_ORIGINS": url,
        "AI_REFINEMENT_ENABLED": "false",
        "AI_BASE_URL": "",
        "AI_API_KEY": "",
        "AI_MODEL": "",
        "EMBEDDING_BASE_URL": "",
        "EMBEDDING_API_KEY": "",
        "EMBEDDING_MODEL": "",
        "WORKER_POLL_SECONDS": "0.1",
        "WORKER_LOG_PATH": str(directory / "worker-diagnostics.log"),
        "KNOWLEDGE_LIVE_URL": url,
        "KNOWLEDGE_LIVE_USERNAME": "smoke_admin",
        "KNOWLEDGE_LIVE_PASSWORD": password,
    }
    report = {
        "artifact_directory": str(directory),
        "backend": "real isolated SQLite API and Worker",
        "provider_calls": "disabled",
    }
    try:
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
        # Bootstrap a test-only administrator in this newly created database.
        bootstrap = (
            "import os; from argon2 import PasswordHasher; from knowledge.db import SessionLocal; "
            "from knowledge.models import KnowledgeAdmin; "
            "db=SessionLocal(); db.add(KnowledgeAdmin(username=os.environ['KNOWLEDGE_LIVE_USERNAME'], "
            "password_hash=PasswordHasher().hash(os.environ['KNOWLEDGE_LIVE_PASSWORD']))); db.commit(); db.close()"
        )
        subprocess.run(
            [sys.executable, "-c", bootstrap],
            cwd=directory,
            env=environment,
            check=True,
            capture_output=True,
        )
        require(
            (ROOT / "web/dist/index.html").is_file(),
            "Build web/dist before running browser acceptance",
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
            start("worker", [sys.executable, "-m", "knowledge.processing"])
            wait_ready(url, api)
            with httpx.Client(
                base_url=url,
                headers={"Authorization": "Bearer " + token},
                trust_env=False,
                timeout=20,
            ) as admin:
                kb = request(
                    admin,
                    "POST",
                    "/internal/v1/knowledge-bases",
                    expected=201,
                    json={"name": "独立浏览器验收库"},
                )["id"]
                key = request(
                    admin,
                    "POST",
                    "/internal/v1/knowledge-keys",
                    expected=201,
                    json={
                        "name": "browser-fixture",
                        "installation_id": "browser-fixture",
                        "knowledge_base_ids": [kb],
                        "scopes": ["records:write", "records:read", "knowledge:read"],
                    },
                )["api_key"]
                record = synthetic_record()
                record["knowledge_base_id"] = kb
                record["title"] = "独立浏览器任务 " + secrets.token_hex(4)
                with httpx.Client(
                    base_url=url,
                    headers={"Authorization": "Bearer " + key},
                    trust_env=False,
                    timeout=20,
                ) as client:
                    request(
                        client,
                        "POST",
                        "/api/v1/records",
                        expected=202,
                        json=record,
                        headers={"Idempotency-Key": "browser-fixture-v1"},
                    )

                def draft_ready():
                    docs = request(admin, "GET", "/internal/v1/documents")
                    return docs[0] if docs and docs[0]["status"] == "draft" else None

                doc = until(draft_ready, "browser fixture draft")
                environment["KNOWLEDGE_LIVE_DOCUMENT_ID"] = doc["id"]
                result = subprocess.run(
                    ["npm", "--prefix", str(ROOT / "web"), "run", "test:live"],
                    cwd=ROOT,
                    env=environment,
                    capture_output=True,
                )
                (directory / "browser.log").write_bytes(result.stdout + result.stderr)
                require(
                    result.returncode == 0,
                    "Live browser test failed; inspect private browser.log",
                )
                final = request(admin, "GET", "/internal/v1/documents/" + doc["id"])
                require(
                    final["published_version"] is None,
                    "Browser scenario must end with an unpublished document",
                )
        report["status"] = "passed"
        report["checks"] = [
            "real_admin_login",
            "source_and_document_detail",
            "edit_publish_search_citation",
            "version_history",
            "unpublish",
            "manual_document_creator",
            "batch_review_publish_real_worker",
        ]
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
