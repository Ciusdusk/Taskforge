import asyncio
import io
import json
import logging
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from pydantic import BaseModel, ConfigDict, field_validator

from taskforge import (
    AttemptClaim,
    AttemptStatus,
    DatabaseConfig,
    ErrorKind,
    ExecutionPolicy,
    Executor,
    ExecutorError,
    Job,
    JobStatus,
    JSONFormatter,
    SQLiteStore,
    TaskRegistry,
    get_execution_context,
)
from taskforge.logging import execution_context

pytestmark = pytest.mark.asyncio


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int
    secret: str = "private-payload"


def claim(*, name="demo", payload='{"count":"3","secret":"private-payload"}', timeout=1):
    return AttemptClaim(
        Job(
            id=str(uuid4()),
            task_name=name,
            params_json=payload,
            status=JobStatus.RUNNING,
            created_at=datetime.now(UTC),
            started_at=datetime.now(UTC),
            finished_at=None,
            next_run_at=None,
            attempt_count=1,
            max_retries=2,
            retry_base_seconds=1,
            retry_cap_seconds=60,
            timeout_seconds=timeout,
            last_error_kind=None,
            last_error_message=None,
        ),
        1,
    )


def registry(function, *, model=Input, timeout=300):
    result = TaskRegistry()
    result.task("demo", input_model=model, policy=ExecutionPolicy(timeout_seconds=timeout))(
        function
    )
    return result


@pytest_asyncio.fixture(autouse=True)
async def bound_test():
    # A guard for broken implementations, never a synchronization mechanism.
    async with asyncio.timeout(3):
        yield


@pytest.fixture
def json_logs():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JSONFormatter())
    logger = logging.getLogger("taskforge")
    level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        yield stream
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
        handler.close()


async def test_success_revalidates_model_and_immutable_result():
    seen = []

    async def function(data: Input) -> None:
        seen.append(data)

    outcome = await Executor(registry(function)).execute(claim())
    assert len(seen) == 1 and isinstance(seen[0], Input) and seen[0].count == 3
    assert outcome.status is AttemptStatus.SUCCESS
    assert all(
        getattr(outcome, key) is None
        for key in ("error_kind", "exception_type", "error_message", "traceback")
    )
    with pytest.raises(FrozenInstanceError):
        outcome.status = AttemptStatus.FAILED


@pytest.mark.parametrize(
    "payload", ["{}", '{"count":"bad"}', "[]", "invalid json", '{"count":3,"unexpected":true}']
)
async def test_invalid_json_is_config_error_without_calling_function(payload):
    async def function(data: Input) -> None:
        pytest.fail("invalid payload reached user code")

    outcome = await Executor(registry(function)).execute(claim(payload=payload))
    assert outcome.status is AttemptStatus.FAILED
    assert outcome.error_kind is ErrorKind.CONFIG_ERROR
    assert outcome.exception_type == "ValidationError"
    assert outcome.traceback and "ValidationError" in outcome.traceback


async def test_unknown_task_is_config_error():
    async def function(data: Input) -> None:
        pytest.fail("unknown task reached user code")

    outcome = await Executor(registry(function)).execute(claim(name="missing"))
    assert outcome.error_kind is ErrorKind.CONFIG_ERROR
    assert outcome.exception_type == "UnknownTaskError"


@pytest.mark.parametrize(
    "error", [ValueError("business"), TimeoutError("user timeout"), ExecutorError("raised by user")]
)
async def test_user_exception_is_task_error_with_details_and_chain(error):
    calls = 0

    async def function(data: Input) -> None:
        nonlocal calls
        calls += 1
        try:
            raise LookupError("root cause")
        except LookupError as cause:
            raise error from cause

    outcome = await Executor(registry(function)).execute(claim())
    assert calls == 1
    assert outcome.error_kind is ErrorKind.TASK_ERROR
    assert outcome.exception_type == type(error).__qualname__
    assert outcome.error_message == str(error)
    assert "root cause" in outcome.traceback and str(error) in outcome.traceback


