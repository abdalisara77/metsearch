"""SQLite cache of objects and of which objects each query matches."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 3
QUERY_TTL = timedelta(hours=24)  # search results can change
OBJECT_TTL = timedelta(days=30)  # object records rarely do
_SQL_BATCH = 500  # stays well under SQLite's bound-variable limit

_SCHEMA = """
CREATE TABLE objects (
    object_id  INTEGER PRIMARY KEY,
    status     TEXT NOT NULL CHECK (status IN ('ok', 'not_found')),
    begin_date INTEGER,
    end_date   INTEGER,
    data       TEXT,
    fetched_at TEXT NOT NULL
);
CREATE INDEX objects_end_date ON objects (end_date DESC, object_id DESC);

CREATE TABLE queries (
    query        TEXT PRIMARY KEY,
    total        INTEGER NOT NULL,
    window_begin INTEGER,
    checked_at   TEXT NOT NULL
);

-- The objects in the query's finished window. Written after they're all fetched.
CREATE TABLE query_objects (
    query     TEXT NOT NULL REFERENCES queries (query) ON DELETE CASCADE,
    object_id INTEGER NOT NULL REFERENCES objects (object_id),
    PRIMARY KEY (query, object_id)
) WITHOUT ROWID;
"""


@dataclass(frozen=True)
class QueryRecord:
    total: int  # matches across all dates
    # The start year of the finished window. None means no walk has finished yet.
    window_begin: int | None


@dataclass(frozen=True)
class Artwork:
    object_id: int
    end_date: int | None  # None means the ID has no record (the API returned 404)
    data: dict[str, Any]

    @property
    def found(self) -> bool:
        return self.end_date is not None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Cache:
    def __init__(
        self, path: Path | str, *, now: Callable[[], datetime] = _utc_now
    ) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._now = now
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute("PRAGMA synchronous = NORMAL")
        self._migrate()

    def _migrate(self) -> None:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            return
        if version != 0:
            # It's only a cache, so rebuilding beats writing migrations.
            for table in ("query_objects", "queries", "objects"):
                self._db.execute(f"DROP TABLE IF EXISTS {table}")
        self._db.executescript(_SCHEMA)
        self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        self._db.close()

    def get_query(self, query: str) -> QueryRecord | None:
        """Return the query's record. An expired one is deleted and reported as absent."""
        row = self._db.execute(
            "SELECT total, window_begin, checked_at FROM queries WHERE query = ?",
            (query,),
        ).fetchone()
        if row is None:
            return None
        if datetime.fromisoformat(row[2]) < self._now() - QUERY_TTL:
            self.forget_query(query)
            return None
        return QueryRecord(total=row[0], window_begin=row[1])

    def save_query(self, query: str, total: int) -> None:
        with self._db:
            self._db.execute("DELETE FROM queries WHERE query = ?", (query,))
            self._db.execute(
                "INSERT INTO queries (query, total, checked_at) VALUES (?, ?, ?)",
                (query, total, self._now().isoformat()),
            )

    def forget_query(self, query: str) -> None:
        with self._db:  # links go too, through ON DELETE CASCADE
            self._db.execute("DELETE FROM queries WHERE query = ?", (query,))

    def query_ids(self, query: str) -> set[int]:
        rows = self._db.execute(
            "SELECT object_id FROM query_objects WHERE query = ?", (query,)
        )
        return {row[0] for row in rows}

    def save_window(
        self, query: str, window_begin: int, object_ids: Iterable[int]
    ) -> None:
        """Record a finished walk: where its window began, and the objects in it."""
        with self._db:
            self._db.execute(
                "UPDATE queries SET window_begin = ? WHERE query = ?",
                (window_begin, query),
            )
            self._db.execute("DELETE FROM query_objects WHERE query = ?", (query,))
            self._db.executemany(
                "INSERT INTO query_objects (query, object_id) VALUES (?, ?)",
                ((query, object_id) for object_id in object_ids),
            )

    def stale_object_ids(self, object_ids: Iterable[int]) -> list[int]:
        """Return the IDs that have no row, or whose row has expired."""
        ids = list(object_ids)
        cutoff = (self._now() - OBJECT_TTL).isoformat()
        fresh: set[int] = set()
        for start in range(0, len(ids), _SQL_BATCH):
            batch = ids[start : start + _SQL_BATCH]
            marks = ",".join("?" * len(batch))
            rows = self._db.execute(
                f"SELECT object_id FROM objects "
                f"WHERE object_id IN ({marks}) AND fetched_at >= ?",
                (*batch, cutoff),
            )
            fresh.update(row[0] for row in rows)
        return [object_id for object_id in ids if object_id not in fresh]

    def count_dated(self, object_ids: Iterable[int]) -> int:
        """Count the IDs whose fetched record exists and has an end date."""
        ids = list(object_ids)
        count = 0
        for start in range(0, len(ids), _SQL_BATCH):
            batch = ids[start : start + _SQL_BATCH]
            marks = ",".join("?" * len(batch))
            row = self._db.execute(
                f"SELECT COUNT(*) FROM objects WHERE object_id IN ({marks}) "
                "AND status = 'ok' AND end_date IS NOT NULL",
                batch,
            ).fetchone()
            count += row[0]
        return count

    def save_object(self, object_id: int, record: dict[str, Any] | None) -> None:
        """Store a fetched record. None records a 404, so it isn't fetched again."""
        begin, end, data = None, None, None
        if record is not None:
            begin = record.get("objectBeginDate")
            end = record.get("objectEndDate")
            data = json.dumps(record)
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO objects "
                "(object_id, status, begin_date, end_date, data, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    object_id,
                    "ok" if record is not None else "not_found",
                    begin,
                    end,
                    data,
                    self._now().isoformat(),
                ),
            )

    def top_objects(self, query: str, *, limit: int) -> list[Artwork]:
        """Return a finished walk's results, most recent first.

        Object ID breaks ties, so repeat runs return the same list. IDs with no
        record have no date to rank by, so they only fill slots after the dated works.
        """
        rows = self._db.execute(
            "SELECT o.object_id, o.end_date, o.data "
            "FROM query_objects q JOIN objects o ON o.object_id = q.object_id "
            "WHERE q.query = ? AND (o.status = 'not_found' OR o.end_date IS NOT NULL) "
            "ORDER BY o.status = 'ok' DESC, o.end_date DESC, o.object_id DESC LIMIT ?",
            (query, limit),
        )
        return [
            Artwork(row[0], row[1], json.loads(row[2]) if row[2] else {}) for row in rows
        ]

    def count_unusable(self, query: str) -> int:
        """Count a finished walk's matches with no record, or with no end date."""
        row = self._db.execute(
            "SELECT COUNT(*) FROM query_objects q "
            "JOIN objects o ON o.object_id = q.object_id "
            "WHERE q.query = ? AND (o.status != 'ok' OR o.end_date IS NULL)",
            (query,),
        ).fetchone()
        return row[0]
