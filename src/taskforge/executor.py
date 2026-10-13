"""One asynchronous attempt; no persistence, scheduling or retry decisions."""

import asyncio
import logging
import traceback
from collections.abc import Awaitable
from typing import cast

from pydantic import BaseModel, ValidationError

from taskforge.errors import ExecutorError, UnknownTaskError
from taskforge.logging import execution_context
from taskforge.models import AttemptClaim, AttemptOutcome, AttemptStatus, ErrorKind
from taskforge.registry import TaskDefinition, TaskRegistry

_logger = logging.getLogger("taskforge.executor")


def _failure(kind: ErrorKind, exc: Exception) -> AttemptOutcome:
    return AttemptOutcome(
        AttemptStatus.FAILED,
        kind,
        type(exc).__qualname__,
        str(exc),
        "".join(traceback.format_exception(exc)),
    )


async def _invoke(definition: TaskDefinition[BaseModel], data: BaseModel) -> AttemptOutcome:
    # Only the user's await belongs to the business-exception boundary.
    try:
        result: object = await cast(Awaitable[object], definition.function(data))
    except Exception as exc:
        return _failure(ErrorKind.TASK_ERROR, exc)
    if result is not None:
        cause = TypeError("Task function must return None")
        raise ExecutorError("Task violated the return None contract") from cause
    return AttemptOutcome(AttemptStatus.SUCCESS)


async def _drain(task: asyncio.Task[AttemptOutcome]) -> asyncio.CancelledError | None:
    """Wait through repeated caller cancellations without interrupting user cleanup."""
    owner = asyncio.current_task()
    assert owner is not None
    previous = owner.cancelling()
    cancellation = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if owner.cancelling() > previous:
                cancellation = exc
                previous = owner.cancelling()
        except Exception:
            # The owner retrieves the result/exception after the task has stopped.
            break
    return cancellation


def _log(claim: AttemptClaim, event: str, kind: str | None = None) -> None:
    _logger.log(
        logging.INFO if kind is None else logging.ERROR,
        event,
        extra={
            "event": event,
            "job_id": claim.job_id,
            "attempt_number": claim.number,
            "error_kind": kind,
        },
    )


class Executor:
    """Execute a committed claim using its timeout snapshot.

    External cancellation wins over timeout: request user cancellation once, wait
    for actual termination, then propagate CancelledError. A task cancelling
    itself also propagates CancelledError. No interrupted outcome is fabricated.
    """

    def __init__(self, registry: TaskRegistry) -> None:
        self._registry = registry

    async def execute(self, claim: AttemptClaim) -> AttemptOutcome:
        """Revalidate stored JSON and return one immutable attempt outcome."""
        with execution_context(claim):
            try:
                _log(claim, "attempt_started")
                outcome = await self._execute(claim)
                _log(
                    claim,
                    "attempt_finished",
                    None if outcome.error_kind is None else outcome.error_kind.value,
                )
                return outcome
            except asyncio.CancelledError:
                try:
                    _log(claim, "attempt_interrupted", ErrorKind.INTERRUPTED.value)
                except Exception:
                    # A diagnostic failure must not replace the cancellation.
                    pass
                raise
            except Exception as exc:
                try:
                    _log(claim, "executor_error", "EXECUTOR_ERROR")
                except Exception:
                    # Preserve the original framework failure and its cause chain.
                    pass
                if isinstance(exc, ExecutorError):
                    raise
                raise ExecutorError("Single-attempt execution failed") from exc

    async def _execute(self, claim: AttemptClaim) -> AttemptOutcome:
        try:
            definition = self._registry.get(claim.job.task_name)
        except UnknownTaskError as exc:
            return _failure(ErrorKind.CONFIG_ERROR, exc)
        try:
            data = definition.validate_json(claim.job.params_json)
        except ValidationError as exc:
            return _failure(ErrorKind.CONFIG_ERROR, exc)

        loop = asyncio.get_running_loop()
        expired: asyncio.Future[None] = loop.create_future()

        def expire() -> None:
            # Completion observed before the timer wins over timeout.
            if not task.done():
                expired.set_result(None)

        timer = loop.call_later(claim.job.timeout_seconds, expire)
        coroutine = _invoke(definition, data)
        try:
            task = asyncio.create_task(coroutine)
        except BaseException:
            timer.cancel()
            coroutine.close()
            raise
        cancellation_requested = False
        try:
            await asyncio.wait((task, expired), return_when=asyncio.FIRST_COMPLETED)
            if expired.done():
                task.cancel()
                cancellation_requested = True
                cancellation = await _drain(task)
                if cancellation is not None:
                    raise cancellation
                # Observe internal faults even if they surfaced during cancellation.
                if not task.cancelled():
                    task.result()
                return AttemptOutcome(
                    AttemptStatus.FAILED,
                    ErrorKind.TIMEOUT,
                    error_message="Task exceeded its timeout_seconds snapshot",
                )
            return task.result()
        except asyncio.CancelledError:
            timer.cancel()
            if not cancellation_requested:
                task.cancel()
            await _drain(task)
            # Retrieve faults to prevent unobserved task exceptions; cancellation wins.
            if not task.cancelled():
                task.exception()
            raise
        finally:
            timer.cancel()
            expired.cancel()
            # Framework failures must not leave user code running either.
            if not task.done():
                if not cancellation_requested:
                    task.cancel()
                cancellation = await _drain(task)
                if not task.cancelled():
                    task.exception()
                if cancellation is not None:
                    raise cancellation
