"""Inspect and replay archived Core evidence; dry run and loopback-only by default.

This adapter deliberately does not infer execution success from terminal text.
The archive lacks the complete typed task result used by Core's UI exporter.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from knowledge.schemas import RecordInput  # noqa: E402


def default_core_data() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/com.opsark.desktop"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", "")) / "com.opsark.desktop"
    return (
        Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
        / "com.opsark.desktop"
    )


def local_endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") not in {"", "/api/v1"}
    ):
        raise ValueError(
            "Replay accepts only a loopback HTTP endpoint without credentials"
        )
    return value.rstrip("/").removesuffix("/api/v1")


def redact(text: str, private_values: set[str] | None = None) -> str:
    value = str(text)
    for item in sorted(private_values or set(), key=len, reverse=True):
        if len(item) >= 3:
            value = value.replace(item, "[私有标识]")
    patterns = [
        (
            r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----[\s\S]*?(?:-----END (?:[A-Z ]+ )?PRIVATE KEY-----|$)",
            "[私钥已移除]",
        ),
        (r"\b(?:authorization|cookie)\s*:[^\r\n]*", "[鉴权头已移除]"),
        (
            r"\b(?:password|passwd|pwd|api[_-]?key|access[_-]?token|secret)\s*[:=]\s*(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s;&]+)",
            "[凭据已移除]",
        ),
        (r"\bBearer\s+\S+", "[令牌已移除]"),
        (r"\b(?:sk-|okk_)[A-Za-z0-9_-]{12,}", "[API 凭据已移除]"),
        (r"https?://[^\s<>\"']+", "[URL 已移除]"),
        (r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[IP 已移除]"),
        (r"(?<!\w)(?:[0-9a-f]{0,4}:){2,}[0-9a-f:]{0,39}(?!\w)", "[IP 已移除]"),
        (r"[\w.+-]+@[\w.-]+", "[用户与主机已移除]"),
        (
            r"\b(?:[a-z0-9-]+\.)+(?:com|cn|net|org|io|dev|local|internal)\b",
            "[主机已移除]",
        ),
        (r"/(?:Users|home)/[^/\s]+", "/home/[用户]"),
        (r"\b[A-Za-z0-9+/=_-]{40,}\b", "[长标识已移除]"),
        (r"\x1b\[[0-?]*[ -/]*[@-~]", ""),
    ]
    for pattern, replacement in patterns:
        value = re.sub(pattern, replacement, value, flags=re.IGNORECASE)
    return value


def excerpt(text: str, limit: int = 2000) -> str:
    if len(text) <= limit:
        return text
    marker = "\n[历史输出节选，中间内容已省略]\n"
    budget = limit - len(marker)
    return text[: budget // 2] + marker + text[-(budget - budget // 2) :]


def collect_records(root: Path, limit: int = 8) -> tuple[list[dict], dict]:
    """Read only the known task/evidence directories; never model-call logs/key stores."""
    if not root.is_dir():
        raise ValueError("Core application data directory does not exist")
    stats: Counter = Counter()
    tasks: dict[str, dict] = {}
    private_values: set[str] = set()
    for file in sorted((root / "logs/tasks").glob("task-*/events.jsonl")):
        if not file.resolve().is_relative_to(root.resolve()) or file.is_symlink():
            stats["outside_directory_skipped"] += 1
            continue
        stats["task_log_files"] += 1
        with file.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if len(line) > 8 * 1024 * 1024:
                    stats["oversized_events"] += 1
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    stats["malformed_events"] += 1
                    continue
                if not isinstance(event, dict):
                    stats["malformed_events"] += 1
                    continue
                stats["events"] += 1
                task_id = event.get("taskId")
                if not isinstance(task_id, str) or not task_id:
                    continue
                task_hash = hashlib.sha256(task_id.encode()).hexdigest()
                if file.parent.name != "task-" + task_hash:
                    stats["task_identity_mismatch"] += 1
                    continue
                tasks[task_hash] = {
                    "title": event.get("taskTitle") or "历史运维任务",
                    "evidence": [],
                }
                for key in ("taskId", "serverId", "serverName"):
                    if isinstance(event.get(key), str):
                        private_values.add(event[key])
    for file in sorted((root / "evidence").glob("*/*.json")):
        if not file.resolve().is_relative_to(root.resolve()) or file.is_symlink():
            stats["outside_directory_skipped"] += 1
            continue
        stats["evidence_files"] += 1
        if file.stat().st_size > 16 * 1024 * 1024:
            stats["oversized_evidence"] += 1
            continue
        raw = file.read_bytes()
        if hashlib.sha256(raw).hexdigest() != file.stem:
            stats["integrity_failures"] += 1
            continue
        try:
            item = json.loads(raw)
        except ValueError:
            stats["malformed_evidence"] += 1
            continue
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("text"), str)
            or not item["text"].strip()
        ):
            stats["empty_evidence"] += 1
            continue
        if file.parent.name not in tasks:
            stats["unlinked_evidence"] += 1
            continue
        tasks[file.parent.name]["evidence"].append(item)
        stats["linked_evidence"] += 1
        stats["partial_capture"] += bool(item.get("capturedPartial"))
    candidates = [(key, task) for key, task in tasks.items() if task["evidence"]]
    candidates.sort(
        key=lambda pair: max(
            str(x.get("collectedAt", "")) for x in pair[1]["evidence"]
        ),
        reverse=True,
    )
    stats["tasks_with_evidence"] = len(candidates)
    records = []
    for task_hash, task in candidates[:limit]:
        items = sorted(
            task["evidence"], key=lambda item: str(item.get("collectedAt", ""))
        )
        steps = []
        for index, item in enumerate(items[-30:], 1):
            safe_output = redact(item["text"], private_values)
            notes = ["Core 历史归档输出；无可验证的退出码、完整步骤状态或独立校验信息"]
            if item.get("capturedPartial"):
                notes.append("原始采集不完整")
            if len(safe_output) > 2000:
                notes.append("回放仅保留输出节选")
            steps.append(
                {
                    "step_id": f"archive-step-{index}",
                    "description": f"历史证据片段 {index}（不能证明当前状态）",
                    "execution_status": "unknown",
                    "validation_status": "unknown",
                    "evidence": [
                        {
                            "evidence_id": f"archive-{index}",
                            "kind": "observation",
                            "summary": "；".join(notes),
                            "excerpt": excerpt(safe_output),
                        }
                    ],
                }
            )
        occurred_at = items[-1].get("collectedAt")
        try:
            if isinstance(occurred_at, (float, int)):
                occurred_at = datetime.fromtimestamp(
                    occurred_at / 1000, timezone.utc
                ).isoformat()
            parsed = datetime.fromisoformat(str(occurred_at).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                occurred_at = parsed.replace(tzinfo=timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            occurred_at = "1970-01-01T00:00:00+00:00"
            stats["missing_timestamps"] += 1
        title = redact(task["title"], private_values)[:200] or "历史运维任务"
        record = {
            "schema_version": "1.0",
            "source_record_id": "replay-" + task_hash[:32],
            "source_revision": 1,
            "knowledge_base_id": "dry-run",
            "record_type": "task_result",
            "title": title,
            "occurred_at": occurred_at,
            "problem": title,
            "steps": steps,
            "context": {},
            "tags": ["本机历史回放"],
            "outcome": {
                "status": "partial",
                "summary": (
                    f"历史归档回放，共 {len(steps)}/{len(items)} 个证据片段。"
                    "未恢复完整计划、执行状态与独立验收，不认定整体任务成功；仅用于人工核对历史资料及本机流程验收。"
                ),
            },
            "redaction": {
                "client_applied": True,
                "ruleset_version": "local-archive-replay-v1",
            },
        }
        records.append(RecordInput.model_validate(record).model_dump(mode="json"))
    stats["selected_records"] = len(records)
    stats["selected_steps"] = sum(len(record["steps"]) for record in records)
    return records, dict(stats)


def upload_records(records: list[dict], endpoint: str, key: str, base_id: str) -> dict:
    endpoint = local_endpoint(endpoint)
    results = Counter()
    with httpx.Client(
        base_url=endpoint, trust_env=False, timeout=20, follow_redirects=False
    ) as client:
        for record in records:
            body = {**record, "knowledge_base_id": base_id}
            encoded = json.dumps(
                body, ensure_ascii=False, separators=(",", ":")
            ).encode()
            response = client.post(
                "/api/v1/records",
                content=encoded,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "Idempotency-Key": hashlib.sha256(encoded).hexdigest(),
                },
            )
            if response.status_code != 202:
                raise RuntimeError(
                    f"Replay upload failed: HTTP {response.status_code}; inspect local service diagnostics"
                )
            results["accepted"] += 1
            results["duplicates"] += bool(response.json().get("duplicate"))
    return dict(results)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-data", type=Path, default=default_core_data())
    parser.add_argument(
        "--limit", type=int, choices=range(1, 21), default=8, metavar="1..20"
    )
    parser.add_argument(
        "--write-fixtures",
        action="store_true",
        help="Write redacted records to a NEW private system temporary directory",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Explicitly upload to a local test service; never automatically publish",
    )
    parser.add_argument("--endpoint", default="http://127.0.0.1:8002")
    parser.add_argument("--knowledge-base-id")
    args = parser.parse_args()
    records, report = collect_records(args.core_data, args.limit)
    report["mode"] = "dry_run"
    if args.write_fixtures:
        directory = Path(tempfile.mkdtemp(prefix="opsark-private-replay-"))
        for index, record in enumerate(records, 1):
            file = directory / f"record-{index:02d}.json"
            with file.open("x", encoding="utf-8") as output:
                os.chmod(file, 0o600)
                json.dump(record, output, ensure_ascii=False, indent=2)
        report["private_fixture_directory"] = str(directory)
    if args.upload:
        key = os.environ.get("REPLAY_KNOWLEDGE_KEY", "")
        if not key or not args.knowledge_base_id:
            parser.error(
                "--upload needs REPLAY_KNOWLEDGE_KEY and --knowledge-base-id for an isolated test base"
            )
        report.update(
            upload_records(records, args.endpoint, key, args.knowledge_base_id)
        )
        report["mode"] = "uploaded_not_published"
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
