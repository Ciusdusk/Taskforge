import asyncio
import sqlite3
from uuid import uuid4

import aiosqlite
import pytest
from integrations.github.config import GitHubConfig, RefreshInput, Source
from integrations.github.errors import CommitUncertainError, GenerationError, StateError
from integrations.github.models import (
    BatchWrite,
    Change,
    Entity,
    ReleaseIdentity,
    SkillChange,
    SkillSnapshot,
)
from integrations.github.storage import BusinessStore

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def batch(store, source=None, generation=1, refresh_id=None, **values):
    data = RefreshInput(
        refresh_id=refresh_id or uuid4(),
        state_directory=store.directory,
        source=source or GitHubConfig.load().sources[0],
        source_generation=generation,
    )
    return BatchWrite(
        input=data, checkpoint={"sha": "new"}, coverage={"full": True}, complete=True, **values
    )


async def ready(tmp_path):
    store = BusinessStore(tmp_path)
    await store.initialize()
    await store.register_source(GitHubConfig.load().sources[0])
    return store


async def test_schema_reopen_unique_identities_and_atomic_commit(tmp_path):
    store = await ready(tmp_path)
    entity = Entity(repository_id=1, kind="commit", remote_id="sha", content={"message": "one"})
    release = Entity(repository_id=1, kind="release", remote_id="5", content={"body": "old"})
    write = batch(
        store,
        entities=(entity, entity, release),
        changes=(Change(entity=release, kind="initial"),) * 2,
        release_ids=(ReleaseIdentity(repository_id=1, remote_id="5"),) * 2,
        snapshots=(SkillSnapshot(repository_id=1, commit_sha="new", files={"a/SKILL.md": "blob"}),)
        * 2,
        skill_changes=(
            SkillChange(
                repository_id=1,
                before_sha="",
                after_sha="new",
                path="a/SKILL.md",
                kind="added",
                blob_sha="blob",
            ),
        )
        * 2,
    )
    await store.save_intent(write.input)
    await store.associate_job(str(write.input.refresh_id), "job")
    await store.record_attempt(str(write.input.refresh_id), "job", 1, "budget exhausted")
    assert await store.apply(write)
    assert not await store.apply(write)
    reopened = BusinessStore(tmp_path)
    await reopened.initialize()
    assert await reopened.completed(str(write.input.refresh_id))
    assert (await reopened.source_state(write.input.source.source_id)).checkpoint == {"sha": "new"}
    assert await reopened.entities(1, "commit") == (entity,)
    assert await reopened.release_ids(1) == ("5",)
    assert (await reopened.skill_snapshot(1, "new")).files == {"a/SKILL.md": "blob"}
    with sqlite3.connect(store.path) as conn:
        for table, count in [
            ("batches", 1),
            ("entities", 2),
            ("versions", 2),
            ("changes", 1),
            ("skill_changes", 1),
            ("skill_snapshots", 1),
            ("release_ids", 1),
            ("attempts", 1),
            ("intents", 1),
        ]:
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == count
        assert conn.execute("SELECT job_id FROM intents").fetchone()[0] == "job"
    updated = Entity(repository_id=1, kind="release", remote_id="5", content={"body": "new"})
    assert await reopened.apply(batch(store, entities=(updated, updated)))
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT count(*) FROM versions").fetchone()[0] == 3
        assert conn.execute("SELECT count(*) FROM entities").fetchone()[0] == 2


@pytest.mark.parametrize("version", [0, 2, 999])
async def test_schema_version_rejection(tmp_path, version):
    store = await ready(tmp_path)
    with sqlite3.connect(store.path) as conn:
        conn.execute(f"PRAGMA user_version={version}")
    with pytest.raises(StateError):
        await BusinessStore(tmp_path).initialize()
    with pytest.raises(StateError):
        await store.completed(str(uuid4()))


async def test_generation_budget_changes_and_old_checkpoint_isolation(tmp_path):
    store = await ready(tmp_path)
    original = GitHubConfig.load().sources[0]
    assert await store.apply(batch(store))
    budget_change = Source(**{**original.model_dump(), "budgets": {"max_pages": 11}})
    state = await store.register_source(budget_change)
    assert state.generation == 1 and state.checkpoint == {"sha": "new"}
    for changes in [
        {"repository": "other/repo"},
        {"scan_skills": False},
        {"kind": "issues", "scan_skills": False, "issue_filter": {"labels": ["a"]}},
    ]:
        altered = Source(**{**original.model_dump(), **changes})
        with pytest.raises(GenerationError):
            await store.register_source(altered)
    altered = Source(**{**original.model_dump(), "repository": "other/repo"})
    state = await store.register_source(altered, rebaseline=True)
    assert state.generation == 2 and state.checkpoint is None
    with pytest.raises(GenerationError):
        await store.apply(batch(store))
    assert (await store.source_state(original.source_id, 1)).checkpoint == {"sha": "new"}
    assert (await store.source_state(original.source_id)).checkpoint is None
    assert await store.apply(batch(store, source=altered, generation=2))