async def test_non_none_return_is_executor_error_with_cause():
    async def function(data):
        return 42

    with pytest.raises(ExecutorError) as caught:
        await Executor(registry(function)).execute(claim())
    assert isinstance(caught.value.__cause__, TypeError)


@pytest.mark.parametrize("phase", ["lookup", "validation", "formatting", "timer", "creation"])
async def test_internal_fault_is_executor_error_not_business_result(monkeypatch, phase):
    import taskforge.executor as module

    calls = 0

    async def function(data: Input) -> None:
        nonlocal calls
        calls += 1
        raise ValueError("business")

    tasks = registry(function)
    fault = RuntimeError("framework broken")

    def fail(*args, **kwargs):
        raise fault

    if phase == "lookup":
        monkeypatch.setattr(tasks, "get", fail)
    elif phase == "validation":
        monkeypatch.setattr(type(tasks.get("demo")), "validate_json", fail)
    elif phase == "formatting":
        monkeypatch.setattr(module.traceback, "format_exception", fail)
    elif phase == "timer":
        monkeypatch.setattr(asyncio.get_running_loop(), "call_later", fail)
    else:
        monkeypatch.setattr(module.asyncio, "create_task", fail)
    with pytest.raises(ExecutorError) as caught:
        await Executor(tasks).execute(claim())
    assert caught.value.__cause__ is fault
    assert calls == (1 if phase == "formatting" else 0)
    assert get_execution_context() is None


async def test_model_validator_programming_fault_is_executor_error():
    class BrokenInput(BaseModel):
        count: int

        @field_validator("count")
        @classmethod
        def broken(cls, value):
            raise TypeError("validator bug")

    async def function(data):
        pytest.fail("validator failed")

    with pytest.raises(ExecutorError) as caught:
        await Executor(registry(function, model=BrokenInput)).execute(claim())
    assert isinstance(caught.value.__cause__, TypeError)


@pytest.mark.parametrize("mode", ["timeout", "cancel", "timeout_then_cancel"])
@pytest.mark.parametrize("suppress", [False, True])
async def test_cancellation_waits_for_real_exit_and_cleanup(mode, suppress, json_logs):
    started, cleaning, release, exited = (asyncio.Event() for _ in range(4))
    user_task = None

    async def function(data: Input) -> None:
        nonlocal user_task
        user_task = asyncio.current_task()
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if not suppress:
                raise
        finally:
            cleaning.set()
            await release.wait()
            assert get_execution_context() is not None
            exited.set()

    executor = Executor(registry(function))
    running = asyncio.create_task(executor.execute(claim(timeout=0.01 if mode != "cancel" else 60)))
    try:
        await started.wait()
        if mode == "cancel":
            running.cancel("external request")
        await cleaning.wait()
        if mode != "timeout":
            running.cancel("repeated request")
            # Give cancellation a turn to reach the shielded cleanup wait.
            await asyncio.sleep(0)
            running.cancel("another request")
            await asyncio.sleep(0)
        assert not running.done()
        assert not exited.is_set()
        assert user_task is not None and not user_task.done()
        assert "attempt_finished" not in json_logs.getvalue()
        assert "attempt_interrupted" not in json_logs.getvalue()
        release.set()
        if mode == "timeout":
            outcome = await running
            assert outcome.error_kind is ErrorKind.TIMEOUT
        else:
            with pytest.raises(asyncio.CancelledError):
                await running
        assert exited.is_set() and user_task.done()
    finally:
        release.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_self_cancellation_propagates_after_finally():
    exited = asyncio.Event()

    async def function(data: Input) -> None:
        try:
            raise asyncio.CancelledError("self cancelled")
        finally:
            exited.set()

    with pytest.raises(asyncio.CancelledError):
        await Executor(registry(function)).execute(claim())
    assert exited.is_set()


async def test_timeout_cleanup_business_exception_remains_timeout():
    async def function(data: Input) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            raise ValueError("cleanup error")

    outcome = await Executor(registry(function)).execute(claim(timeout=0.01))
    assert outcome.error_kind is ErrorKind.TIMEOUT


