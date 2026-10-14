"""Fixed-origin GitHub GETs, conditional pagination and shared durable cooldown."""

import asyncio
import json
import math
import re
import time
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urljoin, urlsplit

import httpx
from pydantic import JsonValue

from integrations.github.auth import Credential
from integrations.github.config import Budgets
from integrations.github.errors import BudgetError, HTTPError, PaginationError
from integrations.github.lifecycle import protected
from integrations.github.locking import InstanceLocks
from integrations.github.storage import BusinessStore, CachedResponse

API_ROOT = "https://api.github.com"
API_VERSION = "2026-03-10"
ACCEPT = "application/vnd.github+json"


@dataclass
class AttemptBudget:
    limits: Budgets
    requests: int = 0
    waited_seconds: float = 0


def api_url(value: str, *, base: str = API_ROOT + "/") -> str:
    candidate = urljoin(base, value)
    parts = urlsplit(candidate)
    if (
        parts.scheme != "https"
        or parts.hostname != "api.github.com"
        or parts.port not in (None, 443)
        or parts.username
        or parts.password
        or parts.fragment
    ):
        raise HTTPError("URL is outside the fixed GitHub API origin")
    return str(httpx.URL(candidate))


def next_link(link: str) -> str | None:
    if not link:
        return None
    pattern = r'\s*<([^<>]+)>\s*;\s*rel="([a-z ]+)"\s*(?:,|$)'
    matches = list(re.finditer(pattern, link))
    if "".join(match.group(0) for match in matches) != link:
        raise PaginationError("Malformed pagination Link")
    next_urls = [match.group(1) for match in matches if "next" in match.group(2).split()]
    if len(next_urls) > 1:
        raise PaginationError("Ambiguous pagination Link")
    for match in matches:
        api_url(match.group(1))
    return next_urls[0] if next_urls else None


