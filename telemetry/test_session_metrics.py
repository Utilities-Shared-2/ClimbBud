import unittest

from telemetry.session_metrics import build_sessions


class SessionMetricsTests(unittest.TestCase):
    def test_groups_unassigned_samples_at_gaps_over_sixty_seconds(self):
        sessions = build_sessions(
            [
                {"ts": 0, "session_id": None},
                {"ts": 60, "session_id": None},
                {"ts": 120.1, "session_id": None},
                {"ts": 1000, "session_id": 7},
                {"ts": 1100, "session_id": 7},
            ]
        )

        self.assertEqual(len(sessions), 3)
        by_id = {session["id"]: session for session in sessions}
        self.assertEqual(by_id["gap-0"]["sample_count"], 2)
        self.assertEqual(by_id["session-7"]["sample_count"], 2)
        self.assertEqual(by_id["session-7"]["duration_sec"], 100)

    def test_calculates_metrics_and_ignores_zero_bpm(self):
        session = build_sessions(
            [
                {
                    "ts": 10,
                    "session_id": 2,
                    "bpm": 0,
                    "accel_g": 1.0,
                    "xyz": [0, 0, 0],
                },
                {
                    "ts": 11,
                    "session_id": 2,
                    "bpm": 90,
                    "accel_g": 1.3,
                    "xyz": [250, 0, 0],
                },
                {
                    "ts": 12,
                    "session_id": 2,
                    "bpm": 120,
                    "xyz": [251, 0, 0],
                },
            ]
        )[0]

        self.assertEqual(session["duration_sec"], 2)
        self.assertEqual(session["avg_bpm"], 105)
        self.assertEqual(session["peak_bpm"], 120)
        self.assertEqual(session["hold_shifts"], 1)
        self.assertEqual(session["stability_pct"], 33.3)
        self.assertEqual(session["accel_spike_count"], 1)

    def test_explicit_session_id_is_not_split_by_timestamp_gaps(self):
        sessions = build_sessions(
            [
                {"ts": 0, "session_id": 4},
                {"ts": 61, "session_id": 4},
            ]
        )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["duration_sec"], 61)


if __name__ == "__main__":
    unittest.main()
