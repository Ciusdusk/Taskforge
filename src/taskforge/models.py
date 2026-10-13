"""The execution defaults needed by task registration."""

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
