"""Explicit task registration and reusable input validation."""

import importlib
import inspect
import json
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, get_type_hints

from pydantic import BaseModel, RootModel

from taskforge.errors import (
    InputSerializationError,
    RegistrationError,
    RegistryLoadError,
    UnknownTaskError,
)
from taskforge.models import ExecutionPolicy

type TaskFunction[InputT: BaseModel] = Callable[[InputT], Coroutine[Any, Any, None]]


@dataclass(frozen=True, slots=True)
class TaskDefinition[InputT: BaseModel]:
    """A stable name, original function, explicit model and policy snapshot."""

    name: str
    function: TaskFunction[InputT]
    input_model: type[InputT]
    policy: ExecutionPolicy

    def validate_input(self, data: object) -> InputT:
        """Validate Python input using the model's own configuration."""
        return self.input_model.model_validate(data)

    def validate_json(self, payload: str) -> InputT:
        """Revalidate stored JSON using Pydantic's JSON-specific semantics."""
        return self.input_model.model_validate_json(payload)

    def serialize_input(self, data: object) -> str:
        """Validate and serialize an object that can be revalidated by a Worker.

        Aliases and round_trip support retain Pydantic input representations.
        ValidationError from the original input remains a Pydantic error.
        """
        model = self.validate_input(data)
        try:
            payload = model.model_dump_json(by_alias=True, round_trip=True, warnings="error")
            if not isinstance(json.loads(payload), dict):
                raise ValueError("task input must serialize to a JSON object")
            self.validate_json(payload)
        except (ValueError, TypeError) as exc:
            raise InputSerializationError(
                f"Task {self.name!r} input cannot round-trip through JSON: {exc}"
            ) from exc
        return payload


class TaskRegistry:
    """An isolated registry; registration never wraps the original function."""

    def __init__(self) -> None:
        # Each entry keeps its own model/function pairing in this heterogeneous collection.
        self._tasks: dict[str, TaskDefinition[Any]] = {}

    def task[InputT: BaseModel](
        self,
        name: str,
        *,
        input_model: type[InputT],
        policy: ExecutionPolicy | None = None,
    ) -> Callable[[TaskFunction[InputT]], TaskFunction[InputT]]:
        """Declare a task with one positional model argument and a None return.

        User annotations are optional; declared annotations must match the model
        and return contract. All checks finish before modifying the registry.
        """
        if not isinstance(name, str) or not name or name.strip() != name:
            raise RegistrationError("Task name must be a nonempty string without edge whitespace")
        if (
            not isinstance(input_model, type)
            or not issubclass(input_model, BaseModel)
            or input_model is BaseModel
            or issubclass(input_model, RootModel)
        ):
            raise RegistrationError(f"Task {name!r} requires a concrete Pydantic object model")
        if policy is not None and not isinstance(policy, ExecutionPolicy):
            raise RegistrationError(f"Task {name!r} policy must be an ExecutionPolicy")
        # Revalidate and copy so even model_construct/model_copy cannot bypass the boundary.
        defaults = ExecutionPolicy() if policy is None else ExecutionPolicy(**policy.model_dump())

        def register(function: TaskFunction[InputT]) -> TaskFunction[InputT]:
            if name in self._tasks:
                raise RegistrationError(f"Task {name!r} is already registered")
            if not inspect.iscoroutinefunction(function):
                raise RegistrationError(f"Task {name!r} must be an async function")
            parameters = list(inspect.signature(function).parameters.values())
            if len(parameters) != 1 or parameters[0].kind not in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            ):
                raise RegistrationError(f"Task {name!r} must accept exactly one positional input")
            try:
                hints = get_type_hints(function)
            except Exception as exc:
                raise RegistrationError(
                    f"Task {name!r} has unresolvable annotations: {exc}"
                ) from exc
            if parameters[0].name in hints and hints[parameters[0].name] is not input_model:
                raise RegistrationError(f"Task {name!r} input annotation must match input_model")
            if "return" in hints and hints["return"] is not type(None):
                raise RegistrationError(f"Task {name!r} return annotation must be None")
            self._tasks[name] = TaskDefinition(name, function, input_model, defaults)
            return function

        return register

    def get(self, name: str) -> TaskDefinition[Any]:
        """Find a registered definition by its exact stable name."""
        try:
            return self._tasks[name]
        except KeyError as exc:
            raise UnknownTaskError(f"Unknown task {name!r}") from exc


def load_registry(reference: str) -> TaskRegistry:
    """Import exactly module:attribute from trusted local task code."""
    if not isinstance(reference, str) or reference.count(":") != 1:
        raise RegistryLoadError("Registry reference must be module:attribute")
    module_name, attribute = reference.split(":")
    if not module_name or module_name.strip() != module_name or not attribute.isidentifier():
        raise RegistryLoadError("Registry reference must be module:attribute")
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        raise RegistryLoadError(f"Cannot import registry module {module_name!r}: {exc}") from exc
    try:
        registry = getattr(module, attribute)
    except Exception as exc:
        raise RegistryLoadError(
            f"Cannot load registry attribute {attribute!r} from {module_name!r}: {exc}"
        ) from exc
    if not isinstance(registry, TaskRegistry):
        raise RegistryLoadError(f"{reference!r} does not refer to a TaskRegistry")
    return registry
