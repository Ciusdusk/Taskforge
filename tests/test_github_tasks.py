"""G04 public submission/query and real core execution over controlled GitHub HTTP."""

import asyncio
import json
import sqlite3
import threading
from uuid import uuid4

import httpx
import pytest
from integrations.github import commands, receipts, tasks
from integrations.github.config import DEFAULT_POLICY, Budgets, GitHubConfig, RefreshInput, Source
from integrations.github.errors import StateError
from integrations.github.http import GitHubHTTP
from integrations.github.locking import InstanceLocks
from integrations.github.project import ExecutionIdentity
from integrations.github.storage import BusinessStore
from pydantic import ValidationError
from tests.test_github_issues import API as IssueAPI
from tests.test_github_issues import TOKEN as ISSUE_TOKEN
from tests.test_github_issues import issue
from tests.test_github_project import API as ProjectAPI
from tests.test_github_project import TOKEN as PROJECT_TOKEN

from taskforge import (
    DatabaseConfig,
    JobStatus,
    SQLiteStore,
    SubmissionUncertainError,
    TaskForge,
    Worker,
    WorkerConfig,
    load_registry,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def ready(tmp_path, monkeypatch, kind="project"):
    source = Source(source_id="sample", kind=kind, repository="a/b")
    config = GitHubConfig(sources=(source,))
    await commands.initialize(tmp_path, config)
    api = (
        ProjectAPI() if kind == "project" else IssueAPI([issue(updated_at="2026-01-02T00:00:00Z")])
    )
    token = PROJECT_TOKEN if kind == "project" else ISSUE_TOKEN
    monkeypatch.setenv("GH_TOKEN", token)
    real_http = tasks.GitHubHTTP
    monkeypatch.setattr(
        tasks,
        "GitHubHTTP",
        lambda *args, **kwargs: real_http(
            *args, **{**kwargs, "transport": httpx.MockTransport(api.handler), "trust_env": False}
        ),
    )
    return BusinessStore(tmp_path), source, config, api


async def wait_for(operation, predicate):
    async with asyncio.timeout(10):
        while True:
            value = await operation()
            if predicate(value):
                return value
            await asyncio.sleep(0.01)


async def work(directory, identifier, predicate=lambda job: job.status == JobStatus.SUCCESS):
    async with SQLiteStore(DatabaseConfig(database_path=directory / "tasks.sqlite")) as core:
        worker = Worker(core, tasks.registry, WorkerConfig(poll_interval_seconds=0.01))
        running = asyncio.create_task(worker.run())
        try:
            result = await wait_for(lambda: TaskForge(core).get(identifier), predicate)
        finally:
            worker.request_stop()
            await running
        return result, await TaskForge(core).history(identifier)


@pytest.mark.parametrize("kind", ["project", "issues"])
async def test_registered_dispatch_real_worker_receipt_and_replay(tmp_path, monkeypatch, kind):
    business, source, config, api = await ready(tmp_path, monkeypatch, kind)
    registry = load_registry("integrations.github.tasks:registry")
    definition = registry.get("github.refresh")
    assert definition.input_model is RefreshInput and definition.policy == DEFAULT_POLICY
    identifier = uuid4()
    result = (await commands.submit(tmp_path, config, refresh_id=identifier))[0]
    job, history = await work(tmp_path, result["job_id"])
    assert job.status == JobStatus.SUCCESS and len(history) == 1
    assert (
        job.max_retries,
        job.retry_base_seconds,
        job.retry_cap_seconds,
        job.timeout_seconds,
    ) == (2, 5, 60, 300)
    intent = await business.intent(str(identifier))
    calls = len(api.calls)
    assert intent.job_id == job.id
    assert not await tasks.execute_refresh(
        intent.input, ExecutionIdentity(execution_id=job.id, attempt_number=2)
    )
    assert len(api.calls) == calls
    batch = await business.batch(str(identifier))
    assert batch["coverage"]["execution"] == {"execution_id": job.id, "attempt_number": 1}
    assert batch["repository_counts"]["entities"] == batch["repository_counts"]["versions"]
    assert batch["repository_counts"]["entities"] > 0
    output = await commands.show(tmp_path, refresh_id=identifier)
    assert output[0]["intents"][0]["core"]["job"]["status"] == "SUCCESS"
    receipt = json.loads((tmp_path / f"receipts/{identifier}.json").read_text())
    assert receipt["scenario"] == "initialization"
    assert receipt["batch"]["coverage"]["source"]["sha256"]
    assert receipt["batch"]["coverage"]["elapsed_seconds"] > 0
    public = json.dumps([output, receipt])
    assert str(tmp_path) not in public
    assert PROJECT_TOKEN not in public and ISSUE_TOKEN not in public
    assert str(identifier) in (tmp_path / "indexes/sample.md").read_text()
    index = (tmp_path / "indexes/sample.md").read_text()
    assert "Generation: 1" in index and "finite_initial_window" in index
    assert "Entities | Versions | Diagnostics" in index and "https://github.com/a/b" in index


@pytest.mark.parametrize("field", ["token", "unexpected", "credential"])
async def test_registered_input_forbids_unknown_fields(tmp_path, field):
    payload = {
        "refresh_id": str(uuid4()),
        "state_directory": str(tmp_path),
        "source": {"source_id": "s", "kind": "issues", "repository": "a/b"},
        "source_generation": 1,
        field: "fake",
    }
    with pytest.raises(ValidationError):
        tasks.registry.get("github.refresh").validate_input(payload)


async def test_task_requires_core_execution_context(tmp_path):
    data = RefreshInput(
        refresh_id=uuid4(),
        state_directory=tmp_path,
        source=Source(source_id="s", kind="project", repository="a/b"),
        source_generation=1,
    )
    with pytest.raises(StateError, match="execution context"):
        await tasks.refresh(data)


async def test_same_intent_and_different_refresh_ids(tmp_path, monkeypatch):
    business, source, config, api = await ready(tmp_path, monkeypatch)
    identifier = uuid4()
    first, second = await asyncio.gather(
        commands.submit(tmp_path, config, refresh_id=identifier),
        commands.submit(tmp_path, config, refresh_id=identifier),
    )
    assert first == second
    third = await commands.submit(tmp_path, config, refresh_id=uuid4())
    assert third[0]["job_id"] != first[0]["job_id"]
    async with SQLiteStore(DatabaseConfig(database_path=tmp_path / "tasks.sqlite")) as core:
        assert len(await TaskForge(core).list()) == 2
    assert not api.calls
    changed = GitHubConfig(sources=(source.model_copy(update={"repository": "a/c"}),))
    conflict = await commands.submit(tmp_path, changed, refresh_id=identifier)
    assert conflict[0]["error"] == "StateError"
    assert len(await business.intents()) == 2


async def test_rebaseline_once_and_stale_pending_job(tmp_path, monkeypatch):
    business, source, config, api = await ready(tmp_path, monkeypatch)
    old = (await commands.submit(tmp_path, config))[0]
    identifier = uuid4()
    first = await commands.submit(tmp_path, config, refresh_id=identifier, rebaseline=True)
    assert await commands.submit(tmp_path, config, refresh_id=identifier, rebaseline=True) == first
    assert (await business.source_state(source.source_id)).generation == 2
    old_intent = await business.intent(old["refresh_id"])
    with pytest.raises(StateError):
        await tasks.execute_refresh(
            old_intent.input, ExecutionIdentity(execution_id=old["job_id"], attempt_number=1)
        )
    assert not api.calls
    assert (await business.source_state(source.source_id, 1)).checkpoint is None


async def test_prepare_intent_and_generation_roll_back_together(tmp_path, monkeypatch):
    business, source, config, api = await ready(tmp_path, monkeypatch)
    with sqlite3.connect(business.path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_intent BEFORE INSERT ON intents "
            "BEGIN SELECT RAISE(ABORT,'fault'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        await business.prepare_intent(source, uuid4(), rebaseline=True)
    assert (await business.source_state(source.source_id)).generation == 1
    assert not await business.intents()


@pytest.mark.parametrize("stage", ["intent_only", "association", "uncertain_reply"])
async def test_submission_reconciliation_never_blindly_resubmits(tmp_path, monkeypatch, stage):
    business, source, config, api = await ready(tmp_path, monkeypatch)
    identifier = uuid4()
    if stage == "intent_only":
        await business.prepare_intent(source, identifier)
    elif stage == "association":
        original = BusinessStore.associate_job

        async def fail(*args):
            raise OSError("fake-private-path-token")

        monkeypatch.setattr(BusinessStore, "associate_job", fail)
        assert "error" in (await commands.submit(tmp_path, config, refresh_id=identifier))[0]
        monkeypatch.setattr(BusinessStore, "associate_job", original)
    else:
        original = SQLiteStore.submit

        async def uncertain(self, *args):
            job = await original(self, *args)
            raise SubmissionUncertainError(job.id)

        monkeypatch.setattr(SQLiteStore, "submit", uncertain)
    repeated = await commands.submit(tmp_path, config, refresh_id=identifier)
    result = await commands.reconcile(tmp_path, identifier)
    async with SQLiteStore(DatabaseConfig(database_path=tmp_path / "tasks.sqlite")) as core:
        jobs = await TaskForge(core).list()
    assert len(jobs) == (0 if stage == "intent_only" else 1)
    assert repeated == result
    assert result[0]["status"] == (
        "pending_reconciliation" if stage == "intent_only" else "associated"
    )


@pytest.mark.parametrize("query", ["get", "list"])
async def test_query_failure_keeps_intent_no_second_job(tmp_path, monkeypatch, query):
    business, source, config, api = await ready(tmp_path, monkeypatch)
    identifier = uuid4()
    if query == "get":
        original_submit = SQLiteStore.submit

        async def uncertain(self, *args):
            job = await original_submit(self, *args)
            raise SubmissionUncertainError(job.id)

        monkeypatch.setattr(SQLiteStore, "submit", uncertain)
    else:
        await business.prepare_intent(source, identifier)
    original = getattr(TaskForge, query)

    async def fail(*args, **kwargs):
        raise RuntimeError("controlled query failure")

    monkeypatch.setattr(TaskForge, query, fail)
    result = await commands.submit(tmp_path, config, refresh_id=identifier)
    assert "error" in result[0]
    assert (await business.intent(str(identifier))).job_id is None
    monkeypatch.setattr(TaskForge, query, original)
    await commands.submit(tmp_path, config, refresh_id=identifier)
    async with SQLiteStore(DatabaseConfig(database_path=tmp_path / "tasks.sqlite")) as core:
        assert len(await TaskForge(core).list()) == (1 if query == "get" else 0)


@pytest.mark.parametrize(
    "conflict", ["source_generation", "operation", "source", "state_directory", "duplicate"]
)
async def test_reconcile_checks_complete_input_and_ambiguity(tmp_path, monkeypatch, conflict):
    business, source, config, api = await ready(tmp_path, monkeypatch)
    data, _ = await business.prepare_intent(source, uuid4())
    updates = {
        "source_generation": 2,
        "operation": "rebaseline",
        "source": source.model_copy(update={"repository": "a/c"}),
        "state_directory": tmp_path / "elsewhere",
    }
    async with SQLiteStore(DatabaseConfig(database_path=tmp_path / "tasks.sqlite")) as core:
        public = TaskForge(core, tasks.registry)
        if conflict == "duplicate":
            await public.submit("github.refresh", data)
            await public.submit("github.refresh", data)
        else:
            await public.submit(
                "github.refresh", data.model_copy(update={conflict: updates[conflict]})
            )
    with pytest.raises(StateError):
        await commands.reconcile(tmp_path, data.refresh_id)
    assert (await business.intent(str(data.refresh_id))).job_id is None


async def test_reconcile_paginates_public_jobs(tmp_path, monkeypatch):
    business, source, config, api = await ready(tmp_path, monkeypatch)
    data, _ = await business.prepare_intent(source, uuid4())
    async with SQLiteStore(DatabaseConfig(database_path=tmp_path / "tasks.sqlite")) as core:
        public = TaskForge(core, tasks.registry)
        for _ in range(101):
            await public.submit("github.refresh", data.model_copy(update={"refresh_id": uuid4()}))
        job = await public.submit("github.refresh", data)
    assert (await commands.reconcile(tmp_path, data.refresh_id))[0]["job_id"] == job.id


@pytest.mark.parametrize("kind", ["project", "issues"])
async def test_export_failure_after_commit_recovers_without_network(tmp_path, monkeypatch, kind):
    business, source, config, api = await ready(tmp_path, monkeypatch, kind)
    result = (await commands.submit(tmp_path, config))[0]
    real_export = tasks.export_source_locked

    async def fail(*args, **kwargs):
        raise OSError("private path " + str(tmp_path) + PROJECT_TOKEN)

    monkeypatch.setattr(tasks, "export_source_locked", fail)
    job, history = await work(
        tmp_path, result["job_id"], lambda job: job.status == JobStatus.RETRY_WAIT
    )
    assert history[0].error_kind == "TASK_ERROR"
    assert PROJECT_TOKEN not in history[0].traceback
    assert await business.completed(result["refresh_id"])
    before = await business.batch(result["refresh_id"])
    calls = len(api.calls)
    monkeypatch.setattr(tasks, "export_source_locked", real_export)
    intent = await business.intent(result["refresh_id"])
    assert not await tasks.execute_refresh(
        intent.input, ExecutionIdentity(execution_id=job.id, attempt_number=2)
    )
    after = await business.batch(result["refresh_id"])
    assert len(api.calls) == calls and before["changes"] == after["changes"]
    assert (tmp_path / f"receipts/{result['refresh_id']}.json").is_file()


async def test_atomic_replace_failure_preserves_old_file_and_cleans_temporary(
    tmp_path, monkeypatch
):
    path = tmp_path / "receipt.json"
    path.write_text("old")

    def fail(*args):
        raise OSError("controlled replacement fault")

    monkeypatch.setattr(receipts.os, "replace", fail)
    with pytest.raises(OSError):
        receipts.atomic_write(path, "new")
    assert path.read_text() == "old" and not list(tmp_path.glob(".export-*"))


async def test_export_thread_repeated_cancel_holds_source_lock(tmp_path, monkeypatch):
    business, source, config, api = await ready(tmp_path, monkeypatch)
    started, release = threading.Event(), threading.Event()
    original = receipts.atomic_write

    def blocked(*args):
        started.set()
        assert release.wait(10)
        original(*args)

    monkeypatch.setattr(receipts, "atomic_write", blocked)
    exporting = asyncio.create_task(commands.export(tmp_path, source.source_id))
    acquired = asyncio.Event()

    async def second():
        async with InstanceLocks(tmp_path).source(source.source_id):
            acquired.set()

    waiting = None
    try:
        assert await asyncio.to_thread(started.wait, 5)
        waiting = asyncio.create_task(second())
        for _ in range(3):
            exporting.cancel()
            await asyncio.sleep(0)
        assert not exporting.done() and not acquired.is_set()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await exporting
        if waiting is not None:
            await waiting
    assert acquired.is_set() and not list(tmp_path.rglob(".export-*"))


@pytest.mark.parametrize("scenario", receipts.SCENARIOS)
async def test_export_scenarios_do_not_change_business(tmp_path, monkeypatch, scenario):
    business, source, config, api = await ready(tmp_path, monkeypatch)
    result = (await commands.direct(tmp_path, config))[0]
    before = await business.batch(result["refresh_id"])
    await commands.export(tmp_path, scenario=scenario)
    receipt = json.loads((tmp_path / f"receipts/{result['refresh_id']}.json").read_text())
    assert receipt["scenario"] == scenario
    assert await business.batch(result["refresh_id"]) == before


@pytest.mark.parametrize("command", ["show", "reconcile", "export"])
async def test_unknown_identities_fail_explicitly(tmp_path, monkeypatch, command):
    await ready(tmp_path, monkeypatch)
    with pytest.raises(StateError):
        if command == "reconcile":
            await commands.reconcile(tmp_path, uuid4())
        else:
            await getattr(commands, command)(tmp_path, "unknown")


async def test_entity_and_version_counts_are_independent(tmp_path, monkeypatch):
    business, source, config, api = await ready(tmp_path, monkeypatch, "issues")
    first = (await commands.direct(tmp_path, config))[0]
    api.records[0]["title"] = "Edited"
    second = (await commands.direct(tmp_path, config))[0]
    batch = await business.batch(second["refresh_id"])
    assert batch["repository_counts"] == {"entities": 1, "versions": 2}
    assert batch["coverage"]["before_checkpoint"] is not None
    receipt = json.loads((tmp_path / f"receipts/{second['refresh_id']}.json").read_text())
    assert receipt["scenario"] == "natural_update"
    assert first["refresh_id"] != second["refresh_id"]


@pytest.mark.parametrize("kind", ["project", "issues"])
async def test_http_total_timeout_records_safe_failure_and_keeps_checkpoint(
    tmp_path, monkeypatch, kind
):
    business, source, config, api = await ready(tmp_path, monkeypatch, kind)
    source = source.model_copy(update={"budgets": Budgets(request_seconds=0.02)})
    data, _ = await business.prepare_intent(source, uuid4())
    original_http = tasks.GitHubHTTP

    async def block(request):
        await asyncio.Event().wait()

    real_http = GitHubHTTP
    monkeypatch.setattr(
        tasks,
        "GitHubHTTP",
        lambda *args, **kwargs: real_http(
            *args, **{**kwargs, "transport": httpx.MockTransport(block), "trust_env": False}
        ),
    )
    with pytest.raises(StateError):
        await tasks.execute_refresh(
            data, ExecutionIdentity(execution_id="timeout", attempt_number=1)
        )
    assert not await business.completed(str(data.refresh_id))
    assert (await business.source_state(source.source_id)).checkpoint is None
    assert (await business.attempts(str(data.refresh_id)))[0]["diagnostic"] == "HTTPError"
    monkeypatch.setattr(tasks, "GitHubHTTP", original_http)


async def test_worker_source_failure_isolation_and_incomplete_diagnostics(tmp_path, monkeypatch):
    business, source, config, api = await ready(tmp_path, monkeypatch)
    other = source.model_copy(update={"source_id": "broken", "repository": "a/broken"})
    config = GitHubConfig(sources=(other, source))
    await commands.initialize(tmp_path, config)
    original_handler = api.handler

    def handler(request):
        if "broken" in request.url.path:
            return httpx.Response(500)
        return original_handler(request)

    real_http = GitHubHTTP
    monkeypatch.setattr(
        tasks,
        "GitHubHTTP",
        lambda *args, **kwargs: real_http(
            *args, **{**kwargs, "transport": httpx.MockTransport(handler), "trust_env": False}
        ),
    )
    results = await commands.submit(tmp_path, config)
    successful = next(row for row in results if row["source"] == source.source_id)
    failed = next(row for row in results if row["source"] == other.source_id)
    await work(tmp_path, successful["job_id"])
    output = (await commands.show(tmp_path, source_id=other.source_id))[0]
    assert output["intents"][0]["core"]["job"]["status"] == "RETRY_WAIT"
    assert output["intents"][0]["business_attempts"][0]["diagnostic"] == "HTTPError"
    assert not output["batches"]
    assert not await business.completed(failed["refresh_id"])
    assert await business.completed(successful["refresh_id"])


async def test_task_source_lock_covers_export_and_next_checkpoint_read(tmp_path, monkeypatch):
    business, source, config, api = await ready(tmp_path, monkeypatch)
    first, _ = await business.prepare_intent(source, uuid4())
    second, _ = await business.prepare_intent(source, uuid4())
    entered, release = asyncio.Event(), asyncio.Event()
    original = tasks.export_source_locked
    calls = 0

    async def block(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(tasks, "export_source_locked", block)
    one = asyncio.create_task(
        tasks.execute_refresh(first, ExecutionIdentity(execution_id="one", attempt_number=1))
    )
    two = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        requests = len(api.calls)
        two = asyncio.create_task(
            tasks.execute_refresh(second, ExecutionIdentity(execution_id="two", attempt_number=1))
        )
        await asyncio.sleep(0)
        assert not two.done() and len(api.calls) == requests
    finally:
        release.set()
        await one
        if two is not None:
            await two
    before = (await business.batch(str(second.refresh_id)))["coverage"]["before_checkpoint"]
    assert before == (await business.batch(str(first.refresh_id)))["checkpoint"]


async def test_no_credential_before_submit_has_no_intent_or_job(tmp_path, monkeypatch):
    business, source, config, api = await ready(tmp_path, monkeypatch)
    from integrations.github.auth import AuthenticationError

    async def unavailable():
        raise AuthenticationError("No credential")

    monkeypatch.setattr(commands, "resolve_credential", unavailable)
    with pytest.raises(AuthenticationError):
        await commands.submit(tmp_path, config)
    assert not await business.intents()
    async with SQLiteStore(DatabaseConfig(database_path=tmp_path / "tasks.sqlite")) as core:
        assert not await TaskForge(core).list()


@pytest.mark.parametrize("kind", ["project", "issues"])
async def test_completed_old_generation_reexports_without_credentials(tmp_path, monkeypatch, kind):
    business, source, config, api = await ready(tmp_path, monkeypatch, kind)
    submitted = (await commands.direct(tmp_path, config))[0]
    data = (await business.intent(submitted["refresh_id"])).input
    await business.register_source(source, rebaseline=True)
    before = await business.batch(submitted["refresh_id"])
    calls = len(api.calls)

    async def forbidden():
        raise AssertionError("Completed replay must not resolve a credential")

    monkeypatch.setattr(tasks, "resolve_credential", forbidden)
    assert not await tasks.execute_refresh(
        data, ExecutionIdentity(execution_id="old-complete", attempt_number=1)
    )
    assert before["changes"] == (await business.batch(submitted["refresh_id"]))["changes"]
    assert (
        len(api.calls) == calls
        and (await business.source_state(source.source_id)).checkpoint is None
    )
    with pytest.raises(StateError, match="another input"):
        await tasks.execute_refresh(
            data.model_copy(update={"source_generation": 2}),
            ExecutionIdentity(execution_id="conflict", attempt_number=1),
        )


async def test_reconcile_explicit_job_id_and_wrong_task(tmp_path, monkeypatch):
    business, source, config, api = await ready(tmp_path, monkeypatch)
    data, _ = await business.prepare_intent(source, uuid4())
    async with SQLiteStore(DatabaseConfig(database_path=tmp_path / "tasks.sqlite")) as core:
        job = await TaskForge(core, tasks.registry).submit("github.refresh", data)
    output = await commands.reconcile(tmp_path, data.refresh_id, job.id)
    assert output[0]["job_id"] == job.id
    with pytest.raises(StateError, match="conflicts"):
        await commands.reconcile(tmp_path, data.refresh_id, str(uuid4()))
    from dataclasses import replace

    with pytest.raises(StateError, match="task"):
        commands.matching_job(replace(job, task_name="other"), data)


async def test_direct_failure_isolation_and_fresh_execution_identity(tmp_path, monkeypatch):
    business, source, config, api = await ready(tmp_path, monkeypatch)
    other = source.model_copy(update={"source_id": "broken", "repository": "a/broken"})
    config = GitHubConfig(sources=(other, source))
    await commands.initialize(tmp_path, config)
    original_handler = api.handler

    def handler(request):
        return httpx.Response(500) if "broken" in request.url.path else original_handler(request)

    monkeypatch.setattr(
        tasks,
        "GitHubHTTP",
        lambda *args, **kwargs: GitHubHTTP(
            *args, **{**kwargs, "transport": httpx.MockTransport(handler), "trust_env": False}
        ),
    )
    result = await commands.direct(tmp_path, config)
    assert result[0]["error"] == "StateError" and result[1]["committed"]
    identifier = result[1]["refresh_id"]
    from uuid import UUID

    repeated = await commands.direct(
        tmp_path, GitHubConfig(sources=(source,)), refresh_id=UUID(identifier)
    )
    assert repeated[0]["committed"] is False
    attempts = await business.attempts(identifier)
    assert len(attempts) == 2 and attempts[0]["execution_id"] != attempts[1]["execution_id"]


async def test_uncommitted_failed_job_show_is_explicit(tmp_path, monkeypatch):
    business, source, config, api = await ready(tmp_path, monkeypatch)
    submitted = (await commands.submit(tmp_path, config))[0]
    async with SQLiteStore(DatabaseConfig(database_path=tmp_path / "tasks.sqlite")) as core:
        from taskforge import AttemptOutcome, AttemptStatus, ErrorKind

        claim = await core.claim_next()
        await core.finish_attempt(
            claim.job_id,
            claim.number,
            AttemptOutcome(
                AttemptStatus.FAILED,
                ErrorKind.CONFIG_ERROR,
                "Controlled",
                "fake-token-private-path",
            ),
            status=JobStatus.FAILED,
        )
    output = await commands.show(tmp_path)
    assert output[0]["intents"][0]["core"]["job"]["status"] == "FAILED"
    assert "fake-token-private-path" not in json.dumps(output)
    assert not await business.completed(submitted["refresh_id"])


async def test_source_fingerprint_without_git_commit(tmp_path, monkeypatch):
    root = tmp_path / "source"
    module = root / "integrations/github/receipts.py"
    module.parent.mkdir(parents=True)
    module.write_text("# controlled source\n")
    (module.parent / "default-sources.json").write_text("{}")
    (root / "pyproject.toml").write_text("[project]\nname = 'controlled'\n")
    (root / "uv.lock").write_text("version = 1\n")
    monkeypatch.setattr(receipts, "__file__", str(module))
    first = receipts.source_provenance()
    assert first["git_commit"] is None
    assert str(root) not in json.dumps(first)
    module.write_text("# changed controlled source\n")
    assert first["sha256"] != receipts.source_provenance()["sha256"]
