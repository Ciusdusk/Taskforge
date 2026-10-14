"""Real G04 business/core CLI processes, persistent crash stages and signal cleanup."""

import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from integrations.github.config import GitHubConfig, Source
from integrations.github.locking import InstanceLocks
from integrations.github.storage import BusinessStore
from tests.test_github_tasks import wait_for

from taskforge import DatabaseConfig, JobStatus, SQLiteStore, TaskForge

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]
ROOT = Path(__file__).resolve().parents[1]
TOKEN = "fake-process-token"

# Instrument only the boundary under test. All SQLite, collection, Executor, Worker,
# registry loading, CLI parsing and signal handling are the production implementations.
CHILD = r"""
import asyncio, json, os, runpy, sys
from pathlib import Path
import httpx
from integrations.github import tasks, receipts
from integrations.github.storage import BusinessStore
from taskforge import TaskForge
from tests.test_github_project import API as ProjectAPI, TOKEN as PROJECT_TOKEN
from tests.test_github_issues import API as IssueAPI, TOKEN as ISSUE_TOKEN, issue

mode, kind, directory, module = sys.argv[1:5]
sys.argv = [module, *sys.argv[5:]]
directory = Path(directory)
api = ProjectAPI() if kind == "project" else IssueAPI([issue(updated_at="2026-01-02T00:00:00Z")])
os.environ["GH_TOKEN"] = PROJECT_TOKEN if kind == "project" else ISSUE_TOKEN
original_http = tasks.GitHubHTTP

class Controlled(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request):
        assert request.url.host == "api.github.com"
        with (directory / "requests.log").open("a") as log:
            log.write(request.url.path + "\n")
        if mode in {"signal", "timeout"}:
            print("http_started", flush=True)
            try:
                await asyncio.Event().wait()
            finally:
                if mode == "signal":
                    print("http_cleanup", flush=True)
                    await asyncio.to_thread(sys.stdin.buffer.read, 1)
        return api.handler(request)

tasks.GitHubHTTP = lambda *args, **kwargs: original_http(
    *args, **{**kwargs, "transport": Controlled(), "trust_env": False})

if mode == "export_crash":
    replace = receipts.os.replace
    def barrier(*args):
        print("export_ready", flush=True)
        sys.stdin.buffer.read(1)
        replace(*args)
    receipts.os.replace = barrier
elif mode == "intent_crash":
    prepare = BusinessStore.prepare_intent
    async def barrier(self, *args, **kwargs):
        result = await prepare(self, *args, **kwargs)
        print("intent_saved", flush=True)
        await asyncio.Event().wait()
        return result
    BusinessStore.prepare_intent = barrier
elif mode == "association_crash":
    submit = TaskForge.submit
    async def barrier(self, *args, **kwargs):
        result = await submit(self, *args, **kwargs)
        print("core_submitted", flush=True)
        await asyncio.Event().wait()
        return result
    TaskForge.submit = barrier
runpy.run_module(module, run_name="__main__")
"""


async def spawn(
    directory,
    *args,
    mode="normal",
    kind="project",
    module="integrations.github",
    controlled=True,
    credentials=True,
):
    env = {k: v for k, v in os.environ.items() if k not in {"GH_TOKEN", "GITHUB_TOKEN"}}
    env.update({"PYTHONPATH": str(ROOT), "NO_COLOR": "1", "GH_TOKEN": TOKEN})
    if not credentials:
        env.pop("GH_TOKEN")
        env["PATH"] = str(directory / "no-credential-tools")
    command = (
        [sys.executable, "-c", CHILD, mode, kind, str(directory), module]
        if controlled
        else [sys.executable, "-m", module]
    )
    return await asyncio.create_subprocess_exec(
        *command,
        *args,
        cwd=ROOT,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def reap(process):
    if process.returncode is None:
        process.kill()
    await process.communicate()


async def line(process, expected):
    value = await asyncio.wait_for(process.stdout.readline(), 10)
    assert value.decode().strip() == expected


async def invoke(directory, *args, **kwargs):
    process = await spawn(directory, *args, **kwargs)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 15)
        return process.returncode, stdout.decode(), stderr.decode()
    finally:
        await reap(process)


def worker_args(directory):
    return (
        "worker",
        "--database",
        str(directory / "tasks.sqlite"),
        "--registry",
        "integrations.github.tasks:registry",
        "--poll-interval-seconds",
        "0.02",
        "--shutdown-grace-seconds",
        "0",
    )


