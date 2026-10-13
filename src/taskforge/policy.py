"""Pure completion decisions using the persisted Job policy snapshot."""

from dataclasses import dataclass
from math import frexp, ldexp

from taskforge.models import AttemptOutcome, AttemptStatus, ErrorKind, Job, JobStatus


@dataclass(frozen=True, slots=True)
class CompletionDecision:
    status: JobStatus
    retry_delay_seconds: float | None = None


def retry_delay_seconds(job: Job) -> float:
    """min(cap, base * 2**(k-1)), without constructing an unbounded power."""
    if job.attempt_count < 1:
        raise ValueError("Retry delay requires a consumed attempt")
    base, base_exponent = frexp(job.retry_base_seconds)
    cap, cap_exponent = frexp(job.retry_cap_seconds)
    exponent = base_exponent + job.attempt_count - 1
    if exponent > cap_exponent or (exponent == cap_exponent and base >= cap):
        return job.retry_cap_seconds
    return ldexp(base, exponent)


def decide_completion(job: Job, outcome: AttemptOutcome) -> CompletionDecision:
    """Retry business failures, timeouts and interruptions with remaining budget."""
    if outcome.status is AttemptStatus.SUCCESS:
        return CompletionDecision(JobStatus.SUCCESS)
    if (
        outcome.status is AttemptStatus.FAILED
        and outcome.error_kind in (ErrorKind.TASK_ERROR, ErrorKind.TIMEOUT)
        or outcome.status is AttemptStatus.INTERRUPTED
        and outcome.error_kind is ErrorKind.INTERRUPTED
    ) and job.attempt_count < job.max_retries + 1:
        return CompletionDecision(JobStatus.RETRY_WAIT, retry_delay_seconds(job))
    return CompletionDecision(JobStatus.FAILED)
