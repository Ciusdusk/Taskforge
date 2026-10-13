import asyncio
import json
import sqlite3
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from taskforge import (
    AttemptStatus,
    DatabaseConfig,
    ErrorKind,
    ExecutionPolicy,
    InputSerializationError,
    JobCursor,
    JobStatus,
    PaginationError,
    SchemaError,
    SQLiteStore,
    StorageError,
    SubmissionUncertainError,
    TaskForge,
    TaskRegistry,
    UnknownJobError,
    UnknownTaskError,
)
from taskforge.db import ERROR_MESSAGE_LIMIT, EXCEPTION_TYPE_LIMIT, TRACEBACK_LIMIT

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int


async def count(data: Input) -> None:
    raise AssertionError("Submission must not execute task code")


def registry(policy=None):
    result = TaskRegistry()
    result.task("demo.count", input_model=Input, policy=policy)(count)
    return result


@pytest_asyncio.fixture
async def store(tmp_path):
    async with SQLiteStore(DatabaseConfig(database_path=tmp_path / "queue.sqlite")) as result:
        await result.initialize()
        yield result


async def test_initial_submission_and_detached_policy_snapshot(store):
    declared = ExecutionPolicy(
        max_retries=2, retry_base_seconds=0.25, retry_cap_seconds=5, timeout_seconds=7
    )
    tasks = registry(declared)
    app = TaskForge(store, tasks)
    before = datetime.now(UTC)
    job = await app.submit("demo.count", {"count": "3"})
    assert UUID(job.id).version == 4 and str(UUID(job.id)) == job.id
    assert job.status is JobStatus.PENDING and job.attempt_count == 0
    assert before <= job.created_at <= datetime.now(UTC)
    assert job.created_at.tzinfo is UTC and job.next_run_at == job.created_at
    assert job.started_at is job.finished_at is None
    assert job.last_error_kind is job.last_error_message is None
    assert json.loads(job.params_json) == {"count": 3}
    assert tasks.get("demo.count").validate_json(job.params_json) == Input(count=3)
    assert (
        job.max_retries,
        job.retry_base_seconds,
        job.retry_cap_seconds,
        job.timeout_seconds,
    ) == (2, 0.25, 5, 7)
    assert await app.get(job.id) == job
    assert await app.history(job.id) == ()
    with pytest.raises(FrozenInstanceError):
        job.status = JobStatus.FAILED
    changed = TaskForge(store, registry(declared.model_copy(update={"max_retries": 5})))
    new = await changed.submit("demo.count", {"count": 4})
    assert new.max_retries == 5
    assert (await app.get(job.id)).max_retries == 2
    with sqlite3.connect(store.config.database_path) as connection:
        value, kind = connection.execute(
            "SELECT created_at, typeof(created_at) FROM jobs WHERE id=?", (job.id,)
        ).fetchone()
        assert kind == "integer"
        assert datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=value) == job.created_at
        assert connection.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0


async def test_restart_and_query_without_registry_or_task_module(tmp_path, monkeypatch):
    config = DatabaseConfig(database_path=tmp_path / "queue.sqlite")
    async with SQLiteStore(config) as original:
        await original.initialize()
        job = await TaskForge(original, registry()).submit("demo.count", {"count": 3})
    monkeypatch.setattr(TaskRegistry, "get", lambda *args: pytest.fail("query loaded registry"))
    async with SQLiteStore(config) as reopened:
        # Versioned databases can be queried directly without calling initialize again.
        app = TaskForge(reopened)
        assert await app.get(job.id) == job
        assert await app.list() == (job,)
        assert await app.history(job.id) == ()
        with pytest.raises(UnknownTaskError, match="explicit TaskRegistry"):
            await app.submit("demo.count", {"count": 3})
    assert job.params_json == '{"count":3}'


@pytest.mark.parametrize("data", [{}, {"count": "bad"}, {"count": 1, "extra": 2}, []])
async def test_validation_failures_do_not_insert(store, data):
    with pytest.raises(ValidationError):
        await TaskForge(store, registry()).submit("demo.count", data)
    assert await store.list() == ()


