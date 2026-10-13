import asyncio
import sqlite3
from contextlib import AsyncExitStack
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from pydantic import BaseModel
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

import taskforge.storage as storage
from taskforge import (
    AttemptConflictError,
    AttemptOutcome,
    AttemptStatus,
    ClaimUncertainError,
    CompletionUncertainError,
    DatabaseConfig,
    ErrorKind,
    ExecutionPolicy,
    InvalidAttemptResultError,
    JobStatus,
    SQLiteStore,
    StorageError,
    TaskRegistry,
    UnknownJobError,
)
from taskforge.db import ERROR_MESSAGE_LIMIT, EXCEPTION_TYPE_LIMIT, TRACEBACK_LIMIT

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]
NOW = datetime(2026, 10, 13, 12, 0, 0, 123456, tzinfo=UTC)


class Input(BaseModel):
    count: int


async def count(data: Input) -> None:
    pytest.fail("storage executed user task code")


def definition(retries=2):
    registry = TaskRegistry()
    registry.task(
        "demo.count",
        input_model=Input,
        policy=ExecutionPolicy(
            max_retries=retries,
            retry_base_seconds=0.25,
            retry_cap_seconds=8,
            timeout_seconds=7,
        ),
    )(count)
    return registry.get("demo.count")


def set_clock(monkeypatch, now=NOW):
    monkeypatch.setattr(storage, "_now_us", lambda: storage._epoch_us(now))
    return now


@pytest_asyncio.fixture
async def store(tmp_path):
    async with SQLiteStore(DatabaseConfig(database_path=tmp_path / "queue.sqlite")) as result:
        await result.initialize()
        yield result


async def submit_claim(store, retries=2):
    job = await store.submit(definition(retries), {"count": 3})
    claim = await store.claim_next()
    assert claim is not None and claim.job_id == job.id and claim.number == 1
    return job, claim


async def test_empty_future_ineligible_and_exhausted_jobs(store):
    assert await store.claim_next() is None
    jobs = [await store.submit(definition(0), {"count": n}) for n in range(9)]
    with sqlite3.connect(store.config.database_path) as connection:
        connection.execute("UPDATE jobs SET next_run_at=2000000000000000")
        for job, status in zip(
            jobs[1:5], ("RUNNING", "SUCCESS", "FAILED", "RETRY_WAIT"), strict=True
        ):
            connection.execute(
                "UPDATE jobs SET status=?, next_run_at=? WHERE id=?",
                (status, 2000000000000000 if status == "RETRY_WAIT" else 0, job.id),
            )
        connection.execute(
            "UPDATE jobs SET next_run_at=0, attempt_count=1 WHERE id=?", (jobs[5].id,)
        )
        connection.execute("UPDATE jobs SET next_run_at=NULL WHERE id=?", (jobs[6].id,))
        connection.execute(
            "UPDATE jobs SET status='RUNNING', next_run_at=0 WHERE id=?", (jobs[7].id,)
        )
        connection.execute(
            "UPDATE jobs SET status='RETRY_WAIT', attempt_count=1, next_run_at=0 WHERE id=?",
            (jobs[8].id,),
        )
    before = await store.list()
    assert await store.claim_next() is None
    assert await store.list() == before
    assert await asyncio.gather(*(store.history(job.id) for job in jobs)) == [()] * len(jobs)


