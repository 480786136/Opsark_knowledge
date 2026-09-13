from types import SimpleNamespace

import pytest

from knowledge.evidence import prepare_evidence, validate_claims
from knowledge.indexing import chunk_content, chunk_id, vector_entity
from knowledge.refinement import Claim


def payload():
    return {
        "context": {},
        "outcome": {"status": "partial"},
        "problem": "检查进程",
        "steps": [
            {
                "step_id": "s",
                "command": "pgrep java 2>/dev/null",
                "execution_status": "succeeded",
                "validation_status": "not_run",
                "evidence": [
                    {
                        "evidence_id": "expected-1",
                        "kind": "observation",
                        "summary": "预期验收标准",
                    },
                    {
                        "evidence_id": "result-1",
                        "kind": "command_result",
                        "summary": "无输出",
                    },
                ],
            }
        ],
    }


@pytest.mark.parametrize(
    "ref", ["s/expected-1", "record/context", "record/outcome", "s/command"]
)
def test_non_observed_sources_cannot_support_facts(ref):
    prepared = prepare_evidence(payload())
    claim = Claim(section="验收标准", kind="事实", text="已确认成功", refs=[ref])
    with pytest.raises(ValueError, match="AI_EVIDENCE_TYPE_MISMATCH"):
        validate_claims([claim], prepared["evidence"])


def test_typed_expectations_reports_and_warnings():
    prepared = prepare_evidence(payload())
    validate_claims(
        [
            Claim(
                section="验收标准",
                kind="验收要求",
                text="应输出结果",
                refs=["s/expected-1"],
            ),
            Claim(
                section="结论边界",
                kind="来源声明",
                text="客户端报告部分完成",
                refs=["record/outcome"],
            ),
            Claim(section="本次发现", kind="事实", text="无输出", refs=["s/result-1"]),
        ],
        prepared["evidence"],
    )
    assert any("错误信息抑制" in x for x in prepared["quality_warnings"])
    assert any("环境字段缺失" in x for x in prepared["quality_warnings"])


def test_sections_and_long_lines_keep_exact_citations():
    content = "# 标题\n## 方法\n" + "x" * 4000 + "\n## 边界\n未知"
    rows = chunk_content(content)
    assert all(len(text) <= 1800 for text, _, _ in rows)
    assert [(start, end) for text, start, end in rows if text.startswith("x")] == [
        (3, 3)
    ] * 3
    assert rows[-1] == ("## 边界\n未知", 4, 5)


def test_partial_runtime_does_not_hide_unknown_scope():
    data = payload()
    data["context"] = {"runtime": {"os": "Linux", "shell": " "}}
    warnings = prepare_evidence(data)["quality_warnings"]
    runtime_warning = next(x for x in warnings if "运行环境细项" in x)
    assert "scope" in runtime_warning and "shell" in runtime_warning
    assert "os" not in runtime_warning


def test_claimed_pass_with_only_expectations_has_no_independent_validation():
    data = payload()
    data["steps"][0]["validation_status"] = "passed"
    # Even a legacy validation-typed expectation is not an actual validation.
    data["steps"][0]["evidence"][0]["kind"] = "validation"
    assert any(
        "没有独立校验证据" in x for x in prepare_evidence(data)["quality_warnings"]
    )
    data["steps"][0]["evidence"].append(
        {
            "evidence_id": "check",
            "kind": "validation",
            "summary": "独立读取结果",
            "excerpt": "OK",
        }
    )
    assert not any(
        "没有独立校验证据" in x for x in prepare_evidence(data)["quality_warnings"]
    )


def test_step_description_preserved_as_statement_not_observation():
    data = payload()
    data["steps"][0]["description"] = "预期检查全部服务"
    prepared = prepare_evidence(data)
    assert prepared["evidence"]["s/description"]["value"] == "预期检查全部服务"
    with pytest.raises(ValueError, match="AI_EVIDENCE_TYPE_MISMATCH"):
        validate_claims(
            [
                Claim(
                    section="本次发现",
                    kind="事实",
                    text="全部服务已检查",
                    refs=["s/description"],
                )
            ],
            prepared["evidence"],
        )


def test_chunk_identity_is_repeatable_and_version_scoped():
    assert chunk_id("v1", 0, "a") == chunk_id("v1", 0, "a")
    assert (
        len(
            {
                chunk_id("v1", 0, "a"),
                chunk_id("v2", 0, "a"),
                chunk_id("v1", 1, "a"),
                chunk_id("v1", 0, "b"),
            }
        )
        == 4
    )


def test_vector_export_requires_matching_published_sql_version():
    doc = SimpleNamespace(
        id="d", published_version=2, knowledge_base_id="kb", source_record_id="r"
    )
    version = SimpleNamespace(
        id="v",
        document_id="d",
        version=2,
        index_version="idx",
        environment="prod",
        software_names=["java"],
        title="检查",
    )
    chunk = SimpleNamespace(
        id="c", version_id="v", content="证据", line_start=1, line_end=1, embedding=None
    )
    entity = vector_entity(chunk, version, doc)
    assert entity["knowledge_base_id"] == "kb" and entity["document_version"] == 2
    doc.published_version = None
    with pytest.raises(ValueError, match="NOT_PUBLISHED"):
        vector_entity(chunk, version, doc)
