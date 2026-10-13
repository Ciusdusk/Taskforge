import asyncio
import io
import json
import logging
import sqlite3
import sys
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from pydantic import BaseModel, ConfigDict
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from taskforge import (
    AttemptOutcome,
    AttemptStatus,
    ClaimUncertainError,
    CompletionUncertainError,
    DatabaseConfig,
    ErrorKind,
    ExecutionPolicy,
    ExecutorError,
    InvalidAttemptResultError,
    JobStatus,
    JSONFormatter,
    SQLiteStore,
    StorageError,
    TaskRegistry,
    Worker,
    WorkerConfig,
    WorkerLockError,
    get_execution_context,
    storage,
)
from taskforge.locking import WorkerLock

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]
NOW = datetime(2026, 10, 13, 12, 0, 0, 123456, tzinfo=UTC)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int


def registry(function, **policy):
    tasks = TaskRegistry()
    tasks.task("demo", input_model=Input, policy=ExecutionPolicy(**policy))(function)
    return tasks


@pytest_asyncio.fixture
async def store(tmp_path):
    async with SQLiteStore(DatabaseConfig(database_path=tmp_path / "retries.sqlite")) as result:
        await result.initialize()
        yield result


@pytest.fixture
def clock(monkeypatch):
    time = [storage._epoch_us(NOW)]
    monkeypatch.setattr(storage, "_now_us", lambda: time[0])
    return time


@pytest_asyncio.fixture(autouse=True)
async def deadline():
    async with asyncio.timeout(10):
        yield


@asynccontextmanager
async def running(worker):
    task = asyncio.create_task(worker.run())
    try:
        yield task
    finally:
        worker.request_stop()
        await asyncio.gather(task, return_exceptions=True)


@pytest.fixture
def logs():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JSONFormatter())
    logger = logging.getLogger("taskforge")
    previous = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        yield stream
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
        handler.close()


