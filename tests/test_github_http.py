import asyncio
import json
import logging
import sqlite3
import time
from uuid import uuid4

import httpx
import pytest
from integrations.github.auth import Credential
from integrations.github.config import Budgets, GitHubConfig
from integrations.github.errors import BudgetError, HTTPError, PaginationError
from integrations.github.http import ACCEPT, API_VERSION, AttemptBudget, GitHubHTTP
from integrations.github.storage import BusinessStore

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]
TOKEN = "fake-test-credential-987654"


async def ready(tmp_path):
    store = BusinessStore(tmp_path)
    await store.initialize()
    await store.register_source(GitHubConfig.load().sources[0])
    return store


def client(store, handler, **limits):
    return GitHubHTTP(
        store,
        Credential(TOKEN),
        AttemptBudget(Budgets(**limits)),
        transport=httpx.MockTransport(handler),
        trust_env=False,
    )


def link(path="/repos/a/b/releases", page=2, **params):
    url = httpx.URL("https://api.github.com" + path).copy_with(
        params={"per_page": "100", "page": str(page), **params}
    )
    return f'<{url}>; rel="next"'


async def test_multi_page_304_original_body_link_and_later_page_changes(tmp_path):
    store = await ready(tmp_path)
    calls = []
    round_number = 1

    def handler(request):
        calls.append(str(request.url))
        assert request.headers["authorization"] == "Bearer " + TOKEN
        assert request.headers["accept"] == ACCEPT
        assert request.headers["x-github-api-version"] == API_VERSION
        assert request.url.params["per_page"] == "100"
        page = request.url.params["page"]
        if round_number == 2 and page == "1":
            assert request.headers["if-none-match"] == '"first"'
            return httpx.Response(304, headers={"link": "ignored"})
        return httpx.Response(
            200,
            json=[{"id": int(page), "round": round_number}],
            headers={
                "etag": '"first"' if page == "1" else '"second"',
                "link": link() if page == "1" else "",
            },
        )

    async with client(store, handler) as http:
        assert await http.paginate("/repos/a/b/releases") == [
            {"id": 1, "round": 1},
            {"id": 2, "round": 1},
        ]
        round_number = 2
        assert await http.paginate("/repos/a/b/releases") == [
            {"id": 1, "round": 1},
            {"id": 2, "round": 2},
        ]
        assert http.budget.requests == 4
    assert http.client.is_closed
    assert len(calls) == 4
    assert (await store.source_state(GitHubConfig.load().sources[0].source_id)).checkpoint is None


async def test_complete_request_keys_separate_query_path_and_version(tmp_path):
    store = await ready(tmp_path)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"query": str(request.url)}, headers={"etag": '"etag"'})

    async with client(store, handler) as http:
        for path in [
            "/repos/a/b/issues?state=all",
            "/repos/a/b/issues?state=open",
            "/repos/a/c/issues?state=all",
        ]:
            await http.get(path)
        assert all("if-none-match" not in r.headers for r in calls)
        key = http.request_key("https://api.github.com/repos/a/b/issues?state=all")
        assert json.loads(key) == [
            "GET",
            "https://api.github.com/repos/a/b/issues?state=all",
            ACCEPT,
            API_VERSION,
        ]
        assert TOKEN not in key
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT count(*) FROM http_cache").fetchone()[0] == 3


@pytest.mark.parametrize(
    "headers,status,error",
    [
        ({}, 304, HTTPError),
        ({"link": "broken"}, 200, PaginationError),
        ({"link": '<https://evil.example/page>; rel="next"'}, 200, HTTPError),
        ({"link": link(page=1)}, 200, PaginationError),
        ({"link": link(page=3)}, 200, PaginationError),
        ({"link": link(path="/repos/x/y/releases")}, 200, PaginationError),
        ({"link": link() + ", " + link()}, 200, PaginationError),
    ],
)
async def test_invalid_pagination_and_cache_do_not_complete(tmp_path, headers, status, error):
    store = await ready(tmp_path)
    async with client(store, lambda r: httpx.Response(status, json=[], headers=headers)) as http:
        with pytest.raises(error):
            await http.paginate("/repos/a/b/releases")
        assert http.budget.requests == 1
    assert not await store.completed(str(uuid4()))
    assert (await store.source_state(GitHubConfig.load().sources[0].source_id)).checkpoint is None


@pytest.mark.parametrize("limits", [{"max_pages": 1}, {"max_requests": 1}])
async def test_page_and_actual_request_budget_interrupts(tmp_path, limits):
    store = await ready(tmp_path)
    async with client(
        store, lambda r: httpx.Response(200, json=[], headers={"link": link()}), **limits
    ) as http:
        with pytest.raises(BudgetError):
            await http.paginate("/repos/a/b/releases")
        assert http.budget.requests == 1
    assert (await store.source_state(GitHubConfig.load().sources[0].source_id)).checkpoint is None


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/",
        "http://api.github.com/",
        "https://api.github.com:444/",
        "https://user@api.github.com/",
        "/a?access_token=secret",
        "/a?token=secret",
    ],
)
async def test_unsafe_urls_never_send_credentials(tmp_path, url):
    store = await ready(tmp_path)
    async with client(store, lambda r: pytest.fail("unsafe request reached transport")) as http:
        with pytest.raises(HTTPError):
            await http.get(url)
        assert http.budget.requests == 0


