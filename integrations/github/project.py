"""Project collection independent of TaskForge registration and execution."""

from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import httpx
from pydantic import Field, JsonValue

from integrations.github.auth import Credential
from integrations.github.config import Model, RefreshInput
from integrations.github.errors import (
    CollectionError,
    GenerationError,
    GitHubError,
    HTTPError,
    RebaselineRequired,
    StateError,
)
from integrations.github.http import AttemptBudget, GitHubHTTP
from integrations.github.locking import InstanceLocks
from integrations.github.models import (
    BatchWrite,
    Change,
    Entity,
    ReleaseIdentity,
    SkillChange,
    SkillSnapshot,
)
from integrations.github.remote import (
    CommitRecord,
    Comparison,
    ReleaseRecord,
    ReleaseVisibility,
    Repository,
    parse,
)
from integrations.github.skills import collect_skills
from integrations.github.storage import BusinessStore


class ExecutionIdentity(Model):
    execution_id: str = Field(min_length=1)
    attempt_number: int = Field(strict=True, gt=0)


def commit_entity(repository_id: int, record: CommitRecord) -> Entity:
    return Entity(
        repository_id=repository_id,
        kind="commit",
        remote_id=record.sha,
        content=record.model_dump(mode="json"),
    )


async def collect_commits(
    http: GitHubHTTP, repository: str, repository_id: int, target: str, before: str | None
) -> tuple[tuple[Entity, ...], tuple[Change, ...], dict[str, JsonValue]]:
    root = f"/repos/{repository}"
    records: list[CommitRecord] = []
    if before is None:
        value = await http.get(root + f"/commits?sha={target}&per_page=20&page=1")
        if not isinstance(value.body, list) or not 1 <= len(value.body) <= 20:
            raise CollectionError("Invalid finite Commit baseline")
        records = [parse(CommitRecord, item) for item in value.body]
        if records[0].sha != target:
            raise CollectionError("Commit baseline does not start at the fixed target")
        mode = "finite_initial_window"
    elif before == target:
        mode = "unchanged"
    else:
        path = root + f"/compare/{before}...{target}"
        metadata: tuple[int, int, int, str, str, str] | None = None
        unavailable = False
        try:
            async for value in http.iter_pages(path, list_field="commits"):
                comparison = parse(Comparison, value.body)
                current = (
                    comparison.total_commits,
                    comparison.ahead_by,
                    comparison.behind_by,
                    comparison.status,
                    comparison.base_commit.sha,
                    comparison.merge_base_commit.sha,
                )
                if (
                    comparison.status != "ahead"
                    or comparison.behind_by != 0
                    or comparison.base_commit.sha != before
                    or comparison.merge_base_commit.sha != before
                ):
                    raise RebaselineRequired(
                        "Commit history diverged or rewound; rebaseline required"
                    )
                if comparison.total_commits > http.budget.limits.max_commits:
                    raise RebaselineRequired(
                        "Incremental Commit limit exceeded; rebaseline required"
                    )
                if metadata is not None and metadata != current:
                    raise CollectionError("Compare metadata changed between fixed-SHA pages")
                metadata = current
                records.extend(comparison.commits)
        except HTTPError as error:
            if str(error) not in {"GitHub returned HTTP 404", "GitHub returned HTTP 422"}:
                raise
            unavailable = True
        if unavailable:
            raise RebaselineRequired("Previous SHA is not comparable; rebaseline required")
        if (
            metadata is None
            or not records
            or len(records) != metadata[0]
            or len(records) != metadata[1]
            or target not in {record.sha for record in records}
            or before in {record.sha for record in records}
        ):
            raise CollectionError("Compare count or fixed target identity is incomplete")
        mode = "complete_increment"
    if len({record.sha for record in records}) != len(records):
        raise CollectionError("Duplicate Commit identities in collection")
    entities = tuple(commit_entity(repository_id, record) for record in records)
    changes = tuple(
        Change(entity=entity, kind="initial" if before is None else "added") for entity in entities
    )
    return (
        entities,
        changes,
        {
            "complete": True,
            "mode": mode,
            "count": len(entities),
            "baseline_limit": 20,
            "before_sha": before,
            "target_sha": target,
            "history_scope": "finite baseline, then complete forward increments",
        },
    )


