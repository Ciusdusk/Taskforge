"""Controlled Issue HTTP and real SQLite observations, failures and lifecycle."""

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from integrations.github import issues
from integrations.github.auth import Credential
from integrations.github.config import Budgets, IssueFilter, RefreshInput, Source
from integrations.github.errors import (
    BudgetError,
    CollectionError,
    GenerationError,
    HTTPError,
    RebaselineRequired,
    StateError,
)
from integrations.github.issues import refresh_issues
from integrations.github.locking import InstanceLocks
from integrations.github.project import ExecutionIdentity
from integrations.github.storage import BusinessStore

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]
TOKEN = "fake-issue-credential-123456"
BASE = datetime(2026, 10, 14, 11, tzinfo=UTC)


def stamp(value):
    return value.isoformat(timespec="microseconds")


def issue(number=1, **changes):
    return {
        "id": number + 1000,
        "number": number,
        "title": f"Bug {number}",
        "body": "Original body",
        "state": "open",
        "labels": [{"name": "bug", "id": 9}, {"name": "triage"}],
        "comments": 0,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": stamp(BASE - timedelta(seconds=30)),
        "closed_at": None,
        "html_url": f"https://github.com/a/b/issues/{number}",
        **changes,
    }


@pytest.fixture
def clock(monkeypatch):
    class Clock(datetime):
        value = BASE

        @classmethod
        def now(cls, tz=None):
            return cls.value

    monkeypatch.setattr(issues, "datetime", Clock)
    return Clock


class API:
    def __init__(self, records=()):
        self.records = list(records)
        self.pages = None
        self.calls = []
        self.repository = {"id": 42, "full_name": "a/b", "default_branch": "main"}
        self.redirect = False
        self.metadata_calls = 0
        self.final_id = None
        self.fail_page = None
        self.fail_confirmation = False
        self.cached_first = False
        self.bad_link = False

    def handler(self, request):
        self.calls.append(request)
        assert request.headers["authorization"] == "Bearer " + TOKEN
        assert request.headers["x-github-api-version"] == "2026-03-10"
        if request.url.path in {"/repos/a/b", "/repos/new/name"}:
            if self.redirect and request.url.path == "/repos/a/b":
                return httpx.Response(301, headers={"location": "/repos/new/name"})
            self.metadata_calls += 1
            if self.fail_confirmation and self.metadata_calls > 1:
                return httpx.Response(500)
            record = dict(self.repository)
            if self.final_id and self.metadata_calls > 1:
                record["id"] = self.final_id
            return httpx.Response(200, json=record)
        assert request.url.path == "/repos/" + self.repository["full_name"] + "/issues"
        params = request.url.params
        assert params["state"] == "all"
        assert params["sort"] == "updated"
        assert params["direction"] == "desc"
        assert params["per_page"] == "100"
        assert set(params) <= {"state", "sort", "direction", "per_page", "page", "since"}
        page = int(params["page"])
        if page == self.fail_page:
            return httpx.Response(500)
        if self.cached_first and page == 1 and "if-none-match" in request.headers:
            return httpx.Response(304)
        pages = self.pages
        if pages is None:
            records = sorted(
                self.records, key=lambda item: item.get("updated_at", ""), reverse=True
            )
            pages = [records[i : i + 100] for i in range(0, len(records), 100)] or [[]]
        headers = {"etag": f'"page-{page}"'}
        if page < len(pages):
            following = dict(params)
            following["page"] = str(page + 1)
            if self.bad_link:
                following["state"] = "open"
            headers["link"] = f'<{request.url.copy_with(params=following)}>; rel="next"'
        return httpx.Response(200, json=pages[page - 1], headers=headers)

    def reset(self):
        self.calls.clear()
        self.metadata_calls = 0

    def lists(self):
        return [r for r in self.calls if r.url.path.endswith("/issues")]


async def ready(tmp_path, *, filters=None, **budgets):
    source = Source(
        source_id="issues",
        kind="issues",
        repository="a/b",
        issue_filter=filters,
        budgets=Budgets(**budgets),
    )
    store = BusinessStore(tmp_path)
    await store.initialize()
    await store.register_source(source)
    return store, source


def data_for(store, source, *, generation=1, refresh_id=None, operation="refresh"):
    return RefreshInput(
        refresh_id=refresh_id or uuid4(),
        state_directory=store.directory,
        source=source,
        source_generation=generation,
        operation=operation,
    )


