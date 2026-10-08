"""Safety and semantic-boundary regressions for the local archive adapter."""

import hashlib
import json

import pytest

from replay_core_logs import collect_records, local_endpoint, redact


def fixture_archive(
    tmp_path, output="success does not prove the whole task succeeded", partial=False
):
    task_id = "test-task"
    digest = hashlib.sha256(task_id.encode()).hexdigest()
    events = tmp_path / "logs/tasks" / ("task-" + digest)
    events.mkdir(parents=True)
    (events / "events.jsonl").write_text(
        json.dumps(
            {
                "taskId": task_id,
                "taskTitle": "Archive fixture",
                "serverName": "private-host",
            }
        )
        + "\n[]\nnot-json\n"
    )
    evidence = tmp_path / "evidence" / digest
    evidence.mkdir(parents=True)
    record = {
        "text": output,
        "capturedPartial": partial,
        "collectedAt": "2026-09-23T00:00:00Z",
    }
    raw = json.dumps(record).encode()
    file = evidence / (hashlib.sha256(raw).hexdigest() + ".json")
    file.write_bytes(raw)
    return file


def test_archive_without_typed_execution_never_claims_success(tmp_path):
    fixture_archive(tmp_path)
    records, report = collect_records(tmp_path)
    assert report["malformed_events"] == 2
    assert records[0]["outcome"]["status"] == "partial"
    assert records[0]["steps"][0]["execution_status"] == "unknown"
    assert records[0]["steps"][0]["validation_status"] == "unknown"
    assert "success does not prove" in records[0]["steps"][0]["evidence"][0]["excerpt"]


def test_tampered_archive_is_not_uploaded(tmp_path):
    file = fixture_archive(tmp_path)
    file.write_text('{"text":"tampered"}')
    records, report = collect_records(tmp_path)
    assert records == [] and report["integrity_failures"] == 1


def test_partial_and_excerpt_boundaries_survive_redaction(tmp_path):
    fixture_archive(tmp_path, "private-host " + "运维输出\n" * 1000, partial=True)
    records, _ = collect_records(tmp_path)
    evidence = records[0]["steps"][0]["evidence"][0]
    assert "采集不完整" in evidence["summary"]
    assert "回放仅保留输出节选" in evidence["summary"]
    assert "中间内容已省略" in evidence["excerpt"]
    assert len(evidence["excerpt"]) <= 2000
    assert "private-host" not in evidence["excerpt"]


def test_adapter_does_not_follow_evidence_symlink(tmp_path):
    file = fixture_archive(tmp_path)
    other = file.with_name("outside.json")
    other.symlink_to(file)
    _, report = collect_records(tmp_path)
    assert report["outside_directory_skipped"] == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://192.0.2.1",
        "http://localhost.evil",
        "http://user:pass@127.0.0.1",
        "http://127.0.0.1/?token=x",
        "http://127.0.0.1/wrong",
    ],
)
def test_replay_refuses_nonlocal_or_credentialed_endpoints(url):
    with pytest.raises(ValueError):
        local_endpoint(url)


def test_redaction_removes_headers_credentials_and_local_identifiers():
    source = "Authorization: Bearer credential-value\npassword=private-value\nhttps://user:pass@example.com/p\n192.0.2.55 user@private-host /Users/individual/project\n"
    safe = redact(source)
    for secret in [
        "credential-value",
        "private-value",
        "example.com",
        "192.0.2.55",
        "private-host",
        "individual",
    ]:
        assert secret not in safe