async def test_mid_transaction_failure_rolls_back_everything(tmp_path):
    store = await ready(tmp_path)
    # Real SQLite fault after batch/record writes but before checkpoint.
    with sqlite3.connect(store.path) as conn:
        conn.execute("""CREATE TRIGGER fail_checkpoint BEFORE UPDATE OF checkpoint ON generations
                        BEGIN SELECT RAISE(ABORT,'injected failure'); END""")
    write = batch(
        store,
        entities=(Entity(repository_id=1, kind="commit", remote_id="sha", content={"value": 1}),),
    )
    with pytest.raises(sqlite3.IntegrityError):
        await store.apply(write)
    assert not await store.completed(str(write.input.refresh_id))
    assert await store.entities(1, "commit") == ()
    assert (await store.source_state(write.input.source.source_id)).checkpoint is None
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT count(*) FROM versions").fetchone()[0] == 0


@pytest.mark.parametrize("persisted", [True, False])
async def test_uncertain_commit_waits_then_checks_independent_connection(
    tmp_path, monkeypatch, persisted
):
    store = await ready(tmp_path)
    commit = aiosqlite.Connection.commit
    started, release = asyncio.Event(), asyncio.Event()
    checks = []
    completed = store.completed

    async def uncertain(connection):
        if persisted:
            await commit(connection)
        started.set()
        await release.wait()
        raise OSError("acknowledgement lost")

    async def independent(refresh_id):
        assert release.is_set()
        checks.append(refresh_id)
        return await completed(refresh_id)

    monkeypatch.setattr(aiosqlite.Connection, "commit", uncertain)
    monkeypatch.setattr(store, "completed", independent)
    write = batch(store)
    task = asyncio.create_task(store.apply(write))
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert not task.done() and not checks
        release.set()
        if persisted:
            assert await task
        else:
            with pytest.raises(StateError, match="marker is absent"):
                await task
        assert checks == [str(write.input.refresh_id)]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_uncertain_check_failure_does_not_reapply(tmp_path, monkeypatch):
    store = await ready(tmp_path)
    commit = aiosqlite.Connection.commit
    calls = 0

    async def uncertain(connection):
        nonlocal calls
        calls += 1
        await commit(connection)
        raise OSError("lost")

    async def failed_check(refresh_id):
        raise OSError("unavailable")

    monkeypatch.setattr(aiosqlite.Connection, "commit", uncertain)
    monkeypatch.setattr(store, "completed", failed_check)
    with pytest.raises(CommitUncertainError):
        await store.apply(batch(store))
    assert calls == 1


