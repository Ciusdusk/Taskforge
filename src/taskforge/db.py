"""SQLite schema and a single owner for transaction opening."""

from typing import Any

from sqlalchemy import (
    URL,
    CheckConstraint,
    Connection,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    event,
    text,
)
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from taskforge.config import DatabaseConfig

SCHEMA_VERSION = 1
ERROR_MESSAGE_LIMIT = 2000
EXCEPTION_TYPE_LIMIT = 256
TRACEBACK_LIMIT = 16000


class Base(DeclarativeBase):
    pass


class JobRow(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint("status IN ('PENDING','RUNNING','RETRY_WAIT','SUCCESS','FAILED')"),
        CheckConstraint("typeof(attempt_count) = 'integer' AND attempt_count >= 0"),
        CheckConstraint("typeof(max_retries) = 'integer' AND max_retries >= 0"),
        CheckConstraint("attempt_count <= max_retries + 1"),
        CheckConstraint("retry_base_seconds > 0 AND retry_base_seconds <= 1.7976931348623157e308"),
        CheckConstraint(
            "retry_cap_seconds >= retry_base_seconds "
            "AND retry_cap_seconds <= 1.7976931348623157e308"
        ),
        CheckConstraint("timeout_seconds > 0 AND timeout_seconds <= 1.7976931348623157e308"),
        CheckConstraint("typeof(created_at) = 'integer'"),
        CheckConstraint("started_at IS NULL OR typeof(started_at) = 'integer'"),
        CheckConstraint("finished_at IS NULL OR typeof(finished_at) = 'integer'"),
        CheckConstraint("next_run_at IS NULL OR typeof(next_run_at) = 'integer'"),
        CheckConstraint(
            "last_error_kind IS NULL OR last_error_kind IN "
            "('TASK_ERROR','TIMEOUT','CONFIG_ERROR','INTERRUPTED')"
        ),
        CheckConstraint(
            f"last_error_message IS NULL OR length(last_error_message) <= {ERROR_MESSAGE_LIMIT}"
        ),
        Index("ix_jobs_due", "status", "next_run_at", "created_at", "id"),
        Index("ix_jobs_created", "created_at", "id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_name: Mapped[str] = mapped_column(Text)
    params_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    created_at: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[int | None] = mapped_column(Integer)
    finished_at: Mapped[int | None] = mapped_column(Integer)
    next_run_at: Mapped[int | None] = mapped_column(Integer)
    attempt_count: Mapped[int] = mapped_column(Integer)
    max_retries: Mapped[int] = mapped_column(Integer)
    retry_base_seconds: Mapped[float] = mapped_column(Float)
    retry_cap_seconds: Mapped[float] = mapped_column(Float)
    timeout_seconds: Mapped[float] = mapped_column(Float)
    last_error_kind: Mapped[str | None] = mapped_column(Text)
    last_error_message: Mapped[str | None] = mapped_column(Text)


class AttemptRow(Base):
    __tablename__ = "attempts"
    __table_args__ = (
        CheckConstraint("typeof(number) = 'integer' AND number >= 1"),
        CheckConstraint("status IN ('RUNNING','SUCCESS','FAILED','INTERRUPTED')"),
        CheckConstraint("typeof(started_at) = 'integer'"),
        CheckConstraint("finished_at IS NULL OR typeof(finished_at) = 'integer'"),
        CheckConstraint(
            "error_kind IS NULL OR error_kind IN "
            "('TASK_ERROR','TIMEOUT','CONFIG_ERROR','INTERRUPTED')"
        ),
        CheckConstraint(
            f"exception_type IS NULL OR length(exception_type) <= {EXCEPTION_TYPE_LIMIT}"
        ),
        CheckConstraint(f"error_message IS NULL OR length(error_message) <= {ERROR_MESSAGE_LIMIT}"),
        CheckConstraint(f"traceback IS NULL OR length(traceback) <= {TRACEBACK_LIMIT}"),
        Index(
            "ix_attempts_one_running",
            "job_id",
            unique=True,
            sqlite_where=text("status = 'RUNNING'"),
        ),
    )

    job_id: Mapped[str] = mapped_column(Text, ForeignKey("jobs.id"), primary_key=True)
    number: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(Text)
    started_at: Mapped[int] = mapped_column(Integer)
    finished_at: Mapped[int | None] = mapped_column(Integer)
    error_kind: Mapped[str | None] = mapped_column(Text)
    exception_type: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    traceback: Mapped[str | None] = mapped_column(Text)


def create_engine(config: DatabaseConfig) -> AsyncEngine:
    engine = create_async_engine(
        URL.create("sqlite+aiosqlite", database=str(config.database_path)),
        connect_args={"timeout": config.busy_timeout_ms / 1000},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def configure_connection(dbapi_connection: Any, connection_record: Any) -> None:
        # Disable the driver's implicit BEGIN; the begin hook below is the sole owner.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={config.busy_timeout_ms}")
        cursor.close()

    @event.listens_for(engine.sync_engine, "begin")
    def begin(connection: Connection) -> None:
        immediate = connection.get_execution_options().get("sqlite_immediate", False)
        connection.exec_driver_sql("BEGIN IMMEDIATE" if immediate else "BEGIN")

    return engine
