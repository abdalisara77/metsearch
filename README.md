# metsearch

A command-line tool that finds the most recent works matching a search in the
[Met Museum's collection API](https://metmuseum.github.io/), ranked by `objectEndDate`.

```
metsearch sunflowers --title sunflowers --tags sunflowers --limit 5
```

A work must match the query, the title term and the tags term. Both terms default to the
query, so a query that appears in no titles or no tags returns nothing. `tessellation`, the
default query, is one of these.

## Setup

Requires Python 3.11 or later.

```
uv venv && uv pip install -e '.[dev]'
.venv/bin/pytest
```

## Usage

```
metsearch [QUERY] [--title TEXT] [--tags TEXT] [--limit 5] [--json] [--refresh] [--rate 40] [--db PATH]
```

`QUERY` defaults to `tessellation`. Results go to stdout and the summary line to stderr.

| Exit code | Meaning |
| --- | --- |
| 0 | Success, including a search with no results |
| 1 | API failure, firewall block, or retries exhausted |
| 2 | Invalid arguments |
| 3 | Query matches more than 10,000 objects |
| 130 | Interrupted |

## How it works

The API can't sort by date, so a simple approach fetches every match, which can take thousands
of requests. Instead, this tool widens a date window back from this year.

A work must match three searches: `q=<query>`, `q=<title>&title=true` and `q=<tags>&tags=true`.
The API's `title` and `tags` parameters are true/false flags that limit `q` to one field, so
each term needs its own search, and the tool keeps the IDs found by all three.

1. Read the total for each search. Refuse if any is above 10,000, and stop if any is 0.
2. Search the narrowest of the three from `current year - 1` to `current year`, then
   `current year - 2` to `current year`, and so on. Most years cost one request.
3. Once that window holds at least 5 IDs, run the other two searches and keep the IDs in all
   three. If at least 5 remain, fetch any not already cached and check them. Search can list
   IDs whose records are gone, so only records with an end date count toward the 5.
4. Stop at the first window with 5 real works, and rank them by end date, then object ID.

If the narrowest search is fully inside the window and there still aren't 5 real works, IDs that returned 404 fill
the remaining slots, shown as `(no match)` in the table and `"found": false` in JSON.

**The tradeoff:** the API only returns a work if its whole date range fits inside the window.
A work begun before the window isn't found, even if it ended recently. The tool accepts this
to keep requests low.

## Design decisions

- **Above 10,000 results is refused.** This applies to each of the three searches. The API can
  only page through the first 10,000, and its results aren't sorted by date, so a larger search
  couldn't be ranked reliably.
- **Title and tags narrow the results.** A work must match all three searches, so the defaults
  are strict: `tessellation` appears in no titles or tags, and returns nothing.
- **Evenly spaced requests.** The limiter has no burst allowance, and defaults to 40 requests
  per second. The documented limit is 80, but in testing the firewall returned 403s after
  roughly 80-110 requests in quick succession, including at 20 per second.
- **A 403 stops the run immediately.** Retrying could prolong a block. Progress is saved.
- **Deleted IDs don't count toward the 5.** Checking records as the window widens costs a few
  extra fetches, but the tool never stops early because of IDs with no record.
- **Other failures never produce partial answers.** Any failure except a 404 ends the run
  with an error.
- **SQLite instead of a dict.** A dict disappears when the process exits. Objects are shared
  between queries and kept for 30 days. Query results are kept for 24 hours.
- **`window_begin` records what's trustworthy.** Links and this column are written together,
  once every object in the window is fetched, so an interrupted run can't pass for a finished one.

## Known limitations

- **The Met's matching is fuzzy.** `tessellation` returns 6,532 works, including scarves and
  drums, and `title=true` returns none. This tool trusts the Met's search.
- **Results depend on the Met's dates.** This tool ranks by `objectEndDate` as published.
- **Queries with only old matches may never finish.** The window grows one year at a time, with
  one request per year, and a rerun starts the walk again. A query that needs about 100 years or
  more can hit the firewall on every attempt.
- **Paging isn't guaranteed stable.** The API's order changes between requests, so a run
  that loses an ID fails with an error instead of risking a wrong answer.
