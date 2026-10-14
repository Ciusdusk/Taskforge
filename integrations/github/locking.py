"""Async in-process exclusion plus nonblocking Linux flock; retain lock files."""

import asyncio
import fcntl
import hashlib
import weakref
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import IO

from integrations.github.errors import LockError

_locks: weakref.WeakValueDictionary[tuple[asyncio.AbstractEventLoop, Path], asyncio.Lock] = (
    weakref.WeakValueDictionary()
)


class InstanceLocks:
    def __init__(self, directory: Path) -> None:
        self.directory = directory.expanduser().resolve() / "locks"
        self.directory.mkdir(parents=True, exist_ok=True)

    def source_path(self, source_id: str) -> Path:
        return self.directory / (
            "source-" + hashlib.sha256(source_id.encode()).hexdigest() + ".lock"
        )

    @property
    def request_path(self) -> Path:
        return self.directory / "api.lock"

    @asynccontextmanager
    async def hold(self, path: Path, *, wait: bool = False) -> AsyncIterator[None]:
        key = (asyncio.get_running_loop(), path)
        mutex = _locks.setdefault(key, asyncio.Lock())
        async with mutex:
            file: IO[bytes] | None = None
            try:
                file = path.open("a+b")
                while True:
                    try:
                        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if not wait:
                            raise LockError("Source is active in another process") from None
                        await asyncio.sleep(0.02)
                yield
            finally:
                if file is not None:
                    file.close()

    def source(self, source_id: str) -> AbstractAsyncContextManager[None]:
        return self.hold(self.source_path(source_id))