@pytest.mark.parametrize("retries", [0, 1, 3])
@pytest.mark.parametrize("kind", ["business", "user_timeout", "framework_timeout"])
@pytest.mark.parametrize("eventual_success", [False, True])
async def test_bounded_retries_history_snapshot_and_json_identity(
    store, clock, monkeypatch, logs, retries, kind, eventual_success
):
    calls, cleaned, completions = [], [], []

    async def function(data: Input) -> None:
        identity = get_execution_context()
        calls.append((data.value, identity.job_id, identity.attempt_number))
        if eventual_success and len(calls) == retries + 1:
            return
        if kind == "business":
            raise ValueError("business failure")
        if kind == "user_timeout":
            raise TimeoutError("user timeout")
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append(identity.attempt_number)

    submitted = registry(
        function,
        max_retries=retries,
        retry_base_seconds=0.125,
        retry_cap_seconds=0.25,
        timeout_seconds=0.01,
    )
    job = await store.submit(submitted.get("demo"), {"value": 7})
    # A fresh registry has different defaults; every attempt must use the Job snapshot.
    worker = Worker(store, registry(function), WorkerConfig(concurrency=1))
    original = store.finish_attempt

    async def observe(*args, **kwargs):
        done = await original(*args, **kwargs)
        completions.append(done)
        if done.status is JobStatus.RETRY_WAIT:
            history = await store.history(job.id)
            expected = min(0.25, 0.125 * 2 ** (done.attempt_count - 1))
            assert done.next_run_at == history[-1].finished_at + timedelta(seconds=expected)
            assert done.finished_at is None
            clock[0] = storage._epoch_us(done.next_run_at)
        else:
            worker.request_stop()
        return done

    monkeypatch.setattr(store, "finish_attempt", observe)
    async with running(worker) as run:
        await run
    done = await store.get(job.id)
    history = await store.history(job.id)
    assert calls == [(7, job.id, number) for number in range(1, retries + 2)]
    assert len(history) == done.attempt_count == retries + 1
    assert done.status is (JobStatus.SUCCESS if eventual_success else JobStatus.FAILED)
    assert done.id == job.id and done.started_at == history[0].started_at == NOW
    assert done.finished_at == history[-1].finished_at and done.next_run_at is None
    error_kind = ErrorKind.TIMEOUT if kind == "framework_timeout" else ErrorKind.TASK_ERROR
    for number, (attempt, snapshot) in enumerate(zip(history, completions, strict=True), 1):
        assert attempt.number == number and attempt.job_id == job.id
        assert snapshot.params_json == job.params_json
        assert (
            snapshot.max_retries,
            snapshot.retry_base_seconds,
            snapshot.retry_cap_seconds,
            snapshot.timeout_seconds,
        ) == (retries, 0.125, 0.25, 0.01)
        assert snapshot.started_at == NOW
        if eventual_success and number == retries + 1:
            assert attempt.status is AttemptStatus.SUCCESS
            assert attempt.error_kind is attempt.exception_type is None
            assert attempt.error_message is attempt.traceback is None
            assert done.last_error_kind is done.last_error_message is None
        else:
            assert attempt.status is AttemptStatus.FAILED and attempt.error_kind is error_kind
            assert attempt.error_message
            if kind != "framework_timeout":
                assert attempt.exception_type == (
                    "ValueError" if kind == "business" else "TimeoutError"
                )
                assert attempt.exception_type in attempt.traceback
            assert snapshot.last_error_kind is error_kind
    assert cleaned == (
        list(range(1, retries + (0 if eventual_success else 1) + 1))
        if kind == "framework_timeout"
        else []
    )
    assert await store.claim_next() is None
    entries = [json.loads(line) for line in logs.getvalue().splitlines()]
    assert "value" not in logs.getvalue()
    for number in range(1, retries + 2):
        events = [item for item in entries if item["attempt_number"] == number]
        assert [item["event"] for item in events] == ["attempt_started", "attempt_finished"]
        assert all(item["job_id"] == job.id for item in events)
        assert events[-1]["error_kind"] == (
            None if eventual_success and number == retries + 1 else error_kind.value
        )


@pytest.mark.parametrize("configuration", ["success", "unknown", "invalid_input"])
async def test_success_and_configuration_errors_never_retry(
    store, clock, monkeypatch, configuration
):
    calls = []

    async def function(data: Input) -> None:
        calls.append(data.value)

    tasks = registry(function, max_retries=5)
    job = await store.submit(tasks.get("demo"), {"value": 1})
    with sqlite3.connect(store.config.database_path) as connection:
        if configuration == "unknown":
            connection.execute("UPDATE jobs SET task_name='missing' WHERE id=?", (job.id,))
        if configuration == "invalid_input":
            connection.execute("UPDATE jobs SET params_json='{}' WHERE id=?", (job.id,))
    worker = Worker(store, tasks)
    original = store.finish_attempt

    async def observe(*args, **kwargs):
        result = await original(*args, **kwargs)
        worker.request_stop()
        return result

    monkeypatch.setattr(store, "finish_attempt", observe)
    async with running(worker) as run:
        await run
    done = await store.get(job.id)
    (attempt,) = await store.history(job.id)
    assert done.attempt_count == 1 and done.next_run_at is None
    assert calls == ([1] if configuration == "success" else [])
    assert done.status is (JobStatus.SUCCESS if configuration == "success" else JobStatus.FAILED)
    assert attempt.error_kind is (None if configuration == "success" else ErrorKind.CONFIG_ERROR)
    assert await store.claim_next() is None