async def test_timeout_uses_persisted_policy_and_executor_does_not_write(tmp_path, monkeypatch):
    cleaning = asyncio.Event()

    async def function(data: Input) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()

    submitted_registry = registry(function, timeout=0.01)
    async with SQLiteStore(DatabaseConfig(database_path=tmp_path / "jobs.sqlite")) as store:
        await store.initialize()
        submitted = await store.submit(submitted_registry.get("demo"), {"count": 5})
        credential = await store.claim_next()
        assert credential is not None

        def forbidden(*args, **kwargs):
            pytest.fail("Executor wrote to the store")

        for method in ("submit", "claim_next", "finish_attempt"):
            monkeypatch.setattr(store, method, forbidden)
        # Execution remains independent of the store's connections and new defaults.
        await store.close()
        outcome = await Executor(registry(function, timeout=60)).execute(credential)
        assert outcome.error_kind is ErrorKind.TIMEOUT
        assert cleaning.is_set()
        assert credential.job.timeout_seconds == submitted.timeout_seconds == 0.01
        unchanged = await store.get(submitted.id)
        assert unchanged == credential.job
        assert (await store.history(submitted.id))[0].status is AttemptStatus.RUNNING


async def test_error_details_round_trip_through_finish_attempt(tmp_path):
    async def function(data: Input) -> None:
        raise ValueError("错误" * 1500)

    tasks = registry(function)
    async with SQLiteStore(DatabaseConfig(database_path=tmp_path / "errors.sqlite")) as store:
        await store.initialize()
        await store.submit(tasks.get("demo"), {"count": 5})
        credential = await store.claim_next()
        assert credential is not None
        outcome = await Executor(tasks).execute(credential)
        assert len(outcome.error_message) == 3000
        await store.finish_attempt(
            credential.job_id, credential.number, outcome, status=JobStatus.FAILED
        )
        history = (await store.history(credential.job_id))[0]
        assert history.exception_type == "ValueError"
        assert history.error_message == outcome.error_message[:2000]
        assert history.traceback == outcome.traceback[:16000]


async def test_json_logs_concurrent_context_isolation_and_restoration(json_logs):
    barrier = asyncio.Barrier(2)
    identities = []

    async def function(data: Input) -> None:
        before = get_execution_context()
        identities.append(before)
        with pytest.raises(FrozenInstanceError):
            before.job_id = "changed"
        logging.getLogger("taskforge.user").info("user_started")
        await barrier.wait()
        assert get_execution_context() == before
        logging.getLogger("taskforge.user").info("user_finished")

    executor = Executor(registry(function))
    first, second, parent = claim(), claim(), claim()
    second = replace(second, number=2)
    assert get_execution_context() is None
    with execution_context(parent) as previous:
        results = await asyncio.gather(executor.execute(first), executor.execute(second))
        assert get_execution_context() is previous
    assert get_execution_context() is None
    assert all(result.status is AttemptStatus.SUCCESS for result in results)
    assert {context.job_id for context in identities} == {first.job_id, second.job_id}
    rows = [json.loads(line) for line in json_logs.getvalue().splitlines()]
    assert len(rows) == 8
    for row in rows:
        assert set(row) == {"time", "level", "event", "job_id", "attempt_number", "error_kind"}
        assert datetime.fromisoformat(row["time"]).utcoffset().total_seconds() == 0
        assert row["job_id"] in (first.job_id, second.job_id)
        assert row["attempt_number"] == (1 if row["job_id"] == first.job_id else 2)
        assert row["error_kind"] is None
    assert "private-payload" not in json_logs.getvalue()
    assert "params_json" not in json_logs.getvalue()


