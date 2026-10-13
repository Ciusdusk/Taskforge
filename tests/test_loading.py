import sys

import pytest

from taskforge import RegistryLoadError, TaskRegistry, load_registry


@pytest.fixture
def task_module(tmp_path, monkeypatch):
    name = "t02_registry_fixture"
    path = tmp_path / f"{name}.py"
    path.write_text(
        "from pydantic import BaseModel\n"
        "from taskforge import TaskRegistry\n"
        "class Input(BaseModel):\n"
        "    count: int\n"
        "registry = TaskRegistry()\n"
        "@registry.task('count', input_model=Input)\n"
        "async def count(data: Input) -> None:\n"
        "    pass\n"
        "other = object()\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    yield name
    sys.modules.pop(name, None)


def test_explicit_load_imports_and_returns_the_registry(task_module):
    registry = load_registry(f"{task_module}:registry")
    assert isinstance(registry, TaskRegistry)
    assert registry is load_registry(f"{task_module}:registry")
    assert registry.get("count").validate_input({"count": "2"}).count == 2


@pytest.mark.parametrize(
    "reference", ["", "module", ":registry", "module:", "a:b:c", "a:b.c", None]
)
def test_invalid_reference_is_rejected(reference):
    with pytest.raises(RegistryLoadError, match="module:attribute"):
        load_registry(reference)


def test_missing_module_has_an_explanatory_cause():
    with pytest.raises(RegistryLoadError, match="Cannot import.*t02_nonexistent") as error:
        load_registry("t02_nonexistent:registry")
    assert isinstance(error.value.__cause__, ModuleNotFoundError)


def test_import_execution_failure_is_reported(tmp_path, monkeypatch):
    (tmp_path / "t02_broken_module.py").write_text("raise RuntimeError('import exploded')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(RegistryLoadError, match="import exploded") as error:
        load_registry("t02_broken_module:registry")
    assert isinstance(error.value.__cause__, RuntimeError)


def test_missing_attribute_has_an_explanatory_cause(task_module):
    with pytest.raises(RegistryLoadError, match="missing.*t02_registry_fixture") as error:
        load_registry(f"{task_module}:missing")
    assert isinstance(error.value.__cause__, AttributeError)


def test_nonregistry_attribute_is_rejected(task_module):
    with pytest.raises(RegistryLoadError, match="does not refer to a TaskRegistry"):
        load_registry(f"{task_module}:other")
