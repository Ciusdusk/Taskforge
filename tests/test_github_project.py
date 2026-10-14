"""Controlled GitHub responses backed by the real business SQLite store."""

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from integrations.github.auth import Credential
from integrations.github.config import Budgets, RefreshInput, Source
from integrations.github.errors import (
    BudgetError,
    CollectionError,
    GenerationError,
    HTTPError,
    RebaselineRequired,
    StateError,
)
from integrations.github.project import ExecutionIdentity, refresh_project
from integrations.github.storage import BusinessStore

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]
TOKEN = "fake-project-credential-987654"


def sha(number):
    return f"{number:040x}"


def commit(number):
    return {
        "sha": sha(number),
        "html_url": f"https://github.com/a/b/commit/{sha(number)}",
        "commit": {
            "message": f"commit {number}",
            "author": {"name": "Test", "email": "test@example.com", "date": "2026-10-14T00:00:00Z"},
            "committer": {
                "name": "Test",
                "email": "test@example.com",
                "date": "2026-10-14T00:00:00Z",
            },
            "tree": {"sha": sha(number + 10000)},
        },
        "parents": [{"sha": sha(number - 1)}] if number > 1 else [],
    }


def release(number, *, published="2026-01-01T00:00:00Z", **changes):
    return {
        "id": number,
        "tag_name": f"v{number}",
        "name": f"Release {number}",
        "body": f"body {number}",
        "created_at": "2026-01-01T00:00:00Z",
        "published_at": published,
        "prerelease": False,
        "draft": False,
        "html_url": f"https://github.com/a/b/releases/tag/v{number}",
        **changes,
    }


def entry(path, number=20000, *, mode="100644", kind="blob"):
    return {"path": path, "sha": sha(number), "mode": mode, "type": kind}


def tree(number, entries=(), *, truncated=False):
    return {"sha": sha(number), "tree": list(entries), "truncated": truncated}


class API:
    def __init__(self):
        self.repository = {"id": 42, "full_name": "a/b", "default_branch": "main"}
        self.target = 30
        self.baseline = [commit(n) for n in range(30, 10, -1)]
        self.releases = []
        self.release_pages = None
        self.trees = {}
        self.recursive = None
        self.compare_changes = {}
        self.compare_pages = None
        self.calls = []
        self.fail = None
        self.redirect = False
        self.final_id = None
        self.metadata_calls = 0

    def handler(self, request):
        self.calls.append(request)
        assert request.headers["authorization"] == "Bearer " + TOKEN
        assert request.headers["x-github-api-version"] == "2026-03-10"
        path = request.url.path
        if self.fail and self.fail in path:
            return httpx.Response(500, json={"message": "controlled failure"})
        if path in {"/repos/a/b", "/repos/new/name"}:
            if self.redirect and path == "/repos/a/b":
                return httpx.Response(301, headers={"location": "/repos/new/name"})
            self.metadata_calls += 1
            body = dict(self.repository)
            if self.final_id is not None and self.metadata_calls > 1:
                body["id"] = self.final_id
            return httpx.Response(200, json=body)
        root = "/repos/" + self.repository["full_name"]
        if path == root + "/commits/" + self.repository["default_branch"]:
            return httpx.Response(200, json=commit(self.target))
        if path == root + "/commits":
            assert request.url.params["sha"] == sha(self.target)
            assert request.url.params["per_page"] == "20"
            return httpx.Response(200, json=self.baseline)
        if path.startswith(root + "/compare/"):
            before, after = path.rsplit("/", 1)[-1].split("...")
            assert after == sha(self.target)
            base = int(before, 16)
            records = [commit(n) for n in range(base + 1, self.target + 1)]
            page = int(request.url.params["page"])
            pages = self.compare_pages
            if pages is None:
                pages = [records[i : i + 100] for i in range(0, len(records), 100)] or [[]]
            body = {
                "status": "ahead",
                "ahead_by": len(records),
                "behind_by": 0,
                "total_commits": len(records),
                "base_commit": {"sha": before},
                "merge_base_commit": {"sha": before},
                "commits": pages[page - 1],
                "files": [{"filename": "irrelevant"}] * 300,
                **self.compare_changes,
            }
            return self.page_response(request, body, page, len(pages))
        if path == root + "/releases":
            page = int(request.url.params["page"])
            pages = self.release_pages
            if pages is None:
                pages = [self.releases[i : i + 100] for i in range(0, len(self.releases), 100)] or [
                    []
                ]
            return self.page_response(request, pages[page - 1], page, len(pages))
        if path.startswith(root + "/git/trees/"):
            identity = path.rsplit("/", 1)[-1]
            if "recursive" in request.url.params:
                body = self.recursive or tree(self.target + 10000)
            else:
                body = self.trees[identity]
            return httpx.Response(200, json=body)
        raise AssertionError(f"Unexpected controlled request: {request.url}")

    @staticmethod
    def page_response(request, body, page, count):
        headers = {}
        if page < count:
            params = dict(request.url.params)
            params["page"] = str(page + 1)
            headers["link"] = f'<{request.url.copy_with(params=params)}>; rel="next"'
        return httpx.Response(200, json=body, headers=headers)

    def advance(self, target):
        self.target = target
        self.calls.clear()
        self.metadata_calls = 0
        self.compare_changes = {}
        self.compare_pages = None
        self.recursive = None


