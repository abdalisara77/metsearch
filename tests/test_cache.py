from datetime import datetime, timedelta, timezone

import pytest

from metsearch.cache import OBJECT_TTL, QUERY_TTL, Cache


class Clock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def cache(clock):
    instance = Cache(":memory:", now=clock)
    yield instance
    instance.close()


def record(object_id, end):
    return {
        "objectID": object_id,
        "objectBeginDate": end - 1,
        "objectEndDate": end,
        "title": f"work {object_id}",
    }


def test_query_round_trip(cache):
    cache.save_query("q", 2)
    cache.save_object(1, record(1, 2000))
    assert cache.get_query("q").total == 2
    assert cache.get_query("q").window_begin is None
    cache.save_window("q", 1999, [1])
    assert cache.get_query("q").window_begin == 1999
    assert cache.query_ids("q") == {1}


def test_expired_query_is_deleted_with_its_links(cache, clock):
    cache.save_query("q", 1)
    cache.save_object(1, record(1, 2000))
    cache.save_window("q", 1999, [1])
    clock.now += QUERY_TTL + timedelta(seconds=1)
    assert cache.get_query("q") is None
    assert cache.query_ids("q") == set()


def test_a_later_window_replaces_the_links(cache):
    cache.save_query("q", 3)
    for object_id in (1, 2, 3):
        cache.save_object(object_id, record(object_id, 2000))
    cache.save_window("q", 1999, [1, 2])
    cache.save_window("q", 1998, [2, 3])
    assert cache.query_ids("q") == {2, 3}


def test_stale_ids_cover_missing_and_expired_rows(cache, clock):
    cache.save_object(1, record(1, 2000))
    clock.now += OBJECT_TTL + timedelta(seconds=1)
    cache.save_object(2, record(2, 2000))
    assert sorted(cache.stale_object_ids([1, 2, 3])) == [1, 3]


def test_stale_ids_handle_more_than_one_sql_batch(cache):
    ids = list(range(1, 1202))
    for object_id in ids[:1200]:
        cache.save_object(object_id, record(object_id, 2000))
    assert cache.stale_object_ids(ids) == [1201]


def test_404_is_remembered_and_returned_without_a_record(cache):
    cache.save_query("q", 1)
    cache.save_object(1, None)
    cache.save_window("q", 1999, [1])
    assert cache.stale_object_ids([1]) == []
    assert [(a.object_id, a.found) for a in cache.top_objects("q", limit=5)] == [(1, False)]
    assert cache.count_unusable("q") == 1


def test_object_without_end_date_is_left_out_and_counted(cache):
    cache.save_query("q", 1)
    cache.save_object(1, {"objectID": 1, "title": "undated"})
    cache.save_window("q", 1999, [1])
    assert cache.top_objects("q", limit=5) == []
    assert cache.count_unusable("q") == 1


def test_count_dated_ignores_404s_undated_and_unfetched(cache):
    cache.save_object(1, record(1, 2000))
    cache.save_object(2, None)
    cache.save_object(3, {"objectID": 3, "title": "undated"})
    assert cache.count_dated([1, 2, 3, 4]) == 1


def test_ranking_orders_by_date_then_id(cache):
    cache.save_query("q", 3)
    cache.save_object(1, record(1, 2000))
    cache.save_object(2, record(2, 2010))
    cache.save_object(3, record(3, 2000))
    cache.save_object(5, record(5, 2020))  # not linked to this query
    cache.save_window("q", 1990, [1, 2, 3])
    assert [a.object_id for a in cache.top_objects("q", limit=5)] == [2, 3, 1]
    assert [a.object_id for a in cache.top_objects("q", limit=2)] == [2, 3]


def test_ids_with_no_record_rank_after_dated_works(cache):
    cache.save_query("q", 3)
    cache.save_object(1, None)
    cache.save_object(2, record(2, 1000))
    cache.save_object(3, None)
    cache.save_window("q", 900, [1, 2, 3])
    assert [a.object_id for a in cache.top_objects("q", limit=5)] == [2, 3, 1]
    assert [a.object_id for a in cache.top_objects("q", limit=1)] == [2]


def test_data_survives_reopening(tmp_path):
    path = tmp_path / "nested" / "cache.db"
    first = Cache(path)
    first.save_query("q", 1)
    first.save_object(1, record(1, 2000))
    first.save_window("q", 1000, [1])
    first.close()
    second = Cache(path)
    assert second.top_objects("q", limit=1)[0].data["title"] == "work 1"
    second.close()


def test_older_schema_is_rebuilt(tmp_path):
    path = tmp_path / "cache.db"
    first = Cache(path)
    first.save_query("q", 1)
    first._db.execute("PRAGMA user_version = 1")
    first.close()
    second = Cache(path)
    assert second.get_query("q") is None
    second.close()
