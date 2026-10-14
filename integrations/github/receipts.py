"""Rebuild public receipts from committed state with atomic file replacement."""

import asyncio
import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import JsonValue

from integrations.github.errors import StateError
from integrations.github.lifecycle import protected
from integrations.github.models import canonical
from integrations.github.storage import BusinessStore
from taskforge import DatabaseConfig, Job, SQLiteStore, TaskForge

type Scenario = Literal["initialization", "natural_update", "historical_replay", "fault_experiment"]
SCENARIOS = ("initialization", "natural_update", "historical_replay", "fault_experiment")


def source_provenance() -> dict[str, JsonValue]:
    root = Path(__file__).resolve().parents[2]
    files = sorted(
        [
            *root.glob("src/taskforge/*.py"),
            *root.glob("integrations/**/*.py"),
            root / "integrations/github/default-sources.json",
            root / "pyproject.toml",
            root / "uv.lock",
        ]
    )
    hashes: dict[str, JsonValue] = {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files
    }
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False
    )
    return {
        "git_commit": commit.stdout.strip() if commit.returncode == 0 else None,
        "sha256": hashlib.sha256(canonical(hashes).encode()).hexdigest(),
        "files": hashes,
    }


def public_job(job: Job) -> dict[str, JsonValue]:
    # Persisted inputs and exception text can contain local paths; output selected fields only.
    return cast(
        dict[str, JsonValue],
        json.loads(
            canonical(
                {
                    key: value.isoformat() if isinstance(value, datetime) else value
                    for key, value in asdict(job).items()
                    if key not in {"params_json", "last_error_message"}
                }
            )
        ),
    )


async def core_evidence(directory: Path, job_id: str) -> dict[str, JsonValue]:
    async with SQLiteStore(DatabaseConfig(database_path=directory / "tasks.sqlite")) as store:
        api = TaskForge(store)
        job = await api.get(job_id)
        attempts: list[JsonValue] = [
            {
                key: value.isoformat() if isinstance(value, datetime) else value
                for key, value in asdict(attempt).items()
                if key not in {"traceback", "error_message"}
            }
            for attempt in await api.history(job_id)
        ]
        return {"job": public_job(job), "attempts": attempts}


async def receipt(
    store: BusinessStore, refresh_id: str, scenario: Scenario | None = None
) -> dict[str, JsonValue]:
    batch = await store.batch(refresh_id)
    raw = batch.pop("input")
    assert isinstance(raw, dict)
    raw.pop("state_directory")
    coverage = batch["coverage"]
    assert isinstance(coverage, dict)
    intent = await store.intent(refresh_id)
    evidence = None
    if intent is not None and intent.job_id is not None:
        # Check complete input before presenting a Job as belonging to this batch.
        from integrations.github.commands import matching_job

        async with SQLiteStore(
            DatabaseConfig(database_path=store.directory / "tasks.sqlite")
        ) as core:
            matching_job(await TaskForge(core).get(intent.job_id), intent.input)
        evidence = await core_evidence(store.directory, intent.job_id)
    collection_scenario = (
        "initialization" if coverage.get("before_checkpoint") is None else "natural_update"
    )
    core_status = None
    if evidence is not None:
        job = evidence["job"]
        assert isinstance(job, dict)
        core_status = job["status"]
    return {
        "schema_version": 1,
        "exported_at": datetime.now(UTC).isoformat(timespec="microseconds"),
        "scenario": scenario or collection_scenario,
        "collection_scenario": collection_scenario,
        "configuration_sha256": hashlib.sha256(canonical(raw["source"]).encode()).hexdigest(),
        "input": raw,
        "batch": batch,
        "core": evidence,
        "verification": {
            "business_complete": coverage.get("complete") is True,
            "core_status_at_export": core_status,
        },
    }


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = f".export-{path.name}-"
    for abandoned in path.parent.glob(prefix + "*"):
        abandoned.unlink()
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=prefix, delete=False
        ) as file:
            temporary = Path(file.name)
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def export_source_locked(
    store: BusinessStore, source_id: str, scenario: Scenario | None = None
) -> list[str]:
    """Caller holds the source lock, including actual file/thread cleanup."""
    state = await store.source_state(source_id)
    identifiers = await store.batch_ids(source_id)
    lines = [
        f"# GitHub: {source_id}",
        "",
        f"Generation: {state.generation}; "
        f"repository: [{state.source.repository}](https://github.com/{state.source.repository})",
        "",
        f"Checkpoint: `{canonical(state.checkpoint)}`",
        "",
        "Entity/version counts are current repository totals for this source kind.",
        "",
        "| Refresh | Scenario | Coverage | Requests | Changes | "
        "Entities | Versions | Diagnostics |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    paths = []
    for identifier in identifiers:
        result = await receipt(store, identifier, scenario)
        batch = result["batch"]
        assert isinstance(batch, dict)
        coverage = batch["coverage"]
        assert isinstance(coverage, dict)
        changes = batch["changes"]
        skills = batch["skill_changes"]
        assert isinstance(changes, list) and isinstance(skills, list)
        counts = batch["repository_counts"]
        assert isinstance(counts, dict)
        attempts = batch["attempts"]
        assert isinstance(attempts, list)
        diagnostics = (
            ", ".join(
                str(attempt["diagnostic"])
                for attempt in attempts
                if isinstance(attempt, dict) and attempt.get("diagnostic") is not None
            )
            or "none"
        )
        modes = ", ".join(
            str(part.get("mode", "complete"))
            for name in ("commits", "releases", "issues")
            if isinstance(part := coverage.get(name), dict)
        )
        relative = f"receipts/{identifier}.json"
        await protected(
            asyncio.to_thread(
                atomic_write,
                store.directory / relative,
                json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            )
        )
        paths.append(relative)
        lines.append(
            f"| [{identifier}](../{relative}) | {result['scenario']} | {modes} | "
            f"{coverage['requests']} | {len(changes) + len(skills)} | "
            f"{counts['entities']} | {counts['versions']} | {diagnostics} |"
        )
    index = f"indexes/{source_id}.md"
    await protected(
        asyncio.to_thread(atomic_write, store.directory / index, "\n".join(lines) + "\n")
    )
    paths.append(index)
    return paths


def validate_scenario(value: str | None) -> Scenario | None:
    if value is not None and value not in SCENARIOS:
        raise StateError("Unknown receipt scenario")
    return cast(Scenario | None, value)
