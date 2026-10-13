import asyncio
import io
import json
import logging
import sqlite3
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from pydantic import BaseModel, ValidationError
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
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


class Input(BaseModel):
    value: int


def registry(function, **policy):
    tasks = TaskRegistry()
    tasks.task("demo", input_model=Input, policy=ExecutionPolicy(**policy))(function)
    return tasks


@pytest_asyncio.fixture
async def store(tmp_path):
    async with SQLiteStore(DatabaseConfig(database_path=tmp_path / "recovery.sqlite")) as result:
        await result.initialize()
        yield result


@pytest_asyncio.fixture(autouse=True)
async def deadline():
    async with asyncio.timeout(15):
        yield


@pytest.fixture
def clock(monkeypatch):
    time = [storage._epoch_us(NOW)]
    monkeypatch.setattr(storage, "_now_us", lambda: time[0])
    return time


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


@asynccontextmanager
async def running(worker):
    task = asyncio.create_task(worker.run())
    try:
        yield task
    finally:
        worker.request_stop()
        await asyncio.gather(task, return_exceptions=True)


def assert_locked(store):
    with pytest.raises(WorkerLockError):
        with WorkerLock(store.config.database_path):
            pytest.fail("Worker released its lock before shutdown completed")


@pytest.mark.parametrize("grace", [-1, True, "1", float("inf"), float("nan")])
async def test_grace_configuration_rejects_invalid_values(grace):
    with pytest.raises(ValidationError):
        WorkerConfig(shutdown_grace_seconds=grace)


async def test_grace_defaults_zero_and_immutability():
    assert WorkerConfig().shutdown_grace_seconds == 30.0
    assert WorkerConfig(shutdown_grace_seconds=0).shutdown_grace_seconds == 0.0
    with pytest.raises(ValidationError):
        WorkerConfig().shutdown_grace_seconds = 1


@pytest.mark.parametrize("external", [False, True])
async def test_completion_within_grace_keeps_success(store, external, logs):
    entered, release = asyncio.Event(), asyncio.Event()
    cancelled = False

    async def function(data: Input) -> None:
        nonlocal cancelled
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled = True
            raise

    tasks = registry(function)
    job = await store.submit(tasks.get("demo"), {"value": 1})
    worker = Worker(store, tasks, WorkerConfig(shutdown_grace_seconds=1))
    async with running(worker) as run:
        try:
            await entered.wait()
            if external:
                run.cancel()
            else:
                worker.request_stop()
            await worker._stop.wait()
            assert not run.done()
            assert_locked(store)
        finally:
            release.set()
        if external:
            with pytest.raises(asyncio.CancelledError):
                await run
        else:
            await run
    assert not cancelled
    assert (await store.get(job.id)).status is JobStatus.SUCCESS
    assert (await store.history(job.id))[0].status is AttemptStatus.SUCCESS
    assert "shutdown_incomplete" not in logs.getvalue()