async def test_redirect_origin_and_request_budget(tmp_path):
    store = await ready(tmp_path)
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        return httpx.Response(200, json={"id": 1})

    async with client(store, handler) as http:
        assert (await http.get("/old")).body == {"id": 1}
        assert http.budget.requests == 2
    assert len(calls) == 2
    async with client(
        store, lambda r: httpx.Response(302, headers={"location": "https://evil.test/"})
    ) as http:
        with pytest.raises(HTTPError):
            await http.get("/old")
        assert http.budget.requests == 1
    async with client(store, handler, max_requests=1) as http:
        with pytest.raises(BudgetError):
            await http.get("/old")
        assert http.budget.requests == 1


@pytest.mark.parametrize(
    "status,headers,body,minimum",
    [
        (403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "future"}, {}, 10),
        (429, {"retry-after": "20"}, {}, 20),
        (403, {}, {"message": "secondary rate limit"}, 60),
        (
            403,
            {"retry-after": "30", "x-ratelimit-remaining": "0", "x-ratelimit-reset": "future"},
            {},
            30,
        ),
    ],
)
async def test_rate_limits_persist_and_no_extra_retry(tmp_path, status, headers, body, minimum):
    store = await ready(tmp_path)
    started = time.time()
    headers = {**headers}
    if headers.get("x-ratelimit-reset") == "future":
        headers["x-ratelimit-reset"] = str(started + 10)
    async with client(store, lambda r: httpx.Response(status, headers=headers, json=body)) as http:
        with pytest.raises(HTTPError, match="cooldown persisted"):
            await http.get("/rate")
        assert http.budget.requests == 1
    until, _ = await store.cooldown()
    assert until >= started + minimum
    await store.extend_cooldown(started + 1)
    assert (await store.cooldown())[0] == until
    # A new object and changed token still reads the same deadline before any request.
    async with GitHubHTTP(
        store,
        Credential("different-fake"),
        AttemptBudget(Budgets(max_wait_seconds=0)),
        transport=httpx.MockTransport(lambda r: pytest.fail("cooldown bypassed")),
        trust_env=False,
    ) as http:
        with pytest.raises(BudgetError):
            await http.get("/rate")
        assert http.budget.requests == 0


async def test_secondary_backoff_increases_and_retry_after_date(tmp_path):
    store = await ready(tmp_path)
    async with client(
        store, lambda r: httpx.Response(403, json={"message": "secondary rate limit"})
    ) as http:
        with pytest.raises(HTTPError):
            await http.get("/rate")
    first, count = await store.cooldown()
    assert count == 1
    # Explicitly end the test cooldown to observe another server failure.
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE cooldown SET until_epoch=0")
    async with client(
        store, lambda r: httpx.Response(403, json={"message": "secondary rate limit"})
    ) as http:
        with pytest.raises(HTTPError):
            await http.get("/rate")
    second, count = await store.cooldown()
    assert count == 2 and second >= first + 60
    from email.utils import formatdate

    deadline = time.time() + 200
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE cooldown SET until_epoch=0")
    async with client(
        store,
        lambda r: httpx.Response(429, headers={"retry-after": formatdate(deadline, usegmt=True)}),
    ) as http:
        with pytest.raises(HTTPError):
            await http.get("/rate")
    assert (await store.cooldown())[0] >= int(deadline)


async def test_cumulative_wait_budget_and_requests_after_wait(tmp_path):
    store = await ready(tmp_path)
    async with client(store, lambda r: httpx.Response(200, json=[]), max_wait_seconds=0.08) as http:
        await store.extend_cooldown(time.time() + 0.03)
        await http.get("/a")
        assert http.budget.requests == 1 and http.budget.waited_seconds >= 0.02
        await store.extend_cooldown(time.time() + 0.08)
        with pytest.raises(BudgetError):
            await http.get("/b")
        assert http.budget.requests == 1


@pytest.mark.parametrize("failure", ["phase", "transport", "sensitive-body", "sensitive-etag"])
async def test_non_sensitive_errors_logs_and_persistent_artifacts(tmp_path, caplog, failure):
    store = await ready(tmp_path)

    def handler(request):
        if failure == "phase":
            raise httpx.ReadTimeout(TOKEN, request=request)
        if failure == "transport":
            raise httpx.ConnectError(TOKEN, request=request)
        return httpx.Response(
            200,
            json={"body": TOKEN if failure == "sensitive-body" else "ok"},
            headers={"etag": TOKEN if failure == "sensitive-etag" else "ok"},
        )

    caplog.set_level(logging.DEBUG)
    async with client(store, handler) as http:
        with pytest.raises(HTTPError) as caught:
            await http.get("/safe")
        exc = caught.value
        while exc is not None:
            assert TOKEN not in str(exc) and TOKEN not in repr(exc)
            exc = exc.__cause__ or exc.__context__
    assert TOKEN not in caplog.text
    assert TOKEN.encode() not in store.path.read_bytes()
    assert TOKEN not in repr(Credential(TOKEN))


