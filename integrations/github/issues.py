"""Finite Issue baselines and complete, bounded-time increments."""

from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
from pydantic import JsonValue

from integrations.github.auth import Credential
from integrations.github.config import IssueFilter, RefreshInput
from integrations.github.errors import (
    CollectionError,
    GenerationError,
    GitHubError,
    RebaselineRequired,
    StateError,
)
from integrations.github.http import AttemptBudget, GitHubHTTP, next_link
from integrations.github.locking import InstanceLocks
from integrations.github.models import BatchWrite, Change, Entity
from integrations.github.project import ExecutionIdentity
from integrations.github.remote import IssueRecord, Repository, parse, utc
from integrations.github.storage import BusinessStore


def matches(content: dict[str, JsonValue], filters: IssueFilter) -> bool:
    labels = content["labels"]
    assert isinstance(labels, list)
    names = {str(name).casefold() for name in labels}
    title = str(content["title"]).casefold()
    body = str(content["body"] or "").casefold()
    return (
        all(label.casefold() in names for label in filters.labels)
        and (
            not filters.keywords
            or any(word.casefold() in title or word.casefold() in body for word in filters.keywords)
        )
        and (not filters.numbers or content["number"] in filters.numbers)
    )


def checkpoint_upper(checkpoint: dict[str, JsonValue]) -> datetime:
    value = checkpoint.get("upper_bound")
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(utc(value))
        except ValueError:
            pass
    raise StateError("Issue checkpoint is missing its successful UTC upper bound")


async def refresh_issues(
    data: RefreshInput,
    credential: Credential,
    execution: ExecutionIdentity,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    trust_env: bool = True,
) -> bool:
    """Caller initializes/registers; rebaseline retries use the existing generation."""
    if data.source.kind != "issues":
        raise StateError("Issue collector requires an issues source")
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
        try:
            async with GitHubHTTP(
                store,
                credential,
                AttemptBudget(data.source.budgets),
                transport=transport,
                trust_env=trust_env,
            ) as http:
                batch = await collect_issues(http, store, data, state.checkpoint, execution)
                return await store.apply(batch)
        except GitHubError as error:
            await store.record_attempt(
                refresh_id, execution.execution_id, execution.attempt_number, str(error)
            )
            raise


