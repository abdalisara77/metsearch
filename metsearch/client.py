"""HTTP access to the Met Collection API: rate limiting, retries, typed errors."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from . import __version__

BASE_URL = "https://collectionapi.metmuseum.org/public/collection"
USER_AGENT = f"metsearch/{__version__}"

DEFAULT_RATE = 40.0  # requests per second
MAX_RATE = 80.0  # the limit the Met documents
MAX_CONCURRENCY = 20
MAX_ATTEMPTS = 5
BASE_BACKOFF_SECONDS = 0.5
REQUEST_TIMEOUT_SECONDS = 20.0

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class MetSearchError(Exception):
    """Base class for every failure this package reports to its caller."""


class ApiError(MetSearchError):
    """The API failed, or kept failing after retries."""


class BlockedError(MetSearchError):
    """The firewall blocked us. Retrying could prolong the block, so we never do."""


class RateLimiter:
    """Spaces request starts evenly, with no burst allowance.

    Each caller reserves the next free slot and sleeps until it arrives. No lock
    is needed: asyncio can't switch tasks between reading and updating the slot.
    """

    def __init__(
        self, rate: float, *, clock: Clock = time.monotonic, sleep: Sleep = asyncio.sleep
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self._interval = 1.0 / rate
        self._clock = clock
        self._sleep = sleep
        self._next_slot = 0.0
        self._paused_until = 0.0

    async def acquire(self) -> None:
        while True:
            now = self._clock()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self._interval
            if slot > now:
                await self._sleep(slot - now)
            # A pause may have begun while we slept. If so, take a slot after it.
            if self._clock() >= self._paused_until:
                return

    def pause(self, seconds: float) -> None:
        """Hold every caller for `seconds`, for example after a 429."""
        self._paused_until = max(self._paused_until, self._clock() + seconds)
        self._next_slot = max(self._next_slot, self._paused_until)


@dataclass(frozen=True)
class SearchPage:
    total: int
    object_ids: list[int]


class MetClient:
    """Typed wrapper over the two API endpoints this tool uses."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        limiter: RateLimiter,
        *,
        max_concurrency: int = MAX_CONCURRENCY,
        max_attempts: int = MAX_ATTEMPTS,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._http = http
        self._limiter = limiter
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._max_attempts = max_attempts
        self._sleep = sleep
        self.request_count = 0

    async def search(
        self,
        query: str,
        *,
        in_title: bool = False,
        in_tags: bool = False,
        date_begin: int | None = None,
        date_end: int | None = None,
        offset: int = 0,
        limit: int = 500,
    ) -> SearchPage:
        params: dict[str, Any] = {"q": query, "offset": offset, "limit": limit}
        # The API treats these as flags that limit `q` to one field. Other values,
        # including text, are silently ignored.
        if in_title:
            params["title"] = "true"
        if in_tags:
            params["tags"] = "true"
        if date_begin is not None and date_end is not None:
            params["dateBegin"] = date_begin
            params["dateEnd"] = date_end
        body = await self._get_json(f"{BASE_URL}/v1.1/search", params)
        if body is None:
            raise ApiError("search endpoint returned 404")
        # The API sends `objectIDs: null`, not [], for an empty page.
        return SearchPage(total=body["total"], object_ids=body.get("objectIDs") or [])

    async def get_object(self, object_id: int) -> dict[str, Any] | None:
        """Return an object's record, or None if it no longer exists.

        Search can list IDs whose records are gone, so a 404 here is routine.
        """
        return await self._get_json(f"{BASE_URL}/v1/objects/{object_id}")

    async def _get_json(
        self, url: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        last_problem = "no attempts made"
        for attempt in range(self._max_attempts):
            delay = BASE_BACKOFF_SECONDS * 2**attempt * random.uniform(0.5, 1.5)
            try:
                async with self._semaphore:
                    await self._limiter.acquire()
                    self.request_count += 1
                    response = await self._http.get(url, params=params)
            except httpx.TransportError as exc:  # includes timeouts
                last_problem = f"{type(exc).__name__}: {exc}"
            else:
                status = response.status_code
                if status == 200:
                    return _parse_json(response)
                if status == 404:
                    return None
                if status == 403:
                    raise BlockedError("the Met's firewall returned 403")
                if status == 429:
                    delay = _retry_after(response) or delay
                    self._limiter.pause(delay)
                elif status < 500:
                    raise ApiError(f"unexpected HTTP {status} from {url}")
                last_problem = f"HTTP {status}"
            if attempt + 1 < self._max_attempts:
                await self._sleep(delay)
        raise ApiError(f"gave up after {self._max_attempts} attempts: {last_problem}")


def _parse_json(response: httpx.Response) -> dict[str, Any]:
    # The firewall can serve an HTML challenge page with a 200.
    try:
        body = response.json()
    except ValueError:
        raise BlockedError("the API returned a non-JSON body") from None
    if not isinstance(body, dict):
        raise ApiError("the API returned an unexpected JSON shape")
    return body


def _retry_after(response: httpx.Response) -> float | None:
    """Read Retry-After in its seconds form. The HTTP-date form falls back to backoff."""
    try:
        return max(0.0, float(response.headers["Retry-After"]))
    except (KeyError, ValueError):
        return None


def build_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT_SECONDS
    )