@pytest.mark.parametrize("failure", ["business", "config", "framework", "cancel", "timeout"])
async def test_error_log_classification_and_context_restoration(failure, json_logs):
    async def function(data):
        if failure == "business":
            raise TimeoutError("private-payload")
        if failure == "framework":
            return 1
        if failure == "cancel":
            raise asyncio.CancelledError
        if failure == "timeout":
            await asyncio.Event().wait()

    credential = claim(
        payload='{"count":"private-payload"}' if failure == "config" else '{"count":3}',
        timeout=0.01,
    )
    parent = claim()
    with execution_context(parent) as previous:
        if failure == "framework":
            with pytest.raises(ExecutorError):
                await Executor(registry(function)).execute(credential)
        elif failure == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await Executor(registry(function)).execute(credential)
        else:
            await Executor(registry(function)).execute(credential)
        assert get_execution_context() is previous
    rows = [json.loads(line) for line in json_logs.getvalue().splitlines()]
    assert (
        rows[-1]["error_kind"]
        == {
            "business": "TASK_ERROR",
            "config": "CONFIG_ERROR",
            "framework": "EXECUTOR_ERROR",
            "cancel": "INTERRUPTED",
            "timeout": "TIMEOUT",
        }[failure]
    )
    assert rows[-1]["job_id"] == credential.job_id
    assert rows[-1]["attempt_number"] == credential.number
    assert "private-payload" not in json_logs.getvalue()
    assert get_execution_context() is None


async def test_formatter_without_execution_context(json_logs):
    logging.getLogger("taskforge.user").warning("outside")
    row = json.loads(json_logs.getvalue())
    assert row["level"] == "WARNING" and row["event"] == "outside"
    assert row["job_id"] is None and row["attempt_number"] is None


async def test_timeout_cleanup_return_violation_stays_framework_error():
    async def function(data):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return 1

    with pytest.raises(ExecutorError) as caught:
        await Executor(registry(function)).execute(claim(timeout=0.01))
    assert isinstance(caught.value.__cause__, TypeError)


async def test_external_cancellation_wins_over_cleanup_business_exception():
    started, exited = asyncio.Event(), asyncio.Event()

    async def function(data: Input) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()
            raise ValueError("cleanup error")

    running = asyncio.create_task(Executor(registry(function)).execute(claim()))
    try:
        await started.wait()
        running.cancel("caller message")
        with pytest.raises(asyncio.CancelledError, match="caller message"):
            await running
        assert exited.is_set()
    finally:
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_framework_wait_failure_still_waits_for_user_exit(monkeypatch):
    started, cleaning, release, exited = (asyncio.Event() for _ in range(4))
    fault = RuntimeError("wait machinery broke")

    async def function(data: Input) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            exited.set()

    async def broken_wait(*args, **kwargs):
        await started.wait()
        raise fault

    monkeypatch.setattr(asyncio, "wait", broken_wait)
    running = asyncio.create_task(Executor(registry(function)).execute(claim()))
    try:
        await cleaning.wait()
        assert not running.done() and not exited.is_set()
        release.set()
        with pytest.raises(ExecutorError) as caught:
            await running
        assert caught.value.__cause__ is fault
        assert exited.is_set()
    finally:
        release.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.parametrize("event", ["attempt_started", "attempt_finished", "attempt_interrupted"])
async def test_logging_fault_preserves_framework_cause_or_cancellation(monkeypatch, event):
    import taskforge.executor as module

    fault = RuntimeError("log handler failed")
    calls = 0

    async def function(data: Input) -> None:
        nonlocal calls
        calls += 1
        if event == "attempt_interrupted":
            raise asyncio.CancelledError("user cancelled")

    def broken_log(level, message, **kwargs):
        if message in (event, "executor_error"):
            raise fault

    monkeypatch.setattr(module._logger, "log", broken_log)
    if event == "attempt_interrupted":
        with pytest.raises(asyncio.CancelledError, match="user cancelled"):
            await Executor(registry(function)).execute(claim())
    else:
        with pytest.raises(ExecutorError) as caught:
            await Executor(registry(function)).execute(claim())
        assert caught.value.__cause__ is fault
    assert calls == (0 if event == "attempt_started" else 1)
    assert get_execution_context() is None
