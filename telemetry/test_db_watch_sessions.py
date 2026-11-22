import json
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from telemetry import db


class WatchSessionLoadingTests(unittest.TestCase):
    def test_load_watch_sessions_falls_back_to_jsonl(self):
        points = [
            {"ts": 1795297041.0, "source": "watch", "bpm": 120, "accel_g": 1.0, "xyz": [0, 0, 0]},
            {"ts": 1795297042.0, "source": "watch", "bpm": 130, "accel_g": 1.1, "xyz": [300, 0, 0]},
        ]

        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "watch_log.jsonl"
            log_path.write_text(
                "\n".join(json.dumps(point) for point in points),
                encoding="utf-8",
            )
            with (
                patch.object(db, "TIGER_DATA_URL", None),
                patch.object(db, "SQLITE_DB_PATH", Path(directory) / "missing.sqlite"),
                patch.object(db, "WATCH_LOG_PATH", log_path),
            ):
                data = db.load_watch_sessions()

        self.assertEqual(data["source"], "jsonl")
        self.assertEqual(len(data["sessions"]), 1)
        self.assertEqual(data["sessions"][0]["peak_bpm"], 130)
        self.assertEqual(data["sessions"][0]["hold_shifts"], 1)


class LiveTelemetryLoadingTests(unittest.TestCase):
    def test_load_live_telemetry_uses_newest_watch_samples_across_sessions(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "climb_data.db"
            with closing(db.sqlite3.connect(database_path)) as conn:
                conn.executescript(
                    """
                    CREATE TABLE sessions (
                        id INTEGER PRIMARY KEY,
                        started_at TEXT,
                        ended_at TEXT,
                        status TEXT
                    );
                    CREATE TABLE telemetry_points (
                        time TEXT NOT NULL,
                        session_id INTEGER,
                        source TEXT,
                        bpm INTEGER,
                        accel_g REAL,
                        speed REAL,
                        fall_detected INTEGER
                    );
                    INSERT INTO sessions VALUES
                        (1, '2026-01-01T00:00:00+00:00', '2026-01-01T00:01:00+00:00', 'COMPLETED'),
                        (2, '2026-01-02T00:00:00+00:00', NULL, 'COMPLETED');
                    """
                )
                conn.executemany(
                    "INSERT INTO telemetry_points VALUES (?, ?, 'watch', ?, ?, ?, ?)",
                    [
                        (
                            (now - timedelta(seconds=3)).isoformat(),
                            1,
                            75,
                            1.0,
                            0.4,
                            0,
                        ),
                        (
                            (now - timedelta(seconds=2)).isoformat(),
                            1,
                            80,
                            1.2,
                            0.6,
                            1,
                        ),
                        (
                            (now - timedelta(minutes=5)).isoformat(),
                            2,
                            0,
                            1.4,
                            0.8,
                            0,
                        ),
                    ],
                )
                conn.commit()

            with (
                patch.object(db, "TIGER_DATA_URL", None),
                patch.object(db, "SQLITE_DB_PATH", database_path),
            ):
                data = db.load_live_telemetry(limit=2)

        self.assertEqual(data["session"]["id"], 1)
        self.assertEqual([point["bpm"] for point in data["history"]], [75, 80])
        self.assertEqual(data["latest"]["speed"], 0.6)
        self.assertTrue(data["latest"]["fall_detected"])

    def test_load_live_telemetry_does_not_report_stale_reading_as_live(self):
        stale_time = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "climb_data.db"
            with closing(db.sqlite3.connect(database_path)) as conn:
                conn.executescript(
                    """
                    CREATE TABLE sessions (
                        id INTEGER PRIMARY KEY,
                        started_at TEXT,
                        ended_at TEXT,
                        status TEXT
                    );
                    CREATE TABLE telemetry_points (
                        time TEXT NOT NULL,
                        session_id INTEGER,
                        source TEXT,
                        bpm INTEGER,
                        accel_g REAL,
                        speed REAL,
                        fall_detected INTEGER
                    );
                    """
                )
                conn.execute(
                    "INSERT INTO telemetry_points VALUES (?, NULL, 'watch', 80, 1.0, NULL, NULL)",
                    (stale_time,),
                )
                conn.commit()

            with (
                patch.object(db, "TIGER_DATA_URL", None),
                patch.object(db, "SQLITE_DB_PATH", database_path),
            ):
                data = db.load_live_telemetry()

        self.assertIsNone(data["latest"])
        self.assertEqual(data["history"][0]["bpm"], 80)


if __name__ == "__main__":
    unittest.main()
