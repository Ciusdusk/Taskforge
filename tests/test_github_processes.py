import asyncio
import fcntl
import json
import os
import sys
import time
from pathlib import Path

import httpx
import pytest
from integrations.github.auth import Credential
from integrations.github.config import Budgets
from integrations.github.http import AttemptBudget, GitHubHTTP
from integrations.github.locking import InstanceLocks
from integrations.github.storage import BusinessStore

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]
ROOT = Path(__file__).resolve().parents[1]


async def spawn(code, *args):
    env = {
        key: value for key, value in os.environ.items() if key not in ("GH_TOKEN", "GITHUB_TOKEN")
    }
    env["PYTHONPATH"] = str(ROOT)
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        *map(str, args),
        cwd=ROOT,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def line(process):
    result = await asyncio.wait_for(process.stdout.readline(), 5)
    assert result, await process.stderr.read()
    return result.decode().strip()


async def reap(process):
    if process.returncode is None:
        process.kill()
    await process.communicate()


PROBE = """
import asyncio,sys
from pathlib import Path
from integrations.github.locking import InstanceLocks
from integrations.github.errors import LockError
async def main():
    locks=InstanceLocks(Path(sys.argv[1]))
    try:
        async with locks.source(sys.argv[2]):
            print("acquired",flush=True)
    except LockError:
        print("locked",flush=True)
asyncio.run(main())
"""


async def test_real_process_source_exclusion_different_sources_and_retained_inode(tmp_path):
    locks = InstanceLocks(tmp_path)
    async with locks.source("same"):
        inode = locks.source_path("same").stat().st_ino
        for source, expected in [("same", "locked"), ("different", "acquired")]:
            process = await spawn(PROBE, tmp_path, source)
            try:
                assert await line(process) == expected
                stdout, stderr = await process.communicate()
                assert process.returncode == 0 and not stdout and not stderr
            finally:
                await reap(process)
    process = await spawn(PROBE, tmp_path, "same")
    try:
        assert await line(process) == "acquired"
        await process.communicate()
        assert process.returncode == 0
    finally:
        await reap(process)
    assert locks.source_path("same").stat().st_ino == inode


async def test_same_source_async_mutex_across_lock_instances(tmp_path):
    first, second = InstanceLocks(tmp_path), InstanceLocks(tmp_path)
    entered = asyncio.Event()

    async def wait():
        async with second.source("same"):
            entered.set()

    async with first.source("same"):
        task = asyncio.create_task(wait())
        try:
            await asyncio.sleep(0)
            assert not entered.is_set()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async with second.source("same"):
        pass


GATE = """
import asyncio,json,sys,time
from pathlib import Path
import httpx
from integrations.github.auth import Credential
from integrations.github.config import Budgets
from integrations.github.http import AttemptBudget,GitHubHTTP
from integrations.github.storage import BusinessStore
async def main():
    store=BusinessStore(Path(sys.argv[1]))
    name=sys.argv[2]
    async def handler(request):
        print("begin",flush=True)
        if name == "one":
            await asyncio.to_thread(input)
        with (store.directory/"requests.jsonl").open("a") as f:
            f.write(json.dumps([name,"begin",time.monotonic()])+"\\n")
        await asyncio.sleep(0.02)
        with (store.directory/"requests.jsonl").open("a") as f:
            f.write(json.dumps([name,"end",time.monotonic()])+"\\n")
        return httpx.Response(200,json=[])
    async with GitHubHTTP(store,Credential("fake-"+name),AttemptBudget(Budgets()),
                          transport=httpx.MockTransport(handler),trust_env=False) as http:
        async with http.locks.source(name):
            print("ready",flush=True)
            await asyncio.to_thread(input)
            await http.get("/repos/a/b/releases")
            print("done",flush=True)
asyncio.run(main())
"""


