import pytest

from metsearch.cache import Cache
from metsearch.client import ApiError, BlockedError, SearchPage
from metsearch.search import MAX_RESULTS, TooManyResultsError, find_recent, normalize_query

YEAR = 2026


class FakeMet:
    """Stands in for MetClient, with the date filter behaviour seen on the live API:
    an object matches only if its whole date range fits inside the window."""

    def __init__(self, objects, *, reported_total=None, missing=(), fail_on_object=None):
        self.objects = objects  # {id: (begin, end)}
        self.reported_total = reported_total
        self.missing = set(missing)
        self.fail_on_object = fail_on_object
        self.searches = []
        self.fetched = []

    async def search(self, query, *, date_begin=None, date_end=None, offset=0, limit=500):
        self.searches.append((date_begin, date_end, offset, limit))
        ids = sorted(
            object_id
            for object_id, (begin, end) in self.objects.items()
            if date_begin is None or (begin >= date_begin and end <= date_end)
        )
        total = self.reported_total if self.reported_total is not None else len(ids)
        return SearchPage(total, ids[offset : offset + limit])

    async def get_object(self, object_id):
        if object_id == self.fail_on_object:
            raise BlockedError("blocked")
        self.fetched.append(object_id)
        if object_id in self.missing:
            return None
        begin, end = self.objects[object_id]
        return {
            "objectID": object_id,
            "objectBeginDate": begin,
            "objectEndDate": end,
            "title": str(object_id),
        }


@pytest.fixture
def cache():
    instance = Cache(":memory:")
    yield instance
    instance.close()


def ids(result):
    return [artwork.object_id for artwork in result.artworks]


def spread(count, *, newest=2020):
    """One single-year object per year, counting back from `newest`."""
    return {i: (newest - i, newest - i) for i in range(count)}


async def run(met, cache, **kwargs):
    return await find_recent(met, cache, "q", current_year=YEAR, **kwargs)


async def test_returns_most_recent_and_fetches_only_the_window(cache):
    met = FakeMet(spread(2000))
    result = await run(met, cache)
    assert ids(result) == [0, 1, 2, 3, 4]
    assert result.total == 2000
    assert sorted(met.fetched) == [0, 1, 2, 3, 4]


def windows(met):
    return [(s[0], s[1]) for s in met.searches if s[0] is not None]


async def test_window_widens_one_year_at_a_time_from_this_year(cache):
    met = FakeMet({i: (2024 - i, 2024 - i) for i in range(5)})
    await run(met, cache)
    assert windows(met) == [(begin, YEAR) for begin in (2025, 2024, 2023, 2022, 2021, 2020)]


async def test_windows_with_too_few_ids_are_not_fetched(cache):
    met = FakeMet({i: (2024 - i, 2024 - i) for i in range(6)})
    await run(met, cache)
    assert sorted(met.fetched) == [0, 1, 2, 3, 4]


async def test_stops_at_the_first_window_holding_enough_matches(cache):
    objects = {i: (2025, 2025) for i in range(5)}
    objects[100] = (2000, 2000)
    met = FakeMet(objects)
    result = await run(met, cache)
    assert ids(result) == [4, 3, 2, 1, 0]
    assert 100 not in met.fetched


async def test_work_begun_before_the_window_is_not_found(cache):
    # A known tradeoff of this approach, pinned here so it can't change unnoticed.
    objects = spread(5)
    objects[999] = (1775, 2025)
    result = await run(FakeMet(objects), cache)
    assert 999 not in ids(result)


async def test_deleted_ids_do_not_count_toward_the_limit(cache):
    met = FakeMet(spread(50), missing={0, 1})
    result = await run(met, cache)
    assert ids(result) == [2, 3, 4, 5, 6]
    assert sorted(met.fetched) == [0, 1, 2, 3, 4, 5, 6]
    assert result.unusable == 2


async def test_undated_records_do_not_count_toward_the_limit(cache):
    class SomeUndated(FakeMet):
        async def get_object(self, object_id):
            record = await super().get_object(object_id)
            if object_id == 0:
                del record["objectEndDate"]
            return record

    result = await run(SomeUndated(spread(50)), cache)
    assert ids(result) == [1, 2, 3, 4, 5]