async def test_retry_wait_releases_slot_and_persisted_due_boundary(store, clock, monkeypatch):
    second_started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def function(data: Input) -> None:
        identity = get_execution_context()
        calls.append((data.value, identity.attempt_number))
        if data.value == 1 and identity.attempt_number == 1:
            raise ValueError("try later")
        if data.value == 2:
            second_started.set()
            await release.wait()

    tasks = registry(function, max_retries=1, retry_base_seconds=1, retry_cap_seconds=1)
    first = await store.submit(tasks.get("demo"), {"value": 1})
    clock[0] += 1
    second = await store.submit(tasks.get("demo"), {"value": 2})
    worker = Worker(store, tasks, WorkerConfig(concurrency=1, poll_interval_seconds=0.005))
    original_claim, original_finish = store.claim_next, store.finish_attempt
    empty = asyncio.Event()

    async def claim():
        result = await original_claim()
        if result is None:
            empty.set()
        return result

    async def finish(*args, **kwargs):
        result = await original_finish(*args, **kwargs)
        if result.id == first.id and result.status is JobStatus.SUCCESS:
            worker.request_stop()
        return result

    monkeypatch.setattr(store, "claim_next", claim)
    monkeypatch.setattr(store, "finish_attempt", finish)
    async with running(worker) as run:
        try:
            await second_started.wait()
            waiting = await store.get(first.id)
            assert waiting.status is JobStatus.RETRY_WAIT
            assert waiting.next_run_at == NOW + timedelta(seconds=1, microseconds=1)
            assert calls == [(1, 1), (2, 1)]
            release.set()
            clock[0] = storage._epoch_us(waiting.next_run_at) - 1
            await empty.wait()
            assert calls == [(1, 1), (2, 1)]
            assert (await store.get(first.id)).attempt_count == 1
            clock[0] += 1  # Exactly due, not strictly later.
            await run
        finally:
            release.set()
    assert calls == [(1, 1), (2, 1), (1, 2)]
    assert (await store.get(first.id)).started_at == waiting.started_at
    assert waiting.started_at == (await store.history(first.id))[0].started_at
    assert (await store.get(second.id)).status is JobStatus.SUCCESS


PROCESS = """
import asyncio, json, sys
from taskforge import *
from taskforge import storage
from pydantic import BaseModel

class Input(BaseModel):
    value: int

async def main():
    storage._now_us = lambda: int(sys.argv[2])
    tasks = TaskRegistry()
    calls = []
    @tasks.task('demo', input_model=Input)
    async def function(data: Input) -> None:
        identity = get_execution_context()
        calls.append([identity.job_id, identity.attempt_number, data.value])
    async with SQLiteStore(DatabaseConfig(database_path=sys.argv[1])) as store:
        worker = Worker(store, tasks, WorkerConfig(concurrency=1))
        original_claim, original_finish = store.claim_next, store.finish_attempt
        async def claim():
            result = await original_claim()
            if result is None:
                worker.request_stop()
            return result
        async def finish(*args, **kwargs):
            result = await original_finish(*args, **kwargs)
            worker.request_stop()
            return result
        store.claim_next, store.finish_attempt = claim, finish
        await worker.run()
        job = await store.get(sys.argv[3])
        history = await store.history(job.id)
        print(json.dumps({'calls': calls, 'status': job.status.value,
            'due': None if job.next_run_at is None else storage._epoch_us(job.next_run_at),
            'started': job.started_at.isoformat(), 'history': [a.status.value for a in history]}))
asyncio.run(main())
"""


async def probe_process(path, now, job_id):
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        PROCESS,
        str(path),
        str(now),
        job_id,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await child.communicate()
        assert child.returncode == 0, stderr.decode()
        return json.loads(stdout)
    finally:
        if child.returncode is None:
            child.kill()
        await child.wait()


