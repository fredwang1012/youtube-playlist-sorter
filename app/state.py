"""SQLite state: which videos were seen, which were filed where, quota per day, and run history."""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from app.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (channel_id TEXT PRIMARY KEY, first_run_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS seen_videos (
    video_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, first_seen TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS filed (
    video_id TEXT NOT NULL, playlist_id TEXT NOT NULL, at TEXT NOT NULL,
    UNIQUE (video_id, playlist_id));
CREATE TABLE IF NOT EXISTS quota_log (day TEXT PRIMARY KEY, units INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS attempts (
    video_id TEXT PRIMARY KEY, failures INTEGER NOT NULL, gave_up INTEGER NOT NULL DEFAULT 0, last_error TEXT);
CREATE TABLE IF NOT EXISTS pending (
    video_id TEXT NOT NULL, playlist_id TEXT NOT NULL, channel_id TEXT NOT NULL, created_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, reason TEXT,
    UNIQUE (video_id, playlist_id));
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT, summary TEXT);
"""


class State:
    def __init__(self, path: Path | None = None) -> None:
        path = path or settings.data_dir / "state.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.executescript(SCHEMA)

    @classmethod
    def throwaway_copy(cls, path: Path | None = None) -> "State":
        """In-memory copy of the state file (empty if there is none); writes never reach disk. Used by dry-run."""
        path = path or settings.data_dir / "state.db"
        self = cls.__new__(cls)
        self._db = sqlite3.connect(":memory:")
        if path.is_file():
            disk = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
            try:
                disk.backup(self._db)
            finally:
                disk.close()
        self._db.executescript(SCHEMA)  # a pre-existing file may predate newer tables; this is in memory only
        return self

    def close(self) -> None:
        self._db.close()

    def channel_known(self, channel_id: str) -> bool:
        return self._db.execute("SELECT 1 FROM channels WHERE channel_id = ?", (channel_id,)).fetchone() is not None

    def mark_channel_known(self, channel_id: str, now: datetime) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO channels (channel_id, first_run_at) VALUES (?, ?)", (channel_id, now.isoformat())
            )

    def seen(self, video_ids: list[str]) -> set[str]:
        found: set[str] = set()
        for i in range(0, len(video_ids), 500):  # stay under SQLite's variable limit
            chunk = video_ids[i : i + 500]
            marks = ",".join("?" * len(chunk))
            rows = self._db.execute(f"SELECT video_id FROM seen_videos WHERE video_id IN ({marks})", chunk)
            found.update(r[0] for r in rows)
        return found

    def mark_seen(self, video_id: str, channel_id: str, now: datetime) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO seen_videos (video_id, channel_id, first_seen) VALUES (?, ?, ?)",
                (video_id, channel_id, now.isoformat()),
            )

    def is_filed(self, video_id: str, playlist_id: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM filed WHERE video_id = ? AND playlist_id = ?", (video_id, playlist_id)
        ).fetchone()
        return row is not None

    def mark_filed(self, video_id: str, playlist_id: str, now: datetime) -> bool:
        """Record a filing; False if it was already recorded."""
        with self._db:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO filed (video_id, playlist_id, at) VALUES (?, ?, ?)",
                (video_id, playlist_id, now.isoformat()),
            )
        return cur.rowcount == 1

    def add_quota(self, day: str, units: int) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO quota_log (day, units) VALUES (?, ?) "
                "ON CONFLICT(day) DO UPDATE SET units = units + excluded.units",
                (day, units),
            )

    def quota_used(self, day: str) -> int:
        row = self._db.execute("SELECT units FROM quota_log WHERE day = ?", (day,)).fetchone()
        return row[0] if row else 0

    def start_run(self, now: datetime) -> int:
        with self._db:
            cur = self._db.execute("INSERT INTO runs (started_at) VALUES (?)", (now.isoformat(),))
        return cur.lastrowid

    def finish_run(self, run_id: int, now: datetime, summary: dict) -> None:
        with self._db:
            self._db.execute(
                "UPDATE runs SET finished_at = ?, summary = ? WHERE id = ?",
                (now.isoformat(), json.dumps(summary), run_id),
            )

    def recent_runs(self, limit: int = 20) -> list[dict]:
        """Newest first; summary is None for a run that has not finished (or died mid-run)."""
        rows = self._db.execute(
            "SELECT id, started_at, finished_at, summary FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [
            {"id": r[0], "started_at": r[1], "finished_at": r[2], "summary": json.loads(r[3]) if r[3] else None}
            for r in rows
        ]

    def kv_get(self, key: str) -> tuple[str, datetime] | None:
        row = self._db.execute("SELECT value, at FROM kv WHERE key = ?", (key,)).fetchone()
        return (row[0], datetime.fromisoformat(row[1])) if row else None

    def kv_set(self, key: str, value: str, now: datetime) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO kv (key, value, at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, at = excluded.at",
                (key, value, now.isoformat()),
            )

    def kv_delete(self, key: str) -> None:
        with self._db:
            self._db.execute("DELETE FROM kv WHERE key = ?", (key,))

    def failures(self, video_id: str) -> int:
        row = self._db.execute("SELECT failures FROM attempts WHERE video_id = ?", (video_id,)).fetchone()
        return row[0] if row else 0

    def record_failure(self, video_id: str, error: str, give_up_after: int, allow_give_up: bool = True) -> bool:
        """Count one failed run for the video; True if it has reached the cap and (if allowed) is given up."""
        n = self.failures(video_id) + 1
        gave_up = allow_give_up and n >= give_up_after
        with self._db:
            self._db.execute(
                "INSERT INTO attempts (video_id, failures, gave_up, last_error) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(video_id) DO UPDATE SET failures = excluded.failures, gave_up = excluded.gave_up, "
                "last_error = excluded.last_error",
                (video_id, n, int(gave_up), error),
            )
        return gave_up

    def gave_up(self) -> list[tuple[str, str]]:
        rows = self._db.execute("SELECT video_id, last_error FROM attempts WHERE gave_up = 1 ORDER BY video_id")
        return [(r[0], r[1] or "") for r in rows]

    # pending: filings still to do (attempts == 0: deferred) or that may have been accepted (attempts > 0)

    def pending_rows(self) -> list[tuple[str, str, str, int]]:
        rows = self._db.execute(
            "SELECT video_id, playlist_id, channel_id, attempts FROM pending ORDER BY created_at, rowid"
        )
        return [tuple(r) for r in rows]

    def pending_attempts(self, video_id: str, playlist_id: str) -> int | None:
        row = self._db.execute(
            "SELECT attempts FROM pending WHERE video_id = ? AND playlist_id = ?", (video_id, playlist_id)
        ).fetchone()
        return row[0] if row else None

    def queue_pending(self, video_id: str, playlist_id: str, channel_id: str, now: datetime, reason: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO pending (video_id, playlist_id, channel_id, created_at, reason) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(video_id, playlist_id) DO UPDATE SET reason = excluded.reason",
                (video_id, playlist_id, channel_id, now.isoformat(), reason),
            )

    def begin_attempt(self, video_id: str, playlist_id: str, channel_id: str, now: datetime) -> None:
        """Written before an insert is sent: if the process dies, the row says the insert may have gone through."""
        with self._db:
            self._db.execute(
                "INSERT INTO pending (video_id, playlist_id, channel_id, created_at, attempts, reason) "
                "VALUES (?, ?, ?, ?, 1, 'inserting') "
                "ON CONFLICT(video_id, playlist_id) DO UPDATE SET attempts = attempts + 1, reason = 'inserting'",
                (video_id, playlist_id, channel_id, now.isoformat()),
            )

    def undo_attempt(self, video_id: str, playlist_id: str) -> None:
        """The insert was definitely rejected: it is not a maybe-accepted attempt."""
        with self._db:
            self._db.execute(
                "UPDATE pending SET attempts = MAX(attempts - 1, 0) WHERE video_id = ? AND playlist_id = ?",
                (video_id, playlist_id),
            )

    def clear_attempts(self, video_id: str, playlist_id: str) -> None:
        with self._db:
            self._db.execute(
                "UPDATE pending SET attempts = 0 WHERE video_id = ? AND playlist_id = ?", (video_id, playlist_id)
            )

    def drop_pending(self, video_id: str, playlist_id: str) -> None:
        with self._db:
            self._db.execute("DELETE FROM pending WHERE video_id = ? AND playlist_id = ?", (video_id, playlist_id))

    def complete_filing(self, video_id: str, playlist_id: str, now: datetime) -> None:
        """Record the filing and clear its pending row in one transaction."""
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO filed (video_id, playlist_id, at) VALUES (?, ?, ?)",
                (video_id, playlist_id, now.isoformat()),
            )
            self._db.execute("DELETE FROM pending WHERE video_id = ? AND playlist_id = ?", (video_id, playlist_id))
