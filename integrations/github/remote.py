"""Validate the fields used to establish remote identity and coverage."""

from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
    model_validator,
)

from integrations.github.errors import CollectionError

SHA = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]


def utc(value: str) -> str:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("remote timestamps must include a timezone")
    return parsed.astimezone(UTC).isoformat(timespec="microseconds")


class RemoteModel(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


def parse[T: RemoteModel](model: type[T], value: JsonValue) -> T:
    try:
        return model.model_validate(value)
    except ValidationError:
        pass
    # Do not retain payload-bearing ValidationError in the exception chain.
    raise CollectionError("Invalid or missing remote collection fields")


class Repository(RemoteModel):
    id: int = Field(gt=0)
    full_name: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    default_branch: str = Field(min_length=1)

    @field_validator("full_name")
    @classmethod
    def safe_repository(cls, value: str) -> str:
        if any(part in (".", "..") for part in value.split("/")):
            raise ValueError("invalid repository path")
        return value.lower()


class CommitIdentity(RemoteModel):
    sha: SHA


class CommitContent(RemoteModel):
    message: str
    author: dict[str, JsonValue]
    committer: dict[str, JsonValue]
    tree: CommitIdentity


class CommitRecord(CommitIdentity):
    html_url: str = Field(min_length=1)
    commit: CommitContent
    parents: list[CommitIdentity]


class Comparison(RemoteModel):
    status: str
    ahead_by: int = Field(ge=0)
    behind_by: int = Field(ge=0)
    total_commits: int = Field(ge=0)
    base_commit: CommitIdentity
    merge_base_commit: CommitIdentity
    commits: list[CommitRecord]


class ReleaseRecord(RemoteModel):
    id: int = Field(gt=0)
    tag_name: str
    name: str | None
    body: str | None
    created_at: str
    published_at: str
    prerelease: bool
    html_url: str = Field(min_length=1)

    @field_validator("created_at", "published_at")
    @classmethod
    def normalize_time(cls, value: str) -> str:
        return utc(value)


class ReleaseVisibility(RemoteModel):
    draft: bool


class IssueLabel(RemoteModel):
    name: str = Field(min_length=1)


class IssueRecord(RemoteModel):
    id: int = Field(gt=0)
    number: int = Field(gt=0)
    title: str
    body: str | None
    state: Literal["open", "closed"]
    labels: list[IssueLabel]
    comments: int = Field(ge=0)
    created_at: str
    updated_at: str
    closed_at: str | None
    html_url: str = Field(min_length=1)

    @field_validator("created_at", "updated_at", "closed_at")
    @classmethod
    def normalize_time(cls, value: str | None) -> str | None:
        return None if value is None else utc(value)

    def content(self) -> dict[str, JsonValue]:
        value = self.model_dump(mode="json")
        value["labels"] = sorted({label.name for label in self.labels})
        return value

    @model_validator(mode="after")
    def chronological(self) -> Self:
        if self.created_at > self.updated_at or (
            self.closed_at is not None and not self.created_at <= self.closed_at <= self.updated_at
        ):
            raise ValueError("inconsistent Issue timestamps")
        return self


class TreeEntry(RemoteModel):
    path: str = Field(min_length=1)
    mode: str
    type: str
    sha: SHA

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        if any(part in ("", ".", "..") for part in value.split("/")):
            raise ValueError("invalid tree path")
        return value

    @field_validator("mode")
    @classmethod
    def supported_mode(cls, value: str) -> str:
        if value not in {"100644", "100755", "120000", "160000", "040000"}:
            raise ValueError("unknown Git tree mode")
        return value


class Tree(RemoteModel):
    sha: SHA
    truncated: bool
    tree: list[TreeEntry]