async def test_unknown_and_unserializable_tasks_do_not_insert(store):
    tasks = registry()
    with pytest.raises(UnknownTaskError):
        await TaskForge(store, tasks).submit("missing", {"count": 1})

    class OpaqueInput(BaseModel):
        value: object

    async def opaque(data):
        pass

    tasks.task("opaque", input_model=OpaqueInput)(opaque)
    with pytest.raises(InputSerializationError):
        await TaskForge(store, tasks).submit("opaque", {"value": object()})
    assert await store.list() == ()


async def test_initialization_is_explicit_idempotent_and_transactional(tmp_path):
    config = DatabaseConfig(database_path=tmp_path / "queue.sqlite")
    async with SQLiteStore(config) as store:
        with pytest.raises(SchemaError, match="initialize explicitly"):
            await store.list()
        await store.initialize()
        job = await TaskForge(store, registry()).submit("demo.count", {"count": 1})
        await store.initialize()
        assert await store.get(job.id) == job
        with sqlite3.connect(config.database_path) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
            assert {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            } == {"jobs", "attempts"}
            assert [row[2] for row in connection.execute("PRAGMA index_info(ix_jobs_due)")] == [
                "status",
                "next_run_at",
                "created_at",
                "id",
            ]
            assert [row[2] for row in connection.execute("PRAGMA index_info(ix_jobs_created)")] == [
                "created_at",
                "id",
            ]
            fk = connection.execute("PRAGMA foreign_key_list(attempts)").fetchone()
            assert (fk[2], fk[3], fk[4]) == ("jobs", "job_id", "id")


@pytest.mark.parametrize("version", [2, 99])
async def test_incompatible_version_is_rejected_without_modification(tmp_path, version):
    path = tmp_path / "queue.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA user_version={version}")
    async with SQLiteStore(DatabaseConfig(database_path=path)) as store:
        with pytest.raises(SchemaError, match="Unsupported schema"):
            await store.initialize()
        with pytest.raises(SchemaError):
            await store.list()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == version
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []


async def test_unversioned_existing_database_is_not_adopted(tmp_path):
    path = tmp_path / "queue.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated (value INTEGER)")
    async with SQLiteStore(DatabaseConfig(database_path=path)) as store:
        with pytest.raises(SchemaError, match="unversioned"):
            await store.initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == [("unrelated",)]


async def test_schema_creation_failure_rolls_back_tables_and_version(tmp_path, monkeypatch):
    from taskforge.db import Base

    def fail_after_ddl(connection):
        connection.exec_driver_sql("CREATE TABLE partial (value INTEGER)")
        raise RuntimeError("schema failed")

    monkeypatch.setattr(Base.metadata, "create_all", fail_after_ddl)
    path = tmp_path / "queue.sqlite"
    async with SQLiteStore(DatabaseConfig(database_path=path)) as store:
        with pytest.raises(RuntimeError, match="schema failed"):
            await store.initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []


async def test_versioned_missing_schema_is_rejected(tmp_path):
    path = tmp_path / "queue.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=1")
    async with SQLiteStore(DatabaseConfig(database_path=path)) as store:
        with pytest.raises(SchemaError, match="missing table"):
            await store.initialize()


async def test_unknown_ids_and_stable_keyset_pages(store):
    app = TaskForge(store, registry())
    assert await app.list() == ()
    for job_id in (str(uuid4()), "malformed", ""):
        with pytest.raises(UnknownJobError):
            await app.get(job_id)
        with pytest.raises(UnknownJobError):
            await app.history(job_id)
    submitted = [await app.submit("demo.count", {"count": number}) for number in range(6)]
    # Force a tie: UUID lexical order must break it, with no timestamp-only cursor gaps.
    with sqlite3.connect(store.config.database_path) as connection:
        connection.execute("UPDATE jobs SET created_at=1791849600123456")
    expected = sorted(submitted, key=lambda job: job.id)
    seen = []
    after = None
    while page := await app.list(limit=2, after=after):
        seen.extend(job.id for job in page)
        after = page[-1].cursor
    assert seen == [job.id for job in expected]
    first_page = await app.list(limit=1)
    cursor = first_page[0].cursor
    equivalent = JobCursor(cursor.created_at.astimezone(timezone(timedelta(hours=8))), cursor.id)
    assert await app.list(after=equivalent) == await app.list(after=cursor)
    # Keyset cursors remain valid if the source row no longer exists.
    with sqlite3.connect(store.config.database_path) as connection:
        connection.execute("DELETE FROM jobs WHERE id=?", (cursor.id,))
    assert len(await app.list(after=cursor)) == 5
    assert len(await app.list(limit=1000)) == 5


