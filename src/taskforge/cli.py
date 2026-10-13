"""Typer entry point; persistence and execution remain in the core interfaces."""

import asyncio
import json
import logging
import signal
import sys
from collections.abc import Coroutine
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError

from taskforge.api import TaskForge
from taskforge.config import DatabaseConfig, WorkerConfig
from taskforge.errors import (
    ClaimUncertainError,
    CompletionUncertainError,
    InputSerializationError,
    PaginationError,
    RegistryLoadError,
    SchemaError,
    StorageError,
    SubmissionUncertainError,
    UnknownJobError,
    UnknownTaskError,
    WorkerLockError,
)
from taskforge.logging import JSONFormatter
from taskforge.models import JobCursor
from taskforge.registry import load_registry
from taskforge.storage import SQLiteStore
from taskforge.worker import Worker

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Persistent async tasks.")
Database = Annotated[
    Path, typer.Option("--database", help="Local SQLite file; initialize explicitly.")
]
Registry = Annotated[str, typer.Option("--registry", help="Trusted module:attribute registry.")]


class _InputError(ValueError):
    pass


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Cannot encode {type(value).__name__}")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, default=_json_default, allow_nan=False)


def _report(exc: Exception | asyncio.CancelledError) -> int:
    if isinstance(exc, (SubmissionUncertainError, ClaimUncertainError, CompletionUncertainError)):
        code = 9
    elif isinstance(exc, (_InputError, ValidationError, PaginationError, InputSerializationError)):
        code = 2
    elif isinstance(exc, (UnknownJobError, UnknownTaskError)):
        code = 3
    elif isinstance(exc, RegistryLoadError):
        code = 4
    elif isinstance(exc, SchemaError):
        code = 5
    elif isinstance(exc, WorkerLockError):
        code = 6
    elif isinstance(exc, StorageError):
        code = 7
    elif isinstance(exc, asyncio.CancelledError):
        code = 130
    else:
        code = 8
    message = str(exc)
    if isinstance(exc, ValidationError):
        # Model errors are actionable without echoing potentially sensitive inputs.
        message = _json(
            [
                {"loc": error["loc"], "msg": error["msg"], "type": error["type"]}
                for error in exc.errors(include_input=False, include_url=False)
            ]
        )
    typer.echo(
        _json(
            {
                "error": type(exc).__name__,
                "message": message,
                "job_id": getattr(exc, "job_id", None),
                "attempt_number": getattr(exc, "number", None),
            }
        ),
        err=True,
    )
    return code


def _run(operation: Coroutine[Any, Any, object]) -> None:
    try:
        result = asyncio.run(operation)
    except (Exception, asyncio.CancelledError) as exc:
        raise typer.Exit(_report(exc)) from exc
    if result is not None:
        typer.echo(result if isinstance(result, str) else _json(result))


def _read_input(path: str) -> object:
    try:
        payload = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
        return json.loads(payload, parse_constant=_reject_constant)
    except (OSError, ValueError) as exc:
        raise _InputError(f"Cannot read JSON input: {exc}") from exc


def _reject_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON number {value!r} is invalid")


def _cursor(value: str | None) -> JobCursor | None:
    if value is None:
        return None
    try:
        data = json.loads(value)
        if not isinstance(data, dict) or set(data) != {"created_at", "id"}:
            raise ValueError("expected created_at and id")
        if not isinstance(data["id"], str):
            raise ValueError("id must be a string")
        return JobCursor(datetime.fromisoformat(data["created_at"]), data["id"])
    except (ValueError, TypeError) as exc:
        raise _InputError(f"Invalid --after cursor: {exc}") from exc


def _database_config(database: Path) -> DatabaseConfig:
    config = DatabaseConfig(database_path=database)
    if not config.database_path.parent.is_dir():
        raise StorageError(
            f"Database parent directory does not exist: {config.database_path.parent}"
        )
    if config.database_path.exists() and not config.database_path.is_file():
        raise StorageError(f"Database path must be a local file: {config.database_path}")
    return config