async def test_different_sources_use_shared_real_process_api_gate(tmp_path):
    store = BusinessStore(tmp_path)
    await store.initialize()
    first = await spawn(GATE, tmp_path, "one")
    second = await spawn(GATE, tmp_path, "two")
    pending = None
    try:
        assert await line(first) == "ready"
        assert await line(second) == "ready"
        first.stdin.write(b"start\n")
        await first.stdin.drain()
        assert await line(first) == "begin"
        second.stdin.write(b"start\n")
        await second.stdin.drain()
        with InstanceLocks(tmp_path).request_path.open("a+b") as probe:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        pending = asyncio.create_task(second.stdout.readline())
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(pending), 0.1)
        first.stdin.write(b"release\n")
        await first.stdin.drain()
        assert await line(first) == "done"
        assert await pending == b"begin\n"
        assert await line(second) == "done"
        for process in (first, second):
            _, stderr = await process.communicate()
            assert process.returncode == 0 and not stderr
        records = [
            json.loads(row) for row in (tmp_path / "requests.jsonl").read_text().splitlines()
        ]
        assert [(r[0], r[1]) for r in records] == [
            ("one", "begin"),
            ("one", "end"),
            ("two", "begin"),
            ("two", "end"),
        ]
        assert records[1][2] <= records[2][2]
        assert InstanceLocks(tmp_path).request_path.exists()
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await reap(first)
        await reap(second)


COOLDOWN = """
import asyncio,json,sys
from pathlib import Path
import httpx
from integrations.github.auth import Credential
from integrations.github.config import Budgets
from integrations.github.errors import BudgetError
from integrations.github.http import AttemptBudget,GitHubHTTP
from integrations.github.storage import BusinessStore
async def main():
    store=BusinessStore(Path(sys.argv[1]))
    async def handler(request):
        if sys.argv[2] == "seed":
            return httpx.Response(429,headers={"retry-after":"60"})
        raise AssertionError("request made before persisted cooldown ended")
    async with GitHubHTTP(store,Credential("fake-"+sys.argv[2]),
                          AttemptBudget(Budgets(max_wait_seconds=0)),
                          transport=httpx.MockTransport(handler),trust_env=False) as http:
        try:
            await http.get("/rate")
        except Exception as e:
            print(json.dumps({"error":type(e).__name__,"requests":http.budget.requests}),flush=True)
asyncio.run(main())
"""


async def test_cooldown_survives_actual_process_restart_and_changed_token(tmp_path):
    store = BusinessStore(tmp_path)
    await store.initialize()
    for mode, expected in [
        ("seed", {"error": "HTTPError", "requests": 1}),
        ("new-token", {"error": "BudgetError", "requests": 0}),
    ]:
        process = await spawn(COOLDOWN, tmp_path, mode)
        try:
            assert json.loads(await line(process)) == expected
            _, stderr = await process.communicate()
            assert process.returncode == 0 and not stderr
        finally:
            await reap(process)
    assert (await store.cooldown())[0] > time.time()


async def test_repeated_http_cancel_keeps_source_and_api_locks_until_cleanup(tmp_path):
    store = BusinessStore(tmp_path)
    await store.initialize()
    started, cleaning, release, cleaned = [asyncio.Event() for _ in range(4)]
    http = None

    async def handler(request):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            cleaned.set()

    async def work():
        nonlocal http
        locks = InstanceLocks(tmp_path)
        async with locks.source("source"):
            async with GitHubHTTP(
                store,
                Credential("fake-token"),
                AttemptBudget(Budgets()),
                transport=httpx.MockTransport(handler),
                trust_env=False,
            ) as http:
                await http.get("/slow")

    task = asyncio.create_task(work())
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        await asyncio.wait_for(cleaning.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not http.client.is_closed
        process = await spawn(PROBE, tmp_path, "source")
        try:
            assert await line(process) == "locked"
        finally:
            await reap(process)
        with InstanceLocks(tmp_path).request_path.open("a+b") as probe:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned.is_set() and http.client.is_closed
        async with InstanceLocks(tmp_path).source("source"):
            pass
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_client_close_repeated_cancel_holds_source_until_actual_close(tmp_path, monkeypatch):
    store = BusinessStore(tmp_path)
    await store.initialize()
    entered, release = asyncio.Event(), asyncio.Event()
    close = httpx.AsyncClient.aclose
    clients = []

    async def delayed(client):
        clients.append(client)
        entered.set()
        await release.wait()
        await close(client)

    monkeypatch.setattr(httpx.AsyncClient, "aclose", delayed)

    async def work():
        async with InstanceLocks(tmp_path).source("closing"):
            async with GitHubHTTP(
                store,
                Credential("fake"),
                AttemptBudget(Budgets()),
                transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])),
                trust_env=False,
            ) as http:
                await http.get("/safe")

    task = asyncio.create_task(work())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not clients[0].is_closed
        process = await spawn(PROBE, tmp_path, "closing")
        try:
            assert await line(process) == "locked"
        finally:
            await reap(process)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert clients[0].is_closed
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
