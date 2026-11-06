"""Find a query's most recent works by widening a date window back from this year.

A work must match three searches: the query anywhere, a title term in the title, and
a tags term in the subject tags. The title and tags terms default to the query.

The API can't sort by date, so we search current_year-1..current_year, then
current_year-2..current_year, and so on. Once a window holds enough works matching all
three, we fetch them to check they're real, because search can list IDs whose records
are gone. The walk stops when enough have a record and an end date, then ranks them.

The API only returns an object if its whole date range fits inside the window. A
work that began before the window therefore isn't found, even if it ended recently.
That is a deliberate tradeoff: requests stay low, but results aren't guaranteed to
be the most recent by end date across every match.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from .cache import Artwork, Cache
from .client import ApiError, MetClient, MetSearchError, SearchPage

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


@dataclass(frozen=True)
class _Search:
    term: str
    in_title: bool = False
    in_tags: bool = False


def normalize_query(query: str) -> str:
    return " ".join(query.split()).casefold()


def cache_key(query: str, title: str | None = None, tags: str | None = None) -> str:
    query = normalize_query(query)
    title = normalize_query(title) if title else query
    tags = normalize_query(tags) if tags else query
    return json.dumps([query, title, tags])


async def find_recent(
    client: MetClient,
    cache: Cache,
    query: str,
    *,
    title: str | None = None,
    tags: str | None = None,
    limit: int = 5,
    refresh: bool = False,
    current_year: int | None = None,
) -> SearchResult:
    key = cache_key(query, title, tags)
    query, title, tags = json.loads(key)
    if refresh:
        cache.forget_query(key)

    record = cache.get_query(key)
    if record is not None and record.window_begin is not None:
        cached = cache.top_objects(key, limit=limit)
        if sum(artwork.found for artwork in cached) >= limit or record.exhausted:
            return SearchResult(
                cached, record.total, 0, cache.count_unusable(key), from_cache=True
            )

    searches = [_Search(query), _Search(title, in_title=True), _Search(tags, in_tags=True)]
    totals = {}
    for search in searches:
        totals[search] = (await _search_page(client, search, limit=1)).total
        if totals[search] > MAX_RESULTS:
            raise TooManyResultsError(totals[search])
        if totals[search] == 0:  # nothing can match all three
            return SearchResult([], 0, 0, 0, from_cache=False)

    # The narrowest search is listed first, so most years cost only one request.
    narrowest, *others = sorted(searches, key=totals.__getitem__)
    total = totals[narrowest]
    cache.save_query(key, total)

    year = current_year if current_year is not None else datetime.now(timezone.utc).year
    fetched: set[int] = set()
    begin = year - 1
    while True:
        object_ids = await _list_ids(client, narrowest, begin, year)
        # Every match is also a match of the narrowest search. Once its full list is
        # inside the window, or no earlier year exists, widening can't find more.
        exhausted = len(object_ids) >= total or begin <= EARLIEST_YEAR
        if len(object_ids) >= limit or exhausted:
            for search in others:
                object_ids &= await _list_ids(client, search, begin, year)
            if len(object_ids) >= limit or exhausted:
                to_fetch = object_ids - fetched
                if not refresh:
                    to_fetch = set(cache.stale_object_ids(to_fetch))
                await _fetch_objects(client, cache, to_fetch)
                fetched |= to_fetch
                if cache.count_dated(object_ids) >= limit or exhausted:
                    break
        begin = max(begin - 1, EARLIEST_YEAR)

    # Recorded only now, so an interrupted run can't pass for a finished one.
    cache.save_window(key, begin, object_ids, exhausted=exhausted)
    return SearchResult(
        cache.top_objects(key, limit=limit),
        total,
        len(fetched),
        cache.count_unusable(key),
        from_cache=False,
    )


async def _search_page(
    client: MetClient, search: _Search, **kwargs: int
) -> SearchPage:
    return await client.search(
        search.term, in_title=search.in_title, in_tags=search.in_tags, **kwargs
    )


async def _list_ids(client: MetClient, search: _Search, begin: int, end: int) -> set[int]:
    """Collect every ID in a window, and check that none were lost while paging.

    The API's result order isn't stable between requests. A lost ID could silently
    change the top results, so a mismatch fails the run.
    """
    ids: set[int] = set()
    offset = 0
    while True:
        page = await _search_page(
            client, search, date_begin=begin, date_end=end, offset=offset, limit=PAGE_SIZE
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
