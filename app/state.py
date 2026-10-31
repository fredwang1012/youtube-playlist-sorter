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
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT, summary TEXT);
"""


class State:
    def __init__(self, path: Path | None = None) -> None:
        path = path or settings.data_dir / "state.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.executescript(SCHEMA)

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