async def ready(tmp_path, *, scan=False, **budgets):
    source = Source(
        source_id="project",
        kind="project",
        repository="a/b",
        scan_skills=scan,
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


async def refresh(store, source, api, *, data=None, number=1):
    return await refresh_project(
        data or data_for(store, source),
        Credential(TOKEN),
        ExecutionIdentity(execution_id="direct-test", attempt_number=number),
        transport=httpx.MockTransport(api.handler),
        trust_env=False,
    )


def rows(store, table):
    with sqlite3.connect(store.path) as connection:
        return connection.execute(f"SELECT * FROM {table}").fetchall()


def coverage(store):
    return json.loads(rows(store, "batches")[-1][5])


def durable(store):
    return {
        table: rows(store, table)
        for table in (
            "sources",
            "generations",
            "batches",
            "entities",
            "versions",
            "changes",
            "skill_snapshots",
            "skill_changes",
            "release_ids",
        )
    }


async def test_finite_baseline_forward_and_unchanged(tmp_path):
    store, source = await ready(tmp_path)
    api = API()
    assert await refresh(store, source, api)
    assert len(await store.entities(42, "commit")) == 20
    assert {row[5] for row in rows(store, "changes")} == {"initial"}
    assert coverage(store)["commits"]["mode"] == "finite_initial_window"
    assert (await store.source_state(source.source_id)).checkpoint["sha"] == sha(30)
    assert coverage(store)["releases"]["visible_ids"] == 0
    assert not any("git/trees" in request.url.path for request in api.calls)
    api.advance(33)
    assert await refresh(store, source, api)
    assert len(await store.entities(42, "commit")) == 23
    assert coverage(store)["commits"]["count"] == 3
    assert [row[5] for row in rows(store, "changes")].count("added") == 3
    api.advance(33)
    assert await refresh(store, source, api)
    assert coverage(store)["commits"]["mode"] == "unchanged"
    assert not any("compare" in request.url.path for request in api.calls)
    assert len(rows(store, "versions")) == 23


async def test_compare_complete_pagination_and_target_fixed(tmp_path):
    store, source = await ready(tmp_path)
    api = API()
    await refresh(store, source, api)
    api.advance(260)
    await refresh(store, source, api)
    comparisons = [request for request in api.calls if "/compare/" in request.url.path]
    assert [request.url.params["page"] for request in comparisons] == ["1", "2", "3"]
    assert all(request.url.params["per_page"] == "100" for request in comparisons)
    assert len(await store.entities(42, "commit")) == 250
    assert coverage(store)["commits"]["count"] == 230


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"status": "diverged", "behind_by": 2}, RebaselineRequired),
        ({"status": "behind", "behind_by": 1}, RebaselineRequired),
        ({"status": "identical"}, RebaselineRequired),
        ({"merge_base_commit": {"sha": sha(29)}}, RebaselineRequired),
        ({"base_commit": {"sha": sha(29)}}, RebaselineRequired),
        ({"total_commits": 4}, CollectionError),
        ({"ahead_by": 4}, CollectionError),
        ({"total_commits": 501, "ahead_by": 501}, RebaselineRequired),
        ({"commits": [commit(31), commit(32)]}, CollectionError),
        ({"commits": [commit(31), commit(31), commit(33)]}, CollectionError),
        ({"commits": [commit(30), commit(32), commit(33)]}, CollectionError),
        ({"commits": [commit(31), commit(32), commit(34)]}, CollectionError),
        ({"total_commits": True}, CollectionError),
        ({"total_commits": None}, CollectionError),
    ],
)
async def test_compare_rejects_unproven_history_without_any_business_write(
    tmp_path, changes, expected
):
    store, source = await ready(tmp_path)
    api = API()
    await refresh(store, source, api)
    old = durable(store)
    api.advance(33)
    api.compare_changes = changes
    with pytest.raises(expected):
        await refresh(store, source, api)
    assert durable(store) == old
    assert rows(store, "attempts")[-1][-1]


