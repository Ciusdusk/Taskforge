"""Reproducible real-process acceptance; print a receipt, then remove all scratch data."""

import argparse
import asyncio
import hashlib
import json
import os
import platform
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

from python_cycle import cycle

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = Path(__file__).resolve().parent

# Acceptance-only barrier: the real task has returned and both SUCCESS SQL writes
# have flushed, but the real TaskForge completion commit has not started.
CRASH_WORKER = """
import asyncio, json, os
from pathlib import Path
from taskforge import storage
from taskforge.db import JobRow
from taskforge.cli import main
original = storage._commit
async def pause(session):
    for row in session.identity_map.values():
        if isinstance(row, JobRow) and row.status == 'SUCCESS':
            Path(os.environ['TF_BARRIER']).write_text(json.dumps({
                'job_id': row.id, 'attempt_number': row.attempt_count,
                'stage': 'success_sql_flushed_before_commit'}))
            await asyncio.Event().wait()
    await original(session)
storage._commit = pause
main()
"""


def wait_for(predicate, child, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        if child.poll() is not None:
            stdout, stderr = child.communicate()
            raise AssertionError(("child stopped before barrier", child.returncode, stdout, stderr))
        time.sleep(0.01)
    raise TimeoutError("Timed out waiting for real persisted state or process barrier")


@contextmanager
def process(command, *, directory, env):
    child = subprocess.Popen(
        list(map(str, command)),
        cwd=directory,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        yield child
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)


def rows(database, table):
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
    finally:
        connection.close()


class Commands:
    def __init__(self, directory, receipt):
        self.directory = directory
        self.database = directory / "cli.sqlite"
        self.receipt = receipt
        self.env = {**os.environ, "PYTHONPATH": str(EXAMPLES), "NO_COLOR": "1"}
        self.entry = Path(sys.executable).with_name("taskforge")

    def run(self, *args, payload=None, code=0):
        command = [str(self.entry), *map(str, args), "--database", str(self.database)]
        result = subprocess.run(
            command,
            cwd=self.directory,
            env=self.env,
            text=True,
            capture_output=True,
            input=None if payload is None else json.dumps(payload),
            timeout=20,
        )
        self.receipt["commands"].append({"argv": command, "exit_code": result.returncode})
        assert result.returncode == code, (result.returncode, result.stdout, result.stderr)
        return result.stdout.strip()

    def submit(self, task, payload):
        return self.run("submit", task, "--registry", "local_tasks:registry", payload=payload)

    def snapshot(self, job):
        return {
            "job": json.loads(self.run("show", job)),
            "attempts": json.loads(self.run("history", job))["attempts"],
        }

    def state(self, job):
        return next(row for row in rows(self.database, "jobs") if row["id"] == job)

    @contextmanager
    def worker(self, *, barrier=None):
        command = [
            self.entry,
            "worker",
            "--registry",
            "local_tasks:registry",
            "--database",
            self.database,
            "--concurrency",
            "1",
            "--poll-interval-seconds",
            "0.01",
            "--shutdown-grace-seconds",
            "0",
        ]
        env = self.env
        if barrier is not None:
            command = [sys.executable, "-c", CRASH_WORKER, *command[1:]]
            env = {**env, "TF_BARRIER": str(barrier)}
        with process(command, directory=self.directory, env=env) as child:
            yield child

    def stop(self, child, sig=signal.SIGTERM):
        child.send_signal(sig)
        stdout, stderr = child.communicate(timeout=20)
        assert child.returncode == 0, (stdout, stderr)
        return {
            "signal": sig.name,
            "exit_code": child.returncode,
            "events": [json.loads(line) for line in stderr.splitlines()],
        }

    def finish(self, job):
        with self.worker() as child:
            wait_for(lambda: self.state(job)["status"] in ("SUCCESS", "FAILED"), child)
            stopped = self.stop(child)
        return self.snapshot(job) | {"worker": stopped}


def cli_cycle(commands, port):
    directory = commands.directory
    commands.run("init")
    # Invalid input must not create a Job.
    commands.run(
        "submit",
        "local.sync",
        "--registry",
        "local_tasks:registry",
        payload={"port": 0, "target": str(directory / "invalid")},
        code=2,
    )
    assert rows(commands.database, "jobs") == []
    scratch = directory / "cli-scratch"
    scratch.mkdir()
    (scratch / "expired.tmp").write_text("expired")
    (scratch / "keep.txt").write_text("keep")
    jobs = [
        commands.submit(
            "local.sync", {"port": port, "target": str(directory / "cli.json"), "transient": True}
        ),
        commands.submit("local.backup", {"port": port, "target": str(directory / "cli.bin")}),
        commands.submit("local.maintenance", {"directory": str(scratch)}),
    ]
    assert commands.snapshot(jobs[0])["attempts"] == []
    with commands.worker() as child:
        wait_for(lambda: all(commands.state(job)["status"] == "SUCCESS" for job in jobs), child)
        stopped = commands.stop(child)
    snapshots = [commands.snapshot(job) for job in jobs]
    assert [len(item["attempts"]) for item in snapshots] == [2, 1, 1]
    failed, succeeded = snapshots[0]["attempts"]
    assert failed["error_kind"] == "TASK_ERROR" and succeeded["status"] == "SUCCESS"
    assert datetime.fromisoformat(succeeded["started_at"]) >= (
        datetime.fromisoformat(failed["finished_at"]) + timedelta(seconds=0.1)
    )
    page = json.loads(commands.run("list", "--limit", "2"))
    following = json.loads(commands.run("list", "--after", page["next_cursor"]))
    assert [job["id"] for job in page["jobs"] + following["jobs"]] == jobs
    assert json.loads((directory / "cli.json").read_text())[0]["id"] == 1
    assert (directory / "cli.bin").read_bytes() == b"TaskForge local backup fixture\n"
    assert sorted(path.name for path in scratch.iterdir()) == ["keep.txt"]
    return {"snapshots": snapshots, "worker": stopped, "pagination_ids": jobs}


def failures(commands, port):
    directory = commands.directory
    job = commands.submit(
        "local.sync", {"port": port, "target": str(directory / "missing" / "records.json")}
    )
    failure = commands.finish(job)
    assert failure["job"]["status"] == "FAILED" and len(failure["attempts"]) == 2
    assert all(item["error_kind"] == "TASK_ERROR" for item in failure["attempts"])
    timeout = commands.submit(
        "local.timeout",
        {
            "started": str(directory / "timeout.started"),
            "cleaned": str(directory / "timeout.cleaned"),
        },
    )
    expired = commands.finish(timeout)
    assert expired["job"]["status"] == "FAILED" and len(expired["attempts"]) == 2
    assert all(item["error_kind"] == "TIMEOUT" for item in expired["attempts"])
    assert (directory / "timeout.cleaned").read_text() == "2"
    return {"business_failure": failure, "timeout": expired, "timeout_cleanup_attempt": 2}


def shutdown(commands):
    directory = commands.directory
    started, cleaned = directory / "stop.started", directory / "stop.cleaned"
    job = commands.submit("local.stop", {"started": str(started), "cleaned": str(cleaned)})
    queued = commands.submit(
        "local.ledger", {"database": str(directory / "never.sqlite"), "idempotent": True}
    )
    with commands.worker() as child:
        wait_for(started.exists, child)
        before = commands.snapshot(job)
        stopped = commands.stop(child, signal.SIGINT)
    after = commands.snapshot(job)
    queued_before = commands.snapshot(queued)
    assert before["job"]["status"] == "RUNNING" and before["attempts"][0]["status"] == "RUNNING"
    assert cleaned.read_text() == "1"
    assert after["job"]["status"] == "FAILED"
    assert after["attempts"][0]["status"] == "INTERRUPTED"
    assert queued_before["job"]["status"] == "PENDING" and queued_before["attempts"] == []
    # Finish the queued demonstration Job so later crash runs cannot encounter it.
    assert commands.finish(queued)["job"]["status"] == "SUCCESS"
    return {
        "before": before,
        "after": after,
        "worker": stopped,
        "cleaned": True,
        "queued_at_stop": queued_before,
    }


def crash(commands, idempotent):
    business = commands.directory / f"ledger-{idempotent}.sqlite"
    barrier = commands.directory / f"barrier-{idempotent}.json"
    job = commands.submit("local.ledger", {"database": str(business), "idempotent": idempotent})
    with commands.worker(barrier=barrier) as child:
        wait_for(barrier.exists, child)
        stage = json.loads(barrier.read_text())
        before = commands.snapshot(job)
        effects_before = rows(business, "effects")
        assert stage["job_id"] == job and stage["attempt_number"] == 1
        assert before["job"]["status"] == before["attempts"][0]["status"] == "RUNNING"
        assert effects_before == [{"job_id": job, "number": 1, "value": 7}]
        child.kill()
        _, stderr = child.communicate(timeout=10)
        assert child.returncode == -signal.SIGKILL, stderr
        crash_exit = child.returncode
    after_kill = commands.snapshot(job)
    assert after_kill == before  # The real uncommitted SUCCESS SQL was rolled back.
    recovered = commands.finish(job)
    assert recovered["job"]["status"] == "SUCCESS" and recovered["job"]["attempt_count"] == 2
    assert [attempt["status"] for attempt in recovered["attempts"]] == ["INTERRUPTED", "SUCCESS"]
    calls, effects_after = rows(business, "calls"), rows(business, "effects")
    assert calls == [{"job_id": job, "number": number} for number in (1, 2)]
    assert len(effects_after) == (1 if idempotent else 2)
    assert all(effect["job_id"] == job for effect in effects_after)
    interrupted, succeeded = [
        attempt for attempt in rows(commands.database, "attempts") if attempt["job_id"] == job
    ]
    assert succeeded["started_at"] >= interrupted["finished_at"] + 100_000
    return {
        "idempotent": idempotent,
        "barrier": stage,
        "crash_exit_code": crash_exit,
        "before_kill": before,
        "after_kill": after_kill,
        "recovered": recovered,
        "calls": calls,
        "effects_before": effects_before,
        "effects_after": effects_after,
        "total_value": sum(effect["value"] for effect in effects_after),
    }


def verify():
    receipt = {
        "verified_at": datetime.now(UTC).isoformat(),
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "dependencies": {
                name: version(name)
                for name in ("taskforge", "pydantic", "sqlalchemy", "aiosqlite", "typer")
            },
        },
        "source_sha256": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(
                [
                    *ROOT.glob("src/taskforge/*.py"),
                    *EXAMPLES.glob("*.py"),
                    ROOT / "uv.lock",
                    ROOT / "pyproject.toml",
                ]
            )
        },
        "commands": [],
    }
    with TemporaryDirectory(prefix="taskforge-t10-") as temporary:
        directory = Path(temporary)
        env = {**os.environ, "PYTHONPATH": str(EXAMPLES)}
        with process(
            [sys.executable, "-u", EXAMPLES / "local_service.py"], directory=directory, env=env
        ) as service:
            # communicate(timeout) cannot be used while the fixture server is serving.
            # Read the readiness line with a bounded select, never a fixed startup sleep.
            import select

            ready, _, _ = select.select([service.stdout], [], [], 10)
            assert ready, "Local service did not report readiness"
            port = int(service.stdout.readline())
            receipt["python"] = asyncio.run(cycle(directory, port))
            commands = Commands(directory, receipt)
            receipt["cli"] = cli_cycle(commands, port)
            receipt["failures"] = failures(commands, port)
            receipt["shutdown"] = shutdown(commands)
            receipt["crash_without_idempotency"] = crash(commands, False)
            receipt["crash_with_idempotency"] = crash(commands, True)
            service.terminate()
            service.communicate(timeout=10)
            receipt["local_service_exit_code"] = service.returncode
        # Preserve results, not the transient DBs; replace only this run's scratch path.
        serialized = json.dumps(receipt, default=str).replace(temporary, "<temporary>")
    receipt = json.loads(serialized)
    receipt["cleanup"] = {
        "temporary_directory_removed": not directory.exists(),
        "all_children_reaped": True,
    }
    assert receipt["cleanup"]["temporary_directory_removed"]
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, help="Save the actual acceptance receipt as JSON")
    args = parser.parse_args()
    result = json.dumps(verify(), ensure_ascii=False, indent=2) + "\n"
    if args.output is None:
        print(result, end="")
    else:
        args.output.write_text(result, encoding="utf-8")
        print(f"Verified scenarios; receipt: {args.output}")
