"""G05 paired offline comparison through the production business and CLI boundaries."""

import argparse
import asyncio
import json
import os
import runpy
import shutil
import signal
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import quote
from uuid import uuid4

import httpx

from integrations.github import commands, receipts, tasks
from integrations.github.acceptance import ROOT, digest, save, utc_now, wait_jobs
from integrations.github.auth import resolve_credential
from integrations.github.config import GitHubConfig
from integrations.github.http import ACCEPT, API_ROOT, API_VERSION, AttemptBudget, GitHubHTTP
from integrations.github.remote import CommitRecord, ReleaseRecord, Repository, Tree, parse
from integrations.github.storage import BusinessStore
from taskforge import DatabaseConfig, SQLiteStore, TaskForge

FAKE_TOKEN = "g05-offline-controlled-credential"


async def capture(directory: Path, output: Path) -> None:
    """Freeze a small real project's relevant fields, removing author names/emails."""
    source = GitHubConfig.load().sources[0]
    store = BusinessStore(directory)
    await store.initialize()
    responses: dict[str, object] = {}
    async with GitHubHTTP(store, await resolve_credential(), AttemptBudget(source.budgets)) as http:
        repository_path = "/repos/" + source.repository
        repository = parse(Repository, (await http.get(repository_path)).body)
        responses[repository_path] = repository.model_dump(mode="json")
        branch_path = repository_path + "/commits/" + quote(repository.default_branch, safe="")
        target = parse(CommitRecord, (await http.get(branch_path)).body)

        def commit_fields(record: CommitRecord) -> dict[str, Any]:
            value = record.model_dump(mode="json")
            for field in ("author", "committer"):
                value["commit"][field] = {"date": value["commit"][field].get("date")}
            return value

        responses[branch_path] = commit_fields(target)
        path = repository_path + f"/commits?sha={target.sha}&per_page=20&page=1"
        value = await http.get(path)
        assert isinstance(value.body, list)
        responses[path] = [commit_fields(parse(CommitRecord, item)) for item in value.body]
        path = repository_path + "/releases?per_page=100&page=1"
        value = await http.get(path)
        if value.link:
            raise RuntimeError("Comparison fixture requires a single complete Release page")
        assert isinstance(value.body, list)
        responses[path] = [
            {**parse(ReleaseRecord, item).model_dump(mode="json"), "draft": False}
            for item in value.body
            if isinstance(item, dict) and not item["draft"]
        ]
        path = repository_path + "/git/trees/" + target.commit.tree.sha + "?recursive=1"
        tree = parse(Tree, (await http.get(path)).body)
        if tree.truncated:
            raise RuntimeError("Comparison fixture requires a complete native recursive tree")
        responses[path] = tree.model_dump(mode="json")
    save(
        output,
        {
            "source": source.model_dump(mode="json"),
            "captured_at": utc_now(),
            "fixed_sha": target.sha,
            "api_version": API_VERSION,
            "responses": responses,
            "reduction": "relevant remote fields only; author/committer names and emails omitted",
            "response_sha256": digest(responses),
        },
    )


class FrozenTransport(httpx.AsyncBaseTransport):
    def __init__(self, fixture: dict[str, Any], log: Path) -> None:
        self.responses = fixture["responses"]
        self.log = log

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if (
            str(request.url).split("/", 3)[:3] != API_ROOT.split("/")
            or request.headers["accept"] != ACCEPT
            or request.headers["x-github-api-version"] != API_VERSION
            or request.headers["authorization"] != "Bearer " + FAKE_TOKEN
        ):
            raise RuntimeError("Unexpected offline request contract")
        key = request.url.raw_path.decode()
        # Parameter ordering has no semantic effect on the frozen input identity.
        matches = [
            value
            for path, value in self.responses.items()
            if httpx.URL(API_ROOT + path).path == request.url.path
            and dict(httpx.URL(API_ROOT + path).params) == dict(request.url.params)
        ]
        if len(matches) != 1:
            raise RuntimeError("Request is outside the fixed fixture")
        with self.log.open("a") as file:
            file.write(json.dumps({"utc": utc_now(), "path": key, "status": 200}) + "\n")
        return httpx.Response(200, json=matches[0])