@pytest.mark.parametrize("status", [404, 422])
async def test_old_sha_not_comparable_reports_rebaseline(tmp_path, status):
    store, source = await ready(tmp_path)
    api = API()
    await refresh(store, source, api)
    api.advance(33)
    old = durable(store)
    original = api.handler

    def handler(request):
        if "/compare/" in request.url.path:
            return httpx.Response(status)
        return original(request)

    api.handler = handler
    with pytest.raises(RebaselineRequired, match="not comparable"):
        await refresh(store, source, api)
    assert durable(store) == old


@pytest.mark.parametrize("limit,target", [(500, 530), (500, 531), (2, 33)])
async def test_increment_commit_limit_boundary(tmp_path, limit, target):
    store, source = await ready(tmp_path, max_commits=limit)
    api = API()
    await refresh(store, source, api)
    old = durable(store)
    api.advance(target)
    if target - 30 <= limit:
        await refresh(store, source, api)
        assert coverage(store)["commits"]["count"] == limit
    else:
        with pytest.raises(RebaselineRequired, match="limit exceeded"):
            await refresh(store, source, api)
        assert durable(store) == old
        assert len([r for r in api.calls if "compare" in r.url.path]) == 1


@pytest.mark.parametrize("failure", ["count", "metadata", "http", "page_budget"])
async def test_compare_later_page_incomplete(tmp_path, failure):
    store, source = await ready(tmp_path, max_pages=1 if failure == "page_budget" else 10)
    api = API()
    await refresh(store, source, api)
    old = durable(store)
    api.advance(132)
    original = api.handler

    def handler(request):
        response = original(request)
        if "/compare/" in request.url.path and request.url.params["page"] == "2":
            if failure == "http":
                return httpx.Response(500)
            body = response.json()
            if failure == "count":
                body["commits"].pop()
            if failure == "metadata":
                body["ahead_by"] += 1
            return httpx.Response(200, json=body)
        return response

    api.handler = handler
    with pytest.raises((CollectionError, HTTPError)):
        await refresh(store, source, api)
    assert durable(store) == old


async def test_repository_redirect_keeps_remote_identity(tmp_path):
    store, source = await ready(tmp_path)
    api = API()
    await refresh(store, source, api)
    api.advance(31)
    api.redirect = True
    api.repository["full_name"] = "new/name"
    await refresh(store, source, api)
    state = await store.source_state(source.source_id)
    assert state.checkpoint["repository_id"] == 42
    assert state.checkpoint["repository"] == "new/name"
    assert len(await store.entities(42, "commit")) == 21
    assert coverage(store)["requests"] == len(api.calls)


@pytest.mark.parametrize("when", ["before", "during", "alias"])
async def test_remote_id_replacement_is_not_a_continuation(tmp_path, when):
    store, source = await ready(tmp_path)
    api = API()
    await refresh(store, source, api)
    old = durable(store)
    api.advance(31)
    if when == "before":
        api.repository["id"] = 99
    elif when == "during":
        api.final_id = 99
    else:
        api.redirect = True
        api.repository["full_name"] = "new/name"
        original = api.handler
        alias_calls = 0

        def handler(request):
            nonlocal alias_calls
            if request.url.path == "/repos/a/b":
                alias_calls += 1
                if alias_calls > 1:
                    return httpx.Response(200, json={**api.repository, "id": 99})
            return original(request)

        api.handler = handler
    with pytest.raises(RebaselineRequired, match="identity"):
        await refresh(store, source, api)
    assert durable(store) == old


async def test_release_all_ids_initial_window_and_later_content_edits(tmp_path):
    store, source = await ready(tmp_path)
    api = API()
    api.releases = [release(n, prerelease=n == 15) for n in range(1, 16)]
    api.releases.append(release(99, draft=True, published_at=None))
    await refresh(store, source, api)
    assert set(await store.release_ids(42)) == {str(n) for n in range(1, 16)}
    assert {entity.remote_id for entity in await store.entities(42, "release")} == {
        str(n) for n in range(6, 16)
    }
    assert (
        next(e for e in await store.entities(42, "release") if e.remote_id == "15").content[
            "prerelease"
        ]
        is True
    )
    api.advance(31)
    api.releases[0]["body"] = "unmonitored old edit"
    api.releases[5]["body"] = "tracked older edit"
    recent = datetime.now(UTC).isoformat()
    api.releases.extend([release(16), release(17, published=recent)])
    await refresh(store, source, api)
    saved = {e.remote_id: e for e in await store.entities(42, "release")}
    assert "1" not in saved
    assert saved["6"].content["body"] == "tracked older edit"
    assert saved["16"].content["body"] == "body 16"
    report = coverage(store)["releases"]
    assert report["first_observed"] == 2
    assert report["first_observed_published_in_last_7_days"] == 1
    changes = [(row[3], row[5]) for row in rows(store, "changes") if row[2] == "release"]
    assert ("6", "updated") in changes
    assert ("16", "first_observed") in changes
    assert ("17", "first_observed") in changes
    assert len(saved) == 12


