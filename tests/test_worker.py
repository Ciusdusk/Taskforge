import asyncio
import io
import json
import logging
import sqlite3
import sys
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from pydantic import BaseModel, ConfigDict, ValidationError
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
    SchemaError,
    SQLiteStore,
    StorageError,
    TaskRegistry,
    Worker,
    WorkerConfig,
    WorkerLockError,
    storage,
)
from taskforge.locking import WorkerLock

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int


@pytest_asyncio.fixture
async def store(tmp_path):
    async with SQLiteStore(DatabaseConfig(database_path=tmp_path / "jobs.sqlite")) as result:
        await result.initialize()
        yield result


@pytest_asyncio.fixture(autouse=True)
async def test_deadline():
    async with asyncio.timeout(10):
        yield


def registry(function, *, timeout=2):
    result = TaskRegistry()
    result.task(
        "demo", input_model=Input, policy=ExecutionPolicy(max_retries=3, timeout_seconds=timeout)
    )(function)
    return result


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


@pytest.mark.parametrize(
    "settings",
    [
        {"concurrency": 0},
        {"concurrency": True},
        {"concurrency": "2"},
        {"concurrency": 1.5},
        {"poll_interval_seconds": 0},
        {"poll_interval_seconds": True},
        {"poll_interval_seconds": "1"},
        {"poll_interval_seconds": float("inf")},
        {"poll_interval_seconds": float("nan")},
        {"extra": 1},
    ],
)
async def test_worker_configuration_rejects_invalid_values(settings):
    with pytest.raises(ValidationError):
        WorkerConfig(**settings)


async def test_overlap_bounded_slots_and_no_transaction_during_user_code(store):
    entered, release = asyncio.Event(), asyncio.Event()
    active = maximum = 0
    calls = []

    async def function(data: Input) -> None:
        nonlocal active, maximum
        calls.append(data.value)
        active += 1
        maximum = max(maximum, active)
        if active == 2:
            entered.set()
        try:
            await release.wait()
        finally:
            active -= 1

    tasks = registry(function)
    jobs = [await store.submit(tasks.get("demo"), {"value": i}) for i in range(5)]
    worker = Worker(store, tasks, WorkerConfig(concurrency=2))
    async with running(worker) as run:
        try:
            await entered.wait()
            assert maximum == active == 2
            assert len(calls) == 2
            assert sum(job.status is JobStatus.RUNNING for job in await store.list()) == 2
            # A real independent writer can acquire IMMEDIATE while user code is suspended.
            with sqlite3.connect(store.config.database_path, timeout=0) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("UPDATE jobs SET params_json=params_json")
                connection.commit()
            worker.request_stop()
        finally:
            release.set()
        await run
    assert len(calls) == len(set(calls)) == 2
    assert [(await store.get(job.id)).status for job in jobs] == [
        JobStatus.SUCCESS,
        JobStatus.SUCCESS,
        JobStatus.PENDING,
        JobStatus.PENDING,
        JobStatus.PENDING,
    ]


async def test_slot_and_lock_held_until_completion_commit(store, monkeypatch):
    committing, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def function(data: Input) -> None:
        calls.append(data.value)

    tasks = registry(function)
    first = await store.submit(tasks.get("demo"), {"value": 1})
    second = await store.submit(tasks.get("demo"), {"value": 2})
    original = storage._commit
    commits = 0

    async def pause(session):
        nonlocal commits
        commits += 1
        # Existing-schema check, claim, then completion.
        if commits == 3:
            committing.set()
            await release.wait()
        await original(session)

    monkeypatch.setattr(storage, "_commit", pause)
    worker = Worker(store, tasks, WorkerConfig(concurrency=1))
    async with running(worker) as run:
        try:
            await committing.wait()
            assert calls == [1]
            assert (await store.get(first.id)).status is JobStatus.RUNNING
            assert (await store.get(second.id)).status is JobStatus.PENDING
            worker.request_stop()
            assert not run.done()
            with pytest.raises(WorkerLockError):
                with WorkerLock(store.config.database_path):
                    pytest.fail("lock released during commit")
        finally:
            release.set()
        await run
    assert (await store.get(first.id)).status is JobStatus.SUCCESS
    assert (await store.get(second.id)).attempt_count == 0
    with WorkerLock(store.config.database_path):
        assert store.config.database_path.with_suffix(".sqlite.lock").exists()