async def refresh(store, source, api, *, data=None, number=1, handler=None):
    return await refresh_issues(
        data or data_for(store, source),
        Credential(TOKEN),
        ExecutionIdentity(execution_id="direct-issues-test", attempt_number=number),
        transport=httpx.MockTransport(handler or api.handler),
        trust_env=False,
    )


def rows(store, table):
    with sqlite3.connect(store.path) as connection:
        return connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()


def changes(store, data=None):
    return [
        (r[3], r[5]) for r in rows(store, "changes") if data is None or r[0] == str(data.refresh_id)
    ]


def coverage(store):
    return json.loads(rows(store, "batches")[-1][5])


def business(store):
    return {
        name: rows(store, name)
        for name in ("generations", "batches", "entities", "versions", "changes")
    }


@pytest.mark.parametrize("count", [0, 3, 99, 100, 101, 250])
async def test_finite_initial_window_pr_exclusion_and_checkpoint(tmp_path, clock, count):
    store, source = await ready(tmp_path)
    api = API(
        [{"pull_request": None, "updated_at": stamp(BASE)}]
        + [issue(n) for n in range(1, count + 1)]
    )
    assert await refresh(store, source, api)
    entities = await store.entities(42, "issue")
    assert len(entities) == min(count, 100)
    assert {kind for _, kind in changes(store)} <= {"initial"}
    state = await store.source_state(source.source_id)
    assert state.checkpoint == {
        "repository_id": 42,
        "repository": "a/b",
        "upper_bound": stamp(BASE),
    }
    result = coverage(store)
    assert result["requests"] == len(api.calls)
    assert result["execution"]["attempt_number"] == 1
    scope = result["issues"]
    assert scope["mode"] == "finite_initial_window"
    assert scope["full_history"] is False
    assert scope["list_exhausted"] is (count <= 100)
    assert scope["selected"] == min(count, 100)
    assert scope["pr_excluded"] == 1
    assert all("since" not in r.url.params for r in api.lists())


async def test_exact_100_at_end_is_finite_but_exhausted(tmp_path, clock):
    store, source = await ready(tmp_path)
    api = API([issue(n) for n in range(1, 101)])
    await refresh(store, source, api)
    assert coverage(store)["issues"]["list_exhausted"] is True
    assert len(api.lists()) == 1


async def test_filtered_baseline_crosses_pages_and_stops_at_100_matches(tmp_path, clock):
    store, source = await ready(tmp_path, filters=IssueFilter(labels=("triage",)))
    api = API([issue(n, labels=[] if n <= 100 else [{"name": "triage"}]) for n in range(1, 251)])
    await refresh(store, source, api)
    assert len(await store.entities(42, "issue")) == 100
    assert len(api.lists()) == 2
    assert coverage(store)["issues"]["scanned"] == 200
    assert coverage(store)["issues"]["list_exhausted"] is False


@pytest.mark.parametrize(
    "filters,record,expected",
    [
        (IssueFilter(), issue(), True),
        (IssueFilter(labels=("bug", "triage")), issue(), True),
        (IssueFilter(labels=("bug", "missing")), issue(), False),
        (IssueFilter(labels=("BUG",)), issue(), True),
        (IssueFilter(keywords=("absent", "BUG")), issue(), True),
        (IssueFilter(keywords=("body",)), issue(title="Other"), True),
        (IssueFilter(keywords=("absent",)), issue(body=None), False),
        (IssueFilter(numbers=(1, 7)), issue(), True),
        (IssueFilter(numbers=(7,)), issue(), False),
        (IssueFilter(labels=("bug",), keywords=("body",), numbers=(1,)), issue(), True),
        (IssueFilter(labels=("bug",), keywords=("body",), numbers=(2,)), issue(), False),
        (IssueFilter(labels=("missing",), keywords=("body",), numbers=(1,)), issue(), False),
        (IssueFilter(labels=("bug",), keywords=("absent",), numbers=(1,)), issue(), False),
    ],
)
async def test_filter_dimensions_and_local_and(tmp_path, clock, filters, record, expected):
    store, source = await ready(tmp_path, filters=filters)
    api = API([record])
    await refresh(store, source, api)
    assert bool(await store.entities(42, "issue")) is expected