async def test_release_full_pagination_initial_sorted_window(tmp_path):
    store, source = await ready(tmp_path)
    api = API()
    api.releases = [release(n) for n in range(1, 106)]
    # published_at takes precedence over ID; offset-equivalent dates normalize before sorting.
    api.releases[0]["published_at"] = "2026-10-14T08:00:00+08:00"
    await refresh(store, source, api)
    assert len(await store.release_ids(42)) == 105
    assert {e.remote_id for e in await store.entities(42, "release")} == {
        "1",
        *map(str, range(97, 106)),
    }
    saved = next(e for e in await store.entities(42, "release") if e.remote_id == "1")
    assert saved.content["published_at"] == "2026-10-14T00:00:00.000000+00:00"
    assert set(saved.content) == {
        "id",
        "tag_name",
        "name",
        "body",
        "created_at",
        "published_at",
        "prerelease",
        "html_url",
    }
    assert len([r for r in api.calls if r.url.path.endswith("/releases")]) == 2


async def test_release_disappearance_preserves_history_and_diagnoses(tmp_path):
    store, source = await ready(tmp_path)
    api = API()
    api.releases = [release(1), release(2)]
    await refresh(store, source, api)
    api.advance(31)
    api.releases = [release(2)]
    await refresh(store, source, api)
    assert coverage(store)["releases"]["missing_saved_ids"] == ["1"]
    assert coverage(store)["releases"]["diagnostics"]
    assert len(await store.entities(42, "release")) == 2
    assert set(await store.release_ids(42)) == {"1", "2"}
    api.advance(31)
    api.releases = []
    await refresh(store, source, api)
    assert coverage(store)["releases"]["missing_saved_ids"] == ["1", "2"]
    assert len(await store.entities(42, "release")) == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("tag_name", "new-tag"),
        ("name", None),
        ("body", "new body"),
        ("created_at", "2025-01-01T00:00:00Z"),
        ("published_at", "2025-01-01T00:00:00Z"),
        ("prerelease", True),
        ("html_url", "https://github.com/a/b/releases/tag/new"),
    ],
)
async def test_release_fingerprint_tracks_each_content_field(tmp_path, field, value):
    store, source = await ready(tmp_path)
    api = API()
    api.releases = [release(1)]
    await refresh(store, source, api)
    old = (await store.entities(42, "release"))[0]
    api.advance(30)
    api.releases[0][field] = value
    await refresh(store, source, api)
    new = (await store.entities(42, "release"))[0]
    assert new.fingerprint != old.fingerprint
    assert rows(store, "changes")[-1][5] == "updated"
    assert len([row for row in rows(store, "versions") if row[1] == "release"]) == 2


async def test_release_fingerprint_normalizes_time_and_ignores_updated_at(tmp_path):
    store, source = await ready(tmp_path)
    api = API()
    api.releases = [release(1)]
    await refresh(store, source, api)
    old_versions = rows(store, "versions")
    api.advance(30)
    api.releases[0].update(published_at="2026-01-01T08:00:00+08:00", updated_at="irrelevant")
    await refresh(store, source, api)
    assert rows(store, "versions") == old_versions
    assert len(rows(store, "changes")) == 21


@pytest.mark.parametrize("failure", ["later_page", "duplicate", "invalid_date", "missing_field"])
async def test_release_uncertain_coverage_does_not_commit_commits(tmp_path, failure):
    store, source = await ready(tmp_path)
    api = API()
    await refresh(store, source, api)
    old = durable(store)
    api.advance(33)
    api.releases = [release(1)]
    if failure == "duplicate":
        api.release_pages = [[release(1)], [release(1)]]
    if failure == "invalid_date":
        api.releases[0]["published_at"] = "2026-01-01"
    if failure == "missing_field":
        del api.releases[0]["body"]
    if failure == "later_page":
        api.releases = [release(n) for n in range(1, 102)]
        original = api.handler

        def handler(request):
            if request.url.path.endswith("/releases") and request.url.params["page"] == "2":
                return httpx.Response(500)
            return original(request)

        api.handler = handler
    with pytest.raises((CollectionError, HTTPError)):
        await refresh(store, source, api)
    assert durable(store) == old


