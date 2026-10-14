"""Business commands using only public core submission and queries."""

import json
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import JsonValue

from integrations.github.auth import resolve_credential
from integrations.github.config import GitHubConfig, RefreshInput, Source
from integrations.github.errors import StateError
from integrations.github.locking import InstanceLocks
from integrations.github.project import ExecutionIdentity
from integrations.github.receipts import core_evidence, export_source_locked, validate_scenario
from integrations.github.storage import BusinessStore, SubmissionIntent
from integrations.github.tasks import execute_refresh, registry
from taskforge import DatabaseConfig, Job, SQLiteStore, SubmissionUncertainError, TaskForge


def matching_job(job: Job, data: RefreshInput) -> None:
    if job.task_name != "github.refresh":
        raise StateError("Job task does not match the submission intent")
    try:
        candidate = RefreshInput.model_validate_json(job.params_json)
    except ValueError:
        raise StateError("Job input does not match the submission intent") from None
    if candidate.model_dump_json() != data.model_dump_json():
        raise StateError("Job input does not match the submission intent")


async def reconcile_intent(
    api: TaskForge,
    store: BusinessStore,
    intent: SubmissionIntent,
    job_id: str | None = None,
) -> str | None:
    """Zero matches remains pending; failure or ambiguity never authorizes another submit."""
    if intent.job_id is not None and job_id not in (None, intent.job_id):
        raise StateError("Job ID conflicts with the existing association")
    known = intent.job_id or job_id
    if known is not None:
        job = await api.get(known)
        matching_job(job, intent.input)
        await store.associate_job(str(intent.input.refresh_id), job.id)
        return job.id
    matches = []
    after = None
    while True:
        page = await api.list(limit=100, after=after)
        for job in page:
            # A matching identity with conflicting/invalid complete input is a conflict.
            raw = json.loads(job.params_json)
            if isinstance(raw, dict) and raw.get("refresh_id") == str(intent.input.refresh_id):
                matching_job(job, intent.input)
                matches.append(job.id)
        if not page:
            break
        after = page[-1].cursor
    if len(matches) > 1:
        raise StateError("Multiple Jobs match this intent; manual investigation required")
    if not matches:
        return None
    await store.associate_job(str(intent.input.refresh_id), matches[0])
    return matches[0]


def select_sources(config: GitHubConfig, source_id: str | None) -> tuple[Source, ...]:
    selected = tuple(s for s in config.sources if source_id is None or s.source_id == source_id)
    if not selected:
        raise StateError("Unknown configured source")
    return selected


async def initialize(directory: Path, config: GitHubConfig) -> dict[str, JsonValue]:
    business = BusinessStore(directory)
    await business.initialize()
    async with SQLiteStore(
        DatabaseConfig(database_path=business.directory / "tasks.sqlite")
    ) as core:
        await core.initialize()
    for source in config.sources:
        async with InstanceLocks(directory).source(source.source_id):
            await business.register_source(source)
    return {"initialized": True, "sources": [s.source_id for s in config.sources]}


async def submit_one(
    api: TaskForge,
    business: BusinessStore,
    source: Source,
    refresh_id: UUID,
    rebaseline: bool,
) -> dict[str, JsonValue]:
    async with InstanceLocks(business.directory).source(source.source_id):
        data, fresh = await business.prepare_intent(source, refresh_id, rebaseline=rebaseline)
        if fresh:
            try:
                job = await api.submit("github.refresh", data)
            except SubmissionUncertainError as error:
                # Query the supplied public identity. Query errors leave the durable intent pending.
                intent = await business.intent(str(refresh_id))
                assert intent is not None
                identifier = await reconcile_intent(api, business, intent, error.job_id)
            else:
                matching_job(job, data)
                await business.associate_job(str(refresh_id), job.id)
                identifier = job.id
        else:
            intent = await business.intent(str(refresh_id))
            assert intent is not None
            identifier = await reconcile_intent(api, business, intent)
        return {
            "source": source.source_id,
            "refresh_id": str(refresh_id),
            "job_id": identifier,
            "status": "associated" if identifier else "pending_reconciliation",
        }


async def submit(
    directory: Path,
    config: GitHubConfig,
    source_id: str | None = None,
    refresh_id: UUID | None = None,
    rebaseline: bool = False,
) -> list[dict[str, JsonValue]]:
    sources = select_sources(config, source_id)
    if refresh_id is not None and len(sources) != 1:
        raise StateError("--refresh-id requires exactly one source")
    await resolve_credential()
    business = BusinessStore(directory)
    async with SQLiteStore(
        DatabaseConfig(database_path=business.directory / "tasks.sqlite")
    ) as core:
        api = TaskForge(core, registry)
        results: list[dict[str, JsonValue]] = []
        for source in sources:
            identifier = refresh_id or uuid4()
            try:
                results.append(await submit_one(api, business, source, identifier, rebaseline))
            except Exception as error:
                results.append(
                    {
                        "source": source.source_id,
                        "refresh_id": str(identifier),
                        "status": "pending_reconciliation",
                        "error": type(error).__name__,
                    }
                )
        return results