def child(mode: str, directory: Path, fixture_path: Path, command: list[str]) -> None:
    fixture = json.loads(fixture_path.read_text())
    original = GitHubHTTP

    def controlled(*args: Any, **kwargs: Any) -> GitHubHTTP:
        return original(
            *args,
            **{
                **kwargs,
                "transport": FrozenTransport(fixture, directory / "requests.jsonl"),
                "trust_env": False,
            },
        )

    instrumentation = ExitStack()
    instrumentation.enter_context(patch.object(tasks, "GitHubHTTP", controlled))
    if mode == "fault":
        replace = os.replace

        def barrier(*args: Any) -> None:
            print("business_committed_before_export_replace", flush=True)
            sys.stdin.buffer.read(1)
            replace(*args)

        instrumentation.enter_context(patch.object(os, "replace", barrier))
    module = "taskforge.cli" if command[0] == "worker" else "integrations.github"
    sys.argv = [module, *command]
    with instrumentation:
        runpy.run_module(module, run_name="__main__")


async def spawn(
    directory: Path, fixture: Path, mode: str, command: list[str]
) -> asyncio.subprocess.Process:
    env = {
        key: value for key, value in os.environ.items() if key not in ("GH_TOKEN", "GITHUB_TOKEN")
    }
    env.update(GH_TOKEN=FAKE_TOKEN, PYTHONPATH=str(ROOT), NO_COLOR="1")
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "integrations.github.comparison",
        "child",
        mode,
        str(directory),
        str(fixture),
        *command,
        cwd=ROOT,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )


async def reap(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        process.kill()
    await process.communicate()


async def one(directory: Path, fixture_path: Path, framework: bool, fault: bool) -> dict[str, Any]:
    fixture = json.loads(await asyncio.to_thread(fixture_path.read_text))
    config = GitHubConfig.model_validate({"sources": [fixture["source"]]})
    await commands.initialize(directory, config)
    config_path = directory / "sources.json"
    save(config_path, config.model_dump(mode="json"))
    refresh_id = uuid4()
    source = config.sources[0]
    started_at = utc_now()
    started = time.monotonic()
    job_id = None
    if framework:
        # submit uses the real public core API; this explicit experiment uses a fake credential.
        saved_token = os.environ.get("GH_TOKEN")
        os.environ["GH_TOKEN"] = FAKE_TOKEN
        try:
            result = await commands.submit(directory, config, source.source_id, refresh_id)
        finally:
            if saved_token is None:
                os.environ.pop("GH_TOKEN", None)
            else:
                os.environ["GH_TOKEN"] = saved_token
        job_id = str(result[0]["job_id"])
        command = [
            "worker",
            "--database",
            str(directory / "tasks.sqlite"),
            "--registry",
            "integrations.github.tasks:registry",
        ]
    else:
        command = [
            "direct",
            "--state-directory",
            str(directory),
            "--config",
            str(config_path),
            "--source",
            source.source_id,
            "--refresh-id",
            str(refresh_id),
        ]
    processes = []
    exits = []
    crash_state = None
    recovery_started = None
    try:
        process = await spawn(directory, fixture_path, "fault" if fault else "normal", command)
        processes.append(process)
        if fault:
            assert process.stdout is not None
            async with asyncio.timeout(30):
                marker = (await process.stdout.readline()).decode().strip()
            if marker != "business_committed_before_export_replace":
                raise RuntimeError("Fault barrier was not reached")
            if not await BusinessStore(directory).completed(str(refresh_id)):
                raise RuntimeError("Business commit is missing at barrier")
            crash_state = {
                "stage": marker,
                "business_complete": True,
                "requests": len((directory / "requests.jsonl").read_text().splitlines()),
            }
            if job_id is not None:
                async with SQLiteStore(
                    DatabaseConfig(database_path=directory / "tasks.sqlite")
                ) as core:
                    job = await TaskForge(core).get(job_id)
                    if job.status.value != "RUNNING":
                        raise RuntimeError("Core is not RUNNING at fault stage")
                    crash_state["core_status"] = job.status.value
            process.kill()
            await process.communicate()
            exits.append(process.returncode)
            recovery_started = time.monotonic()
            process = await spawn(directory, fixture_path, "normal", command)
            processes.append(process)
        if framework:
            assert job_id is not None
            await wait_jobs(directory, [job_id], process)
            process.send_signal(signal.SIGTERM)
        await asyncio.wait_for(process.communicate(), 60)
        exits.append(process.returncode)
        if process.returncode != 0:
            raise RuntimeError("Comparison execution failed")
        completed_at = time.monotonic()
        await commands.export(directory, scenario="fault_experiment" if fault else None)
        store = BusinessStore(directory)
        batch = await store.batch(str(refresh_id))
        state = await store.source_state(source.source_id)
        if state.checkpoint is None:
            raise RuntimeError("Missing comparison checkpoint")
        repository_id = state.checkpoint["repository_id"]
        assert isinstance(repository_id, int)
        entities = [
            entity.model_dump(mode="json")
            for kind in ("commit", "release")
            for entity in await store.entities(repository_id, kind)
        ]
        snapshot = await store.skill_snapshot(repository_id, fixture["fixed_sha"])
        semantic = {
            "checkpoint": state.checkpoint,
            "entities": entities,
            "snapshot": None if snapshot is None else snapshot.model_dump(mode="json"),
            "changes": batch["changes"],
            "skill_changes": batch["skill_changes"],
            "counts": batch["repository_counts"],
        }
        request_lines = [
            json.loads(line) for line in (directory / "requests.jsonl").read_text().splitlines()
        ]
        receipt = json.loads((directory / "receipts" / (str(refresh_id) + ".json")).read_text())
        core = receipt["core"]
        queue = (
            0.0
            if core is None
            else (
                datetime_seconds(core["job"]["started_at"])
                - datetime_seconds(core["job"]["created_at"])
            )
        )
        attempts = batch["attempts"]
        assert isinstance(attempts, list)
        if fault and crash_state is not None and len(request_lines) != crash_state["requests"]:
            raise RuntimeError("Recovery repeated HTTP collection")
        save(directory / "semantic.json", semantic)
        return {
            "framework": framework,
            "fault": fault,
            "utc": started_at,
            "finished_at": utc_now(),
            "refresh_id": str(refresh_id),
            "job_id": job_id,
            "total_seconds": completed_at - started,
            "collection_seconds": receipt["batch"]["coverage"]["elapsed_seconds"],
            "queue_seconds": queue,
            "recovery_seconds": None
            if recovery_started is None
            else completed_at - recovery_started,
            "recovery_attempts": len(attempts) - 1,
            "process_exit_codes": exits,
            "requests": request_lines,
            "actual_requests": len(request_lines),
            "human_operations": [],
            "orchestrator_operations": (
                ["SIGKILL at barrier", "restart same command"] if fault else []
            ),
            "fault_stage": crash_state,
            "semantic_sha256": digest(semantic),
            "receipt": "receipts/" + str(refresh_id) + ".json",
            "core": core,
            "source": receipts.source_provenance(),
            "fixture_sha256": digest(fixture),
        }
    finally:
        for process in processes:
            await reap(process)


def datetime_seconds(value: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(value).timestamp()


async def compare(directory: Path, fixture: Path, output: Path) -> None:
    results = []
    protocol = {
        "utc": utc_now(),
        "fixture_sha256": digest(json.loads(await asyncio.to_thread(fixture.read_text))),
        "repetitions": 3,
        "normal_and_fault": True,
        "fault_stage": "business committed; before first atomic export replacement",
        "recovery": "same UUID; restart existing Worker Job or rerun direct command",
        "timing": (
            "from submit/direct spawn through success and process shutdown; "
            "setup and terminal re-export excluded"
        ),
        "policy": "production defaults: 2 retries, 5/60 second backoff, 300 second timeout",
        "gate": (
            "production GitHubHTTP, per-instance API flock, default budgets; fixed local transport"
        ),
        "order": (
            "alternate TaskForge/direct ordering by repetition; compare semantics before timings"
        ),
    }
    save(output / "protocol.json", protocol)
    for fault in (False, True):
        for repetition in range(1, 4):
            pair = []
            for framework in (True, False) if repetition % 2 else (False, True):
                name = (
                    f"{'fault' if fault else 'normal'}-{repetition}-"
                    f"{'taskforge' if framework else 'direct'}"
                )
                state = directory / name
                if state.exists():
                    raise RuntimeError("Use a fresh comparison state directory")
                result = await one(state, fixture, framework, fault)
                target = output / name
                shutil.copytree(state / "receipts", target / "receipts")
                shutil.copytree(state / "indexes", target / "indexes")
                shutil.copyfile(state / "semantic.json", target / "semantic.json")
                save(target / "run.json", result)
                pair.append(result)
            if pair[0]["semantic_sha256"] != pair[1]["semantic_sha256"]:
                raise RuntimeError("Paired business results differ")
            if pair[0]["actual_requests"] != pair[1]["actual_requests"]:
                raise RuntimeError("Paired request counts differ")
            results.append(
                {"fault": fault, "repetition": repetition, "equivalent": True, "runs": pair}
            )
            save(output / "comparison.json", {"protocol": protocol, "pairs": results})
            print(f"{'fault' if fault else 'normal'} pair {repetition}: equal results", flush=True)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "child":
        child(sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]), sys.argv[5:])
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("capture", "compare"))
    parser.add_argument("--state-directory", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.mode == "capture":
        asyncio.run(capture(args.state_directory, args.fixture))
    else:
        if args.output is None:
            parser.error("compare requires --output")
        asyncio.run(compare(args.state_directory, args.fixture, args.output))


if __name__ == "__main__":
    main()