async def test_skill_complete_native_tree_changes_rename_and_fixed_links(tmp_path):
    store, source = await ready(tmp_path, scan=True)
    api = API()
    initial = [
        entry("SKILL.md", 1),
        entry("folder", 2, mode="040000", kind="tree"),
        entry("folder/SKILL.md", 3, mode="100755"),
        entry("other.md", 4),
        entry("link/SKILL.md", 5, mode="120000"),
    ]
    # The symlink itself lives in an ordinary directory, and is excluded.
    initial.append(entry("link", 6, mode="040000", kind="tree"))
    initial.append(entry("submodule", 7, mode="160000", kind="commit"))
    api.recursive = tree(10030, initial)
    await refresh(store, source, api)
    assert (await store.skill_snapshot(42, sha(30))).files == {
        "SKILL.md": sha(1),
        "folder/SKILL.md": sha(3),
    }
    report = coverage(store)["skills"]
    assert report["excluded"] == {"symlinks": 1, "submodules": 1}
    assert all(change["initial"] for change in report["changes"])
    api.advance(31)
    api.recursive = tree(
        10031,
        [
            entry("SKILL.md", 8),
            entry("renamed", 9, mode="040000", kind="tree"),
            entry("renamed/SKILL.md", 3),
            entry("new", 10, mode="040000", kind="tree"),
            entry("new/SKILL.md", 11),
        ],
    )
    await refresh(store, source, api)
    changes = coverage(store)["skills"]["changes"]
    assert {item["path"]: item["kind"] for item in changes} == {
        "SKILL.md": "modified",
        "folder/SKILL.md": "deleted",
        "renamed/SKILL.md": "added",
        "new/SKILL.md": "added",
    }
    deleted = next(item for item in changes if item["kind"] == "deleted")
    assert deleted["before_url"] == f"https://github.com/a/b/blob/{sha(30)}/folder/SKILL.md"
    assert deleted["after_url"] is None
    assert all(sha(31) in item["after_url"] for item in changes if item["after_url"])
    assert len(rows(store, "skill_changes")) == 6
    old_changes = rows(store, "skill_changes")
    old_snapshots = rows(store, "skill_snapshots")
    # New refresh identity, identical target and tree: no duplicate stable changes.
    await refresh(store, source, api)
    assert rows(store, "skill_changes") == old_changes
    assert rows(store, "skill_snapshots") == old_snapshots


async def test_truncated_recursive_tree_is_replaced_by_complete_subtrees(tmp_path):
    store, source = await ready(tmp_path, scan=True)
    api = API()
    api.recursive = tree(10030, [entry("phantom/SKILL.md", 999)], truncated=True)
    api.trees = {
        sha(10030): tree(
            10030,
            [
                entry("one", 200, mode="040000", kind="tree"),
                entry("two", 200, mode="040000", kind="tree"),
            ],
        ),
        sha(200): tree(
            200, [entry("SKILL.md", 300), entry("nested", 201, mode="040000", kind="tree")]
        ),
        sha(201): tree(201, [entry("SKILL.md", 301)]),
    }
    await refresh(store, source, api)
    assert (await store.skill_snapshot(42, sha(30))).files == {
        "one/SKILL.md": sha(300),
        "one/nested/SKILL.md": sha(301),
        "two/SKILL.md": sha(300),
        "two/nested/SKILL.md": sha(301),
    }
    assert coverage(store)["skills"]["method"] == "subtrees"
    assert len([r for r in api.calls if "/git/trees/" in r.url.path]) == 6


@pytest.mark.parametrize(
    "failure",
    [
        "child_http",
        "child_truncated",
        "root_truncated",
        "wrong_identity",
        "budget",
        "bad_type",
        "duplicate_path",
        "unsafe_path",
        "nested_nonrecursive",
        "cycle",
    ],
)
async def test_failed_subtree_never_produces_deletions_or_partial_data(tmp_path, failure):
    store, source = await ready(tmp_path, scan=True, max_requests=6 if failure == "budget" else 100)
    api = API()
    api.recursive = tree(10030, [entry("SKILL.md", 1)])
    await refresh(store, source, api)
    old = durable(store)
    api.advance(31)
    api.releases = [release(1)]
    api.recursive = tree(10031, truncated=True)
    api.trees = {
        sha(10031): tree(10031, [entry("child", 200, mode="040000", kind="tree")]),
        sha(200): tree(200, [entry("SKILL.md", 300)]),
    }
    if failure == "child_http":
        api.fail = sha(200)
    if failure == "child_truncated":
        api.trees[sha(200)]["truncated"] = True
    if failure == "root_truncated":
        api.trees[sha(10031)]["truncated"] = True
    if failure == "wrong_identity":
        api.trees[sha(200)]["sha"] = sha(201)
    if failure == "bad_type":
        api.trees[sha(200)]["tree"][0]["type"] = "commit"
    if failure == "duplicate_path":
        api.trees[sha(200)]["tree"].append(entry("SKILL.md", 301))
    if failure == "unsafe_path":
        api.trees[sha(200)]["tree"][0]["path"] = "../SKILL.md"
    if failure == "nested_nonrecursive":
        api.trees[sha(200)]["tree"][0]["path"] = "nested/SKILL.md"
    if failure == "cycle":
        api.trees[sha(200)]["tree"] = [entry("cycle", 10031, mode="040000", kind="tree")]
    with pytest.raises((CollectionError, HTTPError)):
        await refresh(store, source, api)
    assert durable(store) == old
    assert (await store.skill_snapshot(42, sha(30))).files == {"SKILL.md": sha(1)}


