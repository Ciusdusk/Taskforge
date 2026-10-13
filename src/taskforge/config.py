"""Only the local database settings needed for persistence."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DatabaseConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    database_path: Path
    busy_timeout_ms: int = Field(default=5000, strict=True, ge=0, le=2_147_483_647)

    @field_validator("database_path")
    @classmethod
    def normalize_path(cls, value: Path) -> Path:
        if str(value) == ":memory:":
            raise ValueError("database_path must name a local SQLite file")
        return value.expanduser().resolve()
