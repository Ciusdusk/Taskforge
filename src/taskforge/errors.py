"""Errors at the public registry and persistence boundaries."""


class TaskForgeError(Exception):
    """Base class for TaskForge interface errors."""


class RegistrationError(TaskForgeError, ValueError):
    """A task declaration cannot be registered."""


class UnknownTaskError(TaskForgeError, LookupError):
    """The requested stable task name is not registered."""


class RegistryLoadError(TaskForgeError, ValueError):
    """An explicit module:attribute reference cannot load a registry."""


class InputSerializationError(TaskForgeError, ValueError):
    """Validated input cannot be stored as revalidatable JSON."""


class StorageError(TaskForgeError):
    """A database operation failed; the underlying cause is retained."""


class SchemaError(StorageError):
    """The database needs explicit initialization or has an incompatible schema."""


class UnknownJobError(TaskForgeError, LookupError):
    """No persisted Job has the requested ID."""


class PaginationError(TaskForgeError, ValueError):
    """A page size or cursor is invalid."""


class SubmissionUncertainError(StorageError):
    """Commit was attempted but its result could not be confirmed.

    Query job_id before deciding whether another submission is appropriate.
    """

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        super().__init__(f"Submission result is uncertain; query Job {job_id!r}")


class AttemptConflictError(TaskForgeError):
    """The credential does not identify the current RUNNING attempt."""


class InvalidAttemptResultError(TaskForgeError, ValueError):
    """An outcome, disposition, attempt number or retry time is invalid."""


class ClaimUncertainError(StorageError):
    """Claim commit was attempted; do not execute without confirming ownership."""

    def __init__(self, job_id: str, number: int) -> None:
        self.job_id = job_id
        self.number = number
        super().__init__(f"Claim result is uncertain; query Job {job_id!r}, attempt {number}")


class CompletionUncertainError(StorageError):
    """Completion commit was attempted; query Job and history before retrying."""

    def __init__(self, job_id: str, number: int) -> None:
        self.job_id = job_id
        self.number = number
        super().__init__(f"Completion result is uncertain; query Job {job_id!r}, attempt {number}")
