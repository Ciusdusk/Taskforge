"""TaskForge: persistent async task execution for a single machine."""

from taskforge.api import TaskForge
from taskforge.config import DatabaseConfig, WorkerConfig
from taskforge.errors import (
    AttemptConflictError,
    ClaimUncertainError,
    CompletionUncertainError,
    ExecutorError,
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
    WorkerLockError,
)
from taskforge.executor import Executor
from taskforge.logging import ExecutionContext, JSONFormatter, get_execution_context
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
from taskforge.worker import Worker

__all__ = [
    "Worker",
    "WorkerConfig",
    "WorkerLockError",
    "Executor",
    "ExecutorError",
    "ExecutionContext",
    "JSONFormatter",
    "get_execution_context",
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
