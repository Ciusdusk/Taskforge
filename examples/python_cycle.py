"""Run the Python submission/worker/query cycle using only local fixtures."""

import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from local_tasks import registry

from taskforge import DatabaseConfig, JobStatus, SQLiteStore, TaskForge, Worker, WorkerConfig


async def cycle(directory: Path, port: int) -> dict:
    config = DatabaseConfig(database_path=directory / "python.sqlite")
    scratch = directory / "scratch"
    scratch.mkdir()
    (scratch / "expired.tmp").write_text("remove this local fixture")
    (scratch / "keep.txt").write_text("keep")
    payloads = [
        (
            "local.sync",
            {"port": port, "target": str(directory / "records.json"), "transient": True},
        ),
        ("local.backup", {"port": port, "target": str(directory / "backup.bin")}),
        ("local.maintenance", {"directory": str(scratch)}),
    ]
    async with SQLiteStore(config) as store:
        await store.initialize()
        app = TaskForge(store, registry)
        jobs = [await app.submit(name, payload) for name, payload in payloads]
        worker = Worker(store, registry, WorkerConfig(poll_interval_seconds=0.01))
        run = asyncio.create_task(worker.run())
        try:
            async with asyncio.timeout(15):
                while True:
                    if run.done():
                        await run
                        raise RuntimeError("Worker stopped before completing the cycle")
                    states = [await app.get(job.id) for job in jobs]
                    if all(job.status in (JobStatus.SUCCESS, JobStatus.FAILED) for job in states):
                        break
                    await asyncio.sleep(0.01)
        finally:
            worker.request_stop()
            await run

    # Reopen without a registry: Python queries read actual committed state.
    async with SQLiteStore(config) as store:
        query = TaskForge(store)
        states = [await query.get(job.id) for job in jobs]
        histories = [await query.history(job.id) for job in jobs]
        assert all(job.status is JobStatus.SUCCESS for job in states)
        assert [len(history) for history in histories] == [2, 1, 1]
        assert histories[0][0].error_kind.value == "TASK_ERROR"
        page = await query.list(limit=2)
        assert len(page) == 2 and len(await query.list(after=page[-1].cursor)) == 1
        assert json.loads((directory / "records.json").read_text())[0]["id"] == 1
        assert (directory / "backup.bin").read_bytes() == b"TaskForge local backup fixture\n"
        assert not (scratch / "expired.tmp").exists() and (scratch / "keep.txt").exists()
        return {
            "jobs": [asdict(job) for job in states],
            "histories": [[asdict(attempt) for attempt in history] for history in histories],
            "records": json.loads((directory / "records.json").read_text()),
            "backup": (directory / "backup.bin").read_text(),
            "maintenance_remaining": sorted(path.name for path in scratch.iterdir()),
        }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    with TemporaryDirectory(prefix="taskforge-python-") as temporary:
        print(json.dumps(asyncio.run(cycle(Path(temporary), args.port)), default=str, indent=2))