async def test_fresh_worker_process_obeys_original_persisted_retry_time(store, clock, monkeypatch):
    calls = []

    async def function(data: Input) -> None:
        calls.append(data.value)
        raise ValueError("restart later")

    tasks = registry(function, max_retries=1, retry_base_seconds=8, retry_cap_seconds=8)
    job = await store.submit(tasks.get("demo"), {"value": 3})
    worker = Worker(store, tasks, WorkerConfig(concurrency=1))
    original = store.finish_attempt

    async def finish(*args, **kwargs):
        result = await original(*args, **kwargs)
        worker.request_stop()
        return result

    monkeypatch.setattr(store, "finish_attempt", finish)
    async with running(worker) as run:
        await run
    waiting = await store.get(job.id)
    assert waiting.next_run_at == NOW + timedelta(seconds=8)
    assert calls == [3]
    await store.close()
    due = storage._epoch_us(waiting.next_run_at)
    early = await probe_process(store.config.database_path, due - 1, job.id)
    assert early == {
        "calls": [],
        "status": "RETRY_WAIT",
        "due": due,
        "started": NOW.isoformat(),
        "history": ["FAILED"],
    }
    on_time = await probe_process(store.config.database_path, due, job.id)
    assert on_time == {
        "calls": [[job.id, 2, 3]],
        "status": "SUCCESS",
        "due": None,
        "started": NOW.isoformat(),
        "history": ["FAILED", "SUCCESS"],
    }
    async with SQLiteStore(store.config) as reader:
        done = await reader.get(job.id)
        history = await reader.history(job.id)
        assert done.last_error_kind is done.last_error_message is None
        assert history[0].error_message == "restart later"
        assert history[1].started_at == waiting.next_run_at


async def test_retry_delay_starts_after_real_database_lock_wait(store, clock, monkeypatch):
    blocked = asyncio.Event()
    connection = sqlite3.connect(store.config.database_path, timeout=0)
    locked = False

    def observe_sql(conn, cursor, statement, parameters, context, executemany):
        if locked and statement == "BEGIN IMMEDIATE":
            blocked.set()

    event.listen(store._engine.sync_engine, "before_cursor_execute", observe_sql)

    async def function(data: Input) -> None:
        nonlocal locked
        connection.execute("BEGIN IMMEDIATE")
        locked = True
        raise ValueError("retry after lock")

    tasks = registry(function, max_retries=1, retry_base_seconds=0.125)
    job = await store.submit(tasks.get("demo"), {"value": 1})
    worker = Worker(store, tasks, WorkerConfig(concurrency=1))
    original = store.finish_attempt

    async def finish(*args, **kwargs):
        result = await original(*args, **kwargs)
        worker.request_stop()
        return result

    monkeypatch.setattr(store, "finish_attempt", finish)
    try:
        async with running(worker) as run:
            try:
                await blocked.wait()
                clock[0] += 10_000_000  # Lock waiting consumes much more than the retry delay.
            finally:
                connection.rollback()
                locked = False
            await run
    finally:
        connection.close()
        event.remove(store._engine.sync_engine, "before_cursor_execute", observe_sql)
    waiting = await store.get(job.id)
    (attempt,) = await store.history(job.id)
    assert waiting.status is JobStatus.RETRY_WAIT
    assert attempt.finished_at == NOW + timedelta(seconds=10)
    assert waiting.next_run_at == attempt.finished_at + timedelta(seconds=0.125)


@pytest.mark.parametrize("delay", [5e-324, 0.125, sys.float_info.max])
async def test_relative_delays_microsecond_precision_and_datetime_range(store, clock, delay):
    async def function(data: Input) -> None:
        pass

    job = await store.submit(registry(function, max_retries=1).get("demo"), {"value": 1})
    claim = await store.claim_next()
    waiting = await store.finish_attempt(
        job.id,
        claim.number,
        AttemptOutcome(AttemptStatus.FAILED, ErrorKind.TASK_ERROR),
        status=JobStatus.RETRY_WAIT,
        retry_delay_seconds=delay,
    )
    assert waiting.next_run_at == (
        datetime.max.replace(tzinfo=UTC)
        if delay == sys.float_info.max
        else NOW + timedelta(microseconds=1 if delay == 5e-324 else 125_000)
    )
    assert waiting.next_run_at > (await store.history(job.id))[0].finished_at
    async with SQLiteStore(store.config) as reader:
        assert (await reader.get(job.id)).next_run_at == waiting.next_run_at