async def settings(tmp_path, kind="project"):
    source = Source(source_id="sample", kind=kind, repository="a/b")
    config = GitHubConfig(sources=(source,))
    path = tmp_path / "sources.json"
    path.write_text(config.model_dump_json())
    code, stdout, stderr = await invoke(
        tmp_path,
        "init",
        "--state-directory",
        str(tmp_path),
        "--config",
        str(path),
        controlled=False,
    )
    assert code == 0 and not stderr
    assert json.loads(stdout)["initialized"]
    return source, config, path


@pytest.mark.parametrize("kind", ["project", "issues"])
async def test_six_commands_and_actual_core_cli_registry(tmp_path, kind):
    source, config, path = await settings(tmp_path, kind)
    identifier = uuid4()
    args = (
        "--state-directory",
        str(tmp_path),
        "--config",
        str(path),
        "--refresh-id",
        str(identifier),
    )
    code, stdout, stderr = await invoke(tmp_path, "submit", *args, controlled=False)
    assert code == 0 and not stderr
    job_id = json.loads(stdout)[0]["job_id"]
    code, repeated, _ = await invoke(tmp_path, "submit", *args, controlled=False)
    assert code == 0 and json.loads(repeated)[0]["job_id"] == job_id
    worker = await spawn(tmp_path, *worker_args(tmp_path), module="taskforge.cli", kind=kind)
    try:
        async with SQLiteStore(DatabaseConfig(database_path=tmp_path / "tasks.sqlite")) as core:
            job = await wait_for(
                lambda: TaskForge(core).get(job_id), lambda j: j.status == JobStatus.SUCCESS
            )
        worker.send_signal(signal.SIGTERM)
        output, error = await asyncio.wait_for(worker.communicate(), 10)
        assert worker.returncode == 0 and b"attempt_finished" in error
        assert job.attempt_count == 1
    finally:
        await reap(worker)
    for command in ("reconcile", "show", "export"):
        code, stdout, stderr = await invoke(
            tmp_path, command, "--state-directory", str(tmp_path), controlled=False
        )
        assert code == 0 and not stderr
        assert str(tmp_path) not in stdout and TOKEN not in stdout
    code, stdout, stderr = await invoke(
        tmp_path, "direct", "--state-directory", str(tmp_path), "--config", str(path), kind=kind
    )
    assert code == 0 and json.loads(stdout)[0]["committed"]
    assert len(await BusinessStore(tmp_path).batch_ids(source.source_id)) == 2


@pytest.mark.parametrize("stage", ["intent_crash", "association_crash"])
async def test_real_submit_exit_and_public_reconciliation(tmp_path, stage):
    source, config, path = await settings(tmp_path)
    identifier = uuid4()
    process = await spawn(
        tmp_path,
        "submit",
        "--state-directory",
        str(tmp_path),
        "--config",
        str(path),
        "--refresh-id",
        str(identifier),
        mode=stage,
    )
    try:
        await line(process, "intent_saved" if stage == "intent_crash" else "core_submitted")
        process.kill()
        await process.communicate()
        assert process.returncode == -signal.SIGKILL
    finally:
        await reap(process)
    code, stdout, stderr = await invoke(
        tmp_path,
        "reconcile",
        "--state-directory",
        str(tmp_path),
        "--refresh-id",
        str(identifier),
        controlled=False,
    )
    assert code == (1 if stage == "intent_crash" else 0)
    output = json.loads(stdout)[0]
    assert output["status"] == (
        "pending_reconciliation" if stage == "intent_crash" else "associated"
    )
    code, repeated, _ = await invoke(
        tmp_path,
        "submit",
        "--state-directory",
        str(tmp_path),
        "--config",
        str(path),
        "--refresh-id",
        str(identifier),
        controlled=False,
    )
    assert code == (1 if stage == "intent_crash" else 0)
    async with SQLiteStore(DatabaseConfig(database_path=tmp_path / "tasks.sqlite")) as core:
        assert len(await TaskForge(core).list()) == (0 if stage == "intent_crash" else 1)