async def collect_releases(
    http: GitHubHTTP,
    store: BusinessStore,
    repository: str,
    repository_id: int,
    *,
    initial: bool,
    observed_at: datetime,
) -> tuple[
    tuple[Entity, ...], tuple[Change, ...], tuple[ReleaseIdentity, ...], dict[str, JsonValue]
]:
    values = await http.paginate(f"/repos/{repository}/releases")
    visible: dict[str, ReleaseRecord] = {}
    for value in values:
        if parse(ReleaseVisibility, value).draft:
            continue
        record = parse(ReleaseRecord, value)
        identity = str(record.id)
        if identity in visible:
            raise CollectionError("Duplicate Release identities; list coverage is uncertain")
        visible[identity] = record
    known = set(await store.release_ids(repository_id))
    saved = {entity.remote_id: entity for entity in await store.entities(repository_id, "release")}
    if initial:
        selected = {
            str(record.id)
            for record in sorted(
                visible.values(), key=lambda record: (record.published_at, record.id), reverse=True
            )[:10]
        }
        first_seen: set[str] = set()
    else:
        first_seen = visible.keys() - known
        selected = first_seen | (visible.keys() & saved.keys())
    entities = []
    changes = []
    published_this_week = 0
    week_start = observed_at - timedelta(days=7)
    for identity in sorted(selected, key=int):
        record = visible[identity]
        entity = Entity(
            repository_id=repository_id,
            kind="release",
            remote_id=identity,
            content=record.model_dump(mode="json"),
        )
        entities.append(entity)
        old = saved.get(identity)
        published = datetime.fromisoformat(record.published_at)
        recent = week_start <= published <= observed_at
        if initial:
            kind = "initial"
        elif identity in first_seen:
            kind = "first_observed"
            published_this_week += int(recent)
        elif old is not None and entity.fingerprint != old.fingerprint:
            kind = "updated"
        else:
            continue
        changes.append(
            Change(entity=entity, kind=kind, detail={"published_in_last_7_days": recent})
        )
    missing_ids = sorted(saved.keys() - visible.keys(), key=int)
    missing: list[JsonValue] = [identity for identity in missing_ids]
    identities = tuple(
        ReleaseIdentity(repository_id=repository_id, remote_id=identity)
        for identity in sorted(visible, key=int)
    )
    return (
        tuple(entities),
        tuple(changes),
        identities,
        {
            "complete": True,
            "visible_ids": len(visible),
            "initial_content_limit": 10,
            "saved_content_checked": len(selected),
            "first_observed": len(first_seen),
            "first_observed_published_in_last_7_days": published_this_week,
            "publication_window_start": week_start.isoformat(timespec="microseconds"),
            "publication_window_end": observed_at.isoformat(timespec="microseconds"),
            "missing_saved_ids": missing,
            "diagnostics": ["Saved Release is no longer visible; history retained"]
            if missing
            else [],
            "scope": "published IDs including prereleases; initial window and subsequent new IDs",
        },
    )


