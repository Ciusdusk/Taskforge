import json
from datetime import UTC, datetime

import pytest
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    Json,
    RootModel,
    ValidationError,
    model_serializer,
)

from taskforge import (
    ExecutionPolicy,
    InputSerializationError,
    RegistrationError,
    TaskRegistry,
    UnknownTaskError,
)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int


async def task(data: Input) -> None:
    pass


@pytest.mark.asyncio
async def test_registration_preserves_original_function_and_direct_await():
    registry = TaskRegistry()
    received = []

    async def original(data: Input) -> None:
        received.append(data)

    decorated = registry.task("sync.count", input_model=Input)(original)
    definition = registry.get("sync.count")
    assert decorated is original
    assert definition.function is original
    assert definition.name == "sync.count"
    assert definition.input_model is Input
    value = definition.validate_input({"count": "7"})
    assert value == Input(count=7)
    await decorated(value)
    await definition.function(definition.validate_json('{"count": 8}'))
    assert received == [Input(count=7), Input(count=8)]


def test_duplicate_and_failed_registration_preserve_existing_task():
    registry = TaskRegistry()
    registry.task("count", input_model=Input)(task)
    original = registry.get("count")
    with pytest.raises(RegistrationError, match="already registered"):
        registry.task("count", input_model=Input)(task)
    with pytest.raises(RegistrationError, match="async function"):
        registry.task("invalid", input_model=Input)(lambda data: None)
    assert registry.get("count") is original
    with pytest.raises(UnknownTaskError, match="invalid"):
        registry.get("invalid")


def test_registry_instances_are_isolated():
    first, second = TaskRegistry(), TaskRegistry()
    first.task("count", input_model=Input)(task)
    with pytest.raises(UnknownTaskError, match="count"):
        second.get("count")
    second.task("count", input_model=Input, policy=ExecutionPolicy(max_retries=2))(task)
    assert first.get("count").policy.max_retries == 0
    assert second.get("count").policy.max_retries == 2


async def no_input() -> None:
    pass


async def two_inputs(data: Input, other: Input) -> None:
    pass


async def optional_second(data: Input, other: Input | None = None) -> None:
    pass


async def keyword_input(*, data: Input) -> None:
    pass


async def variadic(*data: Input) -> None:
    pass


async def keyword_variadic(**data: Input) -> None:
    pass


async def wrong_model(data: int) -> None:
    pass


async def wrong_return(data: Input) -> int:
    return data.count


async def unresolved(data) -> None:
    pass


unresolved.__annotations__["data"] = "MissingInput"


async def generator(data: Input):
    yield data


@pytest.mark.parametrize(
    ("function", "message"),
    [
        (lambda data: None, "async function"),
        (generator, "async function"),
        (no_input, "one positional input"),
        (two_inputs, "one positional input"),
        (optional_second, "one positional input"),
        (keyword_input, "one positional input"),
        (variadic, "one positional input"),
        (keyword_variadic, "one positional input"),
        (wrong_model, "input annotation"),
        (wrong_return, "return annotation"),
        (unresolved, "unresolvable annotations"),
    ],
)
def test_illegal_functions_are_rejected_without_registering(function, message):
    registry = TaskRegistry()
    with pytest.raises(RegistrationError, match=message):
        registry.task("invalid", input_model=Input)(function)
    with pytest.raises(UnknownTaskError):
        registry.get("invalid")


@pytest.mark.asyncio
async def test_positional_only_and_unannotated_functions_are_callable():
    registry = TaskRegistry()
    received = []

    async def positional(data: Input, /) -> None:
        received.append(data.count)

    async def unannotated(data):
        received.append(data.count)

    registry.task("positional", input_model=Input)(positional)
    registry.task("unannotated", input_model=Input)(unannotated)
    for name in ("positional", "unannotated"):
        definition = registry.get(name)
        await definition.function(definition.validate_input({"count": 3}))
    assert received == [3, 3]


@pytest.mark.parametrize("model", [int, dict, BaseModel, Input(count=1), RootModel[int]])
def test_illegal_models_are_rejected(model):
    with pytest.raises(RegistrationError, match="concrete Pydantic object model"):
        TaskRegistry().task("invalid", input_model=model)


@pytest.mark.parametrize("name", ["", " leading", "trailing ", None, 1])
def test_invalid_names_are_rejected(name):
    with pytest.raises(RegistrationError, match="Task name"):
        TaskRegistry().task(name, input_model=Input)