@pytest.mark.parametrize(
    "options",
    [
        {"retry_delay_seconds": 0},
        {"retry_delay_seconds": -1},
        {"retry_delay_seconds": True},
        {"retry_delay_seconds": "1"},
        {"retry_delay_seconds": float("inf")},
        {"retry_delay_seconds": float("nan")},
        {"retry_delay_seconds": 1, "next_run_at": NOW + timedelta(seconds=1)},
        {"retry_delay_seconds": 1, "status": JobStatus.FAILED},
    ],
)
async def test_invalid_relative_delay_preserves_running_attempt(store, clock, options):
    async def function(data: Input) -> None:
        pass

    job = await store.submit(registry(function, max_retries=1).get("demo"), {"value": 1})
    claim = await store.claim_next()
    with pytest.raises(InvalidAttemptResultError):
        await store.finish_attempt(
            job.id,
            claim.number,
            AttemptOutcome(AttemptStatus.FAILED, ErrorKind.TASK_ERROR),
            **{"status": JobStatus.RETRY_WAIT, **options},
        )
    assert await store.get(job.id) == claim.job
    assert (await store.history(job.id))[0].status is AttemptStatus.RUNNING


async def test_retry_completion_holds_bounded_slots_lock_and_drains_cancellation(
    store, clock, monkeypatch
):
    committing, release = asyncio.Event(), asyncio.Event()
    completion_context = ContextVar("retry_test_completion", default=False)
    calls = []
    active = maximum = 0

    async def function(data: Input) -> None:
        calls.append(data.value)
        raise ValueError("retry me")

    tasks = registry(function, max_retries=2)
    jobs = [await store.submit(tasks.get("demo"), {"value": i}) for i in range(3)]
    original_finish, original_commit = store.finish_attempt, storage._commit

    async def finish(*args, **kwargs):
        token = completion_context.set(True)
        try:
            return await original_finish(*args, **kwargs)
        finally:
            completion_context.reset(token)

    async def commit(session):
        nonlocal active, maximum
        if completion_context.get():
            active += 1
            maximum = max(maximum, active)
            committing.set()
            try:
                await release.wait()
                await original_commit(session)
            finally:
                active -= 1
        else:
            await original_commit(session)

    monkeypatch.setattr(store, "finish_attempt", finish)
    monkeypatch.setattr(storage, "_commit", commit)
    worker = Worker(store, tasks, WorkerConfig(concurrency=1))
    async with running(worker) as run:
        try:
            await committing.wait()
            assert len(calls) == active == maximum == 1
            states = [await store.get(job.id) for job in jobs]
            assert sorted(job.attempt_count for job in states) == [0, 0, 1]
            assert sum(job.status is JobStatus.RUNNING for job in states) == 1
            run.cancel()
            await asyncio.sleep(0)
            run.cancel()
            assert not run.done()
            with pytest.raises(WorkerLockError):
                with WorkerLock(store.config.database_path):
                    pytest.fail("released lock during retry commit")
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await run
    states = [await store.get(job.id) for job in jobs]
    assert sum(job.status is JobStatus.RETRY_WAIT for job in states) == 1
    assert sum(job.status is JobStatus.PENDING for job in states) == 2
    assert active == 0 and maximum == 1
    with WorkerLock(store.config.database_path):
        pass


