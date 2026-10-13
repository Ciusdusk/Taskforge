import sys
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from math import isfinite, ldexp

import pytest

from taskforge import AttemptOutcome, AttemptStatus, ErrorKind, Job, JobStatus
from taskforge.policy import decide_completion, retry_delay_seconds


@pytest.fixture
def job():
    return Job(
        id="policy-test",
        task_name="demo",
        params_json="{}",
        status=JobStatus.RUNNING,
        created_at=datetime(2026, 10, 13, tzinfo=UTC),
        started_at=datetime(2026, 10, 13, tzinfo=UTC),
        finished_at=None,
        next_run_at=None,
        attempt_count=1,
        max_retries=3,
        retry_base_seconds=0.25,
        retry_cap_seconds=3.0,
        timeout_seconds=1.0,
        last_error_kind=None,
        last_error_message=None,
    )


@pytest.mark.parametrize("number,expected", [(1, 0.25), (2, 0.5), (3, 1), (4, 2), (5, 3)])
def test_exact_exponential_backoff_and_cap(job, number, expected):
    assert retry_delay_seconds(replace(job, attempt_count=number)) == expected


@pytest.mark.parametrize(
    "base,cap,number,expected",
    [
        (1.0, 1.0, 1, 1.0),
        (0.125, 8.0, 7, 8.0),
        (0.125, 8.0, 10**1000, 8.0),
        (sys.float_info.max / 2, sys.float_info.max, 2, sys.float_info.max),
        (sys.float_info.max / 2, sys.float_info.max, 3, sys.float_info.max),
        (5e-324, sys.float_info.max, 1075, 1.0),
        (5e-324, sys.float_info.max, 2098, ldexp(1.0, 1023)),
        (5e-324, sys.float_info.max, 2099, sys.float_info.max),
        (5e-324, 5e-324, 10**1000, 5e-324),
    ],
)
def test_extreme_finite_values_and_large_attempts(job, base, cap, number, expected):
    delay = retry_delay_seconds(
        replace(job, retry_base_seconds=base, retry_cap_seconds=cap, attempt_count=number)
    )
    assert delay == expected and isfinite(delay) and delay > 0


def test_backoff_requires_a_consumed_attempt(job):
    with pytest.raises(ValueError, match="consumed attempt"):
        retry_delay_seconds(replace(job, attempt_count=0))


@pytest.mark.parametrize("kind", [ErrorKind.TASK_ERROR, ErrorKind.TIMEOUT, ErrorKind.CONFIG_ERROR])
@pytest.mark.parametrize("number", [1, 3, 4])
def test_disposition_uses_failure_kind_and_consumed_budget(job, kind, number):
    snapshot = replace(job, attempt_count=number)
    decision = decide_completion(snapshot, AttemptOutcome(AttemptStatus.FAILED, kind))
    retry = kind is not ErrorKind.CONFIG_ERROR and number < 4
    assert decision.status is (JobStatus.RETRY_WAIT if retry else JobStatus.FAILED)
    assert decision.retry_delay_seconds == (retry_delay_seconds(snapshot) if retry else None)
    assert snapshot == replace(job, attempt_count=number)
    with pytest.raises(FrozenInstanceError):
        decision.status = JobStatus.SUCCESS


def test_success_and_zero_retry_budget(job):
    success = decide_completion(job, AttemptOutcome(AttemptStatus.SUCCESS))
    assert success.status is JobStatus.SUCCESS and success.retry_delay_seconds is None
    failed = decide_completion(
        replace(job, max_retries=0), AttemptOutcome(AttemptStatus.FAILED, ErrorKind.TASK_ERROR)
    )
    assert failed.status is JobStatus.FAILED and failed.retry_delay_seconds is None


@pytest.mark.parametrize("number", [1, 2, 4])
def test_interruption_uses_consumed_budget_and_snapshot_backoff(job, number):
    snapshot = replace(job, attempt_count=number)
    decision = decide_completion(
        snapshot, AttemptOutcome(AttemptStatus.INTERRUPTED, ErrorKind.INTERRUPTED)
    )
    assert decision.status is (JobStatus.RETRY_WAIT if number < 4 else JobStatus.FAILED)
    assert decision.retry_delay_seconds == (retry_delay_seconds(snapshot) if number < 4 else None)