async def refresh_project(
    data: RefreshInput,
    credential: Credential,
    execution: ExecutionIdentity,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    trust_env: bool = True,
) -> bool:
    """Collect and atomically apply; False means this exact input was already complete.

    The caller initializes/registers the source. A rebaseline input refers to an
    already-created generation; retries never create or reset a generation.
    """
    if data.source.kind != "project":
        raise StateError("Project collector requires a project source")
    store = BusinessStore(data.state_directory)
    async with InstanceLocks(store.directory).source(data.source.source_id):
        if await store.completed_input(data):
            return False
        state = await store.source_state(data.source.source_id)
        if (
            state.generation != data.source_generation
            or state.source.baseline_key() != data.source.baseline_key()
        ):
            raise GenerationError("Stale source generation or mismatched baseline")
        refresh_id = str(data.refresh_id)
        await store.record_attempt(refresh_id, execution.execution_id, execution.attempt_number)
        budget = AttemptBudget(data.source.budgets)
        try:
            async with GitHubHTTP(
                store, credential, budget, transport=transport, trust_env=trust_env
            ) as http:
                batch = await collect_project(http, store, data, state.checkpoint, execution)
                return await store.apply(batch)
        except GitHubError as error:
            await store.record_attempt(
                refresh_id, execution.execution_id, execution.attempt_number, str(error)
            )
            raise


async def collect_project(
    http: GitHubHTTP,
    store: BusinessStore,
    data: RefreshInput,
    checkpoint: dict[str, JsonValue] | None,
    execution: ExecutionIdentity,
) -> BatchWrite:
    """Build a complete batch under the caller's source lock; no business writes."""
    observed_at = datetime.now(UTC)
    repository = parse(Repository, (await http.get(f"/repos/{data.source.repository}")).body)
    if checkpoint is not None and checkpoint.get("repository_id") != repository.id:
        raise RebaselineRequired("Remote repository identity changed; rebaseline required")
    root = f"/repos/{repository.full_name}"
    target_record = parse(
        CommitRecord,
        (await http.get(root + "/commits/" + quote(repository.default_branch, safe=""))).body,
    )
    target = target_record.sha
    before: str | None = None
    if checkpoint is not None:
        saved_sha = checkpoint.get("sha")
        if not isinstance(saved_sha, str):
            raise StateError("Project checkpoint is missing its successful SHA")
        before = saved_sha
    commits, commit_changes, commit_coverage = await collect_commits(
        http, repository.full_name, repository.id, target, before
    )
    releases, release_changes, identities, release_coverage = await collect_releases(
        http,
        store,
        repository.full_name,
        repository.id,
        initial=checkpoint is None,
        observed_at=observed_at,
    )
    snapshots: tuple[SkillSnapshot, ...] = ()
    skill_changes: tuple[SkillChange, ...] = ()
    skill_coverage: dict[str, JsonValue] = {"enabled": False}
    if data.source.scan_skills:
        previous = None if before is None else await store.skill_snapshot(repository.id, before)
        if before is not None and previous is None:
            raise StateError("Previous complete skill snapshot is missing")
        snapshot, skill_changes, skill_coverage = await collect_skills(
            http,
            repository.full_name,
            repository.id,
            target,
            target_record.commit.tree.sha,
            previous,
        )
        snapshots = (snapshot,)
        skill_coverage["enabled"] = True
    # Confirm both the configured alias and the canonical path still name the same repository.
    for path in dict.fromkeys((data.source.repository, repository.full_name)):
        confirmed = parse(Repository, (await http.get(f"/repos/{path}")).body)
        if confirmed.id != repository.id:
            raise RebaselineRequired(
                "Repository identity changed during collection; rebaseline required"
            )
    return BatchWrite(
        input=data,
        checkpoint={
            "repository_id": repository.id,
            "repository": repository.full_name,
            "default_branch": repository.default_branch,
            "sha": target,
        },
        coverage={
            "complete": True,
            "observed_at": observed_at.isoformat(timespec="microseconds"),
            "before_checkpoint": checkpoint,
            "execution": execution.model_dump(mode="json"),
            "requests": http.budget.requests,
            "target_sha": target,
            "commits": commit_coverage,
            "releases": release_coverage,
            "skills": skill_coverage,
        },
        complete=True,
        entities=commits + releases,
        changes=commit_changes + release_changes,
        release_ids=identities,
        snapshots=snapshots,
        skill_changes=skill_changes,
    )
