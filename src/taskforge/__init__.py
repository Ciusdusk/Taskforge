"""TaskForge: persistent async task execution for a single machine."""

from taskforge.api import TaskForge
from taskforge.config import DatabaseConfig
from taskforge.errors import (
    AttemptConflictError,
    ClaimUncertainError,
    CompletionUncertainError,
    InputSerializationError,
    InvalidAttemptResultError,
    PaginationError,
    RegistrationError,
    RegistryLoadError,
    SchemaError,
    StorageError,
    SubmissionUncertainError,
    TaskForgeError,
    UnknownJobError,
    UnknownTaskError,
)
from taskforge.models import (
    Attempt,
    AttemptClaim,
    AttemptOutcome,
    AttemptStatus,
    ErrorKind,
    ExecutionPolicy,
    Job,
    JobCursor,
    JobStatus,
)
from taskforge.registry import TaskDefinition, TaskRegistry, load_registry
from taskforge.storage import SQLiteStore

__all__ = [
    "Attempt",
    "AttemptClaim",
    "AttemptOutcome",
    "AttemptConflictError",
    "ClaimUncertainError",
    "CompletionUncertainError",
    "InvalidAttemptResultError",
    "AttemptStatus",
    "DatabaseConfig",
    "ErrorKind",
    "Job",
    "JobCursor",
    "JobStatus",
    "PaginationError",
    "SchemaError",
    "SQLiteStore",
    "StorageError",
    "SubmissionUncertainError",
    "TaskForge",
    "UnknownJobError",
    "ExecutionPolicy",
    "InputSerializationError",
    "RegistrationError",
    "RegistryLoadError",
    "TaskDefinition",
    "TaskForgeError",
    "TaskRegistry",
    "UnknownTaskError",
    "load_registry",
]
