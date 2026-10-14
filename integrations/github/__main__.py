"""Six manual business commands; workers remain the core CLI's responsibility."""

import asyncio
import json
from collections.abc import Coroutine
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import typer

from integrations.github import commands
from integrations.github.config import GitHubConfig

app = typer.Typer(no_args_is_help=True, add_completion=False)
State = Annotated[Path, typer.Option("--state-directory")]
Config = Annotated[Path | None, typer.Option("--config")]
Source = Annotated[str | None, typer.Option("--source")]
Refresh = Annotated[UUID | None, typer.Option("--refresh-id")]
Baseline = Annotated[bool, typer.Option("--rebaseline")]
DEFAULT_STATE = Path(".taskforge/github")


def run(operation: Coroutine[Any, Any, object]) -> None:
    try:
        result = asyncio.run(operation)
    except (Exception, asyncio.CancelledError) as error:
        # Suppress arbitrary third-party messages and paths on public output.
        typer.echo(json.dumps({"error": type(error).__name__, "status": "failed"}), err=True)
        raise typer.Exit(130 if isinstance(error, asyncio.CancelledError) else 1) from None
    typer.echo(json.dumps(result, ensure_ascii=False, allow_nan=False))
    if isinstance(result, list) and any(
        isinstance(item, dict)
        and ("error" in item or item.get("status") == "pending_reconciliation")
        for item in result
    ):
        raise typer.Exit(1)


async def configured(
    command: str,
    directory: Path,
    config: Path | None,
    source: str | None = None,
    refresh_id: UUID | None = None,
    rebaseline: bool = False,
) -> object:
    settings = GitHubConfig.load(config)
    if command == "init":
        return await commands.initialize(directory, settings)
    function = commands.submit if command == "submit" else commands.direct
    return await function(directory, settings, source, refresh_id, rebaseline)


@app.command("init")
def initialize(state_directory: State = DEFAULT_STATE, config: Config = None) -> None:
    run(configured("init", state_directory, config))


@app.command()
def submit(
    state_directory: State = DEFAULT_STATE,
    config: Config = None,
    source: Source = None,
    refresh_id: Refresh = None,
    rebaseline: Baseline = False,
) -> None:
    run(configured("submit", state_directory, config, source, refresh_id, rebaseline))


@app.command()
def direct(
    state_directory: State = DEFAULT_STATE,
    config: Config = None,
    source: Source = None,
    refresh_id: Refresh = None,
    rebaseline: Baseline = False,
) -> None:
    run(configured("direct", state_directory, config, source, refresh_id, rebaseline))


@app.command()
def reconcile(
    state_directory: State = DEFAULT_STATE,
    refresh_id: Refresh = None,
    job_id: Annotated[str | None, typer.Option("--job-id")] = None,
) -> None:
    run(commands.reconcile(state_directory, refresh_id, job_id))


@app.command()
def show(
    state_directory: State = DEFAULT_STATE, source: Source = None, refresh_id: Refresh = None
) -> None:
    run(commands.show(state_directory, source, refresh_id))


@app.command("export")
def export_receipts(
    state_directory: State = DEFAULT_STATE,
    source: Source = None,
    scenario: Annotated[str | None, typer.Option("--scenario")] = None,
) -> None:
    run(commands.export(state_directory, source, scenario))


if __name__ == "__main__":
    app()