@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("ignore", [False, True])
@pytest.mark.parametrize("retries", [0, 2])
async def test_grace_cancellation_waits_for_user_and_repeated_cancellation(
    store, clock, external, ignore, retries, logs
):
    entered, cleaning, release = (asyncio.Event() for _ in range(3))
    cancellations, identities = [], []

    async def function(data: Input) -> None:
        identity = get_execution_context()
        identities.append(identity)
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellations.append("requested")
            cleaning.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancellations.append("cleanup interrupted")
                raise
            assert get_execution_context() == identity
            if not ignore:
                raise

    tasks = registry(
        function, max_retries=retries, retry_base_seconds=0.125, retry_cap_seconds=0.25
    )
    job = await store.submit(tasks.get("demo"), {"value": 1})
    clock[0] += 1  # Ensure the first Job wins the deterministic creation-time ordering.
    pending = await store.submit(tasks.get("demo"), {"value": 2})
    worker = Worker(
        store, registry(function), WorkerConfig(concurrency=1, shutdown_grace_seconds=0.02)
    )
    async with running(worker) as run:
        try:
            await entered.wait()
            stop_time = asyncio.get_running_loop().time()
            if external:
                run.cancel()
            else:
                worker.request_stop()
            await cleaning.wait()
            assert asyncio.get_running_loop().time() - stop_time >= 0.02
            for _ in range(3):
                worker.request_stop()
                run.cancel()
                await asyncio.sleep(0)
            assert not run.done() and cancellations == ["requested"]
            assert_locked(store)
            assert (await store.get(job.id)).status is JobStatus.RUNNING
            assert (await store.history(job.id))[0].finished_at is None
            assert (await store.get(pending.id)).attempt_count == 0
            entries = [json.loads(line) for line in logs.getvalue().splitlines()]
            (incomplete,) = [item for item in entries if item["event"] == "shutdown_incomplete"]
            assert incomplete["job_id"] == job.id and incomplete["attempt_number"] == 1
            assert incomplete["error_kind"] == "INTERRUPTED"
            clock[0] += 10_000_000
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await run
    done = await store.get(job.id)
    (attempt,) = await store.history(job.id)
    assert cancellations == ["requested"] and len(identities) == 1
    assert attempt.status is AttemptStatus.INTERRUPTED
    assert attempt.error_kind is done.last_error_kind is ErrorKind.INTERRUPTED
    assert attempt.finished_at == NOW + timedelta(seconds=10, microseconds=1)
    assert done.status is (JobStatus.RETRY_WAIT if retries else JobStatus.FAILED)
    assert done.next_run_at == (attempt.finished_at + timedelta(seconds=0.125) if retries else None)
    assert done.finished_at == (None if retries else attempt.finished_at)
    assert done.started_at == attempt.started_at == NOW + timedelta(microseconds=1)
    assert done.attempt_count == 1
    with WorkerLock(store.config.database_path):
        pass


async def test_cooperative_stop_returns_only_after_interruption_commit(store, monkeypatch):
    entered, cleaned, committing, release = (asyncio.Event() for _ in range(4))

    async def function(data: Input) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    tasks = registry(function, max_retries=1)
    job = await store.submit(tasks.get("demo"), {"value": 1})
    original = storage._commit

    async def pause(session):
        if cleaned.is_set():
            committing.set()
            await release.wait()
        await original(session)

    monkeypatch.setattr(storage, "_commit", pause)
    worker = Worker(store, tasks, WorkerConfig(concurrency=1, shutdown_grace_seconds=0))
    async with running(worker) as run:
        try:
            await entered.wait()
            worker.request_stop()
            await committing.wait()
            assert not run.done()
            assert_locked(store)
            assert (await store.get(job.id)).status is JobStatus.RUNNING
            assert (await store.history(job.id))[0].status is AttemptStatus.RUNNING
        finally:
            release.set()
        await run
    assert (await store.get(job.id)).status is JobStatus.RETRY_WAIT
    assert (await store.history(job.id))[0].status is AttemptStatus.INTERRUPTED


async def test_parallel_shutdown_keeps_slots_and_json_identity_until_cleanup(store, logs):
    entered, cleaning, release = (asyncio.Event() for _ in range(3))
    active = cancelled = 0
    identities = []

    async def function(data: Input) -> None:
        nonlocal active, cancelled
        identity = get_execution_context()
        identities.append(identity)
        active += 1
        if active == 2:
            entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled += 1
            if cancelled == 2:
                cleaning.set()
            await release.wait()
            assert get_execution_context() == identity
            active -= 1

    tasks = registry(function, max_retries=1)
    jobs = [await store.submit(tasks.get("demo"), {"value": value}) for value in range(3)]
    worker = Worker(store, tasks, WorkerConfig(concurrency=2, shutdown_grace_seconds=0))
    async with running(worker) as run:
        try:
            await entered.wait()
            worker.request_stop()
            await cleaning.wait()
            assert active == cancelled == len(identities) == 2
            assert not run.done()
            assert_locked(store)
            assert len(worker._executions) == 2
            assert (await store.get(jobs[-1].id)).status is JobStatus.PENDING
            for job in jobs[:2]:
                assert (await store.history(job.id))[0].status is AttemptStatus.RUNNING
        finally:
            release.set()
        await run
    assert active == 0 and len(identities) == 2
    entries = [json.loads(line) for line in logs.getvalue().splitlines()]
    assert get_execution_context() is None
    for job in jobs[:2]:
        assert (await store.get(job.id)).status is JobStatus.RETRY_WAIT
        assert (await store.history(job.id))[0].status is AttemptStatus.INTERRUPTED
        events = [item for item in entries if item["job_id"] == job.id]
        assert [item["event"] for item in events] == [
            "attempt_started",
            "shutdown_incomplete",
            "attempt_interrupted",
        ]
        assert all(item["attempt_number"] == 1 for item in events)


