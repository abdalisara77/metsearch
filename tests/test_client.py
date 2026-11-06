import httpx
import pytest
import respx

from metsearch.client import (
    BASE_URL,
    ApiError,
    BlockedError,
    MetClient,
    RateLimiter,
)

SEARCH_URL = f"{BASE_URL}/v1.1/search"
OBJECT_URL = f"{BASE_URL}/v1/objects/1"


class FakeTime:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def clock(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def fake_time():
    return FakeTime()


@pytest.fixture
async def client(fake_time):
    async with httpx.AsyncClient() as http:
        limiter = RateLimiter(1000, clock=fake_time.clock, sleep=fake_time.sleep)
        yield MetClient(http, limiter, max_attempts=3, sleep=fake_time.sleep)


async def test_limiter_spaces_calls_evenly(fake_time):
    limiter = RateLimiter(10, clock=fake_time.clock, sleep=fake_time.sleep)
    starts = []
    for _ in range(4):
        await limiter.acquire()
        starts.append(fake_time.now)
    assert [round(b - a, 6) for a, b in zip(starts, starts[1:])] == [0.1, 0.1, 0.1]


async def test_limiter_has_no_burst_after_idle(fake_time):
    limiter = RateLimiter(10, clock=fake_time.clock, sleep=fake_time.sleep)
    await limiter.acquire()
    fake_time.now += 60
    await limiter.acquire()
    start = fake_time.now
    await limiter.acquire()
    assert round(fake_time.now - start, 6) == 0.1


async def test_limiter_pause_holds_later_callers(fake_time):
    limiter = RateLimiter(10, clock=fake_time.clock, sleep=fake_time.sleep)
    await limiter.acquire()
    resume_at = fake_time.now + 5
    limiter.pause(5)
    await limiter.acquire()
    assert fake_time.now >= resume_at


def test_limiter_rejects_non_positive_rate():
    with pytest.raises(ValueError):
        RateLimiter(0)


@respx.mock
async def test_search_treats_null_ids_as_empty(client):
    respx.get(SEARCH_URL).respond(json={"total": 0, "objectIDs": None})
    page = await client.search("x")
    assert page.total == 0
    assert page.object_ids == []


@respx.mock
async def test_search_sends_date_window(client):
    route = respx.get(SEARCH_URL).respond(json={"total": 1, "objectIDs": [7]})
    await client.search("x", date_begin=-5, date_end=9, offset=500, limit=1)
    params = route.calls[0].request.url.params
    assert (params["dateBegin"], params["dateEnd"]) == ("-5", "9")
    assert (params["offset"], params["limit"]) == ("500", "1")


@respx.mock
async def test_object_404_returns_none_without_retry(client):
    route = respx.get(OBJECT_URL).respond(404, json={"message": "ObjectID not found"})
    assert await client.get_object(1) is None
    assert route.call_count == 1


@respx.mock
async def test_403_stops_without_retry(client):
    route = respx.get(OBJECT_URL).respond(403, text="<html>blocked</html>")
    with pytest.raises(BlockedError):
        await client.get_object(1)
    assert route.call_count == 1


@respx.mock
async def test_html_body_with_200_counts_as_blocked(client):
    respx.get(OBJECT_URL).respond(200, text="<html>challenge</html>")
    with pytest.raises(BlockedError):
        await client.get_object(1)


@respx.mock
async def test_5xx_retries_then_succeeds(client):
    route = respx.get(OBJECT_URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={"objectID": 1})]
    )
    assert await client.get_object(1) == {"objectID": 1}
    assert route.call_count == 2


@respx.mock
async def test_timeout_retries_then_gives_up(client):
    route = respx.get(OBJECT_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ApiError, match="3 attempts"):
        await client.get_object(1)
    assert route.call_count == 3


@respx.mock
async def test_429_honours_retry_after_for_all_callers(client, fake_time):
    respx.get(OBJECT_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "30"}),
            httpx.Response(200, json={"objectID": 1}),
        ]
    )
    start = fake_time.now
    assert await client.get_object(1) == {"objectID": 1}
    assert fake_time.now - start >= 30


@respx.mock
async def test_other_4xx_fails_immediately(client):
    route = respx.get(SEARCH_URL).respond(410, json={"message": "retired"})
    with pytest.raises(ApiError, match="410"):
        await client.search("x")
    assert route.call_count == 1