@pytest.mark.parametrize(
    "entries",
    [
        [entry("x/SKILL.md")],
        [entry("x", mode="120000"), entry("x/SKILL.md")],
        [entry("x", mode="160000", kind="commit"), entry("x/SKILL.md")],
        [entry("SKILL.md", mode="unknown")],
    ],
)
async def test_invalid_recursive_tree_cannot_prove_skill_coverage(tmp_path, entries):
    store, source = await ready(tmp_path, scan=True)
    api = API()
    api.recursive = tree(10030, entries)
    with pytest.raises(CollectionError):
        await refresh(store, source, api)
    assert not rows(store, "batches")
    assert not rows(store, "entities")


async def test_empty_complete_tree_can_prove_deletion(tmp_path):
    store, source = await ready(tmp_path, scan=True)
    api = API()
    api.recursive = tree(10030, [entry("SKILL.md", 1)])
    await refresh(store, source, api)
    api.advance(31)
    await refresh(store, source, api)
    assert (await store.skill_snapshot(42, sha(31))).files == {}
    assert coverage(store)["skills"]["changes"][0]["kind"] == "deleted"


async def test_same_commit_snapshot_cannot_change(tmp_path):
    store, source = await ready(tmp_path, scan=True)
    api = API()
    await refresh(store, source, api)
    old = durable(store)
    api.recursive = tree(10030, [entry("SKILL.md", 1)])
    with pytest.raises(CollectionError, match="Immutable"):
        await refresh(store, source, api)
    assert durable(store) == old


async def test_complete_replay_checks_full_input_before_network(tmp_path):
    store, source = await ready(tmp_path, scan=True)
    api = API()
    data = data_for(store, source)
    await refresh(store, source, api, data=data)
    old = durable(store)
    api.calls.clear()
    api.fail = "/repos/"
    assert not await refresh(store, source, api, data=data, number=2)
    assert api.calls == []
    assert durable(store) == old
    altered = data.model_copy(update={"operation": "rebaseline"})
    with pytest.raises(StateError, match="another input"):
        await refresh(store, source, api, data=altered)
    assert api.calls == []
    await store.register_source(source, rebaseline=True)
    assert not await refresh(store, source, api, data=data, number=3)
    assert (await store.source_state(source.source_id)).checkpoint is None


async def test_generations_rebaseline_and_identity_change_preserve_history(tmp_path):
    store, source = await ready(tmp_path, scan=True)
    api = API()
    old_data = data_for(store, source)
    await refresh(store, source, api)
    old_checkpoint = (await store.source_state(source.source_id)).checkpoint
    await store.register_source(source, rebaseline=True)
    api.calls.clear()
    with pytest.raises(GenerationError):
        await refresh(store, source, api, data=old_data)
    assert not api.calls
    api.repository["id"] = 99
    new_data = data_for(store, source, generation=2, operation="rebaseline")
    await refresh(store, source, api, data=new_data)
    assert (await store.source_state(source.source_id, 1)).checkpoint == old_checkpoint
    assert (await store.source_state(source.source_id)).checkpoint["repository_id"] == 99
    assert len(await store.entities(42, "commit")) == 20
    assert len(await store.entities(99, "commit")) == 20
    assert coverage(store)["commits"]["mode"] == "finite_initial_window"


async def test_generation_changed_during_network_prevents_commit(tmp_path):
    store, source = await ready(tmp_path)
    api = API()
    original = api.handler

    async def handler(request):
        response = original(request)
        if request.url.path.endswith("/releases"):
            await store.register_source(source, rebaseline=True)
        return response

    api.handler = handler
    with pytest.raises(GenerationError):
        await refresh(store, source, api)
    assert not rows(store, "entities")
    assert not rows(store, "batches")
    assert (await store.source_state(source.source_id)).checkpoint is None