async def test_new_history_close_reopen_and_other_updates(tmp_path, clock):
    store, source = await ready(tmp_path)
    api = API([issue()])
    await refresh(store, source, api)
    clock.value += timedelta(hours=1)
    api.records = [
        issue(
            state="closed",
            closed_at=stamp(BASE + timedelta(minutes=10)),
            updated_at=stamp(BASE + timedelta(minutes=10)),
        ),
        issue(
            2,
            created_at=stamp(BASE + timedelta(minutes=5)),
            updated_at=stamp(BASE + timedelta(minutes=5)),
        ),
        issue(3),
        {"pull_request": {}},
    ]
    data = data_for(store, source)
    await refresh(store, source, api, data=data)
    assert set(changes(store, data)) == {
        ("1001", "closed"),
        ("1002", "added"),
        ("1003", "first_observed_history"),
    }
    assert coverage(store)["before_checkpoint"]["upper_bound"] == stamp(BASE)
    assert coverage(store)["issues"]["since"] == "2026-10-14T10:59:00Z"
    assert (await store.source_state("issues")).checkpoint["upper_bound"] == stamp(clock.value)
    clock.value += timedelta(hours=1)
    api.records = [
        issue(state="open", updated_at=stamp(BASE + timedelta(hours=1, minutes=5))),
        issue(
            2,
            title="Changed",
            updated_at=stamp(BASE + timedelta(hours=1, minutes=6)),
            created_at=stamp(BASE + timedelta(minutes=5)),
        ),
    ]
    data = data_for(store, source)
    await refresh(store, source, api, data=data)
    assert set(changes(store, data)) == {("1001", "reopened"), ("1002", "updated")}
    assert len(await store.entities(42, "issue")) == 3


@pytest.mark.parametrize(
    "filters,exit_change",
    [
        (IssueFilter(labels=("bug",)), {"labels": []}),
        (IssueFilter(keywords=("original",)), {"body": None}),
    ],
)
async def test_tracked_filter_exit_remains_monitored(tmp_path, clock, filters, exit_change):
    store, source = await ready(tmp_path, filters=filters)
    api = API([issue()])
    await refresh(store, source, api)
    clock.value += timedelta(hours=1)
    api.records = [issue(**exit_change, updated_at=stamp(BASE + timedelta(minutes=5)))]
    data = data_for(store, source)
    await refresh(store, source, api, data=data)
    assert set(changes(store, data)) == {("1001", "updated"), ("1001", "exited_filter")}
    clock.value += timedelta(hours=1)
    api.records = [
        issue(**exit_change, state="closed", updated_at=stamp(BASE + timedelta(hours=1, minutes=5)))
    ]
    data = data_for(store, source)
    await refresh(store, source, api, data=data)
    assert changes(store, data) == [("1001", "closed")]
    assert (await store.entities(42, "issue"))[0].content["state"] == "closed"
    assert await store.tracked_issue_ids(42, "issues", 1) == ("1001",)


async def test_equal_timestamp_later_observation_and_stale_no_false_changes(tmp_path, clock):
    store, source = await ready(tmp_path)
    api = API([issue()])
    await refresh(store, source, api)
    api.records = [issue(title="Edit without timestamp change")]
    data = data_for(store, source)
    await refresh(store, source, api, data=data)
    assert changes(store, data) == [("1001", "updated")]
    assert len(rows(store, "versions")) == 2
    api.records = [issue(state="closed", updated_at=stamp(BASE - timedelta(hours=1)))]
    data = data_for(store, source)
    await refresh(store, source, api, data=data)
    assert changes(store, data) == []
    assert len(rows(store, "versions")) == 2
    assert (await store.entities(42, "issue"))[0].content[
        "title"
    ] == "Edit without timestamp change"
    assert coverage(store)["issues"]["stale_ignored"] == 1


async def test_upper_bound_deferred_then_applied_and_recorded_before_request(tmp_path, clock):
    store, source = await ready(tmp_path)
    api = API([issue()])
    await refresh(store, source, api)
    clock.value = BASE + timedelta(hours=1)
    upper = clock.value
    api.records = [
        issue(title="Future update", updated_at=stamp(upper + timedelta(minutes=1))),
        issue(2, created_at=stamp(upper), updated_at=stamp(upper)),
    ]

    def handler(request):
        clock.value = upper + timedelta(minutes=2)
        return api.handler(request)

    data = data_for(store, source)
    await refresh(store, source, api, data=data, handler=handler)
    assert changes(store, data) == [("1002", "added")]
    assert (await store.source_state("issues")).checkpoint["upper_bound"] == stamp(upper)
    assert coverage(store)["issues"]["deferred_after_upper"] == 1
    data = data_for(store, source)
    await refresh(store, source, api, data=data)
    assert changes(store, data) == [("1001", "updated")]
    assert len(rows(store, "versions")) == 3


