"""Explicit G05 live acceptance; never imported by the production registry or pytest."""

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from integrations.github import commands
from integrations.github.auth import resolve_credential
from integrations.github.config import GitHubConfig, Source
from integrations.github.http import API_ROOT, API_VERSION, AttemptBudget, GitHubHTTP, next_link
from integrations.github.locking import InstanceLocks
from integrations.github.models import BatchWrite, canonical
from integrations.github.project import ExecutionIdentity
from integrations.github.receipts import export_source_locked, source_provenance
from integrations.github.remote import CommitRecord, IssueRecord, ReleaseRecord, Repository, parse
from integrations.github.skills import collect_skills
from integrations.github.storage import BusinessStore
from taskforge import DatabaseConfig, JobStatus, SQLiteStore, TaskForge

ROOT = Path(__file__).resolve().parents[2]


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def save(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


async def wait_jobs(
    directory: Path, identifiers: list[str], process: asyncio.subprocess.Process
) -> None:
    async with SQLiteStore(DatabaseConfig(database_path=directory / "tasks.sqlite")) as core:
        api = TaskForge(core)
        async with asyncio.timeout(1100):
            while True:
                jobs = [await api.get(identifier) for identifier in identifiers]
                if all(job.status in (JobStatus.SUCCESS, JobStatus.FAILED) for job in jobs):
                    if any(job.status != JobStatus.SUCCESS for job in jobs):
                        raise RuntimeError("Live Job failed; inspect show and preserved receipts")
                    return
                if process.returncode is not None:
                    raise RuntimeError("Worker exited before Jobs completed")
                await asyncio.sleep(0.2)


async def live(directory: Path, output: Path, scenario: str) -> None:
    """One fresh UUID per configured source, formal submit and actual core CLI Worker."""
    started_at = utc_now()
    started = time.monotonic()
    config = GitHubConfig.load()
    await resolve_credential()
    await commands.initialize(directory, config)
    results = await commands.submit(directory, config)
    save(output / "submissions.json", {"utc": started_at, "results": results})
    identifiers = [str(item["job_id"]) for item in results if item.get("job_id")]
    if len(identifiers) != len(config.sources):
        raise RuntimeError("Not every submission is associated")
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    worker_command = [
        sys.executable,
        "-m",
        "taskforge.cli",
        "worker",
        "--database",
        str(directory / "tasks.sqlite"),
        "--registry",
        "integrations.github.tasks:registry",
    ]
    process = await asyncio.create_subprocess_exec(
        *worker_command,
        cwd=ROOT,
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await wait_jobs(directory, identifiers, process)
        process.send_signal(signal.SIGTERM)
        await asyncio.wait_for(process.wait(), 60)
        if process.returncode != 0:
            raise RuntimeError("Worker shutdown failed")
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
        await commands.export(directory)
    store = BusinessStore(directory)
    for item in results:
        identifier = str(item["refresh_id"])
        shutil.copyfile(
            directory / "receipts" / (identifier + ".json"), output / (identifier + ".json")
        )
    (output / "indexes").mkdir(exist_ok=True)
    for path in (directory / "indexes").glob("*.md"):
        # Snapshots retain links valid within their scenario directory.
        text = path.read_text().replace("../receipts/", "../")
        current_ids = {str(item["refresh_id"]) for item in results}
        text = (
            "\n".join(
                line
                for line in text.splitlines()
                if not line.startswith("| [")
                or any(identifier in line for identifier in current_ids)
            )
            + "\n"
        )
        (output / "indexes" / path.name).write_text(text)
    save(
        output / "run.json",
        {
            "started_at": started_at,
            "finished_at": utc_now(),
            "scenario": scenario,
            "exit_code": 0,
            "worker_exit_code": process.returncode,
            "total_seconds": time.monotonic() - started,
            "credential_available": True,
            "api_version": API_VERSION,
            "configuration_sha256": digest(config.model_dump(mode="json")),
            "source": source_provenance(),
            "commands": [
                "commands.initialize(default config)",
                "commands.submit(default config)",
                "python -m taskforge.cli worker --database <state>/tasks.sqlite "
                "--registry integrations.github.tasks:registry",
                "commands.export",
            ],
            "complete_batches": [
                await store.completed(str(item["refresh_id"])) for item in results
            ],
        },
    )
    print(f"{scenario}: seven successful Jobs and terminal receipts", flush=True)


async def observed_issue_payloads(
    http: GitHubHTTP, store: BusinessStore, source_id: str
) -> dict[tuple[str, str], dict[str, Any]]:
    """Match saved fields to actual collection payloads, including mutable remote records."""
    observations = {}
    for identifier in await store.batch_ids(source_id):
        batch = await store.batch(identifier)
        coverage = batch["coverage"]
        checkpoint = batch["checkpoint"]
        assert isinstance(coverage, dict) and isinstance(checkpoint, dict)
        issues = coverage["issues"]
        assert isinstance(issues, dict)
        params = {"state": "all", "sort": "updated", "direction": "desc"}
        since = issues["since"]
        if isinstance(since, str):
            params["since"] = since
        params.update(per_page="100", page="1")
        url: str | None = str(
            httpx.URL(API_ROOT + f"/repos/{checkpoint['repository']}/issues").copy_with(
                params=params
            )
        )
        seen = set()
        while url is not None and url not in seen:
            seen.add(url)
            value = await store.cache_get(http.request_key(url))
            if value is None:
                # A finite baseline can stop while its last page still has a next Link.
                break
            assert isinstance(value.body, list)
            for raw in value.body:
                if not isinstance(raw, dict) or "pull_request" in raw:
                    continue
                content = parse(IssueRecord, raw).content()
                if str(content["updated_at"]) <= str(issues["upper_bound"]):
                    fingerprint = digest(content)
                    observations[str(content["id"]), fingerprint] = {
                        "sha256": fingerprint,
                        "upper_bound": issues["upper_bound"],
                        "updated_at": content["updated_at"],
                        "request_url": url,
                        "verified": True,
                    }
            url = next_link(value.link)
    return observations


async def audit(directory: Path, output: Path) -> None:
    """Independent live reads of saved fields, native trees and complete Release identities."""
    credential = await resolve_credential()
    checks: list[dict[str, Any]] = (
        json.loads(await asyncio.to_thread(output.read_text))["sources"]
        if await asyncio.to_thread(output.exists)
        else []
    )
    for source in GitHubConfig.load().sources:
        store = BusinessStore(directory)
        state = await store.source_state(source.source_id)
        if state.checkpoint is None:
            raise RuntimeError("Missing live checkpoint")
        repository_id = state.checkpoint["repository_id"]
        assert isinstance(repository_id, int)
        if any(
            check["source_id"] == source.source_id and check["checkpoint"] == state.checkpoint
            for check in checks
        ):
            continue
        checks = [check for check in checks if check["source_id"] != source.source_id]
        audit_store = BusinessStore(directory / "audit" / source.source_id)
        await audit_store.initialize()
        records: list[dict[str, Any]] = []
        audit_limits = source.budgets.model_copy(update={"max_requests": 200})
        async with GitHubHTTP(audit_store, credential, AttemptBudget(audit_limits)) as http:
            repository = parse(Repository, (await http.get("/repos/" + source.repository)).body)
            if repository.id != repository_id:
                raise RuntimeError("Remote repository identity mismatch")
            root = "/repos/" + repository.full_name
            observations = (
                await observed_issue_payloads(http, store, source.source_id)
                if source.kind == "issues"
                else {}
            )
            kinds = ("issue",) if source.kind == "issues" else ("commit", "release")
            for kind in kinds:
                for entity in await store.entities(repository_id, kind):
                    if kind == "commit":
                        remote = parse(
                            CommitRecord,
                            (await http.get(root + "/commits/" + entity.remote_id)).body,
                        )
                        content = remote.model_dump(mode="json")
                    elif kind == "release":
                        remote_release = parse(
                            ReleaseRecord,
                            (await http.get(root + "/releases/" + entity.remote_id)).body,
                        )
                        content = remote_release.model_dump(mode="json")
                    else:
                        value = (
                            await http.get(root + "/issues/" + str(entity.content["number"]))
                        ).body
                        if not isinstance(value, dict) or "pull_request" in value:
                            raise RuntimeError("Saved Issue is a PR or invalid")
                        content = parse(IssueRecord, value).content()
                        if (
                            str(content["id"]) != entity.remote_id
                            or content["number"] != entity.content["number"]
                        ):
                            raise RuntimeError("Issue identity mismatch")
                    equal = digest(content) == entity.fingerprint
                    # Mutable Issue records can move after collection; record that separately.
                    if not equal and (
                        kind != "issue" or str(content["updated_at"]) <= str(entity.updated_at)
                    ):
                        raise RuntimeError("Saved remote content mismatch")
                    records.append(
                        {
                            "kind": kind,
                            "remote_id": entity.remote_id,
                            "url": content["html_url"],
                            "saved_sha256": entity.fingerprint,
                            "remote_sha256": digest(content),
                            "fields": sorted(content),
                            "result": "equal" if equal else "updated_after_collection",
                            "saved_updated_at": entity.updated_at,
                            "remote_updated_at": content.get("updated_at"),
                        }
                    )
                    if kind == "issue":
                        observation = observations.get((entity.remote_id, entity.fingerprint))
                        if observation is None:
                            raise RuntimeError("Saved Issue has no matching collection payload")
                        records[-1]["collection_payload"] = observation
            skills = None
            releases = None
            if source.kind == "project":
                if repository.default_branch != state.checkpoint["default_branch"]:
                    raise RuntimeError("Default branch changed")
                sha = str(state.checkpoint["sha"])
                target = parse(CommitRecord, (await http.get(root + "/commits/" + sha)).body)
                if target.sha != sha:
                    raise RuntimeError("Fixed Commit mismatch")
                release_values = await http.paginate(root + "/releases")
                visible_ids = sorted(
                    str(value["id"])
                    for value in release_values
                    if isinstance(value, dict) and not value["draft"]
                )
                if visible_ids != sorted(await store.release_ids(repository_id)):
                    raise RuntimeError("Release identity set changed")
                releases = {"ids": visible_ids, "complete": True}
                if source.scan_skills:
                    snapshot, _, coverage = await collect_skills(
                        http, repository.full_name, repository_id, sha, target.commit.tree.sha, None
                    )
                    saved = await store.skill_snapshot(repository_id, sha)
                    if saved != snapshot:
                        raise RuntimeError("Native SKILL.md manifest mismatch")
                    skills = {"files": snapshot.files, "coverage": coverage}
            checks.append(
                {
                    "source_id": source.source_id,
                    "repository": repository.full_name,
                    "repository_id": repository_id,
                    "default_branch": repository.default_branch,
                    "checkpoint": state.checkpoint,
                    "utc": utc_now(),
                    "permission": "authenticated repository read succeeded",
                    "requests": http.budget.requests,
                    "audit_budget": audit_limits.model_dump(mode="json"),
                    "audit_budget_sha256": digest(audit_limits.model_dump(mode="json")),
                    "budget_reason": (
                        "100 per-Issue reads plus metadata; production budgets unchanged"
                    ),
                    "records": records,
                    "releases": releases,
                    "skills": skills,
                }
            )
        save(output, {"api_version": API_VERSION, "sources": checks, "complete": len(checks) == 7})
        print(f"audited {source.source_id}: {len(records)} saved contents", flush=True)


async def history(directory: Path, output: Path, source: Source, shas: list[str]) -> None:
    """Replay fixed native trees through collect_skills in an isolated database."""
    store = BusinessStore(directory)
    await store.initialize()
    await store.register_source(source)
    credential = await resolve_credential()
    previous = None
    stages = []
    for sha in shas:
        started = time.monotonic()
        data, _ = await store.prepare_intent(source, uuid4())
        async with InstanceLocks(directory).source(source.source_id):
            async with GitHubHTTP(store, credential, AttemptBudget(source.budgets)) as http:
                repository = parse(Repository, (await http.get("/repos/" + source.repository)).body)
                record = parse(
                    CommitRecord,
                    (await http.get("/repos/" + repository.full_name + "/commits/" + sha)).body,
                )
                if record.sha != sha:
                    raise RuntimeError("History SHA did not resolve exactly")
                snapshot, changes, coverage = await collect_skills(
                    http, repository.full_name, repository.id, sha, record.commit.tree.sha, previous
                )
                before = (await store.source_state(source.source_id)).checkpoint
                batch = BatchWrite(
                    input=data,
                    checkpoint={
                        "repository_id": repository.id,
                        "repository": repository.full_name,
                        "sha": sha,
                    },
                    coverage={
                        "complete": True,
                        "observed_at": utc_now(),
                        "before_checkpoint": before,
                        "source": source_provenance(),
                        "requests": http.budget.requests,
                        "execution": ExecutionIdentity(
                            execution_id="history-" + str(data.refresh_id), attempt_number=1
                        ).model_dump(mode="json"),
                        "skills": coverage,
                        "elapsed_seconds": time.monotonic() - started,
                    },
                    complete=True,
                    snapshots=(snapshot,),
                    skill_changes=changes,
                )
                await store.apply(batch)
                paths = await export_source_locked(store, source.source_id, "historical_replay")
                stages.append(
                    {
                        "sha": sha,
                        "tree_sha": record.commit.tree.sha,
                        "commit_url": record.html_url,
                        "parents": [parent.sha for parent in record.parents],
                        "files": snapshot.files,
                        "changes": [c.model_dump(mode="json") for c in changes],
                        "coverage": coverage,
                        "receipt": "receipts/" + str(data.refresh_id) + ".json",
                    }
                )
                previous = snapshot
            for path in paths:
                target = output / path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(directory / path, target)
        save(
            output / "history.json",
            {
                "source": source.model_dump(mode="json"),
                "stages": stages,
                "utc": utc_now(),
                "selection": (
                    "fixed real native Commit/tree identities; "
                    "explicit repeated SHA proves unchanged path"
                ),
            },
        )
        print(
            f"history {source.source_id}: {sha} "
            f"{len(snapshot.files)} definitions, {len(changes)} changes",
            flush=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("live", "audit", "history"))
    parser.add_argument("--state-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--scenario", choices=("initialization", "natural_update"), default="initialization"
    )
    parser.add_argument("--source")
    parser.add_argument("--sha", action="append", default=[])
    args = parser.parse_args()
    if args.mode == "live":
        operation = live(args.state_directory, args.output, args.scenario)
    elif args.mode == "audit":
        operation = audit(args.state_directory, args.output)
    else:
        sources = commands.select_sources(GitHubConfig.load(), args.source)
        if len(sources) != 1 or not sources[0].scan_skills or not args.sha:
            parser.error("history requires one skill source and fixed --sha values")
        operation = history(args.state_directory, args.output, sources[0], args.sha)
    asyncio.run(operation)


if __name__ == "__main__":
    main()
