import asyncio
import os
import sys

import pytest
from integrations.github.auth import resolve_credential
from integrations.github.errors import AuthenticationError
from integrations.github.locking import InstanceLocks

pytestmark = pytest.mark.asyncio
SECRET = "fake-auth-test-secret-012345"


def environment(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


async def test_env_order_and_whitespace(monkeypatch):
    environment(monkeypatch)
    monkeypatch.setenv("GH_TOKEN", "  first-fake  ")
    monkeypatch.setenv("GITHUB_TOKEN", "second-fake")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", lambda *a, **k: pytest.fail("gh called"))
    assert (await resolve_credential()).token == "first-fake"
    monkeypatch.setenv("GH_TOKEN", " ")
    assert (await resolve_credential()).token == "second-fake"


async def test_gh_fallback_real_child_and_no_secret_diagnostics(tmp_path, monkeypatch, caplog):
    environment(monkeypatch)
    executable = tmp_path / "gh"
    executable.write_text(
        f"#!{sys.executable}\nimport sys\n"
        "assert sys.argv[1:] == ['auth','token','--hostname','github.com']\n"
        f"print('{SECRET}')\n"
    )
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    credential = await resolve_credential()
    assert credential.token == SECRET and SECRET not in repr(credential)
    executable.write_text(
        f"#!{sys.executable}\nimport sys\nprint('{SECRET}', file=sys.stderr)\nsys.exit(1)\n"
    )
    with pytest.raises(AuthenticationError) as caught:
        await resolve_credential()
    exc = caught.value
    while exc is not None:
        assert SECRET not in str(exc)
        exc = exc.__context__ or exc.__cause__
    assert SECRET not in caplog.text


async def test_missing_gh_non_sensitive_diagnosis(monkeypatch, tmp_path):
    environment(monkeypatch)
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(AuthenticationError, match="GH_TOKEN/GITHUB_TOKEN") as caught:
        await resolve_credential()
    assert caught.value.__context__ is None


async def test_cancel_and_repeated_cancel_wait_for_real_credential_child(tmp_path, monkeypatch):
    environment(monkeypatch)
    real_spawn = asyncio.create_subprocess_exec
    entered, cleanup_entered, cleanup_release = [asyncio.Event() for _ in range(3)]
    children = []

    async def spawn(*args, **kwargs):
        process = await real_spawn(
            sys.executable,
            "-c",
            "import sys,time; print('ready',flush=True); time.sleep(60)",
            **kwargs,
        )
        children.append(process)
        communicate = process.communicate
        count = 0

        async def delayed(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 1:
                assert await process.stdout.readline() == b"ready\n"
                entered.set()
                return await communicate(*args, **kwargs)
            cleanup_entered.set()
            await cleanup_release.wait()
            return await communicate(*args, **kwargs)

        process.communicate = delayed
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    locks = InstanceLocks(tmp_path)

    async def work():
        async with locks.source("auth"):
            await resolve_credential()

    task = asyncio.create_task(work())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.wait_for(cleanup_entered.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        import fcntl

        with locks.source_path("auth").open("a+b") as probe:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert children[0].returncode is not None
        with pytest.raises(ProcessLookupError):
            os.kill(children[0].pid, 0)
        async with locks.source("auth"):
            assert locks.source_path("auth").exists()
    finally:
        cleanup_release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for process in children:
            if process.returncode is None:
                process.kill()
            await process.wait()


async def test_cancel_during_spawn_reaps_the_child(tmp_path, monkeypatch):
    environment(monkeypatch)
    real_spawn = asyncio.create_subprocess_exec
    entered, release = asyncio.Event(), asyncio.Event()
    children = []

    async def spawn(*args, **kwargs):
        process = await real_spawn(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        children.append(process)
        entered.set()
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(resolve_credential())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert children[0].returncode is not None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        for process in children:
            if process.returncode is None:
                process.kill()
            await process.wait()