async def test_timeout_finally_retains_slot_and_lock(store):
    cleaning, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def function(data: Input) -> None:
        calls.append(data.value)
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    tasks = registry(function, timeout=0.02)
    first = await store.submit(tasks.get("demo"), {"value": 1})
    second = await store.submit(tasks.get("demo"), {"value": 2})
    worker = Worker(store, tasks, WorkerConfig(concurrency=1))
    async with running(worker) as run:
        try:
            await cleaning.wait()
            assert calls == [1] and not run.done()
            assert (await store.get(first.id)).status is JobStatus.RUNNING
            assert (await store.get(second.id)).status is JobStatus.PENDING
            worker.request_stop()
            with pytest.raises(WorkerLockError):
                with WorkerLock(store.config.database_path):
                    pytest.fail("lock released during user finally")
        finally:
            release.set()
        await run
    assert (await store.get(first.id)).last_error_kind is ErrorKind.TIMEOUT
    assert (await store.history(first.id))[0].error_kind is ErrorKind.TIMEOUT
    assert (await store.get(second.id)).attempt_count == 0


async def test_success_business_errors_timeout_and_configuration_history(store, monkeypatch, logs):
    calls = []
    finished = asyncio.Event()

    async def function(data: Input) -> None:
        calls.append(data.value)
        if data.value == 1:
            raise ValueError("business failure")
        if data.value == 2:
            raise TimeoutError("user timeout")
        if data.value == 3:
            await asyncio.Event().wait()

    tasks = registry(function, timeout=0.02)
    jobs = [await store.submit(tasks.get("demo"), {"value": i}) for i in range(7)]
    with sqlite3.connect(store.config.database_path) as connection:
        connection.execute("UPDATE jobs SET task_name='missing' WHERE id=?", (jobs[4].id,))
        connection.execute("UPDATE jobs SET params_json='{}' WHERE id=?", (jobs[5].id,))
    original = store.finish_attempt
    count = 0
    worker = Worker(store, tasks, WorkerConfig(concurrency=2))

    async def observe(*args, **kwargs):
        nonlocal count
        result = await original(*args, **kwargs)
        count += 1
        if count == len(jobs):
            worker.request_stop()
            finished.set()
        return result

    monkeypatch.setattr(store, "finish_attempt", observe)
    async with running(worker) as run:
        await finished.wait()
        await run
    kinds = [
        None,
        ErrorKind.TASK_ERROR,
        ErrorKind.TASK_ERROR,
        ErrorKind.TIMEOUT,
        ErrorKind.CONFIG_ERROR,
        ErrorKind.CONFIG_ERROR,
        None,
    ]
    for job, kind in zip(jobs, kinds, strict=True):
        done = await store.get(job.id)
        (attempt,) = await store.history(job.id)
        assert done.status is (JobStatus.SUCCESS if kind is None else JobStatus.FAILED)
        assert attempt.status.value == done.status.value
        assert done.attempt_count == 1 and done.last_error_kind is attempt.error_kind is kind
        assert done.finished_at == attempt.finished_at and done.next_run_at is None
        if kind is None:
            assert attempt.exception_type is attempt.error_message is attempt.traceback is None
        if job.id == jobs[2].id:
            assert attempt.exception_type == "TimeoutError" and "user timeout" in attempt.traceback
    assert sorted(calls) == [0, 1, 2, 3, 6]
    entries = [json.loads(line) for line in logs.getvalue().splitlines()]
    assert "value" not in logs.getvalue()
    for job in jobs:
        events = [item for item in entries if item["job_id"] == job.id]
        assert [item["event"] for item in events] == ["attempt_started", "attempt_finished"]
        assert all(item["attempt_number"] == 1 for item in events)


