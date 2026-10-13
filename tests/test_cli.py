"""Real command processes, SQLite state and cooperative signal shutdown."""

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import sysconfig
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

import pytest

pytestmark = pytest.mark.integration
ENTRY = Path(sys.executable).with_name("taskforge")
TASKS = """
import asyncio
import json
import os
import time
from pathlib import Path
from pydantic import BaseModel, ConfigDict
from taskforge import ExecutionPolicy, TaskRegistry, get_execution_context

class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=os.environ.get("TF_STRICT") == "1")
    value: int
    mode: str = "simple"

registry = TaskRegistry()
events = Path(os.environ["TF_EVENTS"])
release = Path(os.environ["TF_RELEASE"])

def mark(event):
    context = get_execution_context()
    with events.open("a") as stream:
        stream.write(json.dumps({"event": event, "time": time.time(),
                                "number": context.attempt_number}) + "\\n")

async def wait_release():
    while not release.exists():
        await asyncio.sleep(0.01)

if os.environ.get("TF_EMPTY") != "1":
    @registry.task("demo", input_model=Input,
                   policy=ExecutionPolicy(max_retries=2, retry_base_seconds=0.25,
                                          retry_cap_seconds=0.5, timeout_seconds=30))
    async def demo(data: Input) -> None:
        mark("entered")
        if data.mode == "fail":
            raise RuntimeError("business failure")
        if data.mode == "retry" and get_execution_context().attempt_number == 1:
            raise RuntimeError("transient failure")
        if data.mode == "bad_return":
            return 1
        if data.mode == "self_cancel":
            raise asyncio.CancelledError()
        if data.mode in ("wait", "ignore", "complete"):
            try:
                await wait_release()
            except asyncio.CancelledError:
                mark("cleaning")
                try:
                    await wait_release()
                except asyncio.CancelledError:
                    mark("cleanup_interrupted")
                    raise
                mark("cleaned")
                if data.mode != "ignore":
                    raise
        mark("returned")
"""


def wait_for(predicate, *, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.01)
    pytest.fail("Timed out waiting for CLI process/state barrier")