def test_policy_must_be_explicit_valid_model():
    registry = TaskRegistry()
    with pytest.raises(RegistrationError, match="ExecutionPolicy"):
        registry.task("invalid", input_model=Input, policy={"max_retries": 1})
    with pytest.raises(ValidationError):
        registry.task(
            "invalid", input_model=Input, policy=ExecutionPolicy.model_construct(max_retries=-1)
        )
    with pytest.raises(UnknownTaskError):
        registry.get("invalid")


@pytest.mark.parametrize("data", [{"count": "bad"}, {}, {"count": 1, "unknown": 2}, []])
def test_invalid_python_input_retains_pydantic_errors(data):
    registry = TaskRegistry()
    registry.task("count", input_model=Input)(task)
    definition = registry.get("count")
    with pytest.raises(ValidationError):
        definition.validate_input(data)
    with pytest.raises(ValidationError):
        definition.serialize_input(data)


@pytest.mark.parametrize("payload", ['{"count":"bad"}', "{}", "[]", "not json"])
def test_stored_json_is_revalidated(payload):
    registry = TaskRegistry()
    registry.task("count", input_model=Input)(task)
    with pytest.raises(ValidationError):
        registry.get("count").validate_json(payload)


@pytest.mark.parametrize("extra", ["ignore", "allow", "forbid"])
def test_unknown_fields_follow_the_declared_model(extra):
    class ConfiguredInput(BaseModel):
        model_config = ConfigDict(extra=extra)
        count: int

    async def configured(data: ConfiguredInput) -> None:
        pass

    registry = TaskRegistry()
    registry.task("configured", input_model=ConfiguredInput)(configured)
    definition = registry.get("configured")
    raw = {"count": "4", "extra": "kept"}
    assert ConfiguredInput.model_config["extra"] == extra
    if extra == "forbid":
        with pytest.raises(ValidationError):
            definition.validate_input(raw)
        with pytest.raises(ValidationError):
            definition.validate_json(json.dumps(raw))
    else:
        restored = definition.validate_json(definition.serialize_input(raw))
        assert restored.count == 4
        assert ("extra" in restored.model_dump()) == (extra == "allow")


def test_aliases_json_fields_and_strict_json_types_round_trip():
    class RichInput(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid")
        count: int = Field(alias="number")
        timestamp: datetime
        nested: Json[list[int]]

    async def rich(data: RichInput) -> None:
        pass

    registry = TaskRegistry()
    registry.task("rich", input_model=RichInput)(rich)
    definition = registry.get("rich")
    raw = {"number": 1, "timestamp": datetime(2026, 10, 13, tzinfo=UTC), "nested": "[2, 3]"}
    original = definition.validate_input(raw)
    payload = definition.serialize_input(raw)
    assert "number" in json.loads(payload)
    assert definition.validate_json(payload) == original
    with pytest.raises(ValidationError):
        definition.validate_input({**raw, "number": "1"})
    with pytest.raises(ValidationError):
        definition.validate_input(json.loads(payload))
    assert RichInput.model_config["strict"] is True


def test_unserializable_input_has_a_clear_error():
    class OpaqueInput(BaseModel):
        model_config = ConfigDict(arbitrary_types_allowed=True)
        value: object

    async def opaque(data: OpaqueInput) -> None:
        pass

    registry = TaskRegistry()
    registry.task("opaque", input_model=OpaqueInput)(opaque)
    definition = registry.get("opaque")
    raw = {"value": object()}
    assert definition.validate_input(raw).value is raw["value"]
    with pytest.raises(InputSerializationError, match="opaque.*JSON") as error:
        definition.serialize_input(raw)
    assert error.value.__cause__ is not None


def test_serialized_input_must_be_revalidatable():
    class ExcludedInput(BaseModel):
        value: int = Field(exclude=True)

    async def excluded(data: ExcludedInput) -> None:
        pass

    registry = TaskRegistry()
    registry.task("excluded", input_model=ExcludedInput)(excluded)
    with pytest.raises(InputSerializationError, match="excluded.*round-trip"):
        registry.get("excluded").serialize_input({"value": 1})


def test_nonobject_serialization_is_rejected():
    class ArrayInput(BaseModel):
        value: int

        @model_serializer
        def serialize(self) -> list[int]:
            return [self.value]

    async def array(data: ArrayInput) -> None:
        pass

    registry = TaskRegistry()
    registry.task("array", input_model=ArrayInput)(array)
    with pytest.raises(InputSerializationError, match="JSON object"):
        registry.get("array").serialize_input({"value": 1})