async def test_repeated_cancel_waits_for_commit_and_connection_cleanup(tmp_path, monkeypatch):
    store = await ready(tmp_path)
    commit = aiosqlite.Connection.commit
    started, release = asyncio.Event(), asyncio.Event()
    cleaned = asyncio.Event()
    close = aiosqlite.Connection.close

    async def delayed(connection):
        started.set()
        await release.wait()
        await commit(connection)

    async def closed(connection):
        await close(connection)
        cleaned.set()

    monkeypatch.setattr(aiosqlite.Connection, "commit", delayed)
    monkeypatch.setattr(aiosqlite.Connection, "close", closed)
    write = batch(store)
    task = asyncio.create_task(store.apply(write))
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not cleaned.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned.is_set()
        assert await store.completed(str(write.input.refresh_id))
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_bounded_database_lock_wait(tmp_path):
    store = await ready(tmp_path)
    blocked = BusinessStore(tmp_path, busy_timeout_ms=30)
    with sqlite3.connect(store.path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            async with asyncio.timeout(1):
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    await blocked.register_source(GitHubConfig.load().sources[1])
        finally:
            conn.rollback()


async def test_intent_identity_and_issue_version_order(tmp_path):
    store = await ready(tmp_path)
    write = batch(store)
    await store.save_intent(write.input)
    await store.save_intent(write.input)
    altered = RefreshInput(**{**write.input.model_dump(), "operation": "rebaseline"})
    with pytest.raises(StateError):
        await store.save_intent(altered)
    await store.associate_job(str(write.input.refresh_id), "job")
    with pytest.raises(StateError):
        await store.associate_job(str(write.input.refresh_id), "other")
    issue = Entity(
        repository_id=1,
        kind="issue",
        remote_id="1",
        content={"body": "new"},
        updated_at="2026-10-14T10:00:00Z",
    )
    await store.apply(batch(store, entities=(issue,)))
    old = Entity(
        **{**issue.model_dump(), "content": {"body": "old"}, "updated_at": "2026-10-14T09:00:00Z"}
    )
    await store.apply(batch(store, entities=(old,)))
    assert await store.entities(1, "issue") == (issue,)
    same_time = Entity(**{**issue.model_dump(), "content": {"body": "observed later"}})
    await store.apply(batch(store, entities=(same_time,)))
    assert await store.entities(1, "issue") == (same_time,)


async def test_atomic_visibility_and_cancel_holds_source_lock_through_database_cleanup(
    tmp_path, monkeypatch
):
    import fcntl

    from integrations.github.locking import InstanceLocks

    store = await ready(tmp_path)
    locks = InstanceLocks(tmp_path)
    commit_started, release, close_started, close_release = [asyncio.Event() for _ in range(4)]
    commit, close = aiosqlite.Connection.commit, aiosqlite.Connection.close
    connection_ids = []

    async def delayed_commit(connection):
        connection_ids.append(id(connection))
        commit_started.set()
        await release.wait()
        await commit(connection)

    async def delayed_close(connection):
        await close(connection)
        if id(connection) in connection_ids:
            close_started.set()
            await close_release.wait()

    monkeypatch.setattr(aiosqlite.Connection, "commit", delayed_commit)
    monkeypatch.setattr(aiosqlite.Connection, "close", delayed_close)
    write = batch(
        store,
        entities=(Entity(repository_id=1, kind="commit", remote_id="x", content={"body": "one"}),),
    )

    async def work():
        async with locks.source(write.input.source.source_id):
            await store.apply(write)

    task = asyncio.create_task(work())
    try:
        await asyncio.wait_for(commit_started.wait(), 2)
        with sqlite3.connect(store.path) as conn:
            assert conn.execute("SELECT count(*) FROM batches").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM entities").fetchone()[0] == 0
            assert conn.execute("SELECT checkpoint FROM generations").fetchone()[0] is None
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        await asyncio.wait_for(close_started.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        with locks.source_path(write.input.source.source_id).open("a+b") as probe:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with sqlite3.connect(store.path) as conn:
            assert conn.execute("SELECT count(*) FROM batches").fetchone()[0] == 1
            assert conn.execute("SELECT count(*) FROM entities").fetchone()[0] == 1
            assert (
                conn.execute("SELECT checkpoint FROM generations").fetchone()[0] == '{"sha":"new"}'
            )
        close_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        close_release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("cleanup_fault", ["close", "rollback"])
async def test_uncertain_commit_cleanup_fault_still_checks_independent_connection(
    tmp_path, monkeypatch, cleanup_fault
):
    store = await ready(tmp_path)
    commit, close = aiosqlite.Connection.commit, aiosqlite.Connection.close
    failing_connection = None
    check_calls = 0
    completed = store.completed

    async def uncertain(connection):
        nonlocal failing_connection
        failing_connection = connection
        if cleanup_fault == "close":
            await commit(connection)
        raise OSError("lost acknowledgement")

    async def close_fault(connection):
        await close(connection)
        if connection is failing_connection:
            raise OSError("close failure")

    async def rollback_fault(connection):
        raise OSError("rollback failure")

    async def independent(refresh_id):
        nonlocal check_calls
        check_calls += 1
        return await completed(refresh_id)

    monkeypatch.setattr(aiosqlite.Connection, "commit", uncertain)
    if cleanup_fault == "close":
        monkeypatch.setattr(aiosqlite.Connection, "close", close_fault)
    else:
        monkeypatch.setattr(aiosqlite.Connection, "rollback", rollback_fault)
    monkeypatch.setattr(store, "completed", independent)
    if cleanup_fault == "close":
        assert await store.apply(batch(store))
    else:
        with pytest.raises(StateError, match="marker is absent"):
            await store.apply(batch(store))
    assert check_calls == 1


async def test_completion_marker_rejects_other_input_and_incomplete_batch(tmp_path):
    from pydantic import ValidationError

    store = await ready(tmp_path)
    write = batch(store)
    await store.apply(write)
    other_source = GitHubConfig.load().sources[1]
    await store.register_source(other_source)
    with pytest.raises(StateError, match="another input"):
        await store.apply(batch(store, source=other_source, refresh_id=write.input.refresh_id))
    with pytest.raises(ValidationError):
        await store.apply(write.model_copy(update={"complete": False}))


async def test_equivalent_timestamps_and_repeated_changes_preserve_history(tmp_path):
    store = await ready(tmp_path)
    entity = Entity(
        repository_id=1,
        kind="issue",
        remote_id="1",
        content={"state": "open"},
        updated_at="2026-10-14T10:00:00+08:00",
    )
    first = batch(store, entities=(entity,), changes=(Change(entity=entity, kind="reopened"),))
    await store.apply(first)
    later = Entity(
        **{
            **entity.model_dump(),
            "content": {"state": "closed"},
            "updated_at": "2026-10-14T02:30:00Z",
        }
    )
    await store.apply(batch(store, entities=(later,)))
    await store.apply(batch(store, changes=first.changes))
    assert await store.entities(1, "issue") == (later,)
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT count(*) FROM changes").fetchone()[0] == 2
