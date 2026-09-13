from types import SimpleNamespace

from knowledge.quality import prepare_draft
from knowledge.schemas import RecordInput


def source(steps, status="partial"):
    payload = RecordInput.model_validate(
        {
            "schema_version": "1.0",
            "source_record_id": "task-quality",
            "source_revision": 3,
            "knowledge_base_id": "kb",
            "record_type": "task_result",
            "title": "获取仓库并验收",
            "occurred_at": "2026-09-08T14:14:18Z",
            "problem": "获取仓库到目标目录",
            "steps": steps,
            "outcome": {"status": status, "summary": "Core 声明完成，需核对证据"},
            "redaction": {"client_applied": True, "ruleset_version": "core-upload-v2"},
        }
    ).model_dump(mode="json")
    return SimpleNamespace(
        payload=payload,
        id="record-quality",
        source_record_id="task-quality",
        source_revision=3,
    )


def test_evidence_based_draft_keeps_verification_and_provenance():
    record = source(
        [
            {
                "step_id": "clone",
                "description": "先检查目标目录不存在，再获取仓库",
                "command": "git clone https://example.com/repo.git /opt/repo",
                "execution_status": "succeeded",
                "validation_status": "passed",
                "evidence": [
                    {
                        "evidence_id": "main",
                        "kind": "command_result",
                        "summary": "退出码=0",
                        "excerpt": "克隆完成",
                    },
                    {
                        "evidence_id": "check",
                        "kind": "validation",
                        "summary": "独立校验通过",
                        "excerpt": "FETCH_OK",
                    },
                ],
            }
        ],
        "succeeded",
    )
    content = prepare_draft(record)
    for text in [
        "适用范围与前提",
        "操作步骤",
        "验收标准与校验证据",
        "FETCH_OK",
        "关键执行输出",
        "来源修订：3",
        "knowledge-draft-v2",
    ]:
        assert text in content
    assert "未由知识服务重新执行验证" in content
    assert "环境：来源未提供" in content
    assert "没有输出摘录" not in content


def test_legacy_or_failed_records_are_not_rewritten_as_verified_success():
    record = source(
        [
            {
                "step_id": "legacy",
                "description": "旧步骤",
                "execution_status": "failed",
                "validation_status": "unknown",
                "evidence": [],
            }
        ]
    )
    content = prepare_draft(record)
    assert "命令未提供" in content
    assert "没有输出摘录" in content
    assert "不能将该尝试写成成功经验" in content
    assert "独立校验状态：未知" in content
    assert "未调用 AI 模型" in content


def test_claimed_validation_without_evidence_is_flagged():
    content = prepare_draft(
        source(
            [
                {
                    "step_id": "x",
                    "description": "检查",
                    "execution_status": "succeeded",
                    "validation_status": "passed",
                    "evidence": [],
                }
            ],
            "succeeded",
        )
    )
    assert "缺少独立校验证据" in content