async def test_empty_queue_polling_delayed_and_stop_wakes_idle_slots(store, monkeypatch):
    first, second = asyncio.Event(), asyncio.Event()
    ticks = []
    original = store.claim_next

    async def observe():
        ticks.append(asyncio.get_running_loop().time())
        (first if len(ticks) == 1 else second).set()
        return await original()

    monkeypatch.setattr(store, "claim_next", observe)
    worker = Worker(store, TaskRegistry(), WorkerConfig(concurrency=1, poll_interval_seconds=0.1))
    async with running(worker) as run:
        await first.wait()
        await second.wait()
        assert len(ticks) == 2 and ticks[1] - ticks[0] >= 0.09
        worker.request_stop()
        await run
    assert len(ticks) == 2


async def test_stop_before_run_claims_nothing_and_instance_runs_once(store, monkeypatch):
    async def forbidden():
        pytest.fail("claim after stop")

    monkeypatch.setattr(store, "claim_next", forbidden)
    worker = Worker(store, TaskRegistry())
    worker.request_stop()
    await worker.run()
    with pytest.raises(RuntimeError, match="only run once"):
        await worker.run()


@pytest.mark.parametrize("stage", ["claim", "finish"])
async def test_real_sql_failure_stops_without_reexecution(store, stage, logs):
    calls = []

    async def function(data: Input) -> None:
        calls.append(data.value)

    tasks = registry(function)
    first = await store.submit(tasks.get("demo"), {"value": 1})
    second = await store.submit(tasks.get("demo"), {"value": 2})
    with sqlite3.connect(store.config.database_path) as connection:
        connection.execute(
            "CREATE TRIGGER fail_worker AFTER INSERT ON attempts "
            "BEGIN SELECT RAISE(FAIL, 'claim broke'); END"
            if stage == "claim"
            else "CREATE TRIGGER fail_worker AFTER UPDATE ON jobs WHEN NEW.status='SUCCESS' "
            "BEGIN SELECT RAISE(FAIL, 'finish broke'); END"
        )
    worker = Worker(store, tasks, WorkerConfig(concurrency=1))
    with pytest.raises(StorageError) as error:
        await worker.run()
    assert error.value.__cause__ is not None
    assert calls == ([] if stage == "claim" else [1])
    assert (await store.get(second.id)).attempt_count == 0
    assert (await store.get(first.id)).status is (
        JobStatus.PENDING if stage == "claim" else JobStatus.RUNNING
    )
    entries = [json.loads(line) for line in logs.getvalue().splitlines()]
    assert entries[-1]["error_kind"] == "STORAGE_ERROR"
    if stage == "finish":
        assert entries[-1]["job_id"] == first.id and entries[-1]["attempt_number"] == 1


@pytest.mark.parametrize("stage", ["claim", "finish"])
@pytest.mark.parametrize("persisted", [False, True])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_uncertain_commit_stops_with_queryable_identity(
    store, monkeypatch, logs, stage, persisted, cancelled
):
    calls = []

    async def function(data: Input) -> None:
        calls.append(data.value)

    tasks = registry(function)
    first = await store.submit(tasks.get("demo"), {"value": 1})
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
            raise OSError("lost acknowledgement")
        await original(session)

    monkeypatch.setattr(AsyncSession, "commit", lose_ack)
    worker = Worker(store, tasks, WorkerConfig(concurrency=1))
    error_type = ClaimUncertainError if stage == "claim" else CompletionUncertainError
    with pytest.raises(error_type) as error:
        await worker.run()
    assert error.value.job_id == first.id and error.value.number == 1
    assert isinstance(error.value.__cause__, asyncio.CancelledError if cancelled else OSError)
    assert calls == ([] if stage == "claim" else [1])
    assert (await store.get(second.id)).attempt_count == 0
    async with SQLiteStore(store.config) as reader:
        done, history = await reader.get(first.id), await reader.history(first.id)
    expected = (
        (JobStatus.RUNNING if persisted else JobStatus.PENDING)
        if stage == "claim"
        else (JobStatus.SUCCESS if persisted else JobStatus.RUNNING)
    )
    assert done.status is expected
    assert len(history) == (0 if expected is JobStatus.PENDING else 1)
    entry = json.loads(logs.getvalue().splitlines()[-1])
    assert entry["job_id"] == first.id and entry["attempt_number"] == 1
    assert entry["error_kind"] == "STORAGE_ERROR"