@pytest.mark.parametrize("stage", ["claim", "completion"])
async def test_grace_does_not_cancel_inflight_database_work(store, monkeypatch, stage):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def function(data: Input) -> None:
        calls.append(data.value)

    tasks = registry(function)
    job = await store.submit(tasks.get("demo"), {"value": 1})
    pending = await store.submit(tasks.get("demo"), {"value": 2})
    original = store.claim_next if stage == "claim" else store.finish_attempt

    async def pause(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(store, "claim_next" if stage == "claim" else "finish_attempt", pause)
    worker = Worker(store, tasks, WorkerConfig(concurrency=1, shutdown_grace_seconds=0))
    grace_expired = asyncio.Event()
    original_shutdown = worker._shutdown

    async def observe_grace():
        await original_shutdown()
        grace_expired.set()

    monkeypatch.setattr(worker, "_shutdown", observe_grace)
    async with running(worker) as run:
        try:
            await entered.wait()
            worker.request_stop()
            # Wait for the actual grace expiry, not a wall-clock guess.
            await grace_expired.wait()
            run.cancel()
            await asyncio.sleep(0)
            assert not run.done()
            assert_locked(store)
            assert (await store.get(pending.id)).attempt_count == 0
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await run
    done = await store.get(job.id)
    assert done.attempt_count == 1
    if stage == "claim":
        assert calls == [] and done.status is JobStatus.FAILED
        assert (await store.history(job.id))[0].status is AttemptStatus.INTERRUPTED
    else:
        assert calls == [1] and done.status is JobStatus.SUCCESS


async def stop_when_empty(store, tasks, monkeypatch):
    original = store.claim_next
    worker = Worker(store, tasks, WorkerConfig(concurrency=1))

    async def claim():
        result = await original()
        if result is None:
            worker.request_stop()
        return result

    monkeypatch.setattr(store, "claim_next", claim)
    await worker.run()


async def test_recovery_preserves_prior_history_waits_terminal_states_and_snapshots(
    store, clock, monkeypatch
):
    async def function(data: Input) -> None:
        pytest.fail("not-yet-due or terminal Job executed")

    tasks = registry(function, max_retries=2, retry_base_seconds=2, retry_cap_seconds=3)
    jobs = []
    for disposition in (JobStatus.RETRY_WAIT, JobStatus.SUCCESS, JobStatus.FAILED, None):
        job = await store.submit(tasks.get("demo"), {"value": len(jobs)})
        claim = await store.claim_next()
        if disposition is not None:
            await store.finish_attempt(
                job.id,
                claim.number,
                AttemptOutcome(AttemptStatus.SUCCESS)
                if disposition is JobStatus.SUCCESS
                else AttemptOutcome(
                    AttemptStatus.FAILED, ErrorKind.TASK_ERROR, error_message="old"
                ),
                status=disposition,
                retry_delay_seconds=20 if disposition is JobStatus.RETRY_WAIT else None,
            )
        jobs.append(await store.get(job.id))
    # The last Job has a completed failure followed by a consumed RUNNING retry.
    orphan = jobs[-1]
    await store.finish_attempt(
        orphan.id,
        1,
        AttemptOutcome(AttemptStatus.FAILED, ErrorKind.TASK_ERROR, error_message="old"),
        status=JobStatus.RETRY_WAIT,
        retry_delay_seconds=0.125,
    )
    clock[0] += 125_000
    claim = await store.claim_next()
    assert claim.job_id == orphan.id and claim.number == 2
    before_history = await store.history(orphan.id)
    clock[0] += 1_000_000
    await stop_when_empty(store, registry(function), monkeypatch)
    for untouched in jobs[:-1]:
        assert await store.get(untouched.id) == untouched
    done = await store.get(orphan.id)
    history = await store.history(orphan.id)
    assert history[0] == before_history[0]
    assert history[1].status is AttemptStatus.INTERRUPTED
    assert len(history) == done.attempt_count == 2
    assert done.next_run_at == history[1].finished_at + timedelta(seconds=3)
    assert done.started_at == orphan.started_at and done.params_json == orphan.params_json
    assert (done.max_retries, done.retry_base_seconds, done.retry_cap_seconds) == (2, 2, 3)
    snapshot = done
    await stop_when_empty(store, registry(function), monkeypatch)
    assert await store.get(orphan.id) == snapshot
    assert await store.history(orphan.id) == history


@pytest.mark.parametrize("stage", ["read", "attempt", "job"])
async def test_recovery_sql_fault_rolls_back_and_prevents_claims(store, monkeypatch, stage):
    async def function(data: Input) -> None:
        pytest.fail("execution after recovery SQL failure")

    tasks = registry(function, max_retries=1)
    job = await store.submit(tasks.get("demo"), {"value": 1})
    claim = await store.claim_next()
    pending = await store.submit(tasks.get("demo"), {"value": 2})
    if stage == "read":
        # Exercise a real SELECT failure while retaining a queryable Job/Attempt pair.
        original = store.get_running_attempt

        async def broken_read():
            with sqlite3.connect(store.config.database_path) as connection:
                connection.execute("ALTER TABLE jobs RENAME TO hidden_jobs")
            try:
                return await original()
            finally:
                with sqlite3.connect(store.config.database_path) as connection:
                    connection.execute("ALTER TABLE hidden_jobs RENAME TO jobs")

        monkeypatch.setattr(store, "get_running_attempt", broken_read)
    else:
        table = "attempts" if stage == "attempt" else "jobs"
        with sqlite3.connect(store.config.database_path) as connection:
            connection.execute(
                f"CREATE TRIGGER fail_recovery AFTER UPDATE ON {table} "
                "BEGIN SELECT RAISE(FAIL, 'recovery broke'); END"
            )
    with pytest.raises(StorageError) as error:
        await Worker(store, tasks).run()
    assert error.value.__cause__ is not None
    assert await store.get(job.id) == claim.job
    assert (await store.history(job.id))[0].status is AttemptStatus.RUNNING
    assert (await store.get(pending.id)).attempt_count == 0
    with WorkerLock(store.config.database_path):
        pass


@pytest.mark.parametrize("persisted", [False, True])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_recovery_uncertain_commit_preserves_credentials_and_stops(
    store, clock, monkeypatch, persisted, cancelled, logs
):
    async def function(data: Input) -> None:
        pytest.fail("execution after uncertain recovery")

    tasks = registry(function, max_retries=1, retry_base_seconds=2)
    job = await store.submit(tasks.get("demo"), {"value": 1})
    await store.claim_next()
    pending = await store.submit(tasks.get("demo"), {"value": 2})
    original = AsyncSession.commit
    commits = 0

    async def lose_ack(session):
        nonlocal commits
        commits += 1
        if commits == 2:  # Schema validation, then recovery completion.
            if persisted:
                await original(session)
            if cancelled:
                raise asyncio.CancelledError
            raise OSError("recovery acknowledgement lost")
        await original(session)

    monkeypatch.setattr(AsyncSession, "commit", lose_ack)
    with pytest.raises(CompletionUncertainError) as error:
        await Worker(store, tasks).run()
    assert commits == 2  # No automatic completion resubmission or claims.
    assert (error.value.job_id, error.value.number) == (job.id, 1)
    assert isinstance(error.value.__cause__, asyncio.CancelledError if cancelled else OSError)
    async with SQLiteStore(store.config) as reader:
        done = await reader.get(job.id)
        (attempt,) = await reader.history(job.id)
        assert done.status is (JobStatus.RETRY_WAIT if persisted else JobStatus.RUNNING)
        assert attempt.status is (AttemptStatus.INTERRUPTED if persisted else AttemptStatus.RUNNING)
        assert done.attempt_count == 1
        if persisted:
            assert done.next_run_at == attempt.finished_at + timedelta(seconds=2)
        assert (await reader.get(pending.id)).attempt_count == 0
    entry = json.loads(logs.getvalue().splitlines()[-1])
    assert entry["job_id"] == job.id and entry["attempt_number"] == 1
    assert entry["error_kind"] == "STORAGE_ERROR"


async def test_recovery_cancellation_before_commit_rolls_back(store, monkeypatch):
    async def function(data: Input) -> None:
        pass

    tasks = registry(function, max_retries=1)
    job = await store.submit(tasks.get("demo"), {"value": 1})
    claim = await store.claim_next()
    original = AsyncSession.flush
    cancellation = asyncio.CancelledError("before recovery commit")

    async def cancel_after_attempt_flush(session, *args, **kwargs):
        await original(session, *args, **kwargs)
        if any(row.status == "INTERRUPTED" for row in session.identity_map.values()):
            raise cancellation

    monkeypatch.setattr(AsyncSession, "flush", cancel_after_attempt_flush)
    with pytest.raises(asyncio.CancelledError) as error:
        await Worker(store, tasks).run()
    assert error.value is cancellation
    assert await store.get(job.id) == claim.job
    assert (await store.history(job.id))[0].status is AttemptStatus.RUNNING


async def test_external_repeated_cancellation_during_recovery_drains_commit(store, monkeypatch):
    async def function(data: Input) -> None:
        pytest.fail("claim started after external stop during recovery")

    tasks = registry(function, max_retries=1)
    job = await store.submit(tasks.get("demo"), {"value": 1})
    await store.claim_next()
    entered, release = asyncio.Event(), asyncio.Event()
    original = storage._commit
    commits = 0

    async def pause(session):
        nonlocal commits
        commits += 1
        if commits == 2:
            entered.set()
            await release.wait()
        await original(session)

    monkeypatch.setattr(storage, "_commit", pause)
    worker = Worker(store, tasks, WorkerConfig(shutdown_grace_seconds=0))
    async with running(worker) as run:
        try:
            await entered.wait()
            run.cancel()
            await worker._stop.wait()
            run.cancel()
            await asyncio.sleep(0)
            assert not run.done()
            assert_locked(store)
            assert (await store.get(job.id)).status is JobStatus.RUNNING
            assert (await store.history(job.id))[0].status is AttemptStatus.RUNNING
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await run
    assert (await store.get(job.id)).status is JobStatus.RETRY_WAIT
    assert (await store.history(job.id))[0].status is AttemptStatus.INTERRUPTED


@pytest.mark.parametrize("stage", ["claim", "completion", "interruption"])
@pytest.mark.parametrize("persisted", [False, True])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_uncertain_commit_after_stop_never_reexecutes_or_resubmits(
    store, monkeypatch, stage, persisted, cancelled
):
    entered, release, user_entered = (asyncio.Event() for _ in range(3))
    calls = []

    async def function(data: Input) -> None:
        calls.append(data.value)
        user_entered.set()
        if stage == "interruption":
            await asyncio.Event().wait()

    tasks = registry(function, max_retries=1)
    job = await store.submit(tasks.get("demo"), {"value": 1})
    pending = await store.submit(tasks.get("demo"), {"value": 2})
    original = AsyncSession.commit
    commits = 0
    injected = asyncio.CancelledError("commit cancelled") if cancelled else OSError("lost ack")

    async def lose_ack(session):
        nonlocal commits
        commits += 1
        if commits == (2 if stage == "claim" else 3):
            entered.set()
            await release.wait()
            if persisted:
                await original(session)
            raise injected
        await original(session)

    monkeypatch.setattr(AsyncSession, "commit", lose_ack)
    worker = Worker(store, tasks, WorkerConfig(concurrency=1, shutdown_grace_seconds=0))
    grace_done = asyncio.Event()
    original_shutdown = worker._shutdown

    async def observe_grace():
        await original_shutdown()
        grace_done.set()

    monkeypatch.setattr(worker, "_shutdown", observe_grace)
    error_type = ClaimUncertainError if stage == "claim" else CompletionUncertainError
    async with running(worker) as run:
        try:
            if stage == "interruption":
                await user_entered.wait()
                worker.request_stop()
            await entered.wait()
            worker.request_stop()
            await grace_done.wait()
            assert not run.done()
            assert_locked(store)
        finally:
            release.set()
        with pytest.raises(error_type) as error:
            await run
    assert error.value.__cause__ is injected
    assert (error.value.job_id, error.value.number) == (job.id, 1)
    assert commits == (2 if stage == "claim" else 3)
    assert calls == ([] if stage == "claim" else [1])
    assert (await store.get(pending.id)).attempt_count == 0
    async with SQLiteStore(store.config) as reader:
        done, history = await reader.get(job.id), await reader.history(job.id)
    if stage == "claim":
        assert done.status is (JobStatus.RUNNING if persisted else JobStatus.PENDING)
        assert len(history) == int(persisted)
    else:
        target = JobStatus.RETRY_WAIT if stage == "interruption" else JobStatus.SUCCESS
        attempt_target = (
            AttemptStatus.INTERRUPTED if stage == "interruption" else AttemptStatus.SUCCESS
        )
        assert done.status is (target if persisted else JobStatus.RUNNING)
        assert history[0].status is (attempt_target if persisted else AttemptStatus.RUNNING)


@pytest.mark.parametrize("fault", ["sql", "executor"])
async def test_shutdown_fault_does_not_fabricate_completion_or_retry(store, fault):
    entered, release = asyncio.Event(), asyncio.Event()

    async def function(data: Input) -> None:
        entered.set()
        if fault == "executor":
            await release.wait()
            return 42
        await asyncio.Event().wait()

    tasks = registry(function, max_retries=2)
    job = await store.submit(tasks.get("demo"), {"value": 1})
    if fault == "sql":
        with sqlite3.connect(store.config.database_path) as connection:
            connection.execute(
                "CREATE TRIGGER fail_shutdown AFTER UPDATE ON jobs "
                "WHEN NEW.status='RETRY_WAIT' BEGIN SELECT RAISE(FAIL, 'shutdown broke'); END"
            )
    worker = Worker(
        store,
        tasks,
        WorkerConfig(concurrency=1, shutdown_grace_seconds=1 if fault == "executor" else 0),
    )
    async with running(worker) as run:
        try:
            await entered.wait()
            worker.request_stop()
        finally:
            release.set()
        with pytest.raises(StorageError if fault == "sql" else ExecutorError) as error:
            await run
    assert error.value.__cause__ is not None
    assert (await store.get(job.id)).status is JobStatus.RUNNING
    (attempt,) = await store.history(job.id)
    assert attempt.status is AttemptStatus.RUNNING and attempt.finished_at is None


async def test_recovery_backoff_begins_after_database_lock_wait(store, clock, monkeypatch):
    async def function(data: Input) -> None:
        pass

    tasks = registry(function, max_retries=1, retry_base_seconds=0.125)
    job = await store.submit(tasks.get("demo"), {"value": 1})
    await store.claim_next()
    blocked = asyncio.Event()
    connection = sqlite3.connect(store.config.database_path, timeout=0)
    original = store.finish_attempt

    async def lock_before_completion(*args, **kwargs):
        connection.execute("BEGIN IMMEDIATE")
        return await original(*args, **kwargs)

    def observe(conn, cursor, statement, parameters, context, executemany):
        if statement == "BEGIN IMMEDIATE" and connection.in_transaction:
            blocked.set()

    event.listen(store._engine.sync_engine, "before_cursor_execute", observe)
    monkeypatch.setattr(store, "finish_attempt", lock_before_completion)
    worker = Worker(store, tasks)
    worker.request_stop()
    try:
        async with running(worker) as run:
            try:
                await blocked.wait()
                clock[0] += 5_000_000
            finally:
                connection.rollback()
            await run
    finally:
        connection.close()
        event.remove(store._engine.sync_engine, "before_cursor_execute", observe)
    (attempt,) = await store.history(job.id)
    assert attempt.finished_at == NOW + timedelta(seconds=5)
    assert (await store.get(job.id)).next_run_at == attempt.finished_at + timedelta(seconds=0.125)


PROCESS = r"""
import asyncio, json, sys
from pathlib import Path
from pydantic import BaseModel
from taskforge import *
from taskforge import storage
from taskforge.db import JobRow, AttemptRow

class Input(BaseModel):
    value: int

async def main():
    path, mode, effects = sys.argv[1:4]
    if len(sys.argv) > 4:
        storage._now_us = lambda: int(sys.argv[4])
    tasks = TaskRegistry()
    @tasks.task('demo', input_model=Input)
    async def function(data: Input) -> None:
        identity = get_execution_context()
        with Path(effects).open('a') as output:
            output.write(json.dumps([identity.job_id, identity.attempt_number, data.value]) + '\n')
            output.flush()
        if mode == 'execute':
            print('executed', flush=True)
            await asyncio.Event().wait()

    async with SQLiteStore(DatabaseConfig(database_path=path)) as store:
        worker = Worker(store, tasks, WorkerConfig(concurrency=1))
        original_commit = storage._commit
        interrupted = 0
        async def commit(session):
            nonlocal interrupted
            rows = list(session.identity_map.values())
            if mode == 'success_commit' and any(
                isinstance(row, JobRow) and row.status == 'SUCCESS' for row in rows
            ):
                print('success_pending', flush=True)
                await asyncio.Event().wait()
            if mode == 'recovery_commit' and any(
                isinstance(row, AttemptRow) and row.status == 'INTERRUPTED' for row in rows
            ):
                interrupted += 1
                if interrupted == 2:
                    print('recovery_pending', flush=True)
                    await asyncio.Event().wait()
            await original_commit(session)
            if mode == 'recovery_commit' and interrupted == 1:
                print('recovered', flush=True)
        storage._commit = commit
        original_claim, original_finish = store.claim_next, store.finish_attempt
        async def claim():
            result = await original_claim()
            if result is None:
                worker.request_stop()
            return result
        async def finish(*args, **kwargs):
            result = await original_finish(*args, **kwargs)
            if result.status == JobStatus.SUCCESS:
                worker.request_stop()
            return result
        store.claim_next, store.finish_attempt = claim, finish
        await worker.run()
        print('stopped', flush=True)
asyncio.run(main())
"""


@asynccontextmanager
async def process(path, mode, effects, now=None):
    args = [] if now is None else [str(now)]
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-c",
        PROCESS,
        str(path),
        mode,
        str(effects),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        yield child
    finally:
        if child.returncode is None:
            child.kill()
        await child.communicate()


async def kill(child):
    child.kill()
    _, stderr = await child.communicate()
    assert child.returncode == -9, stderr.decode()


@pytest.mark.parametrize("mode", ["execute", "success_commit"])
@pytest.mark.parametrize("retries", [0, 1])
async def test_killed_worker_recovers_budget_and_demonstrates_duplicate_side_effects(
    store, tmp_path, mode, retries
):
    async def function(data: Input) -> None:
        pass

    tasks = registry(function, max_retries=retries, retry_base_seconds=60, retry_cap_seconds=60)
    job = await store.submit(tasks.get("demo"), {"value": 7})
    effects = tmp_path / "effects.jsonl"
    path = store.config.database_path
    async with process(path, mode, effects) as child:
        assert await child.stdout.readline() == (
            b"executed\n" if mode == "execute" else b"success_pending\n"
        )
        before = await store.get(job.id)
        assert before.status is JobStatus.RUNNING and before.attempt_count == 1
        assert_locked(store)
        await kill(child)
    assert json.loads(effects.read_text()) == [job.id, 1, 7]
    lock_path = path.with_suffix(".sqlite.lock")
    inode = lock_path.stat().st_ino
    # A new actual Worker process acquires the same lock and performs recovery.
    async with process(path, "recover", effects) as child:
        stdout, stderr = await child.communicate()
        assert child.returncode == 0 and stdout == b"stopped\n", stderr.decode()
    done = await store.get(job.id)
    (attempt,) = await store.history(job.id)
    assert (
        attempt.status is AttemptStatus.INTERRUPTED and attempt.error_kind is ErrorKind.INTERRUPTED
    )
    assert done.status is (JobStatus.RETRY_WAIT if retries else JobStatus.FAILED)
    assert done.attempt_count == 1 and done.started_at == before.started_at
    assert attempt.started_at == before.started_at and attempt.finished_at >= before.started_at
    assert done.id == job.id and done.params_json == job.params_json
    assert done.max_retries == retries and done.retry_base_seconds == 60
    assert lock_path.stat().st_ino == inode
    if not retries:
        assert done.finished_at == attempt.finished_at and done.next_run_at is None
        return
    assert done.next_run_at == attempt.finished_at + timedelta(seconds=60)
    due = storage._epoch_us(done.next_run_at)
    async with process(path, "recover", effects, due - 1) as child:
        stdout, stderr = await child.communicate()
        assert child.returncode == 0 and stdout == b"stopped\n", stderr.decode()
    assert await store.get(job.id) == done  # Waiting time is retained across another restart.
    async with process(path, "resume", effects, due) as child:
        stdout, stderr = await child.communicate()
        assert child.returncode == 0 and stdout == b"stopped\n", stderr.decode()
    assert [json.loads(line) for line in effects.read_text().splitlines()] == [
        [job.id, 1, 7],
        [job.id, 2, 7],
    ]
    history = await store.history(job.id)
    success = await store.get(job.id)
    assert success.status is JobStatus.SUCCESS and success.attempt_count == len(history) == 2
    assert success.started_at == before.started_at
    assert success.last_error_kind is success.last_error_message is None
    assert history[0] == attempt and history[1].status is AttemptStatus.SUCCESS


async def test_recovery_killed_again_keeps_committed_jobs_and_rolls_back_pending_job(
    store, tmp_path, clock
):
    async def function(data: Input) -> None:
        pass

    tasks = registry(function, max_retries=1, retry_base_seconds=60, retry_cap_seconds=60)
    jobs, claims = [], []
    for value in range(3):
        job = await store.submit(tasks.get("demo"), {"value": value})
        claims.append(await store.claim_next())
        jobs.append(job)
        clock[0] += 1
    effects = tmp_path / "effects.jsonl"
    path = store.config.database_path
    async with process(path, "recovery_commit", effects, clock[0]) as child:
        assert await child.stdout.readline() == b"recovered\n"
        assert await child.stdout.readline() == b"recovery_pending\n"
        first = await store.get(jobs[0].id)
        first_history = await store.history(first.id)
        assert first.status is JobStatus.RETRY_WAIT
        for job in jobs[1:]:
            assert (await store.get(job.id)).status is JobStatus.RUNNING
            assert (await store.history(job.id))[0].status is AttemptStatus.RUNNING
        await kill(child)
    async with process(path, "recover", effects, clock[0] + 10_000_000) as child:
        stdout, stderr = await child.communicate()
        assert child.returncode == 0 and stdout == b"stopped\n", stderr.decode()
    assert await store.get(first.id) == first
    assert await store.history(first.id) == first_history
    for job, claim in zip(jobs[1:], claims[1:], strict=True):
        done = await store.get(job.id)
        (attempt,) = await store.history(job.id)
        assert attempt.status is AttemptStatus.INTERRUPTED and done.status is JobStatus.RETRY_WAIT
        assert done.attempt_count == 1 and done.started_at == claim.job.started_at
        assert done.next_run_at == attempt.finished_at + timedelta(seconds=60)
        assert attempt.finished_at == storage._datetime(clock[0] + 10_000_000)
    assert not effects.exists()