async def test_total_timeout_waits_for_transport_cleanup_and_closes_client(tmp_path):
    store = await ready(tmp_path)
    started, cleanup_started, cleanup_release, cleaned = [asyncio.Event() for _ in range(4)]

    async def handler(request):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await cleanup_release.wait()
            cleaned.set()

    http = client(store, handler, request_seconds=0.03)
    task = asyncio.create_task(http.get("/slow"))
    try:
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.wait_for(cleanup_started.wait(), 2)
        assert not task.done()
        cleanup_release.set()
        with pytest.raises(HTTPError, match="total request wait"):
            await task
        assert cleaned.is_set()
    finally:
        cleanup_release.set()
        await asyncio.gather(task, return_exceptions=True)
        await http.__aexit__()
    assert http.client.is_closed


async def test_escaped_response_token_is_not_persisted(tmp_path):
    store = await ready(tmp_path)
    escaped = "".join(f"\\u{ord(char):04x}" for char in TOKEN)
    async with client(
        store, lambda r: httpx.Response(200, content='{"body":"' + escaped + '"}')
    ) as http:
        with pytest.raises(HTTPError, match="Sensitive response"):
            await http.get("/safe")
    assert TOKEN.encode() not in store.path.read_bytes()
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT count(*) FROM http_cache").fetchone()[0] == 0


@pytest.mark.parametrize(
    "url",
    [
        "https://api.github.com:bad-port/",
        "/a?token=" + TOKEN,
        "https://" + TOKEN + "@api.github.com/",
    ],
)
async def test_url_failures_have_no_sensitive_exception_chain(tmp_path, url):
    store = await ready(tmp_path)
    async with client(store, lambda r: pytest.fail("invalid URL reached network")) as http:
        with pytest.raises(HTTPError) as caught:
            await http.get(url)
        exc = caught.value
        while exc is not None:
            assert TOKEN not in str(exc)
            exc = exc.__cause__ or exc.__context__
        assert http.budget.requests == 0


async def test_ordinary_forbidden_is_not_a_rate_limit(tmp_path):
    store = await ready(tmp_path)
    async with client(store, lambda r: httpx.Response(403, json={"message": "forbidden"})) as http:
        with pytest.raises(HTTPError, match="HTTP 403"):
            await http.get("/forbidden")
    assert await store.cooldown() == (0, 0)


@pytest.mark.parametrize("cursor_only", [False, True])
async def test_canonical_repository_and_cursor_pagination_with_cached_links(tmp_path, cursor_only):
    store = await ready(tmp_path)
    calls = []
    cached = False

    def handler(request):
        calls.append(str(request.url))
        if request.url.path == "/repos/a/b":
            return httpx.Response(200, json={"id": 42})
        if cached:
            return httpx.Response(304)
        if request.url.path == "/repos/a/b/issues":
            params = {"per_page": "100", "state": "all", "after": "opaque=="}
            if not cursor_only:
                params["page"] = "2"
            following = httpx.URL("https://api.github.com/repositories/42/issues").copy_with(
                params=params
            )
            return httpx.Response(
                200, json=[1], headers={"etag": '"one"', "link": f'<{following}>; rel="next"'}
            )
        assert request.url.path == "/repositories/42/issues"
        assert request.url.params["after"] == "opaque=="
        return httpx.Response(200, json=[2], headers={"etag": '"two"'})

    async with client(store, handler) as http:
        assert await http.paginate("/repos/a/b/issues?state=all") == [1, 2]
        cached = True
        assert await http.paginate("/repos/a/b/issues?state=all") == [1, 2]
        assert http.budget.requests == 6
    assert len(calls) == 6


@pytest.mark.parametrize(
    "failure", ["identity", "suffix", "filter", "duplicate", "cursor", "budget"]
)
async def test_canonical_cursor_pages_reject_unproven_coverage(tmp_path, failure):
    store = await ready(tmp_path)

    def handler(request):
        if request.url.path == "/repos/a/b":
            return httpx.Response(200, json={"id": 43 if failure == "identity" else 42})
        page = int(request.url.params["page"])
        path = "/repositories/42/releases" if failure == "suffix" else "/repositories/42/issues"
        state = "open" if failure == "filter" else "all"
        query = f"per_page=100&page={page + 1}&state={state}&after=fixed"
        if failure == "duplicate":
            query += "&state=all"
        return httpx.Response(
            200,
            json=[page],
            headers={"link": f'<https://api.github.com{path}?{query}>; rel="next"'},
        )

    limits = {"max_requests": 1} if failure == "budget" else {}
    async with client(store, handler, **limits) as http:
        with pytest.raises(BudgetError if failure == "budget" else PaginationError):
            await http.paginate("/repos/a/b/issues?state=all")
    assert (await store.source_state(GitHubConfig.load().sources[0].source_id)).checkpoint is None
