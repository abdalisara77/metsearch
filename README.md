# metsearch

A command-line tool that finds the most recent works matching a search in the
[Met Museum's collection API](https://metmuseum.github.io/), ranked by `objectEndDate`.

```
$ metsearch
End date  Title                                     Artist            URL
--------  ----------------------------------------  ----------------  ------------------------------------------------------
2025      Ensemble                                  Yours Truly       https://www.metmuseum.org/art/collection/search/933959
2022      Untitled                                  Kai Althoff       https://www.metmuseum.org/art/collection/search/894333
2022      Life after death                          Elias Not Afraid  https://www.metmuseum.org/art/collection/search/860994
2021      Dress                                     Michael Kors      https://www.metmuseum.org/art/collection/search/935738
2021      Jeffrey Gibson : it can be said of th...  Jeffrey Gibson    https://www.metmuseum.org/art/collection/search/889355
6,532 matches, 5 shown, from 13 requests, 7 objects fetched, 2 unusable
```

## Setup

Requires Python 3.11 or later.

```
uv venv && uv pip install -e '.[dev]'
.venv/bin/pytest
```

## Usage

```
metsearch [QUERY] [--limit 5] [--json] [--refresh] [--rate 40] [--db PATH]
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

The API can't sort by date, so a simple approach fetches every match: about 6,500
requests for `tessellation`. Instead, this tool widens a date window back from this year:

1. Read the total across all dates, and refuse the query if it's above 10,000.
2. Search `current year - 1` to `current year`, then `current year - 2` to `current year`,
   and so on.
3. Once a window holds at least 5 IDs, fetch any not already cached and check them. Search can
   list IDs whose records are gone, so only records with an end date count toward the 5.
4. Stop at the first window with 5 real works, and rank them by end date, then object ID.

If every match has been found and there still aren't 5 real works, IDs that returned 404 fill
the remaining slots, shown as `(no match)` in the table and `"found": false` in JSON.

**The tradeoff:** the API only returns a work if its whole date range fits inside the window.
A work begun before the window isn't found, even if it ended recently. For example, a
headdress dated 1775-2025 is missed because the walk stops at 2021, even though its end date
would rank it near the top. The tool accepts this to keep requests low.

## Design decisions

- **Above 10,000 results is refused.** The API can only page through the first 10,000,
  and its results aren't sorted by date, so a larger query couldn't be ranked reliably.
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
- **Queries with only old matches are slow.** The window grows one year at a time, with one
  request per year. A query whose newest matches are centuries old needs hundreds of requests,
  and may hit the firewall.
- **Paging isn't guaranteed stable.** The API's order changes between requests, so a run
  that loses an ID fails with an error instead of risking a wrong answer.