async def test_due_boundary_claim_snapshot_and_restart(store, monkeypatch):
    now = set_clock(monkeypatch)
    job, claim = await submit_claim(store)
    assert claim.job.status is JobStatus.RUNNING
    assert claim.job.started_at == now == job.created_at
    assert claim.job.finished_at is claim.job.next_run_at is None
    assert claim.job.attempt_count == claim.number == 1
    assert claim.job.params_json == job.params_json == '{"count":3}'
    assert (
        claim.job.max_retries,
        claim.job.retry_base_seconds,
        claim.job.retry_cap_seconds,
        claim.job.timeout_seconds,
    ) == (2, 0.25, 8, 7)
    (attempt,) = await store.history(job.id)
    assert attempt.number == 1 and attempt.status is AttemptStatus.RUNNING
    assert attempt.started_at == now and attempt.finished_at is None
    assert attempt.error_kind is attempt.exception_type is attempt.error_message is None
    assert attempt.traceback is None
    assert await store.claim_next() is None
    for snapshot, field, value in (
        (claim, "number", 8),
        (claim.job, "status", JobStatus.FAILED),
        (attempt, "error_message", "changed"),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(snapshot, field, value)
    await store.close()
    async with SQLiteStore(store.config) as reopened:
        assert await reopened.get(job.id) == claim.job
        assert await reopened.history(job.id) == (attempt,)
        done = await reopened.finish_attempt(
            claim.job_id,
            claim.number,
            AttemptOutcome(AttemptStatus.SUCCESS),
            status=JobStatus.SUCCESS,
        )
    async with SQLiteStore(store.config) as reopened:
        assert await reopened.get(job.id) == done
        (finished,) = await reopened.history(job.id)
        assert finished.status is AttemptStatus.SUCCESS
        assert finished.finished_at == done.finished_at == now
    assert claim.job.status is JobStatus.RUNNING
    assert attempt.status is AttemptStatus.RUNNING
    assert job.status is JobStatus.PENDING


async def test_independent_connections_claim_one_job_once(store):
    job = await store.submit(definition(), {"count": 1})
    async with AsyncExitStack() as stack:
        stores = [await stack.enter_async_context(SQLiteStore(store.config)) for _ in range(8)]
        barrier = asyncio.Barrier(len(stores))

        async def claim(contender):
            await barrier.wait()
            return await contender.claim_next()

        results = await asyncio.wait_for(
            asyncio.gather(*(claim(contender) for contender in stores)), timeout=10
        )
    winners = [result for result in results if result is not None]
    assert len(winners) == 1 and winners[0].job_id == job.id
    assert (await store.get(job.id)).attempt_count == len(await store.history(job.id)) == 1


async def test_concurrent_claims_are_unique_and_ordered_by_all_three_keys(store):
    jobs = [await store.submit(definition(), {"count": n}) for n in range(12)]
    with sqlite3.connect(store.config.database_path) as connection:
        for n, job in enumerate(jobs):
            connection.execute(
                "UPDATE jobs SET next_run_at=?, created_at=? WHERE id=?",
                (n % 3, n % 2, job.id),
            )
    expected = [
        job.id
        for n, job in sorted(
            enumerate(jobs), key=lambda pair: (pair[0] % 3, pair[0] % 2, pair[1].id)
        )
    ]
    writes = []
    ids = {job.id for job in jobs}

    def record(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE jobs"):
            writes.append(
                next(value for value in parameters if isinstance(value, str) and value in ids)
            )

    async with AsyncExitStack() as stack:
        stores = [await stack.enter_async_context(SQLiteStore(store.config)) for _ in range(4)]
        for contender in stores:
            event.listen(contender._engine.sync_engine, "before_cursor_execute", record)
        barrier = asyncio.Barrier(len(stores))

        async def drain(contender):
            await barrier.wait()
            claims = []
            while claim := await contender.claim_next():
                claims.append(claim)
            return claims

        batches = await asyncio.wait_for(
            asyncio.gather(*(drain(contender) for contender in stores)), timeout=10
        )
    claims = [claim for batch in batches for claim in batch]
    assert len(claims) == len({claim.job_id for claim in claims}) == len(jobs)
    assert writes == expected
    for job in jobs:
        assert (await store.get(job.id)).attempt_count == len(await store.history(job.id)) == 1


@pytest.mark.parametrize(
    "attempt_status,kind,job_status",
    [
        (AttemptStatus.SUCCESS, None, JobStatus.SUCCESS),
        (AttemptStatus.FAILED, ErrorKind.TASK_ERROR, JobStatus.FAILED),
        (AttemptStatus.FAILED, ErrorKind.TIMEOUT, JobStatus.FAILED),
        (AttemptStatus.FAILED, ErrorKind.CONFIG_ERROR, JobStatus.FAILED),
        (AttemptStatus.INTERRUPTED, ErrorKind.INTERRUPTED, JobStatus.FAILED),
        (AttemptStatus.FAILED, ErrorKind.TASK_ERROR, JobStatus.RETRY_WAIT),
        (AttemptStatus.FAILED, ErrorKind.TIMEOUT, JobStatus.RETRY_WAIT),
        (AttemptStatus.INTERRUPTED, ErrorKind.INTERRUPTED, JobStatus.RETRY_WAIT),
    ],
)
async def test_finish_dispositions_and_error_fields(store, attempt_status, kind, job_status):
    _, claim = await submit_claim(store)
    success = kind is None
    outcome = AttemptOutcome(
        attempt_status,
        kind,
        None if success else "DemoError",
        None if success else "failed",
        None if success else "trace",
    )
    due = datetime.now(UTC) + timedelta(hours=1) if job_status is JobStatus.RETRY_WAIT else None
    before = datetime.now(UTC)
    done = await store.finish_attempt(
        claim.job_id, claim.number, outcome, status=job_status, next_run_at=due
    )
    (attempt,) = await store.history(claim.job_id)
    assert done == await store.get(claim.job_id)
    assert done.status is job_status and attempt.status is attempt_status
    assert before <= attempt.finished_at <= datetime.now(UTC)
    assert done.started_at == claim.job.started_at == attempt.started_at
    assert done.attempt_count == 1
    assert done.last_error_kind == attempt.error_kind == kind
    assert done.last_error_message == attempt.error_message == outcome.error_message
    assert attempt.exception_type == outcome.exception_type
    assert attempt.traceback == outcome.traceback
    assert done.next_run_at == due
    assert done.finished_at == (None if due else attempt.finished_at)


async def test_retry_reclaim_stale_result_and_success_preserve_history(store):
    job, first = await submit_claim(store, retries=1)
    due = datetime.now(UTC) + timedelta(hours=1)
    failed = AttemptOutcome(AttemptStatus.FAILED, ErrorKind.TASK_ERROR, "DemoError", "old", "trace")
    waiting = await store.finish_attempt(
        job.id, 1, failed, status=JobStatus.RETRY_WAIT, next_run_at=due
    )
    assert await store.claim_next() is None
    with sqlite3.connect(store.config.database_path) as connection:
        connection.execute("UPDATE jobs SET next_run_at=0 WHERE id=?", (job.id,))
    second = await store.claim_next()
    assert second.number == 2 and second.job.started_at == first.job.started_at
    assert second.job.last_error_message == waiting.last_error_message == "old"
    before = await store.get(job.id), await store.history(job.id)
    with pytest.raises(AttemptConflictError):
        await store.finish_attempt(
            job.id, 1, AttemptOutcome(AttemptStatus.SUCCESS), status=JobStatus.SUCCESS
        )
    assert (await store.get(job.id), await store.history(job.id)) == before
    done = await store.finish_attempt(
        job.id, 2, AttemptOutcome(AttemptStatus.SUCCESS), status=JobStatus.SUCCESS
    )
    history = await store.history(job.id)
    assert done.last_error_kind is done.last_error_message is None
    assert history[0].error_message == "old" and history[0].traceback == "trace"
    assert history[1].status is AttemptStatus.SUCCESS and history[1].error_kind is None
    assert done.attempt_count == len(history) == 2
    assert await store.claim_next() is None
    assert first.job.attempt_count == 1 and waiting.status is JobStatus.RETRY_WAIT


async def test_unicode_error_fields_truncate_to_database_limits(store):
    _, claim = await submit_claim(store)
    outcome = AttemptOutcome(
        AttemptStatus.FAILED,
        ErrorKind.TASK_ERROR,
        "类" * (EXCEPTION_TYPE_LIMIT + 1),
        "错" * (ERROR_MESSAGE_LIMIT + 1),
        "栈" * (TRACEBACK_LIMIT + 1),
    )
    done = await store.finish_attempt(claim.job_id, 1, outcome, status=JobStatus.FAILED)
    (attempt,) = await store.history(claim.job_id)
    assert attempt.exception_type == "类" * EXCEPTION_TYPE_LIMIT
    assert attempt.error_message == done.last_error_message == "错" * ERROR_MESSAGE_LIMIT
    assert attempt.traceback == "栈" * TRACEBACK_LIMIT
    assert len(outcome.error_message) == ERROR_MESSAGE_LIMIT + 1


@pytest.mark.parametrize("number", [2, 99])
async def test_wrong_attempt_number_is_rejected(store, number):
    _, claim = await submit_claim(store)
    before = await store.get(claim.job_id), await store.history(claim.job_id)
    with pytest.raises(AttemptConflictError):
        await store.finish_attempt(
            claim.job_id, number, AttemptOutcome(AttemptStatus.SUCCESS), status=JobStatus.SUCCESS
        )
    assert (await store.get(claim.job_id), await store.history(claim.job_id)) == before


async def test_wrong_job_unknown_job_and_duplicate_finish(store):
    _, claim = await submit_claim(store)
    pending = await store.submit(definition(), {"count": 4})
    before = await store.list(), await store.history(claim.job_id)
    with pytest.raises(AttemptConflictError):
        await store.finish_attempt(
            pending.id, 1, AttemptOutcome(AttemptStatus.SUCCESS), status=JobStatus.SUCCESS
        )
    for job_id in (str(uuid4()), "bad", ""):
        with pytest.raises(UnknownJobError):
            await store.finish_attempt(
                job_id, 1, AttemptOutcome(AttemptStatus.SUCCESS), status=JobStatus.SUCCESS
            )
    assert (await store.list(), await store.history(claim.job_id)) == before
    done = await store.finish_attempt(
        claim.job_id, 1, AttemptOutcome(AttemptStatus.SUCCESS), status=JobStatus.SUCCESS
    )
    history = await store.history(claim.job_id)
    with pytest.raises(AttemptConflictError):
        await store.finish_attempt(
            claim.job_id,
            1,
            AttemptOutcome(AttemptStatus.FAILED, ErrorKind.TIMEOUT),
            status=JobStatus.FAILED,
        )
    assert await store.get(claim.job_id) == done and await store.history(claim.job_id) == history


async def test_concurrent_completions_only_one_wins(store):
    _, claim = await submit_claim(store)
    barrier = asyncio.Barrier(2)

    async def finish(contender):
        await barrier.wait()
        return await contender.finish_attempt(
            claim.job_id, 1, AttemptOutcome(AttemptStatus.SUCCESS), status=JobStatus.SUCCESS
        )

    async with SQLiteStore(store.config) as contender:
        results = await asyncio.gather(finish(store), finish(contender), return_exceptions=True)
    assert sum(isinstance(result, AttemptConflictError) for result in results) == 1
    assert (await store.get(claim.job_id)).status is JobStatus.SUCCESS
    assert len(await store.history(claim.job_id)) == 1


@pytest.mark.parametrize(
    "number,outcome,status,due",
    [
        (True, AttemptOutcome(AttemptStatus.SUCCESS), JobStatus.SUCCESS, None),
        (0, AttemptOutcome(AttemptStatus.SUCCESS), JobStatus.SUCCESS, None),
        (-1, AttemptOutcome(AttemptStatus.SUCCESS), JobStatus.SUCCESS, None),
        ("1", AttemptOutcome(AttemptStatus.SUCCESS), JobStatus.SUCCESS, None),
        (1.0, AttemptOutcome(AttemptStatus.SUCCESS), JobStatus.SUCCESS, None),
        (1, None, JobStatus.SUCCESS, None),
        (1, AttemptOutcome("SUCCESS"), JobStatus.SUCCESS, None),
        (1, AttemptOutcome(AttemptStatus.RUNNING), JobStatus.SUCCESS, None),
        (1, AttemptOutcome(AttemptStatus.SUCCESS), "SUCCESS", None),
        (1, AttemptOutcome(AttemptStatus.SUCCESS), JobStatus.PENDING, None),
        (1, AttemptOutcome(AttemptStatus.SUCCESS), JobStatus.RUNNING, None),
        (1, AttemptOutcome(AttemptStatus.SUCCESS), JobStatus.FAILED, None),
        (1, AttemptOutcome(AttemptStatus.SUCCESS, ErrorKind.TASK_ERROR), JobStatus.SUCCESS, None),
        (1, AttemptOutcome(AttemptStatus.SUCCESS, error_message=""), JobStatus.SUCCESS, None),
        (1, AttemptOutcome(AttemptStatus.SUCCESS, exception_type="Error"), JobStatus.SUCCESS, None),
        (1, AttemptOutcome(AttemptStatus.SUCCESS, traceback="trace"), JobStatus.SUCCESS, None),
        (1, AttemptOutcome(AttemptStatus.SUCCESS), JobStatus.SUCCESS, NOW),
        (1, AttemptOutcome(AttemptStatus.FAILED), JobStatus.FAILED, None),
        (1, AttemptOutcome(AttemptStatus.FAILED, "TASK_ERROR"), JobStatus.FAILED, None),
        (1, AttemptOutcome(AttemptStatus.FAILED, ErrorKind.TASK_ERROR), JobStatus.SUCCESS, None),
        (1, AttemptOutcome(AttemptStatus.FAILED, ErrorKind.INTERRUPTED), JobStatus.FAILED, None),
        (1, AttemptOutcome(AttemptStatus.INTERRUPTED, ErrorKind.TIMEOUT), JobStatus.FAILED, None),
        (
            1,
            AttemptOutcome(AttemptStatus.FAILED, ErrorKind.TASK_ERROR, error_message=1),
            JobStatus.FAILED,
            None,
        ),
        (
            1,
            AttemptOutcome(AttemptStatus.FAILED, ErrorKind.TASK_ERROR, exception_type=1),
            JobStatus.FAILED,
            None,
        ),
        (
            1,
            AttemptOutcome(AttemptStatus.FAILED, ErrorKind.TASK_ERROR, traceback=1),
            JobStatus.FAILED,
            None,
        ),
        (1, AttemptOutcome(AttemptStatus.FAILED, ErrorKind.TASK_ERROR), JobStatus.RETRY_WAIT, None),
        (
            1,
            AttemptOutcome(AttemptStatus.FAILED, ErrorKind.TASK_ERROR),
            JobStatus.RETRY_WAIT,
            datetime(2026, 10, 13),
        ),
        (
            1,
            AttemptOutcome(AttemptStatus.FAILED, ErrorKind.TASK_ERROR),
            JobStatus.RETRY_WAIT,
            datetime(1970, 1, 1, tzinfo=UTC),
        ),
        (
            1,
            AttemptOutcome(AttemptStatus.FAILED, ErrorKind.CONFIG_ERROR),
            JobStatus.RETRY_WAIT,
            NOW + timedelta(days=1),
        ),
    ],
)
async def test_invalid_results_preserve_records(store, number, outcome, status, due):
    _, claim = await submit_claim(store)
    before = await store.get(claim.job_id), await store.history(claim.job_id)
    with pytest.raises(InvalidAttemptResultError):
        await store.finish_attempt(claim.job_id, number, outcome, status=status, next_run_at=due)
    assert (await store.get(claim.job_id), await store.history(claim.job_id)) == before


async def test_retry_budget_exhausted_rejected_without_finishing_attempt(store):
    _, claim = await submit_claim(store, retries=0)
    before = await store.get(claim.job_id), await store.history(claim.job_id)
    with pytest.raises(InvalidAttemptResultError, match="budget"):
        await store.finish_attempt(
            claim.job_id,
            1,
            AttemptOutcome(AttemptStatus.FAILED, ErrorKind.TASK_ERROR),
            status=JobStatus.RETRY_WAIT,
            next_run_at=datetime.now(UTC) + timedelta(days=1),
        )
    assert (await store.get(claim.job_id), await store.history(claim.job_id)) == before


async def test_real_mid_claim_sql_failure_rolls_back_job_and_attempt(store):
    job = await store.submit(definition(), {"count": 1})
    with sqlite3.connect(store.config.database_path) as connection:
        connection.execute("""CREATE TRIGGER fail_claim AFTER INSERT ON attempts
            BEGIN SELECT RAISE(FAIL, 'mid claim failure'); END""")
    with pytest.raises(StorageError, match="mid claim failure") as error:
        await store.claim_next()
    assert not isinstance(error.value, ClaimUncertainError)
    assert error.value.__cause__ is not None
    assert await store.get(job.id) == job and await store.history(job.id) == ()
    with sqlite3.connect(store.config.database_path) as connection:
        connection.execute("DROP TRIGGER fail_claim")
    assert (await store.claim_next()).number == 1


async def test_real_mid_finish_sql_failure_rolls_back_attempt_and_job(store):
    _, claim = await submit_claim(store)
    before = await store.get(claim.job_id), await store.history(claim.job_id)
    with sqlite3.connect(store.config.database_path) as connection:
        connection.execute("""CREATE TRIGGER fail_finish AFTER UPDATE ON jobs
            WHEN NEW.status='SUCCESS'
            BEGIN SELECT RAISE(FAIL, 'mid finish failure'); END""")
    with pytest.raises(StorageError, match="mid finish failure") as error:
        await store.finish_attempt(
            claim.job_id, 1, AttemptOutcome(AttemptStatus.SUCCESS), status=JobStatus.SUCCESS
        )
    assert not isinstance(error.value, CompletionUncertainError)
    assert error.value.__cause__ is not None
    assert (await store.get(claim.job_id), await store.history(claim.job_id)) == before
    with sqlite3.connect(store.config.database_path) as connection:
        connection.execute("DROP TRIGGER fail_finish")
    assert (
        await store.finish_attempt(
            claim.job_id, 1, AttemptOutcome(AttemptStatus.SUCCESS), status=JobStatus.SUCCESS
        )
    ).status is JobStatus.SUCCESS


@pytest.mark.parametrize("operation", ["claim", "finish"])
async def test_state_and_history_invisible_before_commit_and_return_after_commit(
    store, monkeypatch, operation
):
    job = await store.submit(definition(), {"count": 1})
    if operation == "finish":
        await store.claim_next()
    before = await store.get(job.id), await store.history(job.id)
    ready, release = asyncio.Event(), asyncio.Event()
    original = storage._commit

    async def pause(session):
        ready.set()
        await release.wait()
        await original(session)

    monkeypatch.setattr(storage, "_commit", pause)
    task = asyncio.create_task(
        store.claim_next()
        if operation == "claim"
        else store.finish_attempt(
            job.id, 1, AttemptOutcome(AttemptStatus.SUCCESS), status=JobStatus.SUCCESS
        )
    )
    try:
        await asyncio.wait_for(ready.wait(), timeout=2)
        assert not task.done()
        async with SQLiteStore(store.config) as reader:
            assert (await reader.get(job.id), await reader.history(job.id)) == before
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=2)
    current = await store.get(job.id)
    history = await store.history(job.id)
    expected = "RUNNING" if operation == "claim" else "SUCCESS"
    assert current.status.value == history[0].status.value == expected
    assert current.attempt_count == len(history) == 1


@pytest.mark.parametrize("operation", ["claim", "finish"])
@pytest.mark.parametrize("persisted", [False, True])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_uncertain_commit_identifies_attempt(
    store, monkeypatch, operation, persisted, cancelled
):
    job = await store.submit(definition(), {"count": 1})
    if operation == "finish":
        await store.claim_next()
    before = await store.get(job.id), await store.history(job.id)
    original = AsyncSession.commit

    async def lose_ack(session):
        if persisted:
            await original(session)
        if cancelled:
            raise asyncio.CancelledError
        raise OSError("lost commit response")

    monkeypatch.setattr(AsyncSession, "commit", lose_ack)
    error_type = ClaimUncertainError if operation == "claim" else CompletionUncertainError
    with pytest.raises(error_type) as error:
        if operation == "claim":
            await store.claim_next()
        else:
            await store.finish_attempt(
                job.id, 1, AttemptOutcome(AttemptStatus.SUCCESS), status=JobStatus.SUCCESS
            )
    assert error.value.job_id == job.id and error.value.number == 1
    assert isinstance(error.value.__cause__, asyncio.CancelledError if cancelled else OSError)
    async with SQLiteStore(store.config) as reader:
        current = await reader.get(job.id), await reader.history(job.id)
    if persisted:
        expected = "RUNNING" if operation == "claim" else "SUCCESS"
        assert current[0].status.value == current[1][0].status.value == expected
    else:
        assert current == before


@pytest.mark.parametrize("operation", ["claim", "finish"])
async def test_actual_commit_lock_failure_discards_connection_and_recovers(store, operation):
    job = await store.submit(definition(), {"count": 1})
    if operation == "finish":
        await store.claim_next()
    before = await store.get(job.id), await store.history(job.id)
    async with SQLiteStore(
        DatabaseConfig(database_path=store.config.database_path, busy_timeout_ms=20)
    ) as writer:
        with sqlite3.connect(store.config.database_path) as reader:
            reader.execute("BEGIN")
            reader.execute("SELECT * FROM jobs").fetchall()
            error_type = ClaimUncertainError if operation == "claim" else CompletionUncertainError
            with pytest.raises(error_type) as error:
                await asyncio.wait_for(
                    writer.claim_next()
                    if operation == "claim"
                    else writer.finish_attempt(
                        job.id, 1, AttemptOutcome(AttemptStatus.SUCCESS), status=JobStatus.SUCCESS
                    ),
                    timeout=2,
                )
            assert error.value.job_id == job.id and error.value.number == 1
            reader.rollback()
        assert (await writer.get(job.id), await writer.history(job.id)) == before
        if operation == "claim":
            assert (await writer.claim_next()).number == 1
        else:
            assert (
                await writer.finish_attempt(
                    job.id, 1, AttemptOutcome(AttemptStatus.SUCCESS), status=JobStatus.SUCCESS
                )
            ).status is JobStatus.SUCCESS


@pytest.mark.parametrize("operation", ["claim", "finish"])
async def test_cancel_before_commit_rolls_back_partial_writes(store, monkeypatch, operation):
    job = await store.submit(definition(), {"count": 1})
    if operation == "finish":
        await store.claim_next()
    before = await store.get(job.id), await store.history(job.id)
    flushed, release = asyncio.Event(), asyncio.Event()
    original = AsyncSession.flush

    async def pause(session, objects=None):
        await original(session, objects)
        flushed.set()
        await release.wait()

    monkeypatch.setattr(AsyncSession, "flush", pause)
    task = asyncio.create_task(
        store.claim_next()
        if operation == "claim"
        else store.finish_attempt(
            job.id, 1, AttemptOutcome(AttemptStatus.SUCCESS), status=JobStatus.SUCCESS
        )
    )
    try:
        await asyncio.wait_for(flushed.wait(), timeout=2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert (await store.get(job.id), await store.history(job.id)) == before


@pytest.mark.parametrize("operation", ["claim", "finish"])
async def test_begin_lock_failure_is_storage_error_and_preserves_records(store, operation):
    job = await store.submit(definition(), {"count": 1})
    if operation == "finish":
        await store.claim_next()
    before = await store.get(job.id), await store.history(job.id)
    async with SQLiteStore(
        DatabaseConfig(database_path=store.config.database_path, busy_timeout_ms=20)
    ) as writer:
        with sqlite3.connect(store.config.database_path) as lock:
            lock.execute("BEGIN IMMEDIATE")
            with pytest.raises(StorageError, match="database is locked") as error:
                await asyncio.wait_for(
                    writer.claim_next()
                    if operation == "claim"
                    else writer.finish_attempt(
                        job.id, 1, AttemptOutcome(AttemptStatus.SUCCESS), status=JobStatus.SUCCESS
                    ),
                    timeout=2,
                )
            assert not isinstance(error.value, (ClaimUncertainError, CompletionUncertainError))
            lock.rollback()
        assert (await writer.get(job.id), await writer.history(job.id)) == before


async def test_timezone_retry_time_preserves_microseconds(store):
    _, claim = await submit_claim(store)
    due = (datetime.now(UTC) + timedelta(days=1)).replace(microsecond=654321)
    local = due.astimezone(timezone(timedelta(hours=8)))
    done = await store.finish_attempt(
        claim.job_id,
        1,
        AttemptOutcome(AttemptStatus.FAILED, ErrorKind.TIMEOUT),
        status=JobStatus.RETRY_WAIT,
        next_run_at=local,
    )
    assert done.next_run_at == due and done.next_run_at.tzinfo is UTC
    assert done.next_run_at.microsecond == 654321


async def test_retry_due_boundary_and_first_start_are_preserved(store, monkeypatch):
    now = set_clock(monkeypatch)
    job, first = await submit_claim(store)
    due = now + timedelta(microseconds=1)
    failed = AttemptOutcome(AttemptStatus.FAILED, ErrorKind.TIMEOUT)
    before = await store.get(job.id), await store.history(job.id)
    with pytest.raises(InvalidAttemptResultError, match="future"):
        await store.finish_attempt(job.id, 1, failed, status=JobStatus.RETRY_WAIT, next_run_at=now)
    assert (await store.get(job.id), await store.history(job.id)) == before
    await store.finish_attempt(job.id, 1, failed, status=JobStatus.RETRY_WAIT, next_run_at=due)
    assert await store.claim_next() is None
    set_clock(monkeypatch, due)
    second = await store.claim_next()
    assert second.number == 2 and second.job.started_at == first.job.started_at == now
    assert (await store.history(job.id))[1].started_at == due


async def test_nonrunning_attempt_cannot_finish_running_job(store):
    _, claim = await submit_claim(store)
    with sqlite3.connect(store.config.database_path) as connection:
        connection.execute("UPDATE attempts SET status='FAILED' WHERE job_id=?", (claim.job_id,))
    before = await store.get(claim.job_id), await store.history(claim.job_id)
    with pytest.raises(AttemptConflictError):
        await store.finish_attempt(
            claim.job_id, 1, AttemptOutcome(AttemptStatus.SUCCESS), status=JobStatus.SUCCESS
        )
    assert (await store.get(claim.job_id), await store.history(claim.job_id)) == before
