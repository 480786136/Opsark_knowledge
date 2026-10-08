from types import SimpleNamespace

import pytest

from knowledge.indexing import chunk_content, vector_entity


def test_code_fence_fits_in_one_chunk_even_near_previous_boundary():
    content = (
        "# 方法\n" + "intro " * 8 + "\n```sh\n# not a heading\nprintf ok\n```\n验证"
    )
    rows = chunk_content(content, limit=70)
    block = next(text for text, _, _ in rows if "printf ok" in text)
    assert "```sh\n# not a heading\nprintf ok\n```" in block
    for text, first, last in rows:
        assert len(text) <= 70
        assert text == "\n".join(content.splitlines()[first - 1 : last])


def test_oversized_block_retains_every_line_in_order():
    content = "## 检查\n~~~sh\n" + "echo test\n" * 30 + "~~~\n## 边界\n未确认"
    rows = chunk_content(content, limit=80)
    assert "\n".join(text for text, _, _ in rows) == content
    assert all(len(text) <= 80 for text, _, _ in rows)
    assert all(
        text == "\n".join(content.splitlines()[first - 1 : last])
        for text, first, last in rows
    )


def test_vector_export_uses_active_reindex_fingerprint_not_original_version():
    document = SimpleNamespace(
        id="d", published_version=1, knowledge_base_id="kb", source_record_id="new"
    )
    version = SimpleNamespace(
        id="v",
        document_id="d",
        version=1,
        active_build_id="b",
        index_version="old",
        source_record_ids=["original"],
        environment="",
        software_names=[],
        title="Nginx",
    )
    chunk = SimpleNamespace(
        id="c",
        version_id="v",
        build_id="b",
        content="check",
        line_start=1,
        line_end=1,
        embedding=None,
    )
    build = SimpleNamespace(
        id="b", version_id="v", status="ready", index_version="reindexed"
    )
    with pytest.raises(ValueError, match="INDEX_BUILD_REQUIRED"):
        vector_entity(chunk, version, document)
    entity = vector_entity(chunk, version, document, build)
    assert entity["index_version"] == "reindexed" and entity["source_record_ids"] == [
        "original"
    ]
