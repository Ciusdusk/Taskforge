import pytest
from pydantic import BaseModel, ValidationError

from taskforge import ExecutionPolicy, TaskRegistry


class Input(BaseModel):
    count: int


async def task(data: Input) -> None:
    pass


@pytest.mark.parametrize(
    "changes",
    [
        {"max_retries": -1},
        {"max_retries": 1.5},
        {"max_retries": True},
        {"max_retries": "1"},
        {"retry_base_seconds": 0},
        {"retry_base_seconds": -1},
        {"retry_base_seconds": float("nan")},
        {"retry_base_seconds": float("inf")},
        {"retry_base_seconds": True},
        {"retry_base_seconds": "1"},
        {"retry_cap_seconds": 0},
        {"retry_cap_seconds": -1},
        {"retry_cap_seconds": float("nan")},
        {"retry_cap_seconds": float("inf")},
        {"retry_base_seconds": 2, "retry_cap_seconds": 1},
        {"timeout_seconds": 0},
        {"timeout_seconds": -1},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
        {"timeout_seconds": True},
        {"timeout_seconds": "300"},
        {"timeout_seconds": None},
        {"unknown": 1},
    ],
)
def test_invalid_policy_is_rejected(changes):
    with pytest.raises(ValidationError):
        ExecutionPolicy(**changes)


def test_defaults_and_boundary_values():
    assert ExecutionPolicy().model_dump() == {
        "max_retries": 0,
        "retry_base_seconds": 1.0,
        "retry_cap_seconds": 60.0,
        "timeout_seconds": 300.0,
    }
    policy = ExecutionPolicy(
        max_retries=2, retry_base_seconds=0.25, retry_cap_seconds=0.25, timeout_seconds=0.01
    )
    assert policy.max_retries == 2
    assert policy.retry_base_seconds == policy.retry_cap_seconds


def test_task_policies_are_immutable_independent_snapshots():
    registry = TaskRegistry()
    original = ExecutionPolicy(max_retries=2)
    registry.task("first", input_model=Input, policy=original)(task)
    registry.task("second", input_model=Input, policy=original)(task)
    registry.task("default", input_model=Input)(task)
    first, second = registry.get("first").policy, registry.get("second").policy
    assert first == second == original
    assert first is not second and first is not original
    with pytest.raises(ValidationError, match="frozen"):
        first.max_retries = 7
    assert second.max_retries == 2
    assert registry.get("default").policy.max_retries == 0
    replacement = original.model_copy(update={"max_retries": 5})
    registry.task("new", input_model=Input, policy=replacement)(task)
    assert registry.get("new").policy.max_retries == 5
    assert first.max_retries == 2