async def test_executor_fault_stops_new_claims_and_drains_other_user(store, logs):
    entered, fail, cleaned, release = (asyncio.Event() for _ in range(4))
    calls = []

    async def function(data: Input) -> None:
        calls.append(data.value)
        if data.value == 1:
            await entered.wait()
            fail.set()
            return 42
        entered.set()
        try:
            await release.wait()
        finally:
            cleaned.set()

    tasks = registry(function)
    jobs = [await store.submit(tasks.get("demo"), {"value": i}) for i in (1, 2, 3)]
    worker = Worker(store, tasks, WorkerConfig(concurrency=2))
    async with running(worker) as run:
        try:
            await fail.wait()
            # Wait for the actual fatal event, not just the user returning.
            await worker._stop.wait()
            assert not run.done() and not cleaned.is_set()
            assert (await store.get(jobs[2].id)).attempt_count == 0
            with pytest.raises(WorkerLockError):
                with WorkerLock(store.config.database_path):
                    pytest.fail("lock released while another user is active")
        finally:
            release.set()
        with pytest.raises(ExecutorError) as error:
            await run
    assert isinstance(error.value.__cause__, TypeError)
    assert calls == [1, 2] and cleaned.is_set()
    assert (await store.get(jobs[0].id)).status is JobStatus.RUNNING
    assert (await store.get(jobs[1].id)).status is JobStatus.SUCCESS
    assert (await store.get(jobs[2].id)).status is JobStatus.PENDING
    entry = next(
        json.loads(line)
        for line in logs.getvalue().splitlines()
        if json.loads(line)["event"] == "worker_error"
    )
    assert entry["error_kind"] == "EXECUTOR_ERROR"
    assert entry["job_id"] == jobs[0].id and entry["attempt_number"] == 1


async def test_database_fault_drains_started_user_and_blocks_waiting_claims(store, monkeypatch):
    entered, release, failed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    async def function(data: Input) -> None:
        calls.append(data.value)
        entered.set()
        await release.wait()

    tasks = registry(function)
    jobs = [await store.submit(tasks.get("demo"), {"value": i}) for i in range(3)]
    original = store.claim_next
    claims = 0

    async def fault():
        nonlocal claims
        claims += 1
        if claims == 2:
            await entered.wait()
            try:
                raise OSError("database unavailable")
            except OSError as exc:
                failed.set()
                raise StorageError("claim unavailable") from exc
        return await original()

    monkeypatch.setattr(store, "claim_next", fault)
    worker = Worker(store, tasks, WorkerConfig(concurrency=3))
    async with running(worker) as run:
        try:
            await failed.wait()
            await worker._stop.wait()
            assert claims == 2 and calls == [0] and not run.done()
        finally:
            release.set()
        with pytest.raises(StorageError) as error:
            await run
    assert isinstance(error.value.__cause__, OSError)
    assert (await store.get(jobs[0].id)).status is JobStatus.SUCCESS
    assert [(await store.get(job.id)).attempt_count for job in jobs[1:]] == [0, 0]


