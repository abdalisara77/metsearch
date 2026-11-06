"""Command-line interface."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .cache import Artwork, Cache
from .client import (
    DEFAULT_RATE,
    MAX_RATE,
    BlockedError,
    MetClient,
    MetSearchError,
    RateLimiter,
    build_http_client,
)
from .search import SearchResult, TooManyResultsError, find_recent, normalize_query

DEFAULT_QUERY = "tessellation"
DEFAULT_DB = Path.home() / ".cache" / "metsearch" / "metsearch.db"

NO_MATCH = "(no match)"

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2  # argparse's own code for bad arguments
EXIT_TOO_MANY = 3
EXIT_INTERRUPTED = 130


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def _rate(text: str) -> float:
    value = float(text)
    if not 0 < value <= MAX_RATE:
        raise argparse.ArgumentTypeError(f"must be above 0 and at most {MAX_RATE:g}")
    return value


def _query(text: str) -> str:
    if not normalize_query(text):
        raise argparse.ArgumentTypeError("must not be empty")
    return text


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="metsearch",
        description="Find the most recent works matching a search in the Met's collection.",
    )
    parser.add_argument(
        "query", nargs="?", type=_query, default=DEFAULT_QUERY,
        help="search term (default: %(default)s)",
    )
    parser.add_argument(
        "--limit", type=_positive_int, default=5,
        help="how many works to return (default: %(default)s)",
    )
    parser.add_argument("--json", action="store_true", help="print results as JSON")
    parser.add_argument(
        "--refresh", action="store_true", help="ignore cached data for this query"
    )
    parser.add_argument(
        "--rate", type=_rate, default=DEFAULT_RATE,
        help="maximum requests per second (default: %(default)g)",
    )
    parser.add_argument(
        "--db", type=Path, default=DEFAULT_DB, help="cache path (default: %(default)s)"
    )
    return parser


async def _run(args: argparse.Namespace, cache: Cache) -> tuple[SearchResult, int]:
    async with build_http_client() as http:
        client = MetClient(http, RateLimiter(args.rate))
        result = await find_recent(
            client,
            cache,
            args.query,
            limit=args.limit,
            refresh=args.refresh,
        )
        return result, client.request_count


def _as_dict(artwork: Artwork) -> dict[str, object]:
    data = artwork.data
    return {
        "objectID": artwork.object_id,
        "found": artwork.found,
        "objectEndDate": artwork.end_date,
        "objectDate": data.get("objectDate", ""),
        "title": data.get("title", ""),
        "artist": data.get("artistDisplayName", ""),
        "url": data.get("objectURL", ""),
    }


def _format_table(artworks: list[Artwork]) -> str:
    headers = ("End date", "Title", "Artist", "URL")
    rows = [
        (
            str(artwork.end_date) if artwork.found else "-",
            _shorten(artwork.data.get("title", "")) if artwork.found else NO_MATCH,
            _shorten(artwork.data.get("artistDisplayName", "")),
            artwork.data.get("objectURL", "")
            if artwork.found
            else f"objectID {artwork.object_id}",
        )
        for artwork in artworks
    ]
    widths = [max(len(row[i]) for row in (headers, *rows)) for i in range(len(headers))]
    lines = [headers, tuple("-" * width for width in widths), *rows]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(line, widths)).rstrip()
        for line in lines
    )


def _shorten(text: str, width: int = 40) -> str:
    return text if len(text) <= width else text[: width - 3] + "..."


def _print_result(result: SearchResult, requests: int, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps([_as_dict(artwork) for artwork in result.artworks], indent=2))
    elif result.artworks:
        print(_format_table(result.artworks))
    else:
        print("No results.")
    # On stderr, so piped output holds only the results.
    source = "cache" if result.from_cache else f"{requests} requests"
    print(
        f"{result.total:,} matches, {len(result.artworks)} shown, from {source}, "
        f"{result.objects_fetched} objects fetched, {result.unusable} unusable",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    cache = Cache(args.db)
    try:
        result, requests = asyncio.run(_run(args, cache))
    except TooManyResultsError as exc:
        print(f"metsearch: {exc}", file=sys.stderr)
        return EXIT_TOO_MANY
    except BlockedError as exc:
        print(
            f"metsearch: {exc}. Progress is saved; wait before trying again.",
            file=sys.stderr,
        )
        return EXIT_FAILURE
    except MetSearchError as exc:
        print(f"metsearch: {exc}. Progress is saved.", file=sys.stderr)
        return EXIT_FAILURE
    except KeyboardInterrupt:
        print("metsearch: interrupted. Progress is saved.", file=sys.stderr)
        return EXIT_INTERRUPTED
    finally:
        cache.close()
    _print_result(result, requests, as_json=args.json)
    return EXIT_OK
