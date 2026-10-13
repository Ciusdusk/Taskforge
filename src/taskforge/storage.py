"""Short-lived transactions for durable jobs and detached snapshots."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy import Connection, func, inspect, or_, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from taskforge.config import DatabaseConfig
from taskforge.db import (
    ERROR_MESSAGE_LIMIT,
    EXCEPTION_TYPE_LIMIT,
    SCHEMA_VERSION,
    TRACEBACK_LIMIT,
    AttemptRow,
    Base,
    JobRow,
    create_engine,
)
from taskforge.errors import (
    AttemptConflictError,
    ClaimUncertainError,
    CompletionUncertainError,
    InvalidAttemptResultError,
    PaginationError,
    SchemaError,
    StorageError,
    SubmissionUncertainError,
    UnknownJobError,
)
from taskforge.models import (
    Attempt,
    AttemptClaim,
    AttemptOutcome,
    AttemptStatus,
    ErrorKind,
    Job,
    JobCursor,
    JobStatus,
)
from taskforge.registry import TaskDefinition

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _column_names(connection: Connection, table_name: str) -> set[str]:
    return {str(column["name"]) for column in inspect(connection).get_columns(table_name)}


def _epoch_us(value: datetime) -> int:
    delta = value.astimezone(UTC) - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _now_us() -> int:
    return _epoch_us(datetime.now(UTC))


def _datetime(value: int | None) -> datetime | None:
    return None if value is None else _EPOCH + timedelta(microseconds=value)


def _job(row: JobRow) -> Job:
    return Job(
        id=row.id,
        task_name=row.task_name,
        params_json=row.params_json,
        status=JobStatus(row.status),
        created_at=_EPOCH + timedelta(microseconds=row.created_at),
        started_at=_datetime(row.started_at),
        finished_at=_datetime(row.finished_at),
        next_run_at=_datetime(row.next_run_at),
        attempt_count=row.attempt_count,
        max_retries=row.max_retries,
        retry_base_seconds=row.retry_base_seconds,
        retry_cap_seconds=row.retry_cap_seconds,
        timeout_seconds=row.timeout_seconds,
        last_error_kind=None if row.last_error_kind is None else ErrorKind(row.last_error_kind),
        last_error_message=row.last_error_message,
    )


def _attempt(row: AttemptRow) -> Attempt:
    return Attempt(
        job_id=row.job_id,
        number=row.number,
        status=AttemptStatus(row.status),
        started_at=_EPOCH + timedelta(microseconds=row.started_at),
        finished_at=_datetime(row.finished_at),
        error_kind=None if row.error_kind is None else ErrorKind(row.error_kind),
        exception_type=row.exception_type,
        error_message=row.error_message,
        traceback=row.traceback,
    )


async def _commit(session: AsyncSession) -> None:
    try:
        await session.commit()
    except BaseException:
        # Failed SQLite COMMIT can leave its transaction open after SQLAlchemy
        # marks it inactive. Discard the connection so close rolls it back and
        # the pool cannot hand a still-open transaction to another operation.
        await session.invalidate()
        raise


def _validate_page(limit: int, after: JobCursor | None) -> None:
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise PaginationError("limit must be an integer between 1 and 1000")
    if after is None:
        return
    if not isinstance(after, JobCursor):
        raise PaginationError("after must be a JobCursor")
    if not isinstance(after.created_at, datetime) or after.created_at.utcoffset() is None:
        raise PaginationError("cursor created_at must be timezone-aware")
    try:
        identifier = UUID(after.id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise PaginationError("cursor id must be a canonical UUID4 string") from exc
    if identifier.version != 4 or str(identifier) != after.id:
        raise PaginationError("cursor id must be a canonical UUID4 string")


def _validate_result(
    number: int, outcome: AttemptOutcome, status: JobStatus, next_run_at: datetime | None
) -> None:
    if type(number) is not int or number < 1:
        raise InvalidAttemptResultError("number must be a positive integer")
    if not isinstance(outcome, AttemptOutcome) or not isinstance(outcome.status, AttemptStatus):
        raise InvalidAttemptResultError("outcome must contain an AttemptStatus")
    if not isinstance(status, JobStatus) or status not in (
        JobStatus.SUCCESS,
        JobStatus.FAILED,
        JobStatus.RETRY_WAIT,
    ):
        raise InvalidAttemptResultError("Job disposition must be SUCCESS, FAILED or RETRY_WAIT")
    for value in (outcome.exception_type, outcome.error_message, outcome.traceback):
        if value is not None and not isinstance(value, str):
            raise InvalidAttemptResultError("error details must be strings or None")
    if outcome.status is AttemptStatus.SUCCESS:
        if status is not JobStatus.SUCCESS or any(
            value is not None
            for value in (
                outcome.error_kind,
                outcome.exception_type,
                outcome.error_message,
                outcome.traceback,
            )
        ):
            raise InvalidAttemptResultError(
                "SUCCESS requires a successful Job and no error details"
            )
    elif outcome.status in (AttemptStatus.FAILED, AttemptStatus.INTERRUPTED):
        if status is JobStatus.SUCCESS or not isinstance(outcome.error_kind, ErrorKind):
            raise InvalidAttemptResultError("unsuccessful outcomes require an error kind")
        interrupted = outcome.status is AttemptStatus.INTERRUPTED
        if interrupted != (outcome.error_kind is ErrorKind.INTERRUPTED):
            raise InvalidAttemptResultError("INTERRUPTED status and error kind must agree")
        if status is JobStatus.RETRY_WAIT and outcome.error_kind is ErrorKind.CONFIG_ERROR:
            raise InvalidAttemptResultError("CONFIG_ERROR cannot be retried")
    else:
        raise InvalidAttemptResultError("RUNNING is not a completion result")
    if status is JobStatus.RETRY_WAIT:
        if not isinstance(next_run_at, datetime) or next_run_at.utcoffset() is None:
            raise InvalidAttemptResultError("RETRY_WAIT requires a timezone-aware next_run_at")
    elif next_run_at is not None:
        raise InvalidAttemptResultError("terminal Jobs cannot have next_run_at")


class SQLiteStore:
    """Explicit initialization; no task code or registry is needed for queries."""

    def __init__(self, config: DatabaseConfig) -> None:
        self.config = DatabaseConfig(**config.model_dump())
        self._engine = create_engine(self.config)
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        """Release pooled connections; snapshots remain usable."""
        await self._engine.dispose()

    @asynccontextmanager
    async def _session(
        self, *, write: bool = False, check_schema: bool = True
    ) -> AsyncIterator[AsyncSession]:
        try:
            async with self._sessions() as session:
                await session.connection(execution_options={"sqlite_immediate": write})
                if check_schema:
                    version = (await session.execute(text("PRAGMA user_version"))).scalar_one()
                    if version != SCHEMA_VERSION:
                        raise SchemaError(
                            f"Expected schema {SCHEMA_VERSION}, found {version}; "
                            "initialize explicitly"
                        )
                yield session
                # Uncommitted work, including read transactions, is rolled back on close.
        except SQLAlchemyError as exc:
            raise StorageError(f"SQLite operation failed: {exc}") from exc

    async def initialize(self) -> None:
        """Create version 1 atomically or verify an existing compatible database."""
        async with self._session(write=True, check_schema=False) as session:
            connection = await session.connection()
            version = (await session.execute(text("PRAGMA user_version"))).scalar_one()
            tables = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
            if version == 0:
                if tables:
                    raise SchemaError("Refusing to initialize an unversioned database with tables")
                await connection.run_sync(Base.metadata.create_all)
                await session.execute(text(f"PRAGMA user_version={SCHEMA_VERSION}"))
            elif version != SCHEMA_VERSION:
                raise SchemaError(
                    f"Unsupported schema version {version}; expected {SCHEMA_VERSION}"
                )
            else:
                for table in Base.metadata.sorted_tables:
                    if table.name not in tables:
                        raise SchemaError(f"Schema {version} is missing table {table.name!r}")
                    columns = await connection.run_sync(_column_names, table.name)
                    if columns != set(table.columns.keys()):
                        raise SchemaError(
                            f"Schema {version} has incompatible columns in {table.name!r}"
                        )
            await _commit(session)

    async def submit[InputT: BaseModel](
        self, definition: TaskDefinition[InputT], data: object
    ) -> Job:
        """Validate before opening a transaction; return only after commit."""
        payload = definition.serialize_input(data)
        policy = definition.policy
        created_at = _now_us()
        row = JobRow(
            id=str(uuid4()),
            task_name=definition.name,
            params_json=payload,
            status=JobStatus.PENDING.value,
            created_at=created_at,
            started_at=None,
            finished_at=None,
            next_run_at=created_at,
            attempt_count=0,
            max_retries=policy.max_retries,
            retry_base_seconds=policy.retry_base_seconds,
            retry_cap_seconds=policy.retry_cap_seconds,
            timeout_seconds=policy.timeout_seconds,
            last_error_kind=None,
            last_error_message=None,
        )
        commit_attempted = False
        try:
            async with self._session(write=True) as session:
                session.add(row)
                await session.flush()
                snapshot = _job(row)
                commit_attempted = True
                await _commit(session)
            return snapshot
        except (Exception, asyncio.CancelledError) as exc:
            if commit_attempted:
                raise SubmissionUncertainError(row.id) from exc
            raise

    async def claim_next(self) -> AttemptClaim | None:
        """Atomically claim the first due waiting Job; None means none eligible."""
        commit_attempted = False
        try:
            async with self._session(write=True) as session:
                now = _now_us()
                eligible = (
                    JobRow.status.in_((JobStatus.PENDING.value, JobStatus.RETRY_WAIT.value)),
                    JobRow.next_run_at <= now,
                    JobRow.attempt_count < JobRow.max_retries + 1,
                )
                row = await session.scalar(
                    select(JobRow)
                    .where(*eligible)
                    .order_by(JobRow.next_run_at, JobRow.created_at, JobRow.id)
                    .limit(1)
                )
                if row is None:
                    return None
                # Repeat eligibility in the write, even under the IMMEDIATE lock.
                changed = await session.scalar(
                    update(JobRow)
                    .where(JobRow.id == row.id, *eligible)
                    .values(
                        status=JobStatus.RUNNING.value,
                        attempt_count=JobRow.attempt_count + 1,
                        started_at=func.coalesce(JobRow.started_at, now),
                        next_run_at=None,
                        finished_at=None,
                    )
                    .returning(JobRow.id)
                    .execution_options(synchronize_session=False)
                )
                if changed is None:
                    return None
                await session.refresh(row)
                session.add(
                    AttemptRow(
                        job_id=row.id,
                        number=row.attempt_count,
                        status=AttemptStatus.RUNNING.value,
                        started_at=now,
                        finished_at=None,
                        error_kind=None,
                        exception_type=None,
                        error_message=None,
                        traceback=None,
                    )
                )
                await session.flush()
                claim = AttemptClaim(_job(row), row.attempt_count)
                commit_attempted = True
                await _commit(session)
            return claim
        except (Exception, asyncio.CancelledError) as exc:
            if commit_attempted:
                raise ClaimUncertainError(claim.job_id, claim.number) from exc
            raise

    async def finish_attempt(
        self,
        job_id: str,
        number: int,
        outcome: AttemptOutcome,
        *,
        status: JobStatus,
        next_run_at: datetime | None = None,
    ) -> Job:
        """Finish the current attempt and persist a caller-selected disposition."""
        _validate_result(number, outcome, status, next_run_at)
        commit_attempted = False
        try:
            async with self._session(write=True) as session:
                row = await session.get(JobRow, job_id)
                if row is None:
                    raise UnknownJobError(f"Unknown Job {job_id!r}")
                attempt = await session.get(AttemptRow, (job_id, number))
                if (
                    row.status != JobStatus.RUNNING.value
                    or row.attempt_count != number
                    or attempt is None
                    or attempt.status != AttemptStatus.RUNNING.value
                ):
                    raise AttemptConflictError(
                        f"Job {job_id!r}, attempt {number} is not the current RUNNING attempt"
                    )
                now = _now_us()
                due = None if next_run_at is None else _epoch_us(next_run_at)
                if status is JobStatus.RETRY_WAIT and (
                    row.attempt_count >= row.max_retries + 1 or due is None or due <= now
                ):
                    raise InvalidAttemptResultError(
                        "retry requires remaining budget and a future time"
                    )
                attempt.status = outcome.status.value
                attempt.finished_at = now
                attempt.error_kind = (
                    None if outcome.error_kind is None else outcome.error_kind.value
                )
                attempt.exception_type = (
                    None
                    if outcome.exception_type is None
                    else outcome.exception_type[:EXCEPTION_TYPE_LIMIT]
                )
                attempt.error_message = (
                    None
                    if outcome.error_message is None
                    else outcome.error_message[:ERROR_MESSAGE_LIMIT]
                )
                attempt.traceback = (
                    None if outcome.traceback is None else outcome.traceback[:TRACEBACK_LIMIT]
                )
                # Flush separately: failure during the subsequent Job write must
                # roll back this already-written attempt too.
                await session.flush()
                row.status = status.value
                row.finished_at = None if status is JobStatus.RETRY_WAIT else now
                row.next_run_at = due
                row.last_error_kind = attempt.error_kind
                row.last_error_message = attempt.error_message
                await session.flush()
                snapshot = _job(row)
                commit_attempted = True
                await _commit(session)
            return snapshot
        except (Exception, asyncio.CancelledError) as exc:
            if commit_attempted:
                raise CompletionUncertainError(job_id, number) from exc
            raise

    async def get(self, job_id: str) -> Job:
        async with self._session() as session:
            row = await session.get(JobRow, job_id)
            if row is None:
                raise UnknownJobError(f"Unknown Job {job_id!r}")
            return _job(row)

    async def list(self, *, limit: int = 100, after: JobCursor | None = None) -> tuple[Job, ...]:
        """Ascending keyset page; cursor need not refer to a still-existing Job."""
        _validate_page(limit, after)
        statement = select(JobRow).order_by(JobRow.created_at, JobRow.id).limit(limit)
        if after is not None:
            timestamp = _epoch_us(after.created_at)
            statement = statement.where(
                or_(
                    JobRow.created_at > timestamp,
                    (JobRow.created_at == timestamp) & (JobRow.id > after.id),
                )
            )
        async with self._session() as session:
            rows = (await session.scalars(statement)).all()
            return tuple(_job(row) for row in rows)

    async def history(self, job_id: str) -> tuple[Attempt, ...]:
        """Attempts by number; existing unclaimed Jobs have empty history."""
        async with self._session() as session:
            if await session.get(JobRow, job_id) is None:
                raise UnknownJobError(f"Unknown Job {job_id!r}")
            rows = (
                await session.scalars(
                    select(AttemptRow)
                    .where(AttemptRow.job_id == job_id)
                    .order_by(AttemptRow.number)
                )
            ).all()
            return tuple(_attempt(row) for row in rows)