@pytest.mark.parametrize("stage", ["claim", "finish"])
async def test_sql_fault_during_retry_stops_without_reexecution(store, clock, stage):
    calls = []

    async def function(data: Input) -> None:
        calls.append(data.value)
        raise ValueError("business failure")

    tasks = registry(function, max_retries=2)
    first = await store.submit(tasks.get("demo"), {"value": 1})
    clock[0] += 1
    second = await store.submit(tasks.get("demo"), {"value": 2})
    with sqlite3.connect(store.config.database_path) as connection:
        connection.execute(
            "CREATE TRIGGER fail_retry AFTER INSERT ON attempts "
            "BEGIN SELECT RAISE(FAIL, 'claim broke'); END"
            if stage == "claim"
            else "CREATE TRIGGER fail_retry AFTER UPDATE ON jobs WHEN NEW.status='RETRY_WAIT' "
            "BEGIN SELECT RAISE(FAIL, 'retry finish broke'); END"
        )
    with pytest.raises(StorageError) as error:
        await Worker(store, tasks, WorkerConfig(concurrency=1)).run()
    assert error.value.__cause__ is not None
    assert calls == ([] if stage == "claim" else [1])
    assert (await store.get(first.id)).status is (
        JobStatus.PENDING if stage == "claim" else JobStatus.RUNNING
    )
    history = await store.history(first.id)
    assert history == () if stage == "claim" else history[0].status is AttemptStatus.RUNNING
    assert (await store.get(second.id)).attempt_count == 0


@pytest.mark.parametrize("stage", ["claim", "finish"])
@pytest.mark.parametrize("persisted", [False, True])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_uncertain_retry_commit_stops_and_can_be_queried(
    store, clock, monkeypatch, stage, persisted, cancelled
):
    calls = []

    async def function(data: Input) -> None:
        calls.append(data.value)
        raise ValueError("business failure")

    tasks = registry(function, max_retries=2)
    first = await store.submit(tasks.get("demo"), {"value": 1})
    clock[0] += 1
    second = await store.submit(tasks.get("demo"), {"value": 2})
    original = AsyncSession.commit
    commits = 0

    async def lose_ack(session):
        nonlocal commits
        commits += 1
        if commits == (2 if stage == "claim" else 3):
            if persisted:
                await original(session)
            if cancelled:
                raise asyncio.CancelledError
            raise OSError("lost retry acknowledgement")
        await original(session)

    monkeypatch.setattr(AsyncSession, "commit", lose_ack)
    error_type = ClaimUncertainError if stage == "claim" else CompletionUncertainError
    with pytest.raises(error_type) as error:
        await Worker(store, tasks, WorkerConfig(concurrency=1)).run()
    assert error.value.job_id == first.id and error.value.number == 1
    assert isinstance(error.value.__cause__, asyncio.CancelledError if cancelled else OSError)
    assert calls == ([] if stage == "claim" else [1])
    async with SQLiteStore(store.config) as reader:
        job = await reader.get(first.id)
        history = await reader.history(first.id)
        if stage == "claim":
            assert job.status is (JobStatus.RUNNING if persisted else JobStatus.PENDING)
            assert len(history) == int(persisted)
        else:
            assert job.status is (JobStatus.RETRY_WAIT if persisted else JobStatus.RUNNING)
            assert len(history) == 1
            assert history[0].status is (
                AttemptStatus.FAILED if persisted else AttemptStatus.RUNNING
            )
            if persisted:
                assert job.next_run_at == history[0].finished_at + timedelta(seconds=1)
        assert (await reader.get(second.id)).attempt_count == 0


async def test_executor_fault_on_retry_is_not_a_business_failure_or_reexecuted(
    store, clock, monkeypatch
):
    calls = []

    async def function(data: Input) -> None:
        calls.append(get_execution_context().attempt_number)
        if len(calls) == 1:
            raise ValueError("first business failure")
        return "invalid return"

    tasks = registry(function, max_retries=3)
    job = await store.submit(tasks.get("demo"), {"value": 1})
    original = store.finish_attempt

    async def finish(*args, **kwargs):
        result = await original(*args, **kwargs)
        assert result.status is JobStatus.RETRY_WAIT
        clock[0] = storage._epoch_us(result.next_run_at)
        return result

    monkeypatch.setattr(store, "finish_attempt", finish)
    with pytest.raises(ExecutorError) as error:
        await Worker(store, tasks, WorkerConfig(concurrency=1)).run()
    assert isinstance(error.value.__cause__, TypeError)
    assert calls == [1, 2]
    done = await store.get(job.id)
    history = await store.history(job.id)
    assert done.status is JobStatus.RUNNING and done.attempt_count == 2
    assert [attempt.status for attempt in history] == [AttemptStatus.FAILED, AttemptStatus.RUNNING]
    assert history[0].error_kind is ErrorKind.TASK_ERROR
    assert history[1].error_kind is None


