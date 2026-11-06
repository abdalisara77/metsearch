"""Find a query's most recent works by widening a date window back from this year.

The API can't sort by date, so we search current_year-1..current_year, then
current_year-2..current_year, and so on. Once a window holds enough IDs, we fetch them
to check they're real, because search can list IDs whose records are gone. The walk
stops when enough have a record and an end date, then ranks them by end date.

The API only returns an object if its whole date range fits inside the window. A
work that began before the window therefore isn't found, even if it ended recently.
That is a deliberate tradeoff: requests stay low, but results aren't guaranteed to
be the most recent by end date across every match.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

from .cache import Artwork, Cache
from .client import ApiError, MetClient, MetSearchError

MAX_RESULTS = 10_000  # the query limit the project brief sets
PAGE_SIZE = 500  # the API's maximum
EARLIEST_YEAR = -300_000  # before any object in the collection


class TooManyResultsError(MetSearchError):
    def __init__(self, total: int) -> None:
        super().__init__(
            f"query matches {total:,} objects, above the {MAX_RESULTS:,} limit; "
            "try a more specific query"
        )
        self.total = total


@dataclass(frozen=True)
class SearchResult:
    artworks: list[Artwork]
    total: int
    objects_fetched: int
    unusable: int
    from_cache: bool


def normalize_query(query: str) -> str:
    return " ".join(query.split()).casefold()


async def find_recent(
    client: MetClient,
    cache: Cache,
    query: str,
    *,
    limit: int = 5,
    refresh: bool = False,
    current_year: int | None = None,
) -> SearchResult:
    query = normalize_query(query)
    if refresh:
        cache.forget_query(query)

    record = cache.get_query(query)
    if record is not None and record.window_begin is not None:
        cached = cache.top_objects(query, limit=limit)
        enough_dated = sum(artwork.found for artwork in cached) >= limit
        walk_exhausted = (
            len(cache.query_ids(query)) >= record.total
            or record.window_begin <= EARLIEST_YEAR
        )
        if enough_dated or walk_exhausted:
            return SearchResult(
                cached, record.total, 0, cache.count_unusable(query), from_cache=True
            )

    if record is not None:
        total = record.total
    else:
        total = (await client.search(query, limit=1)).total
        if total > MAX_RESULTS:
            raise TooManyResultsError(total)
        if total == 0:
            return SearchResult([], 0, 0, 0, from_cache=False)
        cache.save_query(query, total)

    year = current_year if current_year is not None else datetime.now(timezone.utc).year
    wanted = min(limit, total)
    fetched: set[int] = set()
    begin = year - 1
    while True:
        object_ids = await _list_ids(client, query, begin, year)
        at_floor = begin <= EARLIEST_YEAR
        # A window with too few IDs can't hold enough real works, so skip fetching it.
        if len(object_ids) >= wanted or at_floor:
            to_fetch = object_ids - fetched
            if not refresh:
                to_fetch = set(cache.stale_object_ids(to_fetch))
            await _fetch_objects(client, cache, to_fetch)
            fetched |= to_fetch
            if (
                cache.count_dated(object_ids) >= limit
                or len(object_ids) >= total
                or at_floor
            ):
                break
        begin = max(begin - 1, EARLIEST_YEAR)

    # Recorded only now, so an interrupted run can't pass for a finished one.
    cache.save_window(query, begin, object_ids)
    return SearchResult(
        cache.top_objects(query, limit=limit),
        total,
        len(fetched),
        cache.count_unusable(query),
        from_cache=False,
    )


async def _list_ids(client: MetClient, query: str, begin: int, end: int) -> set[int]:
    """Collect every ID in a window, and check that none were lost while paging.

    The API's result order isn't stable between requests. A lost ID could silently
    change the top results, so a mismatch fails the run.
    """
    ids: set[int] = set()
    offset = 0
    while True:
        page = await client.search(
            query, date_begin=begin, date_end=end, offset=offset, limit=PAGE_SIZE
        )
        ids.update(page.object_ids)
        offset += PAGE_SIZE
        if not page.object_ids or offset >= page.total:
            break
    if len(ids) != page.total:
        raise ApiError(
            f"paging returned {len(ids)} unique IDs but the API reported {page.total}; "
            "try again"
        )
    return ids


async def _fetch_objects(client: MetClient, cache: Cache, object_ids: set[int]) -> None:
    """Fetch objects concurrently, saving each as it arrives so a crash loses little."""

    async def fetch_one(object_id: int) -> None:
        cache.save_object(object_id, await client.get_object(object_id))

    try:
        async with asyncio.TaskGroup() as group:
            for object_id in object_ids:
                group.create_task(fetch_one(object_id))
    except ExceptionGroup as group_error:
        # The first failure cancels the rest, so callers get one plain exception.
        raise group_error.exceptions[0] from None
