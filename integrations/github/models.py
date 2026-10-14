"""Validated writes for the independent business database."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal

from pydantic import Field, JsonValue, field_validator

from integrations.github.config import Model, RefreshInput


def canonical(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


class Entity(Model):
    repository_id: int = Field(strict=True, gt=0)
    kind: Literal["commit", "release", "issue"]
    remote_id: str = Field(min_length=1)
    content: dict[str, JsonValue]
    updated_at: str | None = None

    @field_validator("updated_at")
    @classmethod
    def normalize_time(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("updated_at must include a timezone")
        return parsed.astimezone(UTC).isoformat(timespec="microseconds")

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(canonical(self.content).encode()).hexdigest()


class Change(Model):
    entity: Entity
    kind: str = Field(min_length=1)
    detail: dict[str, JsonValue] = Field(default_factory=dict)


class SkillSnapshot(Model):
    repository_id: int = Field(strict=True, gt=0)
    commit_sha: str = Field(min_length=1)
    files: dict[str, str]


class SkillChange(Model):
    repository_id: int = Field(strict=True, gt=0)
    before_sha: str
    after_sha: str = Field(min_length=1)
    path: str = Field(min_length=1)
    kind: Literal["added", "modified", "deleted"]
    blob_sha: str | None = None


class ReleaseIdentity(Model):
    repository_id: int = Field(strict=True, gt=0)
    remote_id: str = Field(min_length=1)


class BatchWrite(Model):
    input: RefreshInput
    checkpoint: dict[str, JsonValue]
    coverage: dict[str, JsonValue]
    complete: bool = Field(strict=True)
    entities: tuple[Entity, ...] = ()
    changes: tuple[Change, ...] = ()
    snapshots: tuple[SkillSnapshot, ...] = ()
    skill_changes: tuple[SkillChange, ...] = ()
    release_ids: tuple[ReleaseIdentity, ...] = ()

    @field_validator("complete")
    @classmethod
    def require_complete(cls, value: bool) -> bool:
        if not value:
            raise ValueError("incomplete collection cannot be committed")
        return value