async def test_ids_with_no_record_fill_slots_when_all_matches_are_found(cache):
    result = await run(FakeMet(spread(5), missing={0}), cache)
    assert ids(result) == [1, 2, 3, 4, 0]
    assert [a.found for a in result.artworks] == [True, True, True, True, False]


async def test_larger_limit_is_not_filled_with_cached_no_match_rows(cache):
    await run(FakeMet(spread(50), missing={0, 1}), cache)
    second = FakeMet(spread(50), missing={0, 1})
    result = await run(second, cache, limit=6)
    assert not result.from_cache
    assert ids(result) == [2, 3, 4, 5, 6, 7]


async def test_ties_break_by_object_id(cache):
    objects = {i: (2020, 2020) for i in range(6)}
    result = await run(FakeMet(objects), cache)
    assert ids(result) == [5, 4, 3, 2, 1]


async def test_fewer_matches_than_limit_stops_once_all_are_found(cache):
    met = FakeMet({1: (1990, 1990), 2: (1980, 1985)})
    result = await run(met, cache)
    assert ids(result) == [1, 2]
    assert windows(met)[-1] == (1980, YEAR)


async def test_no_matches(cache):
    met = FakeMet({})
    result = await run(met, cache)
    assert result.artworks == []
    assert len(met.searches) == 1


async def test_exactly_10k_is_allowed(cache):
    met = FakeMet(spread(MAX_RESULTS))
    result = await run(met, cache)
    assert len(result.artworks) == 5
    assert result.total == MAX_RESULTS


async def test_above_10k_is_refused_without_walking(cache):
    met = FakeMet(spread(3), reported_total=MAX_RESULTS + 1)
    with pytest.raises(TooManyResultsError):
        await run(met, cache)
    assert len(met.searches) == 1
    assert cache.get_query("q") is None


async def test_repeat_search_is_served_from_cache(cache):
    await run(FakeMet(spread(50)), cache)
    second = FakeMet(spread(50))
    result = await run(second, cache)
    assert result.from_cache
    assert ids(result) == [0, 1, 2, 3, 4]
    assert second.searches == []
    assert second.fetched == []


async def test_cached_query_with_few_matches_is_served_from_cache(cache):
    objects = {1: (2000, 2000), 2: (1990, 1990)}
    await run(FakeMet(objects), cache)
    second = FakeMet(objects)
    result = await run(second, cache)
    assert result.from_cache
    assert second.searches == []


async def test_larger_limit_widens_without_refetching(cache):
    await run(FakeMet(spread(200)), cache, limit=5)
    second = FakeMet(spread(200))
    result = await run(second, cache, limit=40)
    assert ids(result) == list(range(40))
    assert not set(second.fetched) & {0, 1, 2, 3, 4}


async def test_refresh_refetches(cache):
    await run(FakeMet(spread(50)), cache)
    second = FakeMet(spread(50))
    result = await run(second, cache, refresh=True)
    assert not result.from_cache
    assert sorted(second.fetched) == [0, 1, 2, 3, 4]


async def test_refresh_fetches_each_object_once_across_windows(cache):
    await run(FakeMet(spread(50), missing={0}), cache)
    second = FakeMet(spread(50), missing={0})
    await run(second, cache, refresh=True)
    assert len(second.fetched) == len(set(second.fetched))


async def test_interrupted_run_resumes_without_refetching(cache):
    objects = spread(20)
    first = FakeMet(objects, fail_on_object=2)
    with pytest.raises(BlockedError):
        await run(first, cache)
    assert cache.get_query("q").window_begin is None

    second = FakeMet(objects)
    result = await run(second, cache)
    assert ids(result) == [0, 1, 2, 3, 4]
    assert not set(second.fetched) & set(first.fetched)


async def test_lost_ids_while_paging_fail_the_run(cache):
    class LosesIds(FakeMet):
        async def search(self, query, **kwargs):
            page = await super().search(query, **kwargs)
            return SearchPage(page.total, page.object_ids[:-1])

    with pytest.raises(ApiError, match="paging"):
        await run(LosesIds(spread(10)), cache)


def test_normalize_query():
    assert normalize_query("  Tessellation   Art ") == "tessellation art"
