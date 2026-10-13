"""The public asynchronous submission and query interface."""

from taskforge.errors import UnknownTaskError
from taskforge.models import Attempt, Job, JobCursor
from taskforge.registry import TaskRegistry
from taskforge.storage import SQLiteStore


class TaskForge:
    def __init__(self, store: SQLiteStore, registry: TaskRegistry | None = None) -> None:
        self._store = store
        self._registry = registry

    async def submit(self, task_name: str, data: object) -> Job:
        if self._registry is None:
            raise UnknownTaskError("Submission requires an explicit TaskRegistry")
        return await self._store.submit(self._registry.get(task_name), data)

    async def get(self, job_id: str) -> Job:
        return await self._store.get(job_id)

    async def list(self, *, limit: int = 100, after: JobCursor | None = None) -> tuple[Job, ...]:
        return await self._store.list(limit=limit, after=after)

    async def history(self, job_id: str) -> tuple[Attempt, ...]:
        return await self._store.history(job_id)
