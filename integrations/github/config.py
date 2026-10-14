"""Strict, non-sensitive configuration and persisted refresh inputs."""

import json
from pathlib import Path
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from taskforge import ExecutionPolicy

DEFAULT_POLICY = ExecutionPolicy(
    max_retries=2, retry_base_seconds=5, retry_cap_seconds=60, timeout_seconds=300
)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class Budgets(Model):
    connect_seconds: float = Field(default=5, strict=True, gt=0, allow_inf_nan=False)
    write_seconds: float = Field(default=5, strict=True, gt=0, allow_inf_nan=False)
    pool_seconds: float = Field(default=5, strict=True, gt=0, allow_inf_nan=False)
    read_seconds: float = Field(default=20, strict=True, gt=0, allow_inf_nan=False)
    request_seconds: float = Field(default=30, strict=True, gt=0, allow_inf_nan=False)
    max_pages: int = Field(default=10, strict=True, ge=1)
    max_requests: int = Field(default=100, strict=True, ge=1)
    max_commits: int = Field(default=500, strict=True, ge=1, le=500)
    max_wait_seconds: float = Field(default=30, strict=True, ge=0, allow_inf_nan=False)


class IssueFilter(Model):
    labels: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    numbers: tuple[int, ...] = ()

    @field_validator("labels", "keywords")
    @classmethod
    def normalize_strings(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("filter values must be nonempty")
        return tuple(sorted({value.strip() for value in values}))

    @field_validator("numbers", mode="before")
    @classmethod
    def positive_numbers(cls, values: object) -> object:
        if not isinstance(values, (list, tuple)) or any(
            type(value) is not int or value < 1 for value in values
        ):
            raise ValueError("issue numbers must be positive integers")
        return tuple(sorted(set(values)))


class Source(Model):
    source_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    kind: Literal["project", "issues"]
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    scan_skills: bool = Field(default=False, strict=True)
    issue_filter: IssueFilter | None = None
    budgets: Budgets = Field(default_factory=Budgets)

    @field_validator("repository")
    @classmethod
    def normalize_repository(cls, value: str) -> str:
        if any(part in (".", "..") for part in value.split("/")):
            raise ValueError("repository must be owner/repo")
        return value.lower()

    @model_validator(mode="after")
    def compatible_options(self) -> Self:
        if self.kind == "project" and self.issue_filter is not None:
            raise ValueError("project sources cannot filter issues")
        if self.kind == "issues" and self.scan_skills:
            raise ValueError("issue sources cannot scan skills")
        return self

    def baseline_key(self) -> str:
        """Budgets do not change baseline identity."""
        return json.dumps(
            self.model_dump(mode="json", exclude={"budgets"}), sort_keys=True, separators=(",", ":")
        )


class GitHubConfig(Model):
    sources: tuple[Source, ...]

    @model_validator(mode="after")
    def unique_sources(self) -> Self:
        if len({source.source_id for source in self.sources}) != len(self.sources):
            raise ValueError("source_id must be unique")
        return self

    @classmethod
    def load(cls, path: Path | None = None) -> Self:
        return cls.model_validate_json(
            (path or Path(__file__).with_name("default-sources.json")).read_text(encoding="utf-8")
        )


class RefreshInput(Model):
    refresh_id: UUID
    state_directory: Path = Path(".taskforge/github")
    source: Source
    source_generation: int = Field(strict=True, ge=1)
    operation: Literal["refresh", "rebaseline"] = "refresh"

    @field_validator("state_directory")
    @classmethod
    def normalize_directory(cls, value: Path) -> Path:
        text = str(value)
        if text == ":memory:" or "\\" in text or text.startswith("//"):
            raise ValueError("state_directory must be a Linux local path")
        return value.expanduser().resolve()