@pytest.mark.parametrize("kind", ["project", "issues"])
async def test_real_sigkill_after_business_commit_before_success_recovers_same_job(tmp_path, kind):
    source, config, path = await settings(tmp_path, kind)
    code, stdout, _ = await invoke(
        tmp_path,
        "submit",
        "--state-directory",
        str(tmp_path),
        "--config",
        str(path),
        controlled=False,
    )
    submitted = json.loads(stdout)[0]
    business = BusinessStore(tmp_path)
    receipt_path = tmp_path / f"receipts/{submitted['refresh_id']}.json"
    receipt_path.parent.mkdir()
    receipt_path.write_text("old complete output")
    worker = await spawn(
        tmp_path, *worker_args(tmp_path), module="taskforge.cli", mode="export_crash", kind=kind
    )
    try:
        await line(worker, "export_ready")
        assert await business.completed(submitted["refresh_id"])
        before = await business.batch(submitted["refresh_id"])
        requests = (tmp_path / "requests.log").read_text()
        assert before["repository_counts"]["entities"] > 0
        assert receipt_path.read_text() == "old complete output"
        worker.kill()
        await worker.communicate()
        assert worker.returncode == -signal.SIGKILL
    finally:
        await reap(worker)
    recovered = await spawn(tmp_path, *worker_args(tmp_path), module="taskforge.cli", kind=kind)
    try:
        async with SQLiteStore(DatabaseConfig(database_path=tmp_path / "tasks.sqlite")) as core:
            job = await wait_for(
                lambda: TaskForge(core).get(submitted["job_id"]),
                lambda j: j.status == JobStatus.SUCCESS,
            )
            history = await TaskForge(core).history(job.id)
        assert job.attempt_count == 2
        assert [a.status for a in history] == ["INTERRUPTED", "SUCCESS"]
        after = await business.batch(submitted["refresh_id"])
        assert before["changes"] == after["changes"]
        assert (
            before["coverage"] == after["coverage"]
            and before["repository_counts"] == after["repository_counts"]
        )
        assert (tmp_path / "requests.log").read_text() == requests
        assert (
            json.loads(receipt_path.read_text())["batch"]["refresh_id"] == submitted["refresh_id"]
        )
        assert not list(tmp_path.rglob(".export-*"))
        recovered.send_signal(signal.SIGTERM)
        await asyncio.wait_for(recovered.communicate(), 10)
        assert recovered.returncode == 0
    finally:
        await reap(recovered)


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
async def test_real_worker_repeated_signals_wait_for_http_cleanup(tmp_path, sig):
    source, config, path = await settings(tmp_path)
    code, stdout, _ = await invoke(
        tmp_path,
        "submit",
        "--state-directory",
        str(tmp_path),
        "--config",
        str(path),
        controlled=False,
    )
    submitted = json.loads(stdout)[0]
    worker = await spawn(tmp_path, *worker_args(tmp_path), module="taskforge.cli", mode="signal")
    try:
        await line(worker, "http_started")
        worker.send_signal(sig)
        await line(worker, "http_cleanup")
        worker.send_signal(sig)
        worker.send_signal(sig)
        from integrations.github.errors import LockError

        with pytest.raises(LockError):
            async with InstanceLocks(tmp_path).source(source.source_id):
                pass
        async with SQLiteStore(DatabaseConfig(database_path=tmp_path / "tasks.sqlite")) as core:
            assert (await TaskForge(core).get(submitted["job_id"])).status == JobStatus.RUNNING
        worker.stdin.write(b"x")
        await worker.stdin.drain()
        await asyncio.wait_for(worker.communicate(), 10)
        assert worker.returncode == 0
        async with InstanceLocks(tmp_path).source(source.source_id):
            pass
        async with SQLiteStore(DatabaseConfig(database_path=tmp_path / "tasks.sqlite")) as core:
            history = await TaskForge(core).history(submitted["job_id"])
            assert history[0].status == "INTERRUPTED"
    finally:
        await reap(worker)


async def test_cli_input_unknown_source_and_missing_credentials_fail(tmp_path):
    source, config, path = await settings(tmp_path)
    path.write_text('{"sources": [], "token": "fake-secret"}')
    code, stdout, stderr = await invoke(
        tmp_path,
        "init",
        "--state-directory",
        str(tmp_path),
        "--config",
        str(path),
        controlled=False,
    )
    assert code == 1 and "fake-secret" not in stdout + stderr and str(tmp_path) not in stderr
    for command in ("show", "export"):
        code, stdout, stderr = await invoke(
            tmp_path,
            command,
            "--state-directory",
            str(tmp_path),
            "--source",
            "unknown",
            controlled=False,
        )
        assert code == 1
    path.write_text(config.model_dump_json())
    for command in ("submit", "direct"):
        code, stdout, stderr = await invoke(
            tmp_path,
            command,
            "--state-directory",
            str(tmp_path),
            "--config",
            str(path),
            controlled=False,
            credentials=False,
        )
        assert code == 1 and "AuthenticationError" in stderr
        assert not await BusinessStore(tmp_path).intents()
    async with SQLiteStore(DatabaseConfig(database_path=tmp_path / "tasks.sqlite")) as core:
        assert not await TaskForge(core).list()
