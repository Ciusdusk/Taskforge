"""Single-process execution, cooperative shutdown and crash recovery."""

import asyncio
import logging

from taskforge.config import WorkerConfig
from taskforge.errors import ExecutorError, StorageError, WorkerLockError
from taskforge.executor import Executor
from taskforge.locking import WorkerLock
from taskforge.models import AttemptClaim, AttemptOutcome, AttemptStatus, ErrorKind
from taskforge.policy import decide_completion
from taskforge.registry import TaskRegistry
from taskforge.storage import SQLiteStore

_logger = logging.getLogger("taskforge.worker")


def _log_error(exc: BaseException, claim: AttemptClaim | None = None) -> None:
    if isinstance(exc, ExecutorError):
        kind = "EXECUTOR_ERROR"
    elif isinstance(exc, StorageError):
        kind = "STORAGE_ERROR"
    elif isinstance(exc, WorkerLockError):
        kind = "LOCK_ERROR"
    else:
        kind = "WORKER_ERROR"
    try:
        _logger.error(
            "worker_error",
            extra={
                "event": "worker_error",
                "job_id": getattr(exc, "job_id", None if claim is None else claim.job_id),
                "attempt_number": getattr(exc, "number", None if claim is None else claim.number),
                "error_kind": kind,
            },
        )
    except Exception:
        # A failing diagnostic handler must not replace the original failure.
        pass


class Worker:
    """Use an initialized, caller-owned store; run an instance at most once.

    Drain started work, then request cooperative cancellation after the grace.
    Keep the lock until user cleanup and all started database writes have ended.
    """

    def __init__(
        self, store: SQLiteStore, registry: TaskRegistry, config: WorkerConfig | None = None
    ) -> None:
        self._store = store
        self._executor = Executor(registry)
        self.config = WorkerConfig(**(config or WorkerConfig()).model_dump())
        self._stop = asyncio.Event()
        self._claim_lock = asyncio.Lock()
        self._started = False
        self._executions: dict[asyncio.Task[AttemptOutcome], AttemptClaim] = {}
        self._cancel_executions = False

    def request_stop(self) -> None:
        """Stop new claims, wake idle slots and start the shutdown grace period."""
        self._stop.set()

    async def _recover(self) -> None:
        claim = None
        try:
            while (claim := await self._store.get_running_attempt()) is not None:
                outcome = AttemptOutcome(
                    AttemptStatus.INTERRUPTED,
                    ErrorKind.INTERRUPTED,
                    error_message="Previous Worker exited before attempt completion",
                )
                decision = decide_completion(claim.job, outcome)
                await self._store.finish_attempt(
                    claim.job_id,
                    claim.number,
                    outcome,
                    status=decision.status,
                    retry_delay_seconds=decision.retry_delay_seconds,
                )
                claim = None
        except BaseException as exc:
            self.request_stop()
            _log_error(exc, claim)
            raise

    def _cancel_execution(self, task: asyncio.Task[AttemptOutcome], claim: AttemptClaim) -> None:
        if task.done():
            return
        task.cancel()
        try:
            _logger.warning(
                "shutdown_incomplete",
                extra={
                    "event": "shutdown_incomplete",
                    "job_id": claim.job_id,
                    "attempt_number": claim.number,
                    "error_kind": ErrorKind.INTERRUPTED.value,
                },
            )
        except Exception:
            pass

    async def _shutdown(self) -> None:
        await self._stop.wait()
        await asyncio.sleep(self.config.shutdown_grace_seconds)
        self._cancel_executions = True
        for task, claim in self._executions.items():
            self._cancel_execution(task, claim)

    async def _slot(self) -> None:
        claim = None
        try:
            while not self._stop.is_set():
                # Only one claim is in flight; waiters recheck stop before entering DB.
                async with self._claim_lock:
                    if self._stop.is_set():
                        return
                    claim = await self._store.claim_next()
                if claim is None:
                    try:
                        await asyncio.wait_for(
                            self._stop.wait(), timeout=self.config.poll_interval_seconds
                        )
                    except TimeoutError:
                        pass
                    continue
                execution = asyncio.create_task(self._executor.execute(claim))
                self._executions[execution] = claim
                if self._cancel_executions:
                    self._cancel_execution(execution, claim)
                interruption = None
                try:
                    outcome = await execution
                except asyncio.CancelledError as exc:
                    # Executor only propagates cancellation after user code has ended.
                    outcome = AttemptOutcome(
                        AttemptStatus.INTERRUPTED,
                        ErrorKind.INTERRUPTED,
                        error_message="Task execution was cancelled",
                    )
                    if not self._cancel_executions:
                        self.request_stop()
                        interruption = exc
                finally:
                    del self._executions[execution]
                decision = decide_completion(claim.job, outcome)
                await self._store.finish_attempt(
                    claim.job_id,
                    claim.number,
                    outcome,
                    status=decision.status,
                    retry_delay_seconds=decision.retry_delay_seconds,
                )
                if interruption is not None:
                    raise interruption
                claim = None
        except BaseException as exc:
            self.request_stop()
            _log_error(exc, claim)
            raise

    async def _serve(self) -> None:
        try:
            # Reject uninitialized databases before initialize's existing-schema check.
            await self._store.list(limit=1)
            await self._store.initialize()
        except BaseException as exc:
            self.request_stop()
            _log_error(exc)
            raise
        await self._recover()
        slots = [asyncio.create_task(self._slot()) for _ in range(self.config.concurrency)]
        shutdown = asyncio.create_task(self._shutdown())
        try:
            results = await asyncio.gather(*slots, return_exceptions=True)
        finally:
            shutdown.cancel()
            await asyncio.gather(shutdown, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def run(self) -> None:
        """Hold the lock through schema validation, execution and completion writes."""
        if self._started:
            raise RuntimeError("A Worker instance can only run once")
        self._started = True
        lock = WorkerLock(self._store.config.database_path)
        try:
            lock.__enter__()
        except WorkerLockError as exc:
            _log_error(exc)
            raise
        try:
            serving = asyncio.create_task(self._serve())
            try:
                await asyncio.shield(serving)
            except asyncio.CancelledError:
                owner = asyncio.current_task()
                if serving.cancelled() and owner is not None and not owner.cancelling():
                    # shield synthesizes cancellation; retrieve the original from recovery.
                    serving.result()
                self.request_stop()
                # Repeated cancellation cannot release the lock or interrupt cleanup.
                while not serving.done():
                    try:
                        await asyncio.shield(serving)
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        break
                if not serving.cancelled():
                    serving.exception()
                raise
        finally:
            lock.__exit__(None, None, None)
