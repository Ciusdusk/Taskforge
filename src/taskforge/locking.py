"""A persistent lock inode beside the normalized local database path."""

import fcntl
from pathlib import Path
from types import TracebackType
from typing import IO, Self

from taskforge.errors import WorkerLockError


class WorkerLock:
    """Hold an exclusive nonblocking flock until close; never unlink the file."""

    def __init__(self, database_path: Path) -> None:
        self.path = Path(f"{database_path.expanduser().resolve()}.lock")
        self._file: IO[bytes] | None = None

    def __enter__(self) -> Self:
        if self._file is not None:
            raise WorkerLockError("Worker lock is already held by this instance")
        try:
            file = self.path.open("a+b")
        except OSError as exc:
            raise WorkerLockError(f"Cannot open worker lock {self.path}") from exc
        try:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            file.close()
            raise WorkerLockError(f"Cannot acquire exclusive worker lock {self.path}") from exc
        self._file = file
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
