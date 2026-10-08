"""Deterministic Chinese lexical ranking and bounded PostgreSQL candidates."""

import jieba
from pgvector.sqlalchemy import Vector
from sqlalchemy import case, cast, exists, func, literal, literal_column, select
from sqlalchemy.dialects.postgresql import JSONB

from .models import Chunk, Document, DocumentVersion, IndexBuild, KnowledgeBase

STOP_WORDS = frozenset(
    [
        "如何",
        "怎么",
        "什么",
        "是否",
        "可以",
        "一个",
        "这个",
        "那个",
        "我们",
        "你们",
        "进行",
        "关于",
        "为什么",
        "需要",
        "应该",
        "哪些",
        "查询",
        "请问",
        "的",
        "了",
        "和",
        "是",
        "在",
        "有",
        "与",
        "为",
        "及",
        "到",
        "用",
        "把",
        "将",
        "对",
        "不",
        "吗",
    ]
)


def query_terms(query):
    terms = {word.lower().strip() for word in jieba.cut_for_search(query)}
    return sorted(
        (
            word
            for word in terms
            if word not in STOP_WORDS
            and len(word) >= 2
            and any(c.isalnum() for c in word)
        ),
        key=lambda word: (-len(word), word),
    )[:32]


def searchable_text(title, content):
    return " ".join(jieba.cut_for_search(title + "\n" + content))


def keyword_rank(query, terms, title, content):
    title, content = title.lower(), content.lower()
    score = sum(len(word) * (3 * (word in title) + (word in content)) for word in terms)
    phrase = query.strip().lower()
    if len(phrase) >= 2 and phrase in title + " " + content:
        score += 2 * len(phrase)
    return score


def filter_candidates(statement, filters, exact_paths, dialect):
    """Apply hard applicability constraints before either ranked candidate LIMIT.

    JSON content and paths are bound values. Never interpolate user values into
    SQL or interpret SQL LIKE wildcards in an exact path.
    """
    if filters.environment:
        statement = statement.where(DocumentVersion.environment == filters.environment)
    original = DocumentVersion.title + literal(" ") + Chunk.content
    for path in exact_paths:
        position = (
            func.strpos(original, path)
            if dialect == "postgresql"
            else func.instr(original, path)
        )
        statement = statement.where(position > 0)
    if filters.software_names:
        if dialect == "postgresql":
            statement = statement.where(
                cast(DocumentVersion.software_names, JSONB).contains(
                    filters.software_names
                )
            )
        else:
            names = func.json_each(DocumentVersion.software_names).table_valued("value")
            for name in filters.software_names:
                statement = statement.where(
                    exists(
                        select(literal(1))
                        .select_from(names)
                        .where(names.c.value == name)
                    )
                )
    for software in filters.software:
        if dialect == "postgresql":
            raw = cast(DocumentVersion.context["software"], JSONB)
            array = case(
                (func.jsonb_typeof(raw) == "array", raw),
                else_=cast(literal("[]"), JSONB),
            )
            items = func.jsonb_array_elements(array).table_valued("value")
            item = cast(items.c.value, JSONB)
            name, version = item["name"].astext, item["version"].astext
        else:
            items = func.json_each(DocumentVersion.context, "$.software").table_valued(
                "value"
            )
            name = func.json_extract(items.c.value, "$.name")
            version = func.json_extract(items.c.value, "$.version")
        clause = func.lower(name) == software.name.lower()
        if software.version:
            clause = clause & (func.lower(version) == software.version.lower())
        statement = statement.where(
            exists(select(literal(1)).select_from(items).where(clause))
        )
    return statement


def postgres_candidates(db, statement, terms, query_vector, fingerprint, capacity=300):
    """Push filtering/ranking into PostgreSQL; fetch at most 2*capacity bodies."""
    vector = func.to_tsvector(literal_column("'simple'"), Chunk.search_text)
    query = None
    for word in terms:
        term = func.plainto_tsquery(literal_column("'simple'"), word)
        query = term if query is None else query.op("||")(term)
    lexical = (
        list(
            db.execute(
                statement.where(vector.op("@@")(query))
                .order_by(func.ts_rank_cd(vector, query).desc(), Chunk.id)
                .limit(capacity)
            )
        )
        if query is not None
        else []
    )
    semantic = []
    if query_vector is not None:
        # CASE guards mixed-dimensional history even if the planner reorders WHERE.
        distance = case(
            (
                func.vector_dims(Chunk.embedding) == len(query_vector),
                Chunk.embedding.op("<=>")(literal(query_vector, type_=Vector())),
            ),
            else_=2.0,
        )
        semantic = list(
            db.execute(
                statement.join(IndexBuild, IndexBuild.id == Chunk.build_id)
                .where(
                    IndexBuild.index_version == fingerprint,
                    Chunk.embedding.is_not(None),
                    distance < 0.75,
                )
                .order_by(distance, Chunk.id)
                .limit(capacity)
            )
        )
    unique = {row[0].id: row for row in [*lexical, *semantic]}
    return list(unique.values()), len(lexical) == capacity or len(semantic) == capacity


def snapshot_still_public(db, document_id, version_id, build_id, base_ids):
    """Revalidate after external embedding calls, avoiding stale session objects."""
    return (
        db.scalar(
            select(Document.id)
            .join(DocumentVersion)
            .join(KnowledgeBase, KnowledgeBase.id == Document.knowledge_base_id)
            .where(
                Document.id == document_id,
                DocumentVersion.id == version_id,
                Document.published_version == DocumentVersion.version,
                DocumentVersion.active_build_id == build_id,
                Document.knowledge_base_id.in_(base_ids),
                KnowledgeBase.enabled.is_(True),
                Document.superseded_by.is_(None),
                Document.status.notin_(["deleted", "rejected"]),
            )
        )
        is not None
    )
