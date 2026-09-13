from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

Short = Annotated[str, StringConstraints(min_length=1, max_length=128)]
Tag = Annotated[str, StringConstraints(min_length=1, max_length=40)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Software(Strict):
    name: Tag
    version: str = Field(default="", max_length=100)


class Context(Strict):
    project_ref: str = Field(default="", max_length=128)
    server_ref: str = Field(default="", max_length=128)
    environment: str = Field(default="", max_length=100)
    software: list[Software] = Field(default_factory=list, max_length=20)
    runtime: dict[
        Literal["os", "shell", "scope", "privilege", "visibility"],
        Annotated[str, StringConstraints(max_length=200)],
    ] = Field(default_factory=dict, max_length=5)


class Evidence(Strict):
    evidence_id: Short
    kind: Literal["command_result", "validation", "observation", "expectation"]
    summary: str = Field(max_length=1000)
    excerpt: str = Field(default="", max_length=2000)


class Step(Strict):
    step_id: Short
    description: str = Field(max_length=2000)
    command: str = Field(default="", max_length=4000)
    execution_status: Literal["succeeded", "failed", "blocked", "not_run", "unknown"]
    validation_status: Literal["passed", "failed", "unknown", "not_run"]
    evidence: list[Evidence] = Field(default_factory=list, max_length=5)


class Outcome(Strict):
    status: Literal["succeeded", "failed", "partial", "unknown"]
    summary: str = Field(min_length=1, max_length=4000)


class Redaction(Strict):
    client_applied: Literal[True]
    ruleset_version: Short


class RecordInput(Strict):
    schema_version: Literal["1.0"]
    source_record_id: Short
    source_revision: int = Field(ge=1, strict=True)
    knowledge_base_id: Short
    record_type: Literal["task_result", "incident_note", "manual_note"]
    title: str = Field(min_length=1, max_length=200)
    occurred_at: AwareDatetime
    context: Context = Field(default_factory=Context)
    problem: str = Field(min_length=1, max_length=8000)
    steps: list[Step] = Field(default_factory=list, max_length=30)
    outcome: Outcome
    tags: list[Tag] = Field(default_factory=list, max_length=20)
    redaction: Redaction


class BaseInput(Strict):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    enabled: bool = True


class KeyInput(Strict):
    name: str = Field(min_length=1, max_length=100)
    installation_id: Short
    knowledge_base_ids: list[Short] = Field(min_length=1, max_length=20)
    scopes: list[Literal["records:write", "records:read", "knowledge:read"]] = Field(
        min_length=1, max_length=3
    )
    expires_days: int = Field(default=90, ge=1, le=365)


class DocumentInput(Strict):
    knowledge_base_id: Short
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=500000)
    tags: list[Tag] = Field(default_factory=list, max_length=20)
    environment: str = Field(default="", max_length=100)
    software_names: list[Tag] = Field(default_factory=list, max_length=20)


class DraftInput(DocumentInput):
    revision: int = Field(ge=1)


class RevisionInput(Strict):
    revision: int = Field(ge=1)


class SearchFilters(Strict):
    environment: str | None = Field(default=None, max_length=100)
    software_names: list[Tag] = Field(default_factory=list, max_length=20)


class SearchInput(Strict):
    query: str = Field(min_length=1, max_length=2000)
    knowledge_base_ids: list[Short] = Field(min_length=1, max_length=20)
    filters: SearchFilters = Field(default_factory=SearchFilters)
    top_k: int = Field(default=5, ge=1, le=10)
    max_content_chars: int = Field(default=6000, ge=500, le=12000)
