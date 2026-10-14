"""Acceptance verifies observation payloads without personal credentials or networking."""

from uuid import uuid4

import httpx
import pytest
from integrations.github.acceptance import observed_issue_payloads
from integrations.github.auth import Credential
from integrations.github.config import RefreshInput
from integrations.github.http import AttemptBudget, GitHubHTTP
from integrations.github.issues import refresh_issues
from integrations.github.project import ExecutionIdentity
from integrations.github.storage import CachedResponse
from tests.test_github_issues import API, TOKEN, issue, ready

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_saved_issue_requires_exact_actual_payload_and_utc_upper_bound(tmp_path):
    store, source = await ready(tmp_path)
    data = RefreshInput(
        refresh_id=uuid4(), state_directory=tmp_path, source=source, source_generation=1
    )
    # Clearly historical timestamp; the acceptance path uses real UTC, not a fake clock.
    raw = issue(updated_at="2026-01-02T00:00:00Z")
    api = API([raw])
    await refresh_issues(
        data,
        Credential(TOKEN),
        ExecutionIdentity(execution_id="controlled", attempt_number=1),
        transport=httpx.MockTransport(api.handler),
        trust_env=False,
    )
    entity = (await store.entities(42, "issue"))[0]
    async with GitHubHTTP(
        store,
        Credential(TOKEN),
        AttemptBudget(data.source.budgets),
        transport=httpx.MockTransport(lambda request: pytest.fail("unexpected HTTP")),
        trust_env=False,
    ) as http:
        observations = await observed_issue_payloads(http, store, data.source.source_id)
        assert observations[entity.remote_id, entity.fingerprint]["verified"]
        proof = observations[entity.remote_id, entity.fingerprint]
        key = http.request_key(proof["request_url"])
        cached = await store.cache_get(key)
        assert cached is not None
        raw["title"] = "Changed payload must not prove the saved content"
        await store.cache_put(key, CachedResponse(cached.etag, [raw], cached.link))
        observations = await observed_issue_payloads(http, store, data.source.source_id)
        assert (entity.remote_id, entity.fingerprint) not in observations
        raw["updated_at"] = "2099-01-01T00:00:00Z"
        await store.cache_put(key, CachedResponse(cached.etag, [raw], cached.link))
        assert not await observed_issue_payloads(http, store, data.source.source_id)
        assert http.budget.requests == 0
