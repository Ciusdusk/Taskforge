"""Independent schema, short transactions and confirmed completion markers."""

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import aiosqlite
from pydantic import JsonValue

from integrations.github.config import RefreshInput, Source
from integrations.github.errors import CommitUncertainError, GenerationError, StateError
from integrations.github.lifecycle import protected
from integrations.github.models import BatchWrite, Entity, SkillSnapshot, canonical

SCHEMA_VERSION = 1
SCHEMA = (
    "CREATE TABLE sources (source_id TEXT PRIMARY KEY, generation INTEGER NOT NULL)",
    """CREATE TABLE generations (source_id TEXT NOT NULL, generation INTEGER NOT NULL,
       baseline_key TEXT NOT NULL, config TEXT NOT NULL, checkpoint TEXT,
       PRIMARY KEY(source_id,generation), FOREIGN KEY(source_id) REFERENCES sources(source_id))""",
    """CREATE TABLE intents (refresh_id TEXT PRIMARY KEY, input TEXT NOT NULL, job_id TEXT)""",
    """CREATE TABLE batches (refresh_id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
       generation INTEGER NOT NULL, input TEXT NOT NULL,
       checkpoint TEXT NOT NULL, coverage TEXT NOT NULL,
       complete INTEGER NOT NULL CHECK(complete=1),
       FOREIGN KEY(source_id,generation) REFERENCES generations(source_id,generation))""",
    """CREATE TABLE attempts (refresh_id TEXT NOT NULL, execution_id TEXT NOT NULL,
       number INTEGER NOT NULL CHECK(number>0), diagnostic TEXT,
       PRIMARY KEY(refresh_id,execution_id,number))""",
    """CREATE TABLE entities (repository_id INTEGER NOT NULL, kind TEXT NOT NULL,
       remote_id TEXT NOT NULL, content TEXT NOT NULL, fingerprint TEXT NOT NULL, updated_at TEXT,
       PRIMARY KEY(repository_id,kind,remote_id))""",
    """CREATE TABLE versions (repository_id INTEGER NOT NULL, kind TEXT NOT NULL,
       remote_id TEXT NOT NULL, fingerprint TEXT NOT NULL, content TEXT NOT NULL, updated_at TEXT,
       PRIMARY KEY(repository_id,kind,remote_id,fingerprint))""",
    """CREATE TABLE changes (refresh_id TEXT NOT NULL, repository_id INTEGER NOT NULL,
       kind TEXT NOT NULL, remote_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
       change_kind TEXT NOT NULL, detail TEXT NOT NULL,
       PRIMARY KEY(refresh_id,repository_id,kind,remote_id,fingerprint,change_kind),
       FOREIGN KEY(refresh_id) REFERENCES batches(refresh_id))""",
    """CREATE TABLE skill_snapshots (repository_id INTEGER NOT NULL, commit_sha TEXT NOT NULL,
       files TEXT NOT NULL, PRIMARY KEY(repository_id,commit_sha))""",
    """CREATE TABLE skill_changes (repository_id INTEGER NOT NULL, before_sha TEXT NOT NULL,
       after_sha TEXT NOT NULL, path TEXT NOT NULL, kind TEXT NOT NULL, blob_sha TEXT,
       refresh_id TEXT NOT NULL, PRIMARY KEY(repository_id,before_sha,after_sha,path),
       FOREIGN KEY(refresh_id) REFERENCES batches(refresh_id))""",
    """CREATE TABLE release_ids (repository_id INTEGER NOT NULL, remote_id TEXT NOT NULL,
       first_refresh_id TEXT NOT NULL, PRIMARY KEY(repository_id,remote_id),
       FOREIGN KEY(first_refresh_id) REFERENCES batches(refresh_id))""",
    """CREATE TABLE http_cache (request_key TEXT PRIMARY KEY, etag TEXT, body TEXT NOT NULL,
       link TEXT NOT NULL)""",
    """CREATE TABLE cooldown (singleton INTEGER PRIMARY KEY CHECK(singleton=1),
       until_epoch REAL NOT NULL, secondary_count INTEGER NOT NULL DEFAULT 0)""",
)