@pytest.mark.parametrize(
    "field,value",
    [
        ("number", 9),
        ("title", "Edit"),
        ("body", None),
        ("state", "closed"),
        ("labels", [{"name": "new"}]),
        ("comments", 3),
        ("created_at", "2026-01-02T00:00:00Z"),
        ("updated_at", stamp(BASE)),
        ("closed_at", "2026-01-03T00:00:00Z"),
        ("html_url", "https://github.com/new/name/issues/1"),
    ],
)
async def test_full_content_fingerprint_fields(tmp_path, clock, field, value):
    store, source = await ready(tmp_path)
    api = API([issue()])
    await refresh(store, source, api)
    old = (await store.entities(42, "issue"))[0]
    api.records = [{**issue(), field: value}]
    await refresh(store, source, api)
    new = (await store.entities(42, "issue"))[0]
    assert old.fingerprint != new.fingerprint
    assert new.content[field] == value or field in {"labels", "created_at", "closed_at"}
    assert len(rows(store, "versions")) == 2


async def test_normalized_times_labels_extensions_and_nullable_body(tmp_path, clock):
    store, source = await ready(tmp_path)
    api = API([issue(body=None)])
    await refresh(store, source, api)
    old = (await store.entities(42, "issue"))[0]
    assert old.content["body"] is None and old.content["closed_at"] is None
    assert old.content["labels"] == ["bug", "triage"]
    api.records = [
        issue(
            body=None,
            labels=[{"name": "triage", "color": "ffffff"}, {"name": "bug"}, {"name": "bug"}],
            updated_at="2026-10-14T12:59:30+02:00",
            created_at="2026-01-01T01:00:00+01:00",
            ignored={"arbitrary": "extension"},
        )
    ]
    data = data_for(store, source)
    await refresh(store, source, api, data=data)
    assert (await store.entities(42, "issue"))[0].fingerprint == old.fingerprint
    assert changes(store, data) == []
    assert len(rows(store, "versions")) == 1


async def test_complete_increment_multiple_pages_and_304_later_update(tmp_path, clock):
    store, source = await ready(tmp_path)
    api = API()
    await refresh(store, source, api)
    api.records = [issue(n) for n in range(1, 206)]
    data = data_for(store, source)
    await refresh(store, source, api, data=data)
    assert len(await store.entities(42, "issue")) == 205
    assert len(changes(store, data)) == 205
    assert coverage(store)["issues"]["list_exhausted"] is True
    assert coverage(store)["issues"]["pages"] == 3
    api.reset()
    api.cached_first = True
    api.records[-1] = issue(205, title="Later page edit")
    data = data_for(store, source)
    await refresh(store, source, api, data=data)
    assert len(api.lists()) == 3
    assert api.lists()[0].headers["if-none-match"] == '"page-1"'
    assert changes(store, data) == [("1205", "updated")]


@pytest.mark.parametrize(
    "failure", ["http", "pages", "requests", "confirmation", "duplicate", "order", "link"]
)
async def test_incomplete_increment_retains_all_business_state(tmp_path, clock, failure):
    store, source = await ready(tmp_path)
    api = API([issue()])
    await refresh(store, source, api)
    before = business(store)
    clock.value += timedelta(hours=1)
    api.reset()
    api.records = [issue(n, updated_at=stamp(BASE + timedelta(minutes=5))) for n in range(1, 206)]
    error = (HTTPError, BudgetError, CollectionError)
    if failure == "http":
        api.fail_page = 2
    elif failure == "pages":
        source = source.model_copy(update={"budgets": Budgets(max_pages=1)})
    elif failure == "requests":
        source = source.model_copy(update={"budgets": Budgets(max_requests=2)})
    elif failure == "confirmation":
        api.fail_confirmation = True
    elif failure == "duplicate":
        api.records.append(api.records[0])
    elif failure == "order":
        api.pages = [[issue(1)], [issue(2, updated_at=stamp(BASE))]]
    elif failure == "link":
        api.bad_link = True
        from integrations.github.errors import PaginationError

        error = (PaginationError,)
    data = data_for(store, source)
    with pytest.raises(error):
        await refresh(store, source, api, data=data)
    assert business(store) == before
    assert not await store.completed(str(data.refresh_id))
    assert rows(store, "attempts")[-1][3]