class GitHubHTTP:
    def __init__(
        self,
        store: BusinessStore,
        credential: Credential,
        budget: AttemptBudget,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        trust_env: bool = True,
    ) -> None:
        self.store = store
        self.credential = credential
        self.budget = budget
        self.locks = InstanceLocks(store.directory)
        limits = budget.limits
        self.client = httpx.AsyncClient(
            transport=transport,
            trust_env=trust_env,
            follow_redirects=False,
            timeout=httpx.Timeout(
                connect=limits.connect_seconds,
                write=limits.write_seconds,
                pool=limits.pool_seconds,
                read=limits.read_seconds,
            ),
        )

    async def __aenter__(self) -> "GitHubHTTP":
        return self

    async def __aexit__(self, *args: object) -> None:
        await protected(self.client.aclose())

    def _safe_url(self, value: str, *, base: str = API_ROOT + "/") -> str:
        if self.credential.token in value:
            raise HTTPError("Sensitive data is not allowed in request URLs")
        invalid = False
        url = ""
        try:
            url = api_url(value, base=base)
        except (ValueError, OverflowError):
            invalid = True
        if invalid:
            raise HTTPError("Invalid API URL")
        query = parse_qsl(urlsplit(url).query, keep_blank_values=True)
        if self.credential.token in url or any(
            name.lower() in {"token", "access_token", "authorization", "client_secret"}
            for name, _ in query
        ):
            raise HTTPError("Sensitive data is not allowed in request URLs")
        return url

    def request_key(self, url: str) -> str:
        # Full non-sensitive identity; auth is deliberately absent.
        return json.dumps(["GET", self._safe_url(url), ACCEPT, API_VERSION], separators=(",", ":"))

    async def _wait_cooldown(self) -> None:
        while True:
            until, _ = await self.store.cooldown()
            remaining = max(0.0, until - time.time())
            if self.budget.waited_seconds > self.budget.limits.max_wait_seconds:
                raise BudgetError("Cumulative cooldown wait budget exhausted")
            if remaining == 0:
                return
            available = self.budget.limits.max_wait_seconds - self.budget.waited_seconds
            if remaining > available:
                raise BudgetError("Persistent cooldown exceeds remaining attempt wait budget")
            started = time.monotonic()
            try:
                await asyncio.sleep(remaining)
            finally:
                self.budget.waited_seconds += time.monotonic() - started

    async def _rate_limit(self, response: httpx.Response) -> bool:
        now = time.time()
        deadlines = []
        retry_after = response.headers.get("retry-after")
        if retry_after is not None:
            try:
                seconds = float(retry_after)
                if math.isfinite(seconds) and seconds >= 0:
                    deadlines.append(now + seconds)
            except ValueError:
                try:
                    deadline = parsedate_to_datetime(retry_after).astimezone(UTC).timestamp()
                    deadlines.append(deadline)
                except (ValueError, TypeError, OverflowError):
                    pass
        primary = response.headers.get("x-ratelimit-remaining") == "0"
        if primary:
            try:
                reset = float(response.headers.get("x-ratelimit-reset", ""))
                if math.isfinite(reset):
                    deadlines.append(reset)
            except ValueError:
                pass
        secondary = response.status_code == 429 or (
            response.status_code == 403
            and (
                "secondary rate limit" in response.text.lower()
                or "abuse detection" in response.text.lower()
            )
        )
        limited = response.status_code in (403, 429) and (
            primary or secondary or retry_after is not None
        )
        if limited or primary:
            if limited and not deadlines:
                _, count = await self.store.cooldown()
                deadlines.append(now + 60 * 2 ** min(count, 10))
            if deadlines:
                await self.store.extend_cooldown(max(deadlines), secondary=secondary)
        return limited

    async def get(self, url: str) -> CachedResponse:
        url = self._safe_url(url)
        failure: str | None = None
        try:
            async with asyncio.timeout(self.budget.limits.request_seconds):
                return await protected(self._get(url), cancel=True)
        except TimeoutError:
            failure = "HTTP total request wait timed out"
        except httpx.TimeoutException:
            failure = "HTTP phase timed out"
        except httpx.HTTPError:
            failure = "HTTP transport failed"
        except (ValueError, OverflowError):
            failure = "Invalid HTTP response or URL"
        # Raise outside the handler so the original request/response cannot enter the chain.
        raise HTTPError(failure)

    async def _get(self, url: str) -> CachedResponse:
        async with self.locks.hold(self.locks.request_path, wait=True):
            await self._wait_cooldown()
            seen: set[str] = set()
            while True:
                if url in seen or len(seen) >= 6:
                    raise HTTPError("Redirect cycle or redirect budget exhausted")
                seen.add(url)
                if self.budget.requests >= self.budget.limits.max_requests:
                    raise BudgetError("Actual HTTP request budget exhausted")
                key = self.request_key(url)
                cached = await self.store.cache_get(key)
                headers = {
                    "Accept": ACCEPT,
                    "X-GitHub-Api-Version": API_VERSION,
                    "Authorization": "Bearer " + self.credential.token,
                }
                if cached is not None and cached.etag:
                    headers["If-None-Match"] = cached.etag
                self.budget.requests += 1
                response = await self.client.get(url, headers=headers)
                try:
                    if await self._rate_limit(response):
                        raise HTTPError("GitHub rate limited; shared cooldown persisted")
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise HTTPError("Redirect has no Location")
                        url = self._safe_url(location, base=url)
                        continue
                    if response.status_code == 304:
                        if cached is None:
                            raise HTTPError("304 has no corresponding cached response")
                        return cached
                    if response.status_code != 200:
                        raise HTTPError(f"GitHub returned HTTP {response.status_code}")
                    etag = response.headers.get("etag")
                    link = response.headers.get("link", "")
                    if self.credential.token in response.text or any(
                        self.credential.token in value for value in (etag or "", link)
                    ):
                        raise HTTPError("Sensitive response refused")
                    body: JsonValue = response.json()
                    if self.credential.token in json.dumps(body, ensure_ascii=False):
                        raise HTTPError("Sensitive response refused")
                    next_link(link)
                    value = CachedResponse(etag, body, link)
                    await self.store.cache_put(key, value)
                    return value
                finally:
                    await protected(response.aclose())

    async def paginate(self, path: str, *, list_field: str | None = None) -> list[JsonValue]:
        url = self._safe_url(path)
        parts = urlsplit(url)
        params = dict(parse_qsl(parts.query, keep_blank_values=True))
        params.update(per_page="100", page="1")
        url = str(httpx.URL(url).copy_with(params=params))
        expected = dict(params)
        records: list[JsonValue] = []
        seen: set[str] = set()
        for page in range(1, self.budget.limits.max_pages + 1):
            if url in seen:
                raise PaginationError("Pagination cycle")
            seen.add(url)
            value = await self.get(url)
            items = value.body
            if list_field is not None:
                if not isinstance(items, dict) or list_field not in items:
                    raise PaginationError("Missing paginated list field")
                items = items[list_field]
            if not isinstance(items, list) or len(items) > 100:
                raise PaginationError("Invalid paginated list")
            records.extend(items)
            following = next_link(value.link)
            if following is None:
                return records
            url = self._safe_url(following)
            following_parts = urlsplit(url)
            expected["page"] = str(page + 1)
            if (
                following_parts.path != parts.path
                or dict(parse_qsl(following_parts.query, keep_blank_values=True)) != expected
            ):
                raise PaginationError("Pagination changed endpoint, filters or page sequence")
        raise BudgetError("List page budget exhausted; collection is incomplete")