@dataclass(frozen=True)
class SourceState:
    source: Source
    generation: int
    checkpoint: dict[str, JsonValue] | None


@dataclass(frozen=True)
class CachedResponse:
    etag: str | None
    body: JsonValue
    link: str


class BusinessStore:
    """Open a fresh connection per operation; no network inside a transaction."""

    def __init__(self, directory: Path, *, busy_timeout_ms: int = 5000) -> None:
        if type(busy_timeout_ms) is not int or not 0 <= busy_timeout_ms <= 2_147_483_647:
            raise ValueError("busy_timeout_ms must be a bounded nonnegative integer")
        self.directory = directory.expanduser().resolve()
        self.path = self.directory / "business.sqlite"
        self.busy_timeout_ms = busy_timeout_ms

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[aiosqlite.Connection]:
        async with aiosqlite.connect(
            self.path, timeout=self.busy_timeout_ms / 1000, isolation_level=None
        ) as connection:
            await connection.execute("PRAGMA foreign_keys=ON")
            yield connection

    async def _check(self, connection: aiosqlite.Connection) -> None:
        row = await self._one(connection, "PRAGMA user_version")
        if row is None or row[0] != SCHEMA_VERSION:
            raise StateError("Unsupported GitHub business schema version")

    @staticmethod
    async def _one(
        connection: aiosqlite.Connection, sql: str, parameters: tuple[object, ...] = ()
    ) -> aiosqlite.Row | tuple[object, ...] | None:
        async with connection.execute(sql, parameters) as cursor:
            return await cursor.fetchone()

    async def initialize(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)

        async def run() -> None:
            async with self._connection() as connection:
                await connection.execute("BEGIN IMMEDIATE")
                try:
                    version = await self._one(connection, "PRAGMA user_version")
                    assert version is not None
                    if version[0] == 0:
                        tables = await self._one(
                            connection,
                            "SELECT name FROM sqlite_master WHERE type='table' LIMIT 1",
                        )
                        if tables is not None:
                            raise StateError("Unversioned nonempty business database")
                        for statement in SCHEMA:
                            await connection.execute(statement)
                        await connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                    await self._check(connection)
                    await connection.commit()
                finally:
                    if connection.in_transaction:
                        await connection.rollback()

        await protected(run())

    async def _read[T](self, operation: Callable[[aiosqlite.Connection], Awaitable[T]]) -> T:
        async def run() -> T:
            async with self._connection() as connection:
                await self._check(connection)
                return await operation(connection)

        return await protected(run())

    async def _write[T](self, operation: Callable[[aiosqlite.Connection], Awaitable[T]]) -> T:
        async def run() -> T:
            async with self._connection() as connection:
                await self._check(connection)
                await connection.execute("BEGIN IMMEDIATE")
                try:
                    result = await operation(connection)
                    await connection.commit()
                    return result
                finally:
                    if connection.in_transaction:
                        await connection.rollback()

        return await protected(run())

    async def register_source(self, source: Source, *, rebaseline: bool = False) -> SourceState:
        async def write(connection: aiosqlite.Connection) -> SourceState:
            row = await self._one(
                connection,
                """SELECT g.generation,g.baseline_key,g.checkpoint FROM sources s
                   JOIN generations g ON s.source_id=g.source_id AND s.generation=g.generation
                   WHERE s.source_id=?""",
                (source.source_id,),
            )
            generation = 1 if row is None else int(str(row[0]))
            if row is not None and row[1] != source.baseline_key() and not rebaseline:
                raise GenerationError("Source identity changed; explicit rebaseline required")
            if row is not None and rebaseline:
                generation += 1
            if row is None:
                await connection.execute("INSERT INTO sources VALUES (?,?)", (source.source_id, 1))
            if row is None or rebaseline:
                await connection.execute(
                    "INSERT INTO generations VALUES (?,?,?,?,NULL)",
                    (source.source_id, generation, source.baseline_key(), source.model_dump_json()),
                )
                await connection.execute(
                    "UPDATE sources SET generation=? WHERE source_id=?",
                    (generation, source.source_id),
                )
                checkpoint = None
            else:
                await connection.execute(
                    "UPDATE generations SET config=? WHERE source_id=? AND generation=?",
                    (source.model_dump_json(), source.source_id, generation),
                )
                checkpoint = None if row[2] is None else json.loads(str(row[2]))
            return SourceState(source, generation, checkpoint)

        return await self._write(write)

    async def source_state(self, source_id: str, generation: int | None = None) -> SourceState:
        async def read(connection: aiosqlite.Connection) -> SourceState:
            row = await self._one(
                connection,
                """SELECT g.config,g.generation,g.checkpoint FROM generations g JOIN sources s
                   ON s.source_id=g.source_id WHERE g.source_id=? AND
                   g.generation=coalesce(?,s.generation)""",
                (source_id, generation),
            )
            if row is None:
                raise StateError("Unknown source generation")
            return SourceState(
                Source.model_validate_json(str(row[0])),
                int(str(row[1])),
                None if row[2] is None else json.loads(str(row[2])),
            )

        return await self._read(read)

    async def save_intent(self, data: RefreshInput) -> None:
        async def write(connection: aiosqlite.Connection) -> None:
            old = await self._one(
                connection, "SELECT input FROM intents WHERE refresh_id=?", (str(data.refresh_id),)
            )
            payload = data.model_dump_json()
            if old is not None and old[0] != payload:
                raise StateError("refresh_id already belongs to another input")
            await connection.execute(
                "INSERT OR IGNORE INTO intents VALUES (?,?,NULL)", (str(data.refresh_id), payload)
            )

        await self._write(write)

    async def associate_job(self, refresh_id: str, job_id: str) -> None:
        async def write(connection: aiosqlite.Connection) -> None:
            old = await self._one(
                connection, "SELECT job_id FROM intents WHERE refresh_id=?", (refresh_id,)
            )
            if old is None or old[0] not in (None, job_id):
                raise StateError("Missing intent or conflicting Job association")
            await connection.execute(
                "UPDATE intents SET job_id=? WHERE refresh_id=?", (job_id, refresh_id)
            )

        await self._write(write)

    async def record_attempt(
        self, refresh_id: str, execution_id: str, number: int, diagnostic: str | None = None
    ) -> None:
        async def write(connection: aiosqlite.Connection) -> None:
            await connection.execute(
                "INSERT OR REPLACE INTO attempts VALUES (?,?,?,?)",
                (refresh_id, execution_id, number, diagnostic),
            )

        await self._write(write)

    async def completed(self, refresh_id: str) -> bool:
        async def read(connection: aiosqlite.Connection) -> bool:
            return (
                await self._one(
                    connection,
                    "SELECT 1 FROM batches WHERE refresh_id=? AND complete=1",
                    (refresh_id,),
                )
                is not None
            )

        # Always uses a new connection, including the uncertain-commit path.
        return await self._read(read)

    async def completed_input(self, data: RefreshInput) -> bool:
        """Check replay identity before any network work, even for an old generation."""

        async def read(connection: aiosqlite.Connection) -> bool:
            row = await self._one(
                connection, "SELECT input FROM batches WHERE refresh_id=?", (str(data.refresh_id),)
            )
            if row is None:
                return False
            if row[0] != data.model_dump_json():
                raise StateError("Completed refresh_id belongs to another input")
            return True

        return await self._read(read)

    async def apply(self, batch: BatchWrite) -> bool:
        """True if committed, False if already complete; uncertainty is checked once."""
        batch = BatchWrite.model_validate_json(batch.model_dump_json())

        async def run() -> bool:
            committed = False
            uncertain = False
            commit_started = False
            try:
                async with self._connection() as connection:
                    await self._check(connection)
                    await connection.execute("BEGIN IMMEDIATE")
                    try:
                        row = await self._one(
                            connection,
                            """SELECT s.generation,g.baseline_key FROM sources s JOIN generations g
                               ON s.source_id=g.source_id AND s.generation=g.generation
                               WHERE s.source_id=?""",
                            (batch.input.source.source_id,),
                        )
                        if (
                            row is None
                            or row[0] != batch.input.source_generation
                            or (row[1] != batch.input.source.baseline_key())
                        ):
                            raise GenerationError("Stale source generation or mismatched baseline")
                        previous = await self._one(
                            connection,
                            "SELECT input FROM batches WHERE refresh_id=?",
                            (str(batch.input.refresh_id),),
                        )
                        if previous is not None:
                            if previous[0] != batch.input.model_dump_json():
                                raise StateError("Completed refresh_id belongs to another input")
                            return False
                        await self._apply_rows(connection, batch)
                        commit_started = True
                        await connection.commit()
                        committed = True
                    finally:
                        if connection.in_transaction:
                            await connection.rollback()
            except Exception:
                if not commit_started:
                    raise
                uncertain = True
            if uncertain:
                check_failed = False
                try:
                    committed = await self.completed(str(batch.input.refresh_id))
                except Exception:
                    check_failed = True
                if check_failed:
                    raise CommitUncertainError("Cannot confirm business completion; do not reapply")
                if not committed:
                    raise StateError(
                        "Business commit failed; independent completion marker is absent"
                    )
            return committed

        # Includes commit, connection cleanup and independent check before cancellation propagates.
        return await protected(run())

    async def _apply_rows(self, connection: aiosqlite.Connection, batch: BatchWrite) -> None:
        refresh_id = str(batch.input.refresh_id)
        await connection.execute(
            "INSERT INTO batches VALUES (?,?,?,?,?,?,1)",
            (
                refresh_id,
                batch.input.source.source_id,
                batch.input.source_generation,
                batch.input.model_dump_json(),
                canonical(batch.checkpoint),
                canonical(batch.coverage),
            ),
        )
        for entity in batch.entities:
            identity = (entity.repository_id, entity.kind, entity.remote_id)
            old = await self._one(
                connection,
                "SELECT updated_at FROM entities WHERE repository_id=? AND kind=? AND remote_id=?",
                identity,
            )
            if (
                entity.kind == "issue"
                and old is not None
                and old[0] is not None
                and (entity.updated_at is None or str(old[0]) > entity.updated_at)
            ):
                continue
            content = canonical(entity.content)
            await connection.execute(
                "INSERT OR IGNORE INTO versions VALUES (?,?,?,?,?,?)",
                (*identity, entity.fingerprint, content, entity.updated_at),
            )
            await connection.execute(
                "INSERT OR REPLACE INTO entities VALUES (?,?,?,?,?,?)",
                (*identity, content, entity.fingerprint, entity.updated_at),
            )
        for change in batch.changes:
            entity = change.entity
            await connection.execute(
                "INSERT OR IGNORE INTO changes VALUES (?,?,?,?,?,?,?)",
                (
                    refresh_id,
                    entity.repository_id,
                    entity.kind,
                    entity.remote_id,
                    entity.fingerprint,
                    change.kind,
                    canonical(change.detail),
                ),
            )
        for snapshot in batch.snapshots:
            await connection.execute(
                "INSERT OR IGNORE INTO skill_snapshots VALUES (?,?,?)",
                (snapshot.repository_id, snapshot.commit_sha, canonical(snapshot.files)),
            )
        for skill_change in batch.skill_changes:
            await connection.execute(
                "INSERT OR IGNORE INTO skill_changes VALUES (?,?,?,?,?,?,?)",
                (
                    skill_change.repository_id,
                    skill_change.before_sha,
                    skill_change.after_sha,
                    skill_change.path,
                    skill_change.kind,
                    skill_change.blob_sha,
                    refresh_id,
                ),
            )
        for release in batch.release_ids:
            await connection.execute(
                "INSERT OR IGNORE INTO release_ids VALUES (?,?,?)",
                (release.repository_id, release.remote_id, refresh_id),
            )
        await connection.execute(
            "UPDATE generations SET checkpoint=? WHERE source_id=? AND generation=?",
            (
                canonical(batch.checkpoint),
                batch.input.source.source_id,
                batch.input.source_generation,
            ),
        )

    async def cache_get(self, request_key: str) -> CachedResponse | None:
        async def read(connection: aiosqlite.Connection) -> CachedResponse | None:
            row = await self._one(
                connection,
                "SELECT etag,body,link FROM http_cache WHERE request_key=?",
                (request_key,),
            )
            if row is None:
                return None
            return CachedResponse(
                None if row[0] is None else str(row[0]), json.loads(str(row[1])), str(row[2])
            )

        return await self._read(read)

    async def cache_put(self, key: str, value: CachedResponse) -> None:
        async def write(connection: aiosqlite.Connection) -> None:
            await connection.execute(
                "INSERT OR REPLACE INTO http_cache VALUES (?,?,?,?)",
                (key, value.etag, canonical(value.body), value.link),
            )

        await self._write(write)

    async def cooldown(self) -> tuple[float, int]:
        async def read(connection: aiosqlite.Connection) -> tuple[float, int]:
            row = await self._one(connection, "SELECT until_epoch,secondary_count FROM cooldown")
            return (0.0, 0) if row is None else (float(str(row[0])), int(str(row[1])))

        return await self._read(read)

    async def extend_cooldown(self, until: float, *, secondary: bool = False) -> None:
        async def write(connection: aiosqlite.Connection) -> None:
            await connection.execute(
                """INSERT INTO cooldown VALUES (1,?,?) ON CONFLICT(singleton) DO UPDATE SET
                   until_epoch=max(until_epoch,excluded.until_epoch),
                   secondary_count=secondary_count+excluded.secondary_count""",
                (until, int(secondary)),
            )

        await self._write(write)

    async def entities(self, repository_id: int, kind: str) -> tuple["Entity", ...]:
        async def read(connection: aiosqlite.Connection) -> tuple[Entity, ...]:
            async with connection.execute(
                """SELECT remote_id,content,updated_at FROM entities
                   WHERE repository_id=? AND kind=? ORDER BY remote_id""",
                (repository_id, kind),
            ) as cursor:
                return tuple(
                    Entity.model_validate(
                        {
                            "repository_id": repository_id,
                            "kind": kind,
                            "remote_id": row[0],
                            "content": json.loads(row[1]),
                            "updated_at": row[2],
                        }
                    )
                    for row in await cursor.fetchall()
                )

        return await self._read(read)

    async def release_ids(self, repository_id: int) -> tuple[str, ...]:
        async def read(connection: aiosqlite.Connection) -> tuple[str, ...]:
            async with connection.execute(
                "SELECT remote_id FROM release_ids WHERE repository_id=? ORDER BY remote_id",
                (repository_id,),
            ) as cursor:
                return tuple(str(row[0]) for row in await cursor.fetchall())

        return await self._read(read)

    async def tracked_issue_ids(
        self, repository_id: int, source_id: str, generation: int
    ) -> tuple[str, ...]:
        """Membership comes from committed observations in this source generation."""

        async def read(connection: aiosqlite.Connection) -> tuple[str, ...]:
            async with connection.execute(
                """SELECT DISTINCT c.remote_id FROM changes c JOIN batches b
                   ON c.refresh_id=b.refresh_id WHERE c.repository_id=? AND c.kind='issue'
                   AND b.source_id=? AND b.generation=? ORDER BY c.remote_id""",
                (repository_id, source_id, generation),
            ) as cursor:
                return tuple(str(row[0]) for row in await cursor.fetchall())

        return await self._read(read)

    async def skill_snapshot(self, repository_id: int, commit_sha: str) -> "SkillSnapshot | None":
        async def read(connection: aiosqlite.Connection) -> SkillSnapshot | None:
            row = await self._one(
                connection,
                "SELECT files FROM skill_snapshots WHERE repository_id=? AND commit_sha=?",
                (repository_id, commit_sha),
            )
            return (
                None
                if row is None
                else SkillSnapshot(
                    repository_id=repository_id,
                    commit_sha=commit_sha,
                    files=json.loads(str(row[0])),
                )
            )

        return await self._read(read)