@pytest.mark.parametrize("limit", [1, 2, 3])
async def test_metadata_list_and_confirmation_share_request_budget(tmp_path, clock, limit):
    store, source = await ready(tmp_path, max_requests=limit)
    api = API([issue()])
    data = data_for(store, source)
    if limit < 3:
        with pytest.raises(BudgetError):
            await refresh(store, source, api, data=data)
        assert rows(store, "batches") == []
        assert rows(store, "entities") == []
        assert (await store.source_state("issues")).checkpoint is None
    else:
        assert await refresh(store, source, api, data=data)
    assert len(api.calls) == limit


async def test_redirect_canonical_identity_and_replacement(tmp_path, clock):
    store, source = await ready(tmp_path)
    api = API([issue()])
    api.repository["full_name"] = "new/name"
    api.redirect = True
    await refresh(store, source, api)
    assert (await store.source_state("issues")).checkpoint["repository"] == "new/name"
    assert coverage(store)["requests"] == 6
    old = business(store)
    api.repository["id"] = 43
    with pytest.raises(RebaselineRequired):
        await refresh(store, source, api)
    assert business(store) == old


async def test_identity_changes_during_collection_rejected(tmp_path, clock):
    store, source = await ready(tmp_path)
    api = API([issue()])
    api.final_id = 43
    with pytest.raises(RebaselineRequired):
        await refresh(store, source, api)
    assert rows(store, "entities") == rows(store, "batches") == []


async def test_completed_input_conflict_old_generation_and_history(tmp_path, clock):
    store, source = await ready(tmp_path)
    api = API([issue()])
    data = data_for(store, source)
    await refresh(store, source, api, data=data)
    old = business(store)
    api.reset()
    assert not await refresh(store, source, api, data=data, number=2)
    assert api.calls == [] and business(store) == old
    with pytest.raises(StateError):
        await refresh(store, source, api, data=data.model_copy(update={"operation": "rebaseline"}))
    changed = source.model_copy(update={"issue_filter": IssueFilter(labels=("other",))})
    state = await store.register_source(changed, rebaseline=True)
    assert state.generation == 2
    assert not await refresh(store, source, api, data=data)
    with pytest.raises(GenerationError):
        await refresh(store, source, api)
    assert api.calls == []
    new = data_for(store, changed, generation=2, operation="rebaseline")
    await refresh(store, changed, api, data=new)
    assert await store.tracked_issue_ids(42, "issues", 2) == ()
    assert len(await store.entities(42, "issue")) == 1
    clock.value += timedelta(hours=1)
    api.records = [
        issue(title="Outside new filter", updated_at=stamp(BASE + timedelta(minutes=10)))
    ]
    new = data_for(store, changed, generation=2)
    await refresh(store, changed, api, data=new)
    assert changes(store, new) == []
    assert (await store.entities(42, "issue"))[0].content["title"] == "Bug 1"
    assert (await store.source_state("issues", 1)).checkpoint["upper_bound"] == stamp(BASE)


async def test_failed_old_job_retry_reads_latest_successful_checkpoint(tmp_path, clock):
    store, source = await ready(tmp_path)
    api = API([issue()])
    await refresh(store, source, api)
    failed = data_for(store, source)
    api.fail_page = 1
    with pytest.raises(HTTPError):
        await refresh(store, source, api, data=failed)
    api.fail_page = None
    clock.value += timedelta(hours=1)
    api.records = [issue(title="New", updated_at=stamp(BASE + timedelta(minutes=10)))]
    await refresh(store, source, api)
    latest = (await store.source_state("issues")).checkpoint
    clock.value += timedelta(hours=1)
    api.reset()
    await refresh(store, source, api, data=failed, number=2)
    assert coverage(store)["before_checkpoint"] == latest
    assert api.lists()[0].url.params["since"] == "2026-10-14T11:59:00Z"
    assert changes(store, failed) == []
    assert [r[2] for r in rows(store, "attempts") if r[0] == str(failed.refresh_id)] == [1, 2]


async def test_failed_initial_retry_does_not_recreate_generation(tmp_path, clock):
    store, source = await ready(tmp_path)
    api = API([issue()])
    data = data_for(store, source)
    api.fail_page = 1
    with pytest.raises(HTTPError):
        await refresh(store, source, api, data=data)
    api.fail_page = None
    await refresh(store, source, api, data=data, number=2)
    assert (await store.source_state("issues")).generation == 1
    assert len(rows(store, "batches")) == len(rows(store, "versions")) == 1
    assert changes(store, data) == [("1001", "initial")]


