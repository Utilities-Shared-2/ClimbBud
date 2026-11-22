import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ui.app import app


class StatsApiTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.session = {
            "id": "session-3",
            "session_id": 3,
            "started_at": "2026-01-01T00:00:00+00:00",
            "ended_at": "2026-01-01T00:01:00+00:00",
            "duration_sec": 60,
            "avg_bpm": 120.5,
            "peak_bpm": 150,
            "hold_shifts": 4,
            "stability_pct": 80.0,
            "accel_spike_count": 2,
            "accel_spikes": [1.4, 1.5],
            "peak_accel_g": 1.5,
            "sample_count": 10,
            "summary": None,
            "_points": [],
        }

    def test_stats_api_returns_aggregate_cards_and_session_metrics(self):
        database = types.ModuleType("telemetry.db")
        database.load_watch_sessions = lambda: {
            "source": "postgres",
            "sessions": [self.session],
        }

        with patch.dict(sys.modules, {"telemetry.db": database}):
            response = self.client.get("/api/stats")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["routes_discovered"], 1)
        self.assertEqual(payload["crux_peak"], 150)
        self.assertEqual(payload["longest_ascent"], 60)
        self.assertEqual(payload["terrain_stability"], 80.0)
        self.assertEqual(payload["sessions"][0]["hold_shifts"], 4)

    def test_summary_endpoint_generates_and_saves_report(self):
        calls = {}
        database = types.ModuleType("telemetry.db")
        database.load_watch_sessions = lambda: {
            "source": "postgres",
            "sessions": [self.session],
        }

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, query, params):
                calls["query"] = query
                calls["params"] = params

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def cursor(self):
                return Cursor()

        database.get_postgres_connection = lambda: Connection()
        database.persist_local_sessions = lambda *_args: 0
        database.persist_unassigned_postgres_session = lambda *_args: 0

        psycopg = types.ModuleType("psycopg")
        psycopg.Error = type("PsycopgError", (Exception,), {})
        google = types.ModuleType("google")
        google.__path__ = []
        genai = types.ModuleType("google.genai")
        genai.__path__ = []
        errors = types.ModuleType("google.genai.errors")
        errors.APIError = type("APIError", (Exception,), {})
        gemini = types.ModuleType("telemetry.gemini_detector")
        gemini.get_gemini_client = lambda: object()

        def generate_report(_client, session_id, metrics):
            calls["session_id"] = session_id
            calls["metrics"] = metrics
            return SimpleNamespace(risk_level="LOW", summary="Strong, steady ascent.")

        gemini.generate_field_report = generate_report
        modules = {
            "telemetry.db": database,
            "psycopg": psycopg,
            "google": google,
            "google.genai": genai,
            "google.genai.errors": errors,
            "telemetry.gemini_detector": gemini,
        }

        with patch.dict(sys.modules, modules):
            response = self.client.post("/api/sessions/session-3/summary")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["summary"], "Strong, steady ascent.")
        self.assertEqual(calls["session_id"], 3)
        self.assertEqual(calls["metrics"]["hold_shifts"], 4)
        self.assertEqual(calls["metrics"]["accel_spike_count"], 2)
        self.assertIn("COACHING_SUMMARY", calls["query"])
        self.assertEqual(calls["params"], (3, "LOW", "Strong, steady ascent."))


if __name__ == "__main__":
    unittest.main()