async def test_repeated_run_cancellation_waits_for_user_and_commit(store, monkeypatch):
    entered, release, committing, commit_release = (asyncio.Event() for _ in range(4))
    user_cancelled = False

    async def function(data: Input) -> None:
        nonlocal user_cancelled
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            user_cancelled = True
            raise

    tasks = registry(function)
    job = await store.submit(tasks.get("demo"), {"value": 1})
    original = store.finish_attempt

    async def pause(*args, **kwargs):
        committing.set()
        await commit_release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(store, "finish_attempt", pause)
    worker = Worker(store, tasks, WorkerConfig(concurrency=1))
    async with running(worker) as run:
        try:
            await entered.wait()
            run.cancel()
            await worker._stop.wait()
            run.cancel()
            await asyncio.sleep(0)
            assert not run.done() and not user_cancelled
            release.set()
            await committing.wait()
            run.cancel()
            await asyncio.sleep(0)
            assert not run.done()
            with pytest.raises(WorkerLockError):
                with WorkerLock(store.config.database_path):
                    pytest.fail("cancel released lock during completion")
        finally:
            release.set()
            commit_release.set()
        with pytest.raises(asyncio.CancelledError):
            await run
    assert not user_cancelled and (await store.get(job.id)).status is JobStatus.SUCCESS


async def test_stop_during_claim_drains_committed_claim_without_starting_another(
    store, monkeypatch
):
    claiming, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def function(data: Input) -> None:
        calls.append(data.value)

    tasks = registry(function)
    first = await store.submit(tasks.get("demo"), {"value": 1})
    second = await store.submit(tasks.get("demo"), {"value": 2})
    original = store.claim_next

    async def pause():
        claiming.set()
        await release.wait()
        return await original()

    monkeypatch.setattr(store, "claim_next", pause)
    worker = Worker(store, tasks, WorkerConfig(concurrency=3))
    async with running(worker) as run:
        try:
            await claiming.wait()
            worker.request_stop()
            assert not run.done()
        finally:
            release.set()
        await run
    assert calls == [1]
    assert (await store.get(first.id)).status is JobStatus.SUCCESS
    assert (await store.get(second.id)).attempt_count == 0


async def test_self_cancelled_user_stops_without_fabricating_outcome(store):
    async def function(data: Input) -> None:
        raise asyncio.CancelledError

    tasks = registry(function)
    job = await store.submit(tasks.get("demo"), {"value": 1})
    with pytest.raises(asyncio.CancelledError):
        await Worker(store, tasks, WorkerConfig(concurrency=1)).run()
    assert (await store.get(job.id)).status is JobStatus.RUNNING
    assert (await store.history(job.id))[0].status is AttemptStatus.RUNNING
    with WorkerLock(store.config.database_path):
        pass