async def test_sql_fault_rolls_back_entities_versions_changes_checkpoint(tmp_path, clock):
    store, source = await ready(tmp_path)
    api = API([issue()])
    await refresh(store, source, api)
    before = business(store)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "CREATE TRIGGER fail_checkpoint BEFORE UPDATE OF checkpoint ON generations "
            "BEGIN SELECT RAISE(FAIL, 'controlled'); END"
        )
    api.records = [issue(title="Changed"), issue(2)]
    data = data_for(store, source)
    with pytest.raises(sqlite3.IntegrityError):
        await refresh(store, source, api, data=data)
    assert business(store) == before
    assert not await store.completed(str(data.refresh_id))
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TRIGGER fail_checkpoint")
    await refresh(store, source, api, data=data, number=2)
    assert len(rows(store, "versions")) == 3


@pytest.mark.parametrize(
    "field,bad",
    [
        ("id", True),
        ("number", "1"),
        ("title", None),
        ("body", 4),
        ("state", "unknown"),
        ("labels", [{"name": 3}]),
        ("comments", -1),
        ("created_at", "invalid"),
        ("updated_at", "2026-10-14T00:00:00"),
        ("closed_at", False),
        ("html_url", ""),
    ],
)
async def test_invalid_remote_fields_fail_without_payload_chain(tmp_path, clock, field, bad):
    store, source = await ready(tmp_path)
    api = API([{**issue(), field: bad}])
    with pytest.raises(CollectionError) as caught:
        await refresh(store, source, api)
    assert caught.value.__context__ is None
    assert rows(store, "batches") == rows(store, "entities") == []
    assert TOKEN not in store.path.read_bytes().decode(errors="replace")


@pytest.mark.parametrize(
    "field",
    [
        "id",
        "number",
        "title",
        "body",
        "state",
        "labels",
        "comments",
        "created_at",
        "updated_at",
        "closed_at",
        "html_url",
    ],
)
async def test_missing_required_fields_cannot_establish_baseline(tmp_path, clock, field):
    store, source = await ready(tmp_path)
    record = issue()
    del record[field]
    with pytest.raises(CollectionError):
        await refresh(store, source, API([record]))
    assert (await store.source_state("issues")).checkpoint is None


async def test_same_source_serializes_checkpoint_read(tmp_path, clock):
    store, source = await ready(tmp_path)
    api = API([issue()])
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(request):
        if request.url.path.endswith("/issues") and not entered.is_set():
            entered.set()
            await release.wait()
        return api.handler(request)

    first = asyncio.create_task(refresh(store, source, api, handler=handler))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        second = asyncio.create_task(refresh(store, source, api))
        await asyncio.sleep(0)
        assert not second.done()
        assert len(api.calls) == 1
        release.set()
        await first
        await second
    finally:
        release.set()
        await asyncio.gather(*[task for task in (first, second) if task], return_exceptions=True)
    batches = rows(store, "batches")
    assert json.loads(batches[1][5])["before_checkpoint"] == json.loads(batches[0][4])
    assert len(rows(store, "versions")) == 1
    assert changes(store) == [("1001", "initial")]


async def test_http_cancel_retains_source_and_api_locks_until_cleanup(tmp_path, clock):
    store, source = await ready(tmp_path)
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    api = API([issue()])

    async def handler(request):
        if request.url.path.endswith("/issues"):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
        return api.handler(request)

    data = data_for(store, source)
    task = asyncio.create_task(refresh(store, source, api, data=data, handler=handler))
    locks = InstanceLocks(store.directory)
    source_entered, gate_entered = asyncio.Event(), asyncio.Event()

    async def contender(path, event):
        async with locks.hold(path):
            event.set()

    followers = []
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        await asyncio.wait_for(cleaning.wait(), 5)
        task.cancel()
        followers = [
            asyncio.create_task(contender(locks.source_path("issues"), source_entered)),
            asyncio.create_task(contender(locks.request_path, gate_entered)),
        ]
        await asyncio.sleep(0)
        assert not task.done()
        assert not source_entered.is_set() and not gate_entered.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.gather(*followers)
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, *followers, return_exceptions=True)
    assert not await store.completed(str(data.refresh_id))
    assert (await store.source_state("issues")).checkpoint is None
    assert locks.source_path("issues").exists()