async def collect_issues(
    http: GitHubHTTP,
    store: BusinessStore,
    data: RefreshInput,
    checkpoint: dict[str, JsonValue] | None,
    execution: ExecutionIdentity,
) -> BatchWrite:
    """Build under the caller's source lock; an initial window is never full history."""
    upper = datetime.now(UTC)
    before = None if checkpoint is None else checkpoint_upper(checkpoint)
    if before is not None and upper < before:
        raise StateError("Issue observation clock moved behind the successful checkpoint")
    repository = parse(Repository, (await http.get(f"/repos/{data.source.repository}")).body)
    if checkpoint is not None and checkpoint.get("repository_id") != repository.id:
        raise RebaselineRequired("Remote repository identity changed; rebaseline required")
    saved = {entity.remote_id: entity for entity in await store.entities(repository.id, "issue")}
    tracked = set(
        await store.tracked_issue_ids(repository.id, data.source.source_id, data.source_generation)
    )
    filters = data.source.issue_filter or IssueFilter()
    params = {"state": "all", "sort": "updated", "direction": "desc"}
    since = (
        None if before is None else (before - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    if since is not None:
        params["since"] = since
    path = f"/repos/{repository.full_name}/issues?" + urlencode(params)
    selected: list[Entity] = []
    changes: list[Change] = []
    seen: set[int] = set()
    numbers: set[int] = set()
    pages = scanned = prs = deferred = stale = matching = 0
    list_exhausted = False
    previous_updated: str | None = None
    # Explicitly close a finite baseline iterator; only increments must exhaust it.
    iterator = http.iter_pages(path)
    try:
        async for page in iterator:
            pages += 1
            assert isinstance(page.body, list)
            consumed = 0
            for value in page.body:
                consumed += 1
                scanned += 1
                if not isinstance(value, dict):
                    raise CollectionError("Invalid Issue list item")
                if "pull_request" in value:
                    prs += 1
                    continue
                record = parse(IssueRecord, value)
                if record.id in seen or record.number in numbers:
                    raise CollectionError("Duplicate Issue identity; coverage is uncertain")
                seen.add(record.id)
                numbers.add(record.number)
                if previous_updated is not None and record.updated_at > previous_updated:
                    raise CollectionError("Issue list is not ordered by descending update time")
                previous_updated = record.updated_at
                if datetime.fromisoformat(record.updated_at) > upper:
                    deferred += 1
                    continue
                content = record.content()
                in_scope = matches(content, filters)
                identity = str(record.id)
                if not in_scope and (before is None or identity not in tracked):
                    continue
                matching += 1
                entity = Entity(
                    repository_id=repository.id,
                    kind="issue",
                    remote_id=identity,
                    content=content,
                    updated_at=record.updated_at,
                )
                old = saved.get(identity)
                if (
                    old is not None
                    and old.updated_at is not None
                    and old.updated_at > record.updated_at
                ):
                    stale += 1
                else:
                    selected.append(entity)
                    if before is None:
                        kind = "initial"
                    elif identity not in tracked:
                        kind = (
                            "added"
                            if datetime.fromisoformat(record.created_at) > before
                            else "first_observed_history"
                        )
                    elif old is not None and old.fingerprint != entity.fingerprint:
                        if old.content["state"] == "open" and record.state == "closed":
                            kind = "closed"
                        elif old.content["state"] == "closed" and record.state == "open":
                            kind = "reopened"
                        else:
                            kind = "updated"
                    else:
                        kind = None
                    if kind is not None:
                        changes.append(
                            Change(entity=entity, kind=kind, detail={"matches_filter": in_scope})
                        )
                    if (
                        before is not None
                        and identity in tracked
                        and old is not None
                        and matches(old.content, filters)
                        and not in_scope
                    ):
                        changes.append(
                            Change(
                                entity=entity,
                                kind="exited_filter",
                                detail={"matches_filter": False},
                            )
                        )
                if before is None and matching == 100:
                    break
            if before is None and matching == 100:
                list_exhausted = consumed == len(page.body) and next_link(page.link) is None
                break
        else:
            list_exhausted = True
    finally:
        await iterator.aclose()
    for alias in dict.fromkeys((data.source.repository, repository.full_name)):
        confirmed = parse(Repository, (await http.get(f"/repos/{alias}")).body)
        if confirmed.id != repository.id:
            raise RebaselineRequired(
                "Repository identity changed during collection; rebaseline required"
            )
    return BatchWrite(
        input=data,
        checkpoint={
            "repository_id": repository.id,
            "repository": repository.full_name,
            "upper_bound": upper.isoformat(timespec="microseconds"),
        },
        coverage={
            "complete": True,
            "observed_at": upper.isoformat(timespec="microseconds"),
            "before_checkpoint": checkpoint,
            "execution": execution.model_dump(mode="json"),
            "requests": http.budget.requests,
            "issues": {
                "complete": True,
                "mode": "finite_initial_window" if before is None else "complete_increment",
                "full_history": False,
                "list_exhausted": list_exhausted,
                "baseline_limit": 100,
                "upper_bound": upper.isoformat(timespec="microseconds"),
                "since": since,
                "overlap_seconds": 60,
                "pages": pages,
                "scanned": scanned,
                "pr_excluded": prs,
                "deferred_after_upper": deferred,
                "stale_ignored": stale,
                "selected": len(selected),
                "changes": len(changes),
                "filter": filters.model_dump(mode="json"),
                "scope": (
                    "observed Issue snapshots; no intermediate events or complete comment history"
                ),
            },
        },
        complete=True,
        entities=tuple(selected),
        changes=tuple(changes),
    )