async def test_failed_old_job_retry_reads_newest_successful_checkpoint(tmp_path):
    store, source = await ready(tmp_path)
    api = API()
    await refresh(store, source, api)
    retry_data = data_for(store, source)
    api.advance(31)
    api.fail = "/releases"
    with pytest.raises(HTTPError):
        await refresh(store, source, api, data=retry_data)
    api.fail = None
    api.advance(32)
    await refresh(store, source, api)
    api.advance(33)
    await refresh(store, source, api, data=retry_data, number=2)
    requests = [request for request in api.calls if "/compare/" in request.url.path]
    assert len(requests) == 1
    assert requests[0].url.path.endswith(f"/{sha(32)}...{sha(33)}")
    assert coverage(store)["commits"]["count"] == 1
    assert [row[2] for row in rows(store, "attempts") if row[0] == str(retry_data.refresh_id)] == [
        1,
        2,
    ]


@pytest.mark.parametrize("limit", [4, 5, 6])
async def test_all_collectors_share_one_actual_request_budget(tmp_path, limit):
    store, source = await ready(tmp_path, scan=True, max_requests=limit)
    api = API()
    if limit < 6:
        with pytest.raises(BudgetError):
            await refresh(store, source, api)
        assert not rows(store, "batches")
        assert not rows(store, "entities")
        assert len(api.calls) == limit
    else:
        await refresh(store, source, api)
        assert len(api.calls) == 6
        assert coverage(store)["requests"] == 6


async def test_branch_moving_mid_attempt_does_not_change_fixed_target(tmp_path):
    store, source = await ready(tmp_path, scan=True)
    api = API()
    api.recursive = tree(10030, [entry("SKILL.md", 1)])
    original = api.handler
    moved = False

    def handler(request):
        nonlocal moved
        if request.url.path.endswith("/commits/main"):
            response = original(request)
            moved = True
            api.target = 31
            return response
        if moved and request.url.path.endswith("/commits"):
            assert request.url.params["sha"] == sha(30)
            return httpx.Response(200, json=api.baseline)
        return original(request)

    api.handler = handler
    await refresh(store, source, api)
    assert (await store.source_state(source.source_id)).checkpoint["sha"] == sha(30)
    assert any(request.url.path.endswith(sha(10030)) for request in api.calls)
    assert len([r for r in api.calls if r.url.path.endswith("/commits/main")]) == 1


async def test_same_source_refreshes_serialize_checkpoint_read(tmp_path):
    store, source = await ready(tmp_path)
    api = API()
    entered = asyncio.Event()
    release_gate = asyncio.Event()
    original = api.handler
    first = True

    async def handler(request):
        nonlocal first
        if request.url.path == "/repos/a/b" and first:
            first = False
            entered.set()
            await release_gate.wait()
        return original(request)

    api.handler = handler
    first_task = asyncio.create_task(refresh(store, source, api))
    second_task = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        second_task = asyncio.create_task(refresh(store, source, api))
        await asyncio.sleep(0)
        assert not api.calls
        release_gate.set()
        await asyncio.wait_for(asyncio.gather(first_task, second_task), 5)
    finally:
        release_gate.set()
        await asyncio.gather(
            first_task, *([second_task] if second_task else []), return_exceptions=True
        )
    assert len(rows(store, "batches")) == 2
    assert len(rows(store, "versions")) == 20
    modes = [json.loads(row[5])["commits"]["mode"] for row in rows(store, "batches")]
    assert modes == ["finite_initial_window", "unchanged"]


async def test_sql_failure_rolls_back_complete_collected_batch(tmp_path):
    store, source = await ready(tmp_path, scan=True)
    api = API()
    api.releases = [release(1)]
    api.recursive = tree(10030, [entry("SKILL.md", 1)])
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """CREATE TRIGGER fail_checkpoint BEFORE UPDATE OF checkpoint ON generations
               BEGIN SELECT RAISE(ABORT,'controlled failure'); END"""
        )
    old = durable(store)
    with pytest.raises(sqlite3.IntegrityError):
        await refresh(store, source, api)
    assert durable(store) == old


@pytest.mark.parametrize(
    "values", [[], [commit(29)], [commit(30)] * 2, [commit(n) for n in range(30, 9, -1)]]
)
async def test_invalid_commit_baseline_cannot_advance_checkpoint(tmp_path, values):
    store, source = await ready(tmp_path)
    api = API()
    api.baseline = values
    with pytest.raises(CollectionError):
        await refresh(store, source, api)
    assert not rows(store, "batches")
    assert not rows(store, "entities")
    assert (await store.source_state(source.source_id)).checkpoint is None