async def test_database_cancel_waits_for_atomic_commit_before_unlock(tmp_path, clock, monkeypatch):
    store, source = await ready(tmp_path)
    api = API([issue()])
    entered, release, follower_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = BusinessStore._apply_rows

    async def paused(self, connection, batch):
        await original(self, connection, batch)
        entered.set()
        await release.wait()

    monkeypatch.setattr(BusinessStore, "_apply_rows", paused)
    data = data_for(store, source)
    task = asyncio.create_task(refresh(store, source, api, data=data))

    async def follower():
        async with InstanceLocks(store.directory).source("issues"):
            follower_entered.set()

    contender = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert rows(store, "batches") == []
        assert rows(store, "entities") == []
        task.cancel()
        contender = asyncio.create_task(follower())
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not follower_entered.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await contender
    finally:
        release.set()
        await asyncio.gather(*[t for t in (task, contender) if t], return_exceptions=True)
    assert await store.completed_input(data)
    api.reset()
    assert not await refresh(store, source, api, data=data, number=2)
    assert api.calls == []


async def test_generation_changed_mid_network_cannot_commit(tmp_path, clock):
    store, source = await ready(tmp_path)
    api = API([issue()])

    async def handler(request):
        if request.url.path.endswith("/issues"):
            await store.register_source(source, rebaseline=True)
        return api.handler(request)

    with pytest.raises(GenerationError):
        await refresh(store, source, api, handler=handler)
    assert rows(store, "batches") == rows(store, "entities") == []
    assert (await store.source_state("issues")).generation == 2


async def test_kind_mismatch_and_baseline_mismatch_never_request(tmp_path, clock):
    store, source = await ready(tmp_path)
    api = API()
    with pytest.raises(StateError):
        await refresh(store, source.model_copy(update={"kind": "project"}), api)
    with pytest.raises(GenerationError):
        await refresh(
            store, source.model_copy(update={"issue_filter": IssueFilter(numbers=(9,))}), api
        )
    assert api.calls == []


async def test_initial_future_is_deferred_then_historically_discovered(tmp_path, clock):
    store, source = await ready(tmp_path)
    api = API([issue(updated_at=stamp(BASE + timedelta(minutes=1)))])
    await refresh(store, source, api)
    assert rows(store, "entities") == []
    clock.value += timedelta(minutes=2)
    data = data_for(store, source)
    await refresh(store, source, api, data=data)
    assert changes(store, data) == [("1001", "first_observed_history")]


async def test_clock_regression_preserves_checkpoint_without_network(tmp_path, clock):
    store, source = await ready(tmp_path)
    api = API([issue()])
    await refresh(store, source, api)
    before = business(store)
    clock.value -= timedelta(seconds=1)
    api.reset()
    with pytest.raises(StateError):
        await refresh(store, source, api)
    assert business(store) == before and api.calls == []


@pytest.mark.parametrize("failure", [None, "pages", "http"])
async def test_filtered_small_baseline_requires_end_or_retains_checkpoint(tmp_path, clock, failure):
    store, source = await ready(tmp_path, filters=IssueFilter(numbers=(250,)))
    api = API([issue(n) for n in range(1, 251)])
    if failure == "pages":
        source = source.model_copy(update={"budgets": Budgets(max_pages=2)})
    elif failure == "http":
        api.fail_page = 3
    if failure:
        with pytest.raises((BudgetError, HTTPError)):
            await refresh(store, source, api)
        assert rows(store, "entities") == rows(store, "batches") == []
        assert (await store.source_state("issues")).checkpoint is None
    else:
        await refresh(store, source, api)
        assert len(api.lists()) == 3
        assert changes(store) == [("1250", "initial")]
        assert coverage(store)["issues"]["list_exhausted"] is True


async def test_keywords_match_one_field_without_crossing_boundary(tmp_path, clock):
    store, source = await ready(tmp_path, filters=IssueFilter(keywords=("end\nstart",)))
    await refresh(store, source, API([issue(title="end", body="start")]))
    assert rows(store, "entities") == []


async def test_history_enters_filter_and_reentry_does_not_become_added(tmp_path, clock):
    store, source = await ready(tmp_path, filters=IssueFilter(labels=("bug",)))
    api = API([issue(labels=[])])
    await refresh(store, source, api)
    clock.value += timedelta(hours=1)
    api.records = [issue(updated_at=stamp(BASE + timedelta(minutes=1)))]
    data = data_for(store, source)
    await refresh(store, source, api, data=data)
    assert changes(store, data) == [("1001", "first_observed_history")]
    api.records = [issue(labels=[], updated_at=stamp(BASE + timedelta(minutes=2)))]
    await refresh(store, source, api)
    api.records = [issue(updated_at=stamp(BASE + timedelta(minutes=3)))]
    data = data_for(store, source)
    await refresh(store, source, api, data=data)
    assert changes(store, data) == [("1001", "updated")]


