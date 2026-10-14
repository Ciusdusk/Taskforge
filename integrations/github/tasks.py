"""One registered task and the shared direct/Worker collection boundary."""

import asyncio
import time

import httpx

from integrations.github.auth import Credential, resolve_credential
from integrations.github.config import DEFAULT_POLICY, RefreshInput
from integrations.github.errors import GenerationError, StateError
from integrations.github.http import AttemptBudget, GitHubHTTP
from integrations.github.issues import collect_issues
from integrations.github.lifecycle import protected
from integrations.github.locking import InstanceLocks
from integrations.github.project import ExecutionIdentity, collect_project
from integrations.github.receipts import export_source_locked, source_provenance
from integrations.github.storage import BusinessStore
from taskforge import TaskRegistry, get_execution_context

registry = TaskRegistry()


async def execute_refresh(
    data: RefreshInput,
    execution: ExecutionIdentity,
    *,
    credential: Credential | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    trust_env: bool = True,
) -> bool:
    """Keep completion check, client cleanup, business commit and export under one lock."""
    store = BusinessStore(data.state_directory)
    async with InstanceLocks(store.directory).source(data.source.source_id):
        completed = await store.completed_input(data)
        await store.record_attempt(
            str(data.refresh_id), execution.execution_id, execution.attempt_number
        )
        try:
            if not completed:
                state = await store.source_state(data.source.source_id)
                if (
                    state.generation != data.source_generation
                    or state.source.baseline_key() != data.source.baseline_key()
                ):
                    raise GenerationError("Stale source generation or mismatched baseline")
                credential = credential or await resolve_credential()
                start = time.monotonic()
                async with GitHubHTTP(
                    store,
                    credential,
                    AttemptBudget(data.source.budgets),
                    transport=transport,
                    trust_env=trust_env,
                ) as http:
                    collect = collect_project if data.source.kind == "project" else collect_issues
                    batch = await collect(http, store, data, state.checkpoint, execution)
                    provenance = await protected(asyncio.to_thread(source_provenance))
                    batch = batch.model_copy(
                        update={
                            "coverage": {
                                **batch.coverage,
                                "source": provenance,
                                "elapsed_seconds": time.monotonic() - start,
                                "batch_counts": {
                                    "entities": len(batch.entities),
                                    "versions": len(
                                        {
                                            (e.repository_id, e.kind, e.remote_id, e.fingerprint)
                                            for e in batch.entities
                                        }
                                    ),
                                    "changes": len(batch.changes),
                                    "skill_changes": len(batch.skill_changes),
                                },
                            }
                        }
                    )
                    await store.apply(batch)
            await export_source_locked(store, data.source.source_id)
        except Exception as error:
            # Exception messages from OS/third party code are not public diagnostics.
            await store.record_attempt(
                str(data.refresh_id),
                execution.execution_id,
                execution.attempt_number,
                type(error).__name__,
            )
            raise StateError(
                "GitHub refresh failed; inspect non-sensitive business diagnostics"
            ) from None
        return not completed


@registry.task("github.refresh", input_model=RefreshInput, policy=DEFAULT_POLICY)
async def refresh(data: RefreshInput) -> None:
    context = get_execution_context()
    if context is None:
        raise StateError("github.refresh requires an execution context")
    await execute_refresh(
        data,
        ExecutionIdentity(
            execution_id=context.job_id,
            attempt_number=context.attempt_number,
        ),
    )
