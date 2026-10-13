"""Immutable execution defaults and detached persistence snapshots."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExecutionPolicy(BaseModel):
    """Immutable defaults; max_retries counts additional attempts.

    Retry timing is only configuration here, not a scheduling implementation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)

    max_retries: int = Field(default=0, ge=0, strict=True)
    retry_base_seconds: float = Field(default=1.0, gt=0, allow_inf_nan=False, strict=True)
    retry_cap_seconds: float = Field(default=60.0, gt=0, allow_inf_nan=False, strict=True)
    timeout_seconds: float = Field(default=300.0, gt=0, allow_inf_nan=False, strict=True)

    @model_validator(mode="after")
    def check_retry_range(self) -> Self:
        if self.retry_cap_seconds < self.retry_base_seconds:
            raise ValueError("retry_cap_seconds must be >= retry_base_seconds")
        return self


class JobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class AttemptStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


class ErrorKind(StrEnum):
    TASK_ERROR = "TASK_ERROR"
    TIMEOUT = "TIMEOUT"
    CONFIG_ERROR = "CONFIG_ERROR"
    INTERRUPTED = "INTERRUPTED"


@dataclass(frozen=True, slots=True)
class JobCursor:
    """Exclusive position in ascending (created_at, id) order."""

    created_at: datetime
    id: str


@dataclass(frozen=True, slots=True)
class Job:
    id: str
    task_name: str
    params_json: str
    status: JobStatus
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    next_run_at: datetime | None
    attempt_count: int
    max_retries: int
    retry_base_seconds: float
    retry_cap_seconds: float
    timeout_seconds: float
    last_error_kind: ErrorKind | None
    last_error_message: str | None

    @property
    def cursor(self) -> JobCursor:
        return JobCursor(self.created_at, self.id)


@dataclass(frozen=True, slots=True)
class Attempt:
    job_id: str
    number: int
    status: AttemptStatus
    started_at: datetime
    finished_at: datetime | None
    error_kind: ErrorKind | None
    exception_type: str | None
    error_message: str | None
    traceback: str | None


@dataclass(frozen=True, slots=True)
class AttemptClaim:
    """Committed RUNNING Job snapshot and its completion credential."""

    job: Job
    number: int

    @property
    def job_id(self) -> str:
        return self.job.id


@dataclass(frozen=True, slots=True)
class AttemptOutcome:
    """One execution result; storage validates it before writing.

    Job disposition and retry timing are supplied separately by the caller.
    """

    status: AttemptStatus
    error_kind: ErrorKind | None = None
    exception_type: str | None = None
    error_message: str | None = None
    traceback: str | None = None