async def reconcile(
    directory: Path,
    refresh_id: UUID | None = None,
    job_id: str | None = None,
) -> list[dict[str, JsonValue]]:
    business = BusinessStore(directory)
    if job_id is not None and refresh_id is None:
        raise StateError("--job-id requires --refresh-id")
    intents = await business.intents()
    if refresh_id is not None:
        intents = tuple(i for i in intents if i.input.refresh_id == refresh_id)
        if not intents:
            raise StateError("Unknown submission intent")
    results: list[dict[str, JsonValue]] = []
    async with SQLiteStore(
        DatabaseConfig(database_path=business.directory / "tasks.sqlite")
    ) as core:
        api = TaskForge(core)
        for intent in intents:
            async with InstanceLocks(business.directory).source(intent.input.source.source_id):
                current = await business.intent(str(intent.input.refresh_id))
                assert current is not None
                identifier = await reconcile_intent(api, business, current, job_id)
                results.append(
                    {
                        "source": intent.input.source.source_id,
                        "refresh_id": str(intent.input.refresh_id),
                        "job_id": identifier,
                        "status": "associated" if identifier else "pending_reconciliation",
                    }
                )
    return results


async def show(
    directory: Path, source_id: str | None = None, refresh_id: UUID | None = None
) -> list[dict[str, JsonValue]]:
    business = BusinessStore(directory)
    identifiers = (source_id,) if source_id is not None else await business.source_ids()
    selected_intent = None
    if refresh_id is not None:
        selected_intent = await business.intent(str(refresh_id))
        if selected_intent is None:
            raise StateError("Unknown submission intent")
        if source_id is not None and source_id != selected_intent.input.source.source_id:
            raise StateError("Refresh does not belong to the selected source")
        identifiers = (selected_intent.input.source.source_id,)
    output: list[dict[str, JsonValue]] = []
    for identifier in identifiers:
        state = await business.source_state(identifier)
        batches: list[JsonValue] = []
        for batch_id in await business.batch_ids(identifier):
            if refresh_id is None or batch_id == str(refresh_id):
                batch = await business.batch(batch_id)
                batch.pop("input")
                batches.append(batch)
        intents: list[JsonValue] = []
        for intent in await business.intents():
            if intent.input.source.source_id == identifier and (
                refresh_id is None or intent.input.refresh_id == refresh_id
            ):
                evidence = None
                if intent.job_id is not None:
                    async with SQLiteStore(
                        DatabaseConfig(database_path=business.directory / "tasks.sqlite")
                    ) as core:
                        matching_job(await TaskForge(core).get(intent.job_id), intent.input)
                    evidence = await core_evidence(business.directory, intent.job_id)
                intents.append(
                    {
                        "refresh_id": str(intent.input.refresh_id),
                        "job_id": intent.job_id,
                        "generation": intent.input.source_generation,
                        "operation": intent.input.operation,
                        "business_complete": await business.completed(str(intent.input.refresh_id)),
                        "business_attempts": await business.attempts(str(intent.input.refresh_id)),
                        "association": "associated" if intent.job_id else "unassociated",
                        "core": evidence,
                    }
                )
        output.append(
            {
                "source": state.source.model_dump(mode="json"),
                "generation": state.generation,
                "checkpoint": state.checkpoint,
                "batches": batches,
                "intents": intents,
            }
        )
    return output


async def export(
    directory: Path, source_id: str | None = None, scenario: str | None = None
) -> list[str]:
    label = validate_scenario(scenario)
    business = BusinessStore(directory)
    sources = (source_id,) if source_id is not None else await business.source_ids()
    paths = []
    for source in sources:
        async with InstanceLocks(business.directory).source(source):
            paths.extend(await export_source_locked(business, source, label))
    return paths


async def direct(
    directory: Path,
    config: GitHubConfig,
    source_id: str | None = None,
    refresh_id: UUID | None = None,
    rebaseline: bool = False,
) -> list[dict[str, JsonValue]]:
    sources = select_sources(config, source_id)
    if refresh_id is not None and len(sources) != 1:
        raise StateError("--refresh-id requires exactly one source")
    credential = await resolve_credential()
    business = BusinessStore(directory)
    results: list[dict[str, JsonValue]] = []
    for source in sources:
        identifier = refresh_id or uuid4()
        try:
            async with InstanceLocks(business.directory).source(source.source_id):
                data, _ = await business.prepare_intent(source, identifier, rebaseline=rebaseline)
                intent = await business.intent(str(identifier))
                if intent is not None and intent.job_id is not None:
                    raise StateError("Intent belongs to a core Job")
            committed = await execute_refresh(
                data,
                ExecutionIdentity(
                    execution_id="direct-" + str(uuid4()),
                    attempt_number=1,
                ),
                credential=credential,
            )
            results.append(
                {"source": source.source_id, "refresh_id": str(identifier), "committed": committed}
            )
        except Exception as error:
            results.append(
                {
                    "source": source.source_id,
                    "refresh_id": str(identifier),
                    "error": type(error).__name__,
                }
            )
    return results
