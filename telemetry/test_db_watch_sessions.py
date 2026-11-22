import json
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
