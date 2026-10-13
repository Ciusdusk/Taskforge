"""Errors for the registry and its JSON input boundary."""


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