async def test_uninitialized_schema_rejected_and_lock_released(tmp_path):
    config = DatabaseConfig(database_path=tmp_path / "uninitialized.sqlite")
    async with SQLiteStore(config) as empty:
        with pytest.raises(SchemaError):
            await Worker(empty, TaskRegistry()).run()
    with sqlite3.connect(config.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    with WorkerLock(config.database_path):
        pass


async def test_lock_open_failure_preserves_cause(tmp_path):
    config = DatabaseConfig(database_path=tmp_path / "missing" / "jobs.sqlite")
    async with SQLiteStore(config) as missing:
        with pytest.raises(WorkerLockError) as error:
            await Worker(missing, TaskRegistry()).run()
    assert isinstance(error.value.__cause__, FileNotFoundError)


async def test_existing_running_attempt_is_left_for_t08_recovery(store, monkeypatch):
    async def function(data: Input) -> None:
        pytest.fail("legacy running attempt was executed")

    tasks = registry(function)
    job = await store.submit(tasks.get("demo"), {"value": 1})
    claim = await store.claim_next()
    original = store.claim_next
    worker = Worker(store, tasks, WorkerConfig(concurrency=1))

    async def observe_empty():
        result = await original()
        assert result is None
        worker.request_stop()
        return result

    monkeypatch.setattr(store, "claim_next", observe_empty)
    await worker.run()
    assert (await store.get(job.id)) == claim.job
    assert (await store.history(job.id))[0].status is AttemptStatus.RUNNING


PROCESS = r"""
import asyncio
import sys
from pathlib import Path
from taskforge import (
    DatabaseConfig, SQLiteStore, TaskRegistry, Worker, WorkerConfig, WorkerLockError,
)

async def main():
    async with SQLiteStore(DatabaseConfig(database_path=Path(sys.argv[1]))) as store:
        worker = Worker(
            store, TaskRegistry(), WorkerConfig(concurrency=1, poll_interval_seconds=60)
        )
        original = store.claim_next
        async def claim():
            if sys.argv[2] == "contender":
                raise AssertionError("failed lock holder entered claim")
            result = await original()
            assert result is None
            print("ready", flush=True)
            return result
        store.claim_next = claim
        if sys.argv[2] == "contender":
            try:
                await worker.run()
            except WorkerLockError as exc:
                assert isinstance(exc.__cause__, BlockingIOError)
                print("blocked", flush=True)
            else:
                raise AssertionError("second worker started")
        else:
            run = asyncio.create_task(worker.run())
            try:
                await asyncio.to_thread(sys.stdin.readline)
            finally:
                worker.request_stop()
                await run
            print("stopped", flush=True)
asyncio.run(main())
"""


@asynccontextmanager
async def process(path, mode):
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-c",
        PROCESS,
        str(path),
        mode,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        yield child
    finally:
        if child.returncode is None:
            try:
                child.stdin.write(b"stop\n")
                await child.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
        try:
            await asyncio.wait_for(child.communicate(), timeout=3)
        except TimeoutError:
            child.kill()
            await child.communicate()


async def test_real_process_exclusion_release_and_different_database(store, tmp_path):
    async def function(data: Input) -> None:
        pass

    tasks = registry(function)
    other_config = DatabaseConfig(database_path=tmp_path / "other.sqlite")
    async with SQLiteStore(other_config) as other:
        await other.initialize()
    path = store.config.database_path
    lock_path = path.with_suffix(".sqlite.lock")
    async with process(path, "holder") as holder:
        assert await holder.stdout.readline() == b"ready\n"
        inode = lock_path.stat().st_ino
        job = await store.submit(tasks.get("demo"), {"value": 1})
        # A distinct process normalizes the alternate spelling to the same lock.
        async with process(
            path.parent / ".." / path.parent.name / path.name, "contender"
        ) as contender:
            stdout, stderr = await contender.communicate()
            assert contender.returncode == 0 and stdout == b"blocked\n", stderr.decode()
        assert (await store.get(job.id)).attempt_count == 0
        assert await store.history(job.id) == ()
        async with process(other_config.database_path, "holder") as independent:
            assert await independent.stdout.readline() == b"ready\n"
            independent.stdin.write(b"stop\n")
            stdout, stderr = await independent.communicate()
            assert independent.returncode == 0 and stdout == b"stopped\n", stderr.decode()
        holder.stdin.write(b"stop\n")
        stdout, stderr = await holder.communicate()
        assert holder.returncode == 0 and stdout == b"stopped\n", stderr.decode()
    assert lock_path.exists() and lock_path.stat().st_ino == inode
    # Reacquire in a fresh process after release; clear the queue for the idle probe.
    claim = await store.claim_next()
    await store.finish_attempt(
        job.id, claim.number, AttemptOutcome(AttemptStatus.SUCCESS), status=JobStatus.SUCCESS
    )
    async with process(path, "holder") as restarted:
        assert await restarted.stdout.readline() == b"ready\n"
        assert lock_path.stat().st_ino == inode
        restarted.stdin.write(b"stop\n")
        stdout, stderr = await restarted.communicate()
        assert restarted.returncode == 0 and stdout == b"stopped\n", stderr.decode()
    assert lock_path.exists()