async def test_small_baseline_and_null_release_fields_are_valid(tmp_path):
    store, source = await ready(tmp_path)
    api = API()
    api.baseline = [commit(30)]
    api.releases = [release(1, name=None, body=None), release(2, draft=True)]
    await refresh(store, source, api)
    assert len(await store.entities(42, "commit")) == 1
    assert await store.release_ids(42) == ("1",)
    assert (await store.entities(42, "release"))[0].content["body"] is None


@pytest.mark.parametrize("stage", ["metadata", "branch", "recursive_tree", "releases"])
async def test_missing_identity_and_coverage_fields_fail_safely(tmp_path, stage):
    store, source = await ready(tmp_path, scan=True)
    api = API()
    original = api.handler

    def handler(request):
        response = original(request)
        if (
            stage == "metadata"
            and request.url.path == "/repos/a/b"
            or stage == "branch"
            and request.url.path.endswith("/commits/main")
            or stage == "recursive_tree"
            and "/git/trees/" in request.url.path
            or stage == "releases"
            and request.url.path.endswith("/releases")
        ):
            return httpx.Response(200, json={"invalid": "remote-payload-must-not-leak"})
        return response

    api.handler = handler
    with pytest.raises((CollectionError, HTTPError)) as caught:
        await refresh(store, source, api)
    assert "remote-payload-must-not-leak" not in str(caught.value)
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    assert not rows(store, "batches")
    assert not rows(store, "entities")


async def test_generation_baseline_mismatch_rejected_before_network(tmp_path):
    store, source = await ready(tmp_path)
    altered = Source(**{**source.model_dump(), "scan_skills": True})
    api = API()
    with pytest.raises(GenerationError):
        await refresh(store, altered, api)
    assert not api.calls


async def test_budget_config_snapshot_does_not_reset_checkpoint(tmp_path):
    store, source = await ready(tmp_path)
    api = API()
    await refresh(store, source, api)
    changed = Source(**{**source.model_dump(), "budgets": {"max_requests": 10}})
    state = await store.register_source(changed)
    assert state.generation == 1 and state.checkpoint["sha"] == sha(30)
    api.advance(31)
    await refresh(store, source, api)
    assert coverage(store)["commits"]["mode"] == "complete_increment"


async def test_failed_initial_attempt_retry_creates_one_baseline(tmp_path):
    store, source = await ready(tmp_path, scan=True)
    api = API()
    data = data_for(store, source)
    api.fail = "/git/trees/"
    with pytest.raises(HTTPError):
        await refresh(store, source, api, data=data)
    assert not rows(store, "entities")
    api.fail = None
    await refresh(store, source, api, data=data, number=2)
    assert len(rows(store, "batches")) == 1
    assert len(rows(store, "changes")) == 20
    assert not await refresh(store, source, api, data=data, number=3)
    assert len(rows(store, "changes")) == 20


async def test_missing_previous_skill_snapshot_is_not_an_initial_scan(tmp_path):
    store, source = await ready(tmp_path, scan=True)
    api = API()
    await refresh(store, source, api)
    with sqlite3.connect(store.path) as connection:
        connection.execute("DELETE FROM skill_snapshots")
    old = durable(store)
    api.advance(31)
    with pytest.raises(StateError, match="snapshot is missing"):
        await refresh(store, source, api)
    assert durable(store) == old


async def test_project_collector_rejects_issue_source(tmp_path):
    store, _ = await ready(tmp_path)
    source = Source(source_id="issues", kind="issues", repository="a/b")
    api = API()
    with pytest.raises(StateError, match="project source"):
        await refresh(store, source, api)
    assert not api.calls


async def test_project_cancellation_keeps_source_lock_until_transport_cleanup(tmp_path):
    store, source = await ready(tmp_path)
    api = API()
    entered = asyncio.Event()
    cleaning = asyncio.Event()
    release_gate = asyncio.Event()
    original = api.handler
    paused = True

    async def handler(request):
        nonlocal paused
        if paused:
            paused = False
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release_gate.wait()
        return original(request)

    api.handler = handler
    task = asyncio.create_task(refresh(store, source, api))
    contender = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.wait_for(cleaning.wait(), 2)
        contender = asyncio.create_task(refresh(store, source, api))
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert not contender.done()
        assert not api.calls
        release_gate.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert await asyncio.wait_for(contender, 5)
    finally:
        release_gate.set()
        await asyncio.gather(task, *([contender] if contender else []), return_exceptions=True)
    assert len(rows(store, "batches")) == 1