async def test_rebaseline_new_repository_retains_old_identity_history(tmp_path, clock):
    store, source = await ready(tmp_path)
    api = API([issue()])
    await refresh(store, source, api)
    await store.register_source(source, rebaseline=True)
    api.repository["id"] = 43
    data = data_for(store, source, generation=2, operation="rebaseline")
    await refresh(store, source, api, data=data)
    assert changes(store, data) == [("1001", "initial")]
    assert len(await store.entities(42, "issue")) == 1
    assert len(await store.entities(43, "issue")) == 1
    assert (await store.source_state("issues", 1)).checkpoint["repository_id"] == 42
    assert (await store.source_state("issues")).checkpoint["repository_id"] == 43
    api.reset()
    assert not await refresh(store, source, api, data=data, number=2)
    assert api.calls == [] and (await store.source_state("issues")).generation == 2


async def test_client_close_repeated_cancel_holds_source_lock(tmp_path, clock, monkeypatch):
    store, source = await ready(tmp_path)
    api = API([issue()])
    closing, release, contender_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = httpx.AsyncClient.aclose

    async def close(client):
        closing.set()
        await release.wait()
        await original(client)

    monkeypatch.setattr(httpx.AsyncClient, "aclose", close)
    data = data_for(store, source)
    task = asyncio.create_task(refresh(store, source, api, data=data))

    async def follower():
        async with InstanceLocks(store.directory).source("issues"):
            contender_entered.set()

    contender = None
    try:
        await asyncio.wait_for(closing.wait(), 5)
        assert await store.completed_input(data)
        task.cancel()
        contender = asyncio.create_task(follower())
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not contender_entered.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await contender
    finally:
        release.set()
        await asyncio.gather(*[t for t in (task, contender) if t], return_exceptions=True)
    api.reset()
    assert not await refresh(store, source, api, data=data, number=2)
    assert api.calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("created_at", stamp(BASE + timedelta(days=1))),
        ("closed_at", stamp(BASE + timedelta(days=1))),
        ("closed_at", "2025-01-01T00:00:00Z"),
    ],
)
async def test_inconsistent_issue_times_cannot_produce_added(tmp_path, clock, field, value):
    store, source = await ready(tmp_path)
    with pytest.raises(CollectionError):
        await refresh(store, source, API([{**issue(), field: value}]))
    assert rows(store, "batches") == []


async def test_baseline_selects_latest_matching_and_all_are_initial(tmp_path, clock):
    store, source = await ready(tmp_path)
    api = API([issue(n, updated_at=stamp(BASE - timedelta(seconds=n))) for n in range(1, 151)])
    api.records[0] = issue(1, created_at=stamp(BASE), updated_at=stamp(BASE))
    await refresh(store, source, api)
    assert {entity.remote_id for entity in await store.entities(42, "issue")} == {
        str(1000 + n) for n in range(1, 101)
    }
    assert {kind for _, kind in changes(store)} == {"initial"}
    assert coverage(store)["issues"]["list_exhausted"] is False


async def test_stale_filter_exit_no_pseudo_event_then_simultaneous_close_exit(tmp_path, clock):
    store, source = await ready(tmp_path, filters=IssueFilter(labels=("bug",)))
    api = API([issue()])
    await refresh(store, source, api)
    api.records = [issue(labels=[], state="closed", updated_at=stamp(BASE - timedelta(hours=1)))]
    data = data_for(store, source)
    await refresh(store, source, api, data=data)
    assert changes(store, data) == []
    assert (await store.entities(42, "issue"))[0].content["state"] == "open"
    api.records = [issue(labels=[], state="closed")]
    data = data_for(store, source)
    await refresh(store, source, api, data=data)
    assert set(changes(store, data)) == {("1001", "closed"), ("1001", "exited_filter")}


async def test_creation_at_previous_upper_is_history_not_natural_added(tmp_path, clock):
    store, source = await ready(tmp_path)
    api = API()
    await refresh(store, source, api)
    clock.value += timedelta(minutes=2)
    api.records = [issue(created_at=stamp(BASE), updated_at=stamp(BASE + timedelta(seconds=30)))]
    data = data_for(store, source)
    await refresh(store, source, api, data=data)
    assert changes(store, data) == [("1001", "first_observed_history")]