class Commands:
    def __init__(self, path):
        self.path = path
        self.database = path / "jobs.sqlite"
        self.events = path / "events.jsonl"
        self.release = path / "release"
        self.env = {
            **os.environ,
            "PYTHONPATH": str(path),
            "TF_EVENTS": str(self.events),
            "TF_RELEASE": str(self.release),
            "NO_COLOR": "1",
        }
        (path / "cli_tasks.py").write_text(TASKS)

    def args(self, *args):
        return [str(ENTRY), *map(str, args), "--database", str(self.database)]

    def run(self, *args, input=None, code=0, env=None, prefix=None):
        command = self.args(*args)
        if prefix is not None:
            command = [*prefix, *command[1:]]
        result = subprocess.run(
            command,
            cwd=self.path,
            env=self.env | (env or {}),
            input=input,
            text=True,
            capture_output=True,
            timeout=15,
        )
        assert result.returncode == code, (result.stdout, result.stderr, result.returncode)
        return result

    def submit(self, mode="simple", value=1):
        return self.run(
            "submit",
            "demo",
            "--registry",
            "cli_tasks:registry",
            input=json.dumps({"value": value, "mode": mode}),
        ).stdout.strip()

    def show(self, job):
        return json.loads(self.run("show", job).stdout)

    def history(self, job):
        return json.loads(self.run("history", job).stdout)["attempts"]

    def state(self, job):
        with sqlite3.connect(self.database) as connection:
            return connection.execute("SELECT status FROM jobs WHERE id = ?", (job,)).fetchone()[0]

    def marks(self):
        if not self.events.exists():
            return []
        # Each small append is atomic, but only parse lines ending in a newline.
        return [
            json.loads(line)
            for line in self.events.read_text().splitlines(keepends=True)
            if line.endswith("\n")
        ]

    @contextmanager
    def worker(self, *, grace=0.04, env=None, prefix=None):
        command = self.args(
            "worker",
            "--registry",
            "cli_tasks:registry",
            "--concurrency",
            "1",
            "--poll-interval-seconds",
            "0.01",
            "--shutdown-grace-seconds",
            str(grace),
        )
        if prefix is not None:
            command = [*prefix, *command[1:]]
        child = subprocess.Popen(
            command,
            cwd=self.path,
            env=self.env | (env or {}),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            yield child
        finally:
            self.release.touch()
            if child.poll() is None:
                child.send_signal(signal.SIGTERM)
            try:
                child.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate(timeout=5)


@pytest.fixture
def commands(tmp_path):
    return Commands(tmp_path)


def test_installed_entry_and_all_help_do_not_load_tasks(commands):
    (commands.path / "cli_tasks.py").write_text('raise RuntimeError("must not import")')
    for name in (None, "init", "submit", "worker", "list", "show", "history"):
        args = [str(ENTRY)] + ([name] if name else []) + ["--help"]
        result = subprocess.run(args, cwd=commands.path, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert "Usage" in result.stdout
    assert not commands.database.exists()


def test_init_submit_query_pagination_and_history_without_registry(commands):
    initialized = json.loads(commands.run("init").stdout)
    assert initialized == {"initialized": True, "database_path": str(commands.database)}
    first = commands.submit(value="3")
    assert str(UUID(first)) == first and UUID(first).version == 4
    payload = commands.path / "input.json"
    payload.write_text('{"value": 4}')
    second = commands.run(
        "submit",
        "demo",
        "--registry",
        "cli_tasks:registry",
        "--input",
        payload,
    ).stdout.strip()
    commands.run("init")  # Explicit validation preserves existing data.
    (commands.path / "cli_tasks.py").write_text('raise RuntimeError("query imported tasks")')
    job = commands.show(first)
    assert job["status"] == "PENDING" and job["attempt_count"] == 0
    assert json.loads(job["params_json"]) == {"value": 3, "mode": "simple"}
    assert (
        job["max_retries"],
        job["retry_base_seconds"],
        job["retry_cap_seconds"],
        job["timeout_seconds"],
    ) == (2, 0.25, 0.5, 30.0)
    assert job["next_run_at"] == job["created_at"]
    assert commands.history(first) == []
    page = json.loads(commands.run("list", "--limit", 1).stdout)
    assert [item["id"] for item in page["jobs"]] == [first]
    page = json.loads(commands.run("list", "--limit", 1, "--after", page["next_cursor"]).stdout)
    assert [item["id"] for item in page["jobs"]] == [second]
    page = json.loads(commands.run("list", "--after", page["next_cursor"]).stdout)
    assert page == {"jobs": [], "next_cursor": None}
    assert not commands.events.exists()


@pytest.mark.parametrize(
    "payload",
    [
        "{",
        "",
        "NaN",
        '{"value": NaN}',
        "[]",
        "null",
        '{"value": "bad"}',
        '{"value": 1, "extra": 2}',
    ],
)
def test_invalid_json_or_model_input_does_not_persist(commands, payload):
    commands.run("init")
    result = commands.run(
        "submit", "demo", "--registry", "cli_tasks:registry", input=payload, code=2
    )
    assert not result.stdout and json.loads(result.stderr)["error"]
    assert json.loads(commands.run("list").stdout)["jobs"] == []
    assert not commands.events.exists()


@pytest.mark.parametrize(
    "args,code",
    [
        (("submit", "absent", "--registry", "cli_tasks:registry"), 3),
        (("submit", "demo", "--registry", "missing:registry"), 4),
        (("submit", "demo", "--registry", "cli_tasks:absent"), 4),
        (("submit", "demo", "--registry", "invalid"), 4),
        (("submit", "demo", "--registry", "cli_tasks:registry", "--input", "missing.json"), 2),
        (("show", "unknown"), 3),
        (("history", "unknown"), 3),
        (("list", "--limit", "0"), 2),
        (("list", "--limit", "1001"), 2),
        (("list", "--after", "{}"), 2),
        (("list", "--after", '{"created_at": "2026-10-13", "id": "bad"}'), 2),
        (("worker", "--registry", "cli_tasks:registry", "--concurrency", "0"), 2),
        (("worker", "--registry", "cli_tasks:registry", "--poll-interval-seconds", "nan"), 2),
        (("worker", "--registry", "cli_tasks:registry", "--shutdown-grace-seconds", "-1"), 2),
    ],
)
def test_explicit_errors(commands, args, code):
    commands.run("init")
    result = commands.run(*args, input='{"value": 1}', code=code)
    assert not result.stdout
    error = json.loads(result.stderr)
    assert error["error"] and error["message"]
    assert error["job_id"] is None and error["attempt_number"] is None


@pytest.mark.parametrize("command", ["list", "show", "history", "worker"])
def test_uninitialized_queries_and_worker_do_not_create_schema(commands, command):
    args = [command]
    if command in ("show", "history"):
        args += ["unknown"]
    if command == "worker":
        args += ["--registry", "cli_tasks:registry"]
    result = commands.run(*args, code=5)
    assert json.loads(result.stderr.splitlines()[-1])["error"] == "SchemaError"
    with sqlite3.connect(commands.database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (0,)
        assert (
            connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
        )


def test_incompatible_schema_and_real_storage_error(commands):
    commands.run("init")
    with sqlite3.connect(commands.database) as connection:
        connection.execute("PRAGMA user_version = 99")
    assert json.loads(commands.run("init", code=5).stderr)["error"] == "SchemaError"
    commands.database = commands.path / "absent-parent" / "jobs.sqlite"
    assert json.loads(commands.run("init", code=7).stderr)["error"] == "StorageError"
    commands.database = commands.path
    assert json.loads(commands.run("init", code=7).stderr)["error"] == "StorageError"


@pytest.mark.parametrize(
    "args", [("submit", "demo"), ("worker",), ("show",), ("list", "--limit", "abc")]
)
def test_typer_syntax_errors_exit_two_without_opening_database(commands, args):
    result = commands.run(*args, code=2)
    assert not result.stdout and "Usage" in result.stderr
    assert not commands.database.exists()


def test_input_directory_and_invalid_utf8_fail_before_submission(commands):
    commands.run("init")
    binary = commands.path / "invalid.json"
    binary.write_bytes(b"\xff")
    for path in (commands.path, binary):
        result = commands.run(
            "submit", "demo", "--registry", "cli_tasks:registry", "--input", path, code=2
        )
        assert json.loads(result.stderr)["error"] == "_InputError"
    assert json.loads(commands.run("list").stdout)["jobs"] == []


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_worker_complete_within_grace_and_process_lock(commands, sig):
    commands.run("init")
    job = commands.submit("complete")
    with commands.worker(grace=5) as child:
        wait_for(lambda: any(mark["event"] == "entered" for mark in commands.marks()))
        commands.run("worker", "--registry", "cli_tasks:registry", code=6)
        child.send_signal(sig)
        commands.release.touch()
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, stderr
        assert not stdout and "shutdown_incomplete" not in stderr
    assert commands.show(job)["status"] == "SUCCESS"
    assert commands.history(job)[0]["status"] == "SUCCESS"
    assert Path(f"{commands.database}.lock").exists()


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize("mode", ["wait", "ignore"])
def test_grace_cancel_repeated_signals_cleanup_and_lock(commands, sig, mode):
    commands.run("init")
    job = commands.submit(mode)
    pending = commands.submit()
    with commands.worker(grace=0.1) as child:
        wait_for(lambda: any(mark["event"] == "entered" for mark in commands.marks()))
        stopped_at = time.time()
        child.send_signal(sig)
        cleaning = wait_for(
            lambda: next((mark for mark in commands.marks() if mark["event"] == "cleaning"), None)
        )
        assert cleaning["time"] - stopped_at >= 0.09
        for repeated in (signal.SIGINT, signal.SIGTERM, sig):
            child.send_signal(repeated)
            commands.run("worker", "--registry", "cli_tasks:registry", code=6)
        assert child.poll() is None
        assert commands.show(job)["status"] == "RUNNING"
        assert commands.history(job)[0]["finished_at"] is None
        assert commands.show(pending)["attempt_count"] == 0
        assert [mark["event"] for mark in commands.marks()] == ["entered", "cleaning"]
        commands.release.touch()
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 0 and not stdout, stderr
        entries = [json.loads(line) for line in stderr.splitlines()]
        incomplete = [entry for entry in entries if entry["event"] == "shutdown_incomplete"]
        assert len(incomplete) == 1
        assert incomplete[0]["job_id"] == job and incomplete[0]["attempt_number"] == 1
    assert "cleanup_interrupted" not in [mark["event"] for mark in commands.marks()]
    job_state = commands.show(job)
    assert job_state["status"] == "RETRY_WAIT" and job_state["attempt_count"] == 1
    assert commands.history(job)[0]["status"] == "INTERRUPTED"
    assert commands.show(pending)["status"] == "PENDING"


@pytest.mark.parametrize(
    "mode,env,kind,count",
    [
        ("fail", {}, "TASK_ERROR", 3),
        ("simple", {"TF_EMPTY": "1"}, "CONFIG_ERROR", 1),
        ("simple", {"TF_STRICT": "1"}, "CONFIG_ERROR", 1),
    ],
)
def test_business_and_config_failures_have_queryable_final_states(commands, mode, env, kind, count):
    commands.run("init")
    job = commands.submit(mode)
    if env.get("TF_STRICT"):
        with sqlite3.connect(commands.database) as connection:
            connection.execute(
                "UPDATE jobs SET params_json = ? WHERE id = ?",
                ('{"value": "1", "mode": "simple"}', job),
            )
    with commands.worker(env=env) as child:
        wait_for(lambda: commands.state(job) == "FAILED")
        child.send_signal(signal.SIGTERM)
        _, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, stderr
    assert commands.show(job)["last_error_kind"] == kind
    attempts = commands.history(job)
    assert [item["number"] for item in attempts] == list(range(1, count + 1))
    assert all(item["error_kind"] == kind and item["status"] == "FAILED" for item in attempts)
    if kind == "CONFIG_ERROR":
        assert not commands.events.exists()


@pytest.mark.parametrize(
    "mode,code,error",
    [
        ("bad_return", 8, "ExecutorError"),
        ("self_cancel", 130, "CancelledError"),
    ],
)
def test_framework_error_and_user_cancellation_are_not_business_failures(
    commands, mode, code, error
):
    commands.run("init")
    job = commands.submit(mode)
    with commands.worker() as child:
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == code and not stdout
        assert json.loads(stderr.splitlines()[-1])["error"] == error
    assert commands.history(job)[0]["status"] == (
        "RUNNING" if mode == "bad_return" else "INTERRUPTED"
    )


FAULT_BOOTSTRAP = """
import os
from sqlalchemy import event
from taskforge import db, storage
from taskforge.cli import main
stage = os.environ["TF_FAULT"]
original_commit = storage._commit
async def commit(session):
    affected = session.info.get("cli_fault", False)
    await original_commit(session)
    if affected:
        raise RuntimeError("injected lost commit acknowledgement")
storage._commit = commit
original_session = storage.SQLiteStore._session
from contextlib import asynccontextmanager
@asynccontextmanager
async def session(self, *, write=False, check_schema=True):
    async with original_session(self, write=write, check_schema=check_schema) as current:
        if write:
            connection = await current.connection()
            def before(conn, cursor, statement, parameters, context, many):
                lower = statement.lower()
                affected = ((stage == "submit" and lower.startswith("insert into jobs")) or
                            (stage == "claim" and lower.startswith("insert into attempts")) or
                            (stage in ("finish", "sql") and lower.startswith("update attempts")))
                if affected:
                    if stage == "sql":
                        return "INSERT INTO missing_cli_fault_table VALUES (1)", ()
                    current.info["cli_fault"] = True
                return statement, parameters
            target = connection.sync_connection
            event.listen(target, "before_cursor_execute", before, retval=True)
            try:
                yield current
            finally:
                event.remove(target, "before_cursor_execute", before)
        else:
            yield current
storage.SQLiteStore._session = session
main()
"""


@pytest.mark.parametrize(
    "stage,error,status",
    [
        ("submit", "SubmissionUncertainError", "PENDING"),
        ("claim", "ClaimUncertainError", "RUNNING"),
        ("finish", "CompletionUncertainError", "SUCCESS"),
        ("sql", "StorageError", "RUNNING"),
    ],
)
def test_commit_uncertainty_and_sql_fault_exit_with_queryable_credentials(
    commands, stage, error, status
):
    commands.run("init")
    prefix = [sys.executable, "-c", FAULT_BOOTSTRAP]
    if stage == "submit":
        result = commands.run(
            "submit",
            "demo",
            "--registry",
            "cli_tasks:registry",
            input='{"value": 1}',
            code=9,
            prefix=prefix,
            env={"TF_FAULT": stage},
        )
        stderr = result.stderr
    else:
        job = commands.submit()
        with commands.worker(prefix=prefix, env={"TF_FAULT": stage}) as child:
            stdout, stderr = child.communicate(timeout=10)
            assert not stdout and child.returncode == (7 if stage == "sql" else 9), stderr
    report = json.loads(stderr.splitlines()[-1])
    assert report["error"] == error
    if stage != "sql":
        job = report["job_id"]
        assert UUID(job).version == 4
        assert report["attempt_number"] == (None if stage == "submit" else 1)
    assert commands.show(job)["status"] == status
    attempts = commands.history(job)
    assert len(attempts) == (0 if stage == "submit" else 1)
    assert len([mark for mark in commands.marks() if mark["event"] == "entered"]) == (
        1 if stage in ("finish", "sql") else 0
    )


def test_retry_restart_keeps_due_time_and_complete_history(commands):
    commands.run("init")
    job = commands.submit("retry")
    # Force a long enough stored delay to inspect/restart through a real process.
    with sqlite3.connect(commands.database) as connection:
        connection.execute(
            "UPDATE jobs SET retry_base_seconds=1.5, retry_cap_seconds=1.5 WHERE id=?", (job,)
        )
    with commands.worker() as first:
        wait_for(lambda: commands.state(job) == "RETRY_WAIT")
        first.send_signal(signal.SIGTERM)
        _, stderr = first.communicate(timeout=10)
        assert first.returncode == 0, stderr
    due = commands.show(job)["next_run_at"]
    with commands.worker() as second:
        wait_for(lambda: commands.state(job) == "SUCCESS")
        second.send_signal(signal.SIGTERM)
        _, stderr = second.communicate(timeout=10)
        assert second.returncode == 0, stderr
    from datetime import datetime

    entered = [mark for mark in commands.marks() if mark["event"] == "entered"]
    assert [mark["number"] for mark in entered] == [1, 2]
    assert entered[1]["time"] >= datetime.fromisoformat(due).timestamp()
    assert [item["status"] for item in commands.history(job)] == ["FAILED", "SUCCESS"]


def test_killed_cli_worker_recovers_atomically_and_waits_from_confirmation(commands):
    commands.run("init")
    job = commands.submit("wait")
    with commands.worker() as first:
        wait_for(lambda: commands.marks())
        first.kill()
        first.communicate(timeout=10)
        assert first.returncode == -signal.SIGKILL
    assert commands.show(job)["status"] == "RUNNING"
    with commands.worker() as second:

        def recovered_snapshot():
            with sqlite3.connect(commands.database) as connection:
                return connection.execute(
                    "SELECT j.next_run_at, a.finished_at FROM jobs j JOIN attempts a "
                    "ON j.id=a.job_id WHERE j.id=? AND j.status='RETRY_WAIT' AND a.number=1",
                    (job,),
                ).fetchone()

        due_us, confirmed_us = wait_for(recovered_snapshot)
        assert due_us - confirmed_us == 250_000
        attempts = commands.history(job)
        assert attempts[0]["status"] == "INTERRUPTED"
        wait_for(lambda: commands.state(job) == "SUCCESS")
        second.send_signal(signal.SIGTERM)
        _, stderr = second.communicate(timeout=10)
        assert second.returncode == 0, stderr
    entered = [mark for mark in commands.marks() if mark["event"] == "entered"]
    assert entered[1]["time"] >= due_us / 1_000_000
    assert [item["status"] for item in commands.history(job)] == ["INTERRUPTED", "SUCCESS"]


def test_recovery_uncertainty_reports_original_attempt_and_does_not_execute(commands):
    commands.run("init")
    job = commands.submit("bad_return")
    with commands.worker() as first:
        first.communicate(timeout=10)
        assert first.returncode == 8
    with commands.worker(
        prefix=[sys.executable, "-c", FAULT_BOOTSTRAP], env={"TF_FAULT": "finish"}
    ) as second:
        _, stderr = second.communicate(timeout=10)
        assert second.returncode == 9, stderr
    report = json.loads(stderr.splitlines()[-1])
    assert report["error"] == "CompletionUncertainError"
    assert report["job_id"] == job and report["attempt_number"] == 1
    assert commands.show(job)["status"] == "RETRY_WAIT"
    assert commands.history(job)[0]["status"] == "INTERRUPTED"
    assert len([mark for mark in commands.marks() if mark["event"] == "entered"]) == 1


def test_wheel_installed_outside_project_complete_command_cycle(commands, monkeypatch):
    project = Path(__file__).resolve().parents[1]
    env = {**os.environ, "UV_OFFLINE": "true", "UV_CACHE_DIR": str(project / ".cache/uv")}

    def uv(*args):
        result = subprocess.run(
            ["uv", *map(str, args)],
            cwd=project,
            env=env,
            text=True,
            capture_output=True,
            timeout=40,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)

    distribution = commands.path / "dist"
    isolated = commands.path / "venv"
    uv("build", "--out-dir", distribution)
    uv("venv", "--python", sys.executable, isolated)
    python = isolated / "bin/python"
    # Reuse the locked, installed third-party dependencies without copying the
    # editable project or its .pth. This also works with an unpacked offline cache.
    target = (
        isolated
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    shutil.copytree(
        sysconfig.get_path("purelib"),
        target,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("*taskforge*", "*.pth", "_virtualenv*"),
    )
    uv(
        "pip",
        "install",
        "--python",
        python,
        "--no-deps",
        distribution / "taskforge-0.1.0-py3-none-any.whl",
    )
    uv("pip", "check", "--python", python)
    monkeypatch.setattr(sys.modules[__name__], "ENTRY", isolated / "bin/taskforge")
    location = subprocess.run(
        [str(python), "-I", "-c", "import taskforge; print(taskforge.__file__)"],
        cwd=commands.path,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert location.returncode == 0
    assert str(isolated) in location.stdout and str(project / "src") not in location.stdout
    test_installed_entry_and_all_help_do_not_load_tasks(commands)
    (commands.path / "cli_tasks.py").write_text(TASKS)
    commands.run("init")
    job = commands.submit()
    with commands.worker() as child:
        wait_for(lambda: commands.state(job) == "SUCCESS")
        child.send_signal(signal.SIGTERM)
        _, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, stderr
    (commands.path / "cli_tasks.py").unlink()
    assert commands.show(job)["status"] == "SUCCESS"
    assert commands.history(job)[0]["status"] == "SUCCESS"
    assert json.loads(commands.run("list").stdout)["jobs"][0]["id"] == job