async def _initialize(database: Path) -> object:
    async with SQLiteStore(_database_config(database)) as store:
        await store.initialize()
        return {"database_path": str(store.config.database_path), "initialized": True}


@app.command("init")
def initialize(database: Database = Path("taskforge.sqlite")) -> None:
    """Explicitly create or validate the database schema."""
    _run(_initialize(database))


async def _submit(database: Path, registry: str, task_name: str, input_path: str) -> str:
    tasks = load_registry(registry)
    data = _read_input(input_path)
    async with SQLiteStore(_database_config(database)) as store:
        job = await TaskForge(store, tasks).submit(task_name, data)
        return job.id


@app.command()
def submit(
    task_name: Annotated[str, typer.Argument(help="Registered stable task name.")],
    registry: Registry,
    database: Database = Path("taskforge.sqlite"),
    input_path: Annotated[
        str, typer.Option("--input", help="UTF-8 JSON file, or - for stdin.")
    ] = "-",
) -> None:
    """Validate and persist input; print the committed Job ID."""
    _run(_submit(database, registry, task_name, input_path))


async def _query(
    database: Path, command: str, job_id: str = "", limit: int = 100, after: str | None = None
) -> object:
    cursor = _cursor(after)
    async with SQLiteStore(_database_config(database)) as store:
        query = TaskForge(store)
        if command == "show":
            return asdict(await query.get(job_id))
        if command == "history":
            return {
                "job_id": job_id,
                "attempts": [asdict(item) for item in await query.history(job_id)],
            }
        jobs = await query.list(limit=limit, after=cursor)
        return {
            "jobs": [asdict(job) for job in jobs],
            "next_cursor": _json(asdict(jobs[-1].cursor)) if jobs else None,
        }


@app.command("list")
def list_jobs(
    database: Database = Path("taskforge.sqlite"),
    limit: Annotated[int, typer.Option(help="Page size, 1 through 1000.")] = 100,
    after: Annotated[str | None, typer.Option(help="Previous next_cursor JSON string.")] = None,
) -> None:
    """List Jobs in ascending (created_at, id) order without loading tasks."""
    _run(_query(database, "list", limit=limit, after=after))


@app.command()
def show(job_id: str, database: Database = Path("taskforge.sqlite")) -> None:
    """Print a Job snapshot, including its submitted policy."""
    _run(_query(database, "show", job_id))


@app.command()
def history(job_id: str, database: Database = Path("taskforge.sqlite")) -> None:
    """Print attempts in number order; reject unknown Jobs."""
    _run(_query(database, "history", job_id))


async def _worker(
    database: Path, registry: str, concurrency: int, poll: float, grace: float
) -> None:
    config = WorkerConfig(
        concurrency=concurrency, poll_interval_seconds=poll, shutdown_grace_seconds=grace
    )
    tasks = load_registry(registry)
    async with SQLiteStore(_database_config(database)) as store:
        worker = Worker(store, tasks, config)
        loop = asyncio.get_running_loop()
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            for sig in previous:
                # Repeated signals only set the stop flag; they never cancel cleanup.
                loop.add_signal_handler(sig, worker.request_stop)
            await worker.run()
        finally:
            for sig, handler in previous.items():
                loop.remove_signal_handler(sig)
                signal.signal(sig, handler)


@app.command()
def worker(
    registry: Registry,
    database: Database = Path("taskforge.sqlite"),
    concurrency: Annotated[int, typer.Option(help="Number of asynchronous execution slots.")] = 4,
    poll_interval_seconds: Annotated[float, typer.Option(help="Idle polling interval.")] = 1.0,
    shutdown_grace_seconds: Annotated[
        float, typer.Option(help="Grace before cooperative cancel.")
    ] = 30.0,
) -> None:
    """Run until SIGINT/SIGTERM; hold storage and lock through cleanup."""
    logger = logging.getLogger("taskforge")
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JSONFormatter())
    previous_level, previous_propagate = logger.level, logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        _run(
            _worker(database, registry, concurrency, poll_interval_seconds, shutdown_grace_seconds)
        )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate
        handler.close()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