@pytest.mark.parametrize("limit", [0, -1, 1001, True, 1.5, "2", None])
async def test_invalid_page_size(store, limit):
    with pytest.raises(PaginationError):
        await TaskForge(store).list(limit=limit)


@pytest.mark.parametrize(
    "cursor",
    [
        "bad",
        JobCursor(datetime.now(), str(uuid4())),
        JobCursor(datetime.now(UTC), "bad"),
        JobCursor(datetime.now(UTC), str(UUID(int=0))),
        JobCursor(datetime.now(UTC), str(uuid4()).upper()),
    ],
)
async def test_invalid_cursor(store, cursor):
    with pytest.raises(PaginationError):
        await store.list(after=cursor)


async def test_real_partial_insert_failure_rolls_back(store):
    with sqlite3.connect(store.config.database_path) as connection:
        connection.execute("""CREATE TRIGGER fail_insert AFTER INSERT ON jobs
            BEGIN SELECT RAISE(FAIL, 'forced insert failure'); END""")
    with pytest.raises(StorageError, match="forced insert failure") as error:
        await TaskForge(store, registry()).submit("demo.count", {"count": 1})
    assert error.value.__cause__ is not None
    assert not isinstance(error.value, SubmissionUncertainError)
    assert await store.list() == ()
    with sqlite3.connect(store.config.database_path) as connection:
        connection.execute("DROP TRIGGER fail_insert")
    assert (
        await TaskForge(store, registry()).submit("demo.count", {"count": 2})
    ).attempt_count == 0


@pytest.mark.parametrize("persisted", [False, True])
async def test_uncertain_commit_carries_queryable_id(store, monkeypatch, persisted):
    original = AsyncSession.commit

    async def lose_ack(session):
        if persisted:
            await original(session)
        raise OSError("commit acknowledgment lost")

    monkeypatch.setattr(AsyncSession, "commit", lose_ack)
    app = TaskForge(store, registry())
    with pytest.raises(SubmissionUncertainError) as error:
        await app.submit("demo.count", {"count": 1})
    assert isinstance(error.value.__cause__, OSError)
    if persisted:
        job = await app.get(error.value.job_id)
        assert json.loads(job.params_json) == {"count": 1}
    else:
        with pytest.raises(UnknownJobError):
            await app.get(error.value.job_id)
        assert await app.list() == ()


async def test_cancellation_during_commit_is_queryable(store, monkeypatch):
    original = AsyncSession.commit

    async def cancelled_ack(session):
        await original(session)
        raise asyncio.CancelledError

    monkeypatch.setattr(AsyncSession, "commit", cancelled_ack)
    with pytest.raises(SubmissionUncertainError) as error:
        await TaskForge(store, registry()).submit("demo.count", {"count": 1})
    assert isinstance(error.value.__cause__, asyncio.CancelledError)
    assert (await store.get(error.value.job_id)).status is JobStatus.PENDING


