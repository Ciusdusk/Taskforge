"""JSON logging and task-local execution context; no global logging setup."""

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime

from taskforge.models import AttemptClaim


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    job_id: str
    attempt_number: int


_context: ContextVar[ExecutionContext | None] = ContextVar("taskforge_execution", default=None)


def get_execution_context() -> ExecutionContext | None:
    """Return the current execution identity, if any."""
    return _context.get()


@contextmanager
def execution_context(claim: AttemptClaim) -> Iterator[ExecutionContext]:
    """Bind an identity and restore the previous context on every exit path."""
    context = ExecutionContext(claim.job_id, claim.number)
    token = _context.set(context)
    try:
        yield context
    finally:
        _context.reset(token)


class JSONFormatter(logging.Formatter):
    """Format synchronous logging handlers without exposing parameters or exceptions."""

    def format(self, record: logging.LogRecord) -> str:
        context = get_execution_context()
        return json.dumps(
            {
                "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
                "level": record.levelname,
                "event": getattr(record, "event", record.getMessage()),
                "job_id": getattr(record, "job_id", None if context is None else context.job_id),
                "attempt_number": getattr(
                    record, "attempt_number", None if context is None else context.attempt_number
                ),
                "error_kind": getattr(record, "error_kind", None),
            },
            ensure_ascii=False,
        )
