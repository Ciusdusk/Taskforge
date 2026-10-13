"""Small local fixtures; business side effects belong to these tasks, not the core."""

import asyncio
import base64
import json
import sqlite3
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from taskforge import ExecutionPolicy, TaskRegistry, get_execution_context

registry = TaskRegistry()
RETRY = ExecutionPolicy(
    max_retries=1, retry_base_seconds=0.1, retry_cap_seconds=0.1, timeout_seconds=5
)


class RemoteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    port: int = Field(gt=0, le=65535)
    target: Path
    transient: bool = False


async def request(port: int, operation: str, key: str) -> dict:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(json.dumps({"operation": operation, "key": key}).encode() + b"\n")
        await writer.drain()
        response = json.loads(await reader.readline())
        if "error" in response:
            raise ConnectionError(response["error"])
        return response
    finally:
        writer.close()
        await writer.wait_closed()


@registry.task("local.sync", input_model=RemoteInput, policy=RETRY)
async def sync(data: RemoteInput) -> None:
    response = await request(
        data.port, "transient" if data.transient else "records", str(data.target)
    )
    # Replacing the same tiny snapshot is safe to repeat in this fixture.
    data.target.write_text(json.dumps(response["records"]), encoding="utf-8")


@registry.task("local.backup", input_model=RemoteInput, policy=RETRY)
async def backup(data: RemoteInput) -> None:
    response = await request(data.port, "download", str(data.target))
    data.target.write_bytes(base64.b64decode(response["content"]))


class MaintenanceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    directory: Path


@registry.task("local.maintenance", input_model=MaintenanceInput)
async def maintenance(data: MaintenanceInput) -> None:
    # Use only the demonstration scratch directory; do not point this at user data.
    for path in data.directory.glob("*.tmp"):
        if path.is_file():
            path.unlink(missing_ok=True)
        await asyncio.sleep(0)


class WaitInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    started: Path
    cleaned: Path


async def wait(data: WaitInput) -> None:
    context = get_execution_context()
    assert context is not None
    data.started.write_text(str(context.attempt_number))
    try:
        await asyncio.Event().wait()
    finally:
        await asyncio.sleep(0)
        data.cleaned.write_text(str(context.attempt_number))


registry.task(
    "local.timeout",
    input_model=WaitInput,
    policy=ExecutionPolicy(
        max_retries=1, retry_base_seconds=0.1, retry_cap_seconds=0.1, timeout_seconds=0.15
    ),
)(wait)
registry.task("local.stop", input_model=WaitInput)(wait)


class LedgerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    database: Path
    idempotent: bool
    value: int = 7


@registry.task("local.ledger", input_model=LedgerInput, policy=RETRY)
async def ledger(data: LedgerInput) -> None:
    context = get_execution_context()
    assert context is not None
    await asyncio.sleep(0)
    # A separate business DB commits the idempotency key AND effect together.
    # These are tiny bounded fixtures, not an async driver for production DB I/O.
    connection = sqlite3.connect(data.database)
    try:
        with connection:
            connection.execute("CREATE TABLE IF NOT EXISTS calls (job_id, number)")
            connection.execute("CREATE TABLE IF NOT EXISTS effects (job_id, number, value)")
            if data.idempotent:
                connection.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS applied_job ON effects(job_id)"
                )
            connection.execute(
                "INSERT INTO calls VALUES (?, ?)", (context.job_id, context.attempt_number)
            )
            statement = "INSERT OR IGNORE" if data.idempotent else "INSERT"
            connection.execute(
                f"{statement} INTO effects VALUES (?, ?, ?)",
                (context.job_id, context.attempt_number, data.value),
            )
    finally:
        connection.close()