async def test_real_clock_worker_polls_and_executes_after_persisted_due(store, monkeypatch):
    calls, completions = [], []

    async def function(data: Input) -> None:
        calls.append(datetime.now(UTC))
        if len(calls) < 3:
            raise ValueError("temporary failure")

    tasks = registry(function, max_retries=2, retry_base_seconds=0.0625, retry_cap_seconds=0.125)
    job = await store.submit(tasks.get("demo"), {"value": 1})
    worker = Worker(store, tasks, WorkerConfig(concurrency=1, poll_interval_seconds=0.005))
    original = store.finish_attempt

    async def finish(*args, **kwargs):
        result = await original(*args, **kwargs)
        completions.append(result)
        if result.status is JobStatus.SUCCESS:
            worker.request_stop()
        return result

    monkeypatch.setattr(store, "finish_attempt", finish)
    async with running(worker) as run:
        await run
    history = await store.history(job.id)
    assert len(calls) == len(history) == 3
    for index, delay in enumerate([0.0625, 0.125]):
        due = completions[index].next_run_at
        assert due == history[index].finished_at + timedelta(seconds=delay)
        assert calls[index + 1] >= history[index + 1].started_at >= due
    assert (await store.get(job.id)).status is JobStatus.SUCCESS


async def test_parallel_retries_keep_execution_bounded_and_context_isolated(
    store, clock, monkeypatch
):
    first_started, retry_started = asyncio.Event(), asyncio.Event()
    release_failures, release_successes = asyncio.Event(), asyncio.Event()
    active = maximum = completed = 0
    calls, waiting = [], []

    async def function(data: Input) -> None:
        nonlocal active, maximum
        identity = get_execution_context()
        calls.append((identity.job_id, identity.attempt_number, data.value))
        active += 1
        maximum = max(maximum, active)
        try:
            if identity.attempt_number == 1:
                if active == 2:
                    first_started.set()
                await release_failures.wait()
                raise ValueError("retry concurrently")
            if active == 2:
                retry_started.set()
            await release_successes.wait()
            assert get_execution_context() == identity
        finally:
            active -= 1

    tasks = registry(function, max_retries=1)
    jobs = [await store.submit(tasks.get("demo"), {"value": i}) for i in range(2)]
    worker = Worker(store, tasks, WorkerConfig(concurrency=2, poll_interval_seconds=0.005))
    original = store.finish_attempt

    async def finish(*args, **kwargs):
        nonlocal completed
        result = await original(*args, **kwargs)
        if result.status is JobStatus.RETRY_WAIT:
            waiting.append(result)
            if len(waiting) == 2:
                clock[0] = max(storage._epoch_us(job.next_run_at) for job in waiting)
        else:
            completed += 1
            if completed == 2:
                worker.request_stop()
        return result

    monkeypatch.setattr(store, "finish_attempt", finish)
    async with running(worker) as run:
        try:
            await first_started.wait()
            assert maximum == active == 2
            release_failures.set()
            await retry_started.wait()
            assert maximum == active == 2
            assert len(calls) == 4
        finally:
            release_failures.set()
            release_successes.set()
        await run
    assert active == 0 and maximum == 2
    for job, value in zip(jobs, range(2), strict=True):
        assert [call for call in calls if call[0] == job.id] == [
            (job.id, 1, value),
            (job.id, 2, value),
        ]
        assert (await store.get(job.id)).status is JobStatus.SUCCESS
        assert len(await store.history(job.id)) == 2