async def test_bounded_lock_wait_and_unified_transaction_opening(store):
    statements = []

    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(store._engine.sync_engine, "before_cursor_execute", record)
    app = TaskForge(store, registry())
    job = await app.submit("demo.count", {"count": 1})
    assert [value for value in statements if value.startswith("BEGIN")] == ["BEGIN IMMEDIATE"]
    statements.clear()
    await app.get(job.id)
    assert [value for value in statements if value.startswith("BEGIN")] == ["BEGIN"]
    async with SQLiteStore(
        DatabaseConfig(database_path=store.config.database_path, busy_timeout_ms=20)
    ) as contender:
        with sqlite3.connect(store.config.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with pytest.raises(StorageError, match="database is locked"):
                await asyncio.wait_for(
                    TaskForge(contender, registry()).submit("demo.count", {"count": 2}), timeout=2
                )
            connection.rollback()
        assert len(await contender.list()) == 1
    async with store._session() as session:
        assert (await session.execute(text("PRAGMA foreign_keys"))).scalar_one() == 1
        assert (await session.execute(text("PRAGMA busy_timeout"))).scalar_one() == 5000


@pytest.mark.parametrize(
    "column,value",
    [
        ("status", "invalid"),
        ("attempt_count", -1),
        ("attempt_count", 0.5),
        ("attempt_count", 2),
        ("max_retries", -1),
        ("max_retries", 0.5),
        ("timeout_seconds", 0),
        ("timeout_seconds", -1),
        ("timeout_seconds", float("inf")),
        ("retry_base_seconds", 0),
        ("retry_base_seconds", -1),
        ("retry_base_seconds", float("inf")),
        ("retry_cap_seconds", 0.5),
        ("retry_cap_seconds", float("inf")),
        ("created_at", "not an integer"),
        ("started_at", 0.5),
        ("finished_at", "bad"),
        ("next_run_at", 0.5),
        ("last_error_kind", "invalid"),
        ("last_error_message", "x" * (ERROR_MESSAGE_LIMIT + 1)),
    ],
)
async def test_actual_jobs_constraints(store, column, value):
    job = await TaskForge(store, registry()).submit("demo.count", {"count": 1})
    with sqlite3.connect(store.config.database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(f"UPDATE jobs SET {column}=? WHERE id=?", (value, job.id))
    assert (await store.get(job.id)).status is JobStatus.PENDING


@pytest.mark.parametrize(
    "changes",
    [
        {"job_id": "missing"},
        {"number": 0},
        {"number": -1},
        {"number": 0.5},
        {"status": "invalid"},
        {"started_at": 0.5},
        {"finished_at": "bad"},
        {"error_kind": "invalid"},
        {"exception_type": "x" * (EXCEPTION_TYPE_LIMIT + 1)},
        {"error_message": "x" * (ERROR_MESSAGE_LIMIT + 1)},
        {"traceback": "x" * (TRACEBACK_LIMIT + 1)},
    ],
)
async def test_actual_attempt_constraints_including_foreign_key(store, changes):
    job = await TaskForge(store, registry()).submit("demo.count", {"count": 1})
    values = dict(
        job_id=job.id,
        number=1,
        status="RUNNING",
        started_at=1,
        finished_at=None,
        error_kind=None,
        exception_type=None,
        error_message=None,
        traceback=None,
    )
    values.update(changes)
    async with store._session(write=True) as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text("""INSERT INTO attempts
                (job_id,number,status,started_at,finished_at,error_kind,exception_type,
                 error_message,traceback) VALUES
                (:job_id,:number,:status,:started_at,:finished_at,:error_kind,:exception_type,
                 :error_message,:traceback)"""),
                values,
            )
    assert await store.history(job.id) == ()


async def test_one_running_attempt_and_ordered_history_snapshots(store):
    job = await TaskForge(store, registry()).submit("demo.count", {"count": 1})
    with sqlite3.connect(store.config.database_path) as connection:
        connection.execute(
            """INSERT INTO attempts
            (job_id,number,status,started_at,finished_at,error_kind,error_message)
            VALUES (?,2,'RUNNING',2000001,NULL,NULL,NULL)""",
            (job.id,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO attempts (job_id,number,status,started_at)
                VALUES (?,3,'RUNNING',3000000)""",
                (job.id,),
            )
        connection.execute(
            """INSERT INTO attempts
            (job_id,number,status,started_at,finished_at,error_kind,error_message)
            VALUES (?,1,'FAILED',1000001,1500001,'TASK_ERROR','failed')""",
            (job.id,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO attempts (job_id,number,status,started_at)
                VALUES (?,1,'SUCCESS',3000000)""",
                (job.id,),
            )
    history = await TaskForge(store).history(job.id)
    assert [attempt.number for attempt in history] == [1, 2]
    assert history[0].status is AttemptStatus.FAILED
    assert history[0].error_kind is ErrorKind.TASK_ERROR
    assert history[0].started_at == datetime(1970, 1, 1, 0, 0, 1, 1, tzinfo=UTC)
    assert history[0].finished_at == datetime(1970, 1, 1, 0, 0, 1, 500001, tzinfo=UTC)
    assert history[1].finished_at is None
    with pytest.raises(FrozenInstanceError):
        history[0].error_message = "changed"
    assert (await store.history(job.id))[0].error_message == "failed"


@pytest.mark.parametrize(
    "settings",
    [
        {"busy_timeout_ms": -1},
        {"busy_timeout_ms": True},
        {"busy_timeout_ms": 0.5},
        {"busy_timeout_ms": 2_147_483_648},
        {"database_path": ":memory:"},
        {"extra": 1},
    ],
)
async def test_database_configuration_validation(tmp_path, settings):
    with pytest.raises(ValidationError):
        DatabaseConfig(**{"database_path": tmp_path / "queue.sqlite", **settings})


async def test_concurrent_initialization_and_submissions_use_independent_transactions(tmp_path):
    config = DatabaseConfig(database_path=tmp_path / "queue.sqlite")
    async with SQLiteStore(config) as first, SQLiteStore(config) as second:
        await asyncio.gather(first.initialize(), second.initialize())
        apps = (TaskForge(first, registry()), TaskForge(second, registry()))
        jobs = await asyncio.gather(
            *(apps[number % 2].submit("demo.count", {"count": number}) for number in range(20))
        )
        persisted = await TaskForge(first).list()
        assert len(persisted) == len({job.id for job in jobs}) == 20
        assert {json.loads(job.params_json)["count"] for job in persisted} == set(range(20))
        assert all(job.status is JobStatus.PENDING and job.attempt_count == 0 for job in persisted)
        assert await asyncio.gather(*(first.history(job.id) for job in jobs)) == [()] * 20


async def test_versioned_incompatible_columns_are_rejected(store):
    with sqlite3.connect(store.config.database_path) as connection:
        connection.execute("ALTER TABLE jobs ADD COLUMN unexpected TEXT")
    with pytest.raises(SchemaError, match="incompatible columns"):
        await store.initialize()


async def test_missing_parent_is_a_clear_storage_failure(tmp_path):
    async with SQLiteStore(
        DatabaseConfig(database_path=tmp_path / "missing" / "queue.sqlite")
    ) as store:
        with pytest.raises(StorageError, match="unable to open database file"):
            await store.initialize()


async def test_real_commit_lock_failure_is_uncertain_and_rolls_back(store):
    config = DatabaseConfig(database_path=store.config.database_path, busy_timeout_ms=20)
    async with SQLiteStore(config) as writer:
        with sqlite3.connect(config.database_path) as reader:
            reader.execute("BEGIN")
            reader.execute("SELECT * FROM jobs").fetchall()
            with pytest.raises(SubmissionUncertainError) as error:
                await asyncio.wait_for(
                    TaskForge(writer, registry()).submit("demo.count", {"count": 1}), timeout=2
                )
            reader.rollback()
        with pytest.raises(UnknownJobError):
            await writer.get(error.value.job_id)
        assert await writer.list() == ()
        new = await TaskForge(writer, registry()).submit("demo.count", {"count": 2})
        assert await writer.get(new.id) == new


async def test_cancellation_before_commit_rolls_back_flushed_insert(store, monkeypatch):
    flushed = asyncio.Event()
    release = asyncio.Event()
    original = AsyncSession.flush

    async def pause_after_insert(session, objects=None):
        await original(session, objects)
        flushed.set()
        await release.wait()

    monkeypatch.setattr(AsyncSession, "flush", pause_after_insert)
    submitting = asyncio.create_task(
        TaskForge(store, registry()).submit("demo.count", {"count": 1})
    )
    try:
        await asyncio.wait_for(flushed.wait(), timeout=2)
    finally:
        submitting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await submitting
    assert await store.list() == ()
