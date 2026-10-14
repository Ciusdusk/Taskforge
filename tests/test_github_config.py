import json
from uuid import uuid4

import pytest
from integrations.github.config import (
    DEFAULT_POLICY,
    Budgets,
    GitHubConfig,
    IssueFilter,
    RefreshInput,
    Source,
)
from pydantic import ValidationError


def test_seven_defaults_and_roundtrip(tmp_path):
    config = GitHubConfig.load()
    assert len(config.sources) == 7
    assert [s.repository for s in config.sources] == [
        "mattpocock/skills",
        "addyosmani/agent-skills",
        "yetone/magpie",
        "tt-a1i/archify",
        "deepseek-ai/deepseek-harness",
        "morluto/rea",
        "openai/codex",
    ]
    assert [s.scan_skills for s in config.sources] == [
        True,
        True,
        False,
        False,
        False,
        False,
        False,
    ]
    assert [s.kind for s in config.sources] == ["project"] * 6 + ["issues"]
    assert (
        DEFAULT_POLICY.max_retries,
        DEFAULT_POLICY.retry_base_seconds,
        DEFAULT_POLICY.retry_cap_seconds,
        DEFAULT_POLICY.timeout_seconds,
    ) == (2, 5, 60, 300)
    limits = Budgets()
    assert (
        limits.connect_seconds,
        limits.write_seconds,
        limits.pool_seconds,
        limits.read_seconds,
        limits.request_seconds,
    ) == (5, 5, 5, 20, 30)
    assert (limits.max_pages, limits.max_requests, limits.max_commits, limits.max_wait_seconds) == (
        10,
        100,
        500,
        30,
    )
    data = RefreshInput(
        refresh_id=uuid4(),
        state_directory=tmp_path / "a" / ".." / "state",
        source=config.sources[0],
        source_generation=1,
    )
    assert data.state_directory == tmp_path / "state"
    assert RefreshInput.model_validate_json(data.model_dump_json()) == data
    assert GitHubConfig.model_validate_json(config.model_dump_json()) == config
    assert "token" not in json.dumps(data.model_dump(mode="json"))
    assert RefreshInput(
        refresh_id=uuid4(), source=config.sources[0], source_generation=1
    ).state_directory.is_absolute()


@pytest.mark.parametrize(
    "model,data",
    [
        (Budgets, {"token": "fake"}),
        (IssueFilter, {"extra": 1}),
        (GitHubConfig, {"sources": [], "extra": 1}),
        (Source, {"source_id": "a", "repository": "a/b", "kind": "project", "token": "fake"}),
        (Source, {"source_id": "a", "repository": "a/b", "kind": "project", "issue_filter": {}}),
        (Source, {"source_id": "a", "repository": "a/b", "kind": "issues", "scan_skills": True}),
        (Source, {"source_id": "../a", "repository": "a/b", "kind": "project"}),
        (Source, {"source_id": "a", "repository": "../b", "kind": "project"}),
        (Source, {"source_id": "a", "repository": "a/b", "kind": "other"}),
        (IssueFilter, {"numbers": [True]}),
        (IssueFilter, {"numbers": [0]}),
        (IssueFilter, {"labels": [" "]}),
    ],
)
def test_invalid_configuration(model, data):
    with pytest.raises(ValidationError):
        model.model_validate(data)


@pytest.mark.parametrize("field", list(Budgets.model_fields))
@pytest.mark.parametrize("bad", [True, "1", float("nan"), float("inf"), -1])
def test_budget_types_and_boundaries(field, bad):
    with pytest.raises(ValidationError):
        Budgets.model_validate({field: bad})


@pytest.mark.parametrize("field", [f for f in Budgets.model_fields if f != "max_wait_seconds"])
def test_zero_budget_rejected(field):
    with pytest.raises(ValidationError):
        Budgets.model_validate({field: 0})


def test_filter_and_baseline_identity():
    source = Source(
        source_id="x",
        repository="Owner/Repo",
        kind="issues",
        issue_filter=IssueFilter(labels=["b", "a", "a"], numbers=[2, 1, 2]),
    )
    assert source.repository == "owner/repo"
    assert source.issue_filter.labels == ("a", "b")
    assert source.issue_filter.numbers == (1, 2)
    updated = Source(**{**source.model_dump(), "budgets": {"max_pages": 20}})
    assert source.baseline_key() == updated.baseline_key()
    with pytest.raises(ValidationError):
        GitHubConfig(sources=[source, source])
    with pytest.raises(ValidationError):
        Budgets(max_commits=501)


@pytest.mark.parametrize(
    "changes",
    [
        {"source_generation": 0},
        {"source_generation": True},
        {"operation": "other"},
        {"token": "fake"},
        {"state_directory": ":memory:"},
        {"state_directory": "C:\\state"},
    ],
)
def test_refresh_input_validation(changes):
    data = {
        "refresh_id": str(uuid4()),
        "source": GitHubConfig.load().sources[0].model_dump(),
        "source_generation": 1,
        **changes,
    }
    with pytest.raises(ValidationError):
        RefreshInput.model_validate(data)
