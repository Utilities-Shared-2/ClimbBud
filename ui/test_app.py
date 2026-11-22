import io
import json
import sys
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import URLError

import requests
from telemetry.fall_detection import FallConfig, FallMonitor, PoseSample
from ui.app import PosePoller, app, run_dashboard, state


class PosePollerTests(unittest.TestCase):
    def setUp(self):
        self.poller = PosePoller("http://camera:8080/pose", history_sec=5)

    def poll_payload(self, timestamp, now, detected=True):
        payload = {
            "timestamp": timestamp,
            "detected": detected,
            "body": {"center": [10, 60]},
            "frame_shape": [200, 300],
        }
        with patch("ui.app.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())) as request:
            with patch("ui.app.time.time", return_value=now):
                self.poller.poll_once()
        request.assert_called_once_with("http://camera:8080/pose", timeout=1.0)

    def test_poller_records_distinct_frames_and_prunes_old_samples(self):
        self.poll_payload(10, 100)
        self.poll_payload(10, 101)
        self.poll_payload(11, 102, detected=False)
        with patch("ui.app.time.time", return_value=102):
            self.assertEqual(self.poller.samples(), [
                PoseSample(100, True, 0.3),
                PoseSample(102, False, None),
            ])
        with patch("ui.app.time.time", return_value=106):
            self.assertEqual(self.poller.samples(), [PoseSample(102, False, None)])

    def test_failure_clears_history_and_same_frame_does_not_restore_it(self):
        self.poll_payload(10, 100)
        with patch("ui.app.urlopen", side_effect=URLError("offline")):
            with self.assertLogs(app.logger, level="WARNING"):
                self.poller.poll_once()
        self.poll_payload(10, 101)
        with patch("ui.app.time.time", return_value=101):
            self.assertEqual(self.poller.samples(), [])
        self.poll_payload(11, 102)
        with patch("ui.app.time.time", return_value=102):
            self.assertEqual(self.poller.samples(), [PoseSample(102, True, 0.3)])

    def test_invalid_payload_clears_history(self):
        self.poll_payload(10, 100)
        with patch("ui.app.urlopen", return_value=io.BytesIO(b'{"timestamp": NaN}')):
            with self.assertLogs(app.logger, level="WARNING"):
                self.poller.poll_once()
        self.assertEqual(self.poller.samples(), [])

    def test_frozen_frame_is_reported_and_clears_history(self):
        self.poll_payload(10, 100)
        with self.assertLogs(app.logger, level="WARNING") as logs:
            self.poll_payload(10, 106)
        self.assertIn("stopped producing new frames", logs.output[0])
        self.assertEqual(self.poller.samples(), [])

    def test_timestamp_reset_starts_new_history(self):
        self.poll_payload(10, 100)
        self.poll_payload(1, 101)
        with patch("ui.app.time.time", return_value=101):
            self.assertEqual(self.poller.samples(), [PoseSample(101, True, 0.3)])

    def test_reads_pose_from_http_server(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({
                    "timestamp": 10,
                    "detected": True,
                    "body": {"center": [10, 60]},
                    "frame_shape": [200, 300],
                }).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            poller = PosePoller(f"http://127.0.0.1:{server.server_port}/pose", 5)
            poller.poll_once()
            self.assertEqual(len(poller.samples()), 1)
            self.assertEqual(poller.samples()[0].y, 0.3)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()


class FallApiTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.monitor = FallMonitor(FallConfig(), clock=lambda: 100.0)
        self.monitor_patch = patch("ui.app.fall_monitor", self.monitor)
        self.pose_patch = patch("ui.app.pose_poller", None)
        self.monitor_patch.start()
        self.pose_patch.start()
        self.addCleanup(self.monitor_patch.stop)
        self.addCleanup(self.pose_patch.stop)

    def live(self, latest=None, history=None):
        database = types.ModuleType("telemetry.db")
        database.load_live_telemetry = lambda: {
            "session": None,
            "latest": latest,
            "history": history or [],
        }
        with patch.dict(sys.modules, {"telemetry.db": database}):
            return self.client.get("/api/live")

    def test_watch_alert_is_latched_and_dismissed(self):
        point = {
            "time": 99,
            "bpm": 120,
            "speed": 0.5,
            "accel_g": 1.0,
            "fall_detected": True,
        }
        self.assertFalse(self.live().get_json()["fall"]["active"])
        fall = self.live(point, [point]).get_json()["fall"]
        self.assertTrue(fall["active"])
        self.assertEqual(fall["source"], "watch")
        self.assertTrue(self.live().get_json()["fall"]["active"])
        dismissed = self.client.post("/api/fall/dismiss")
        self.assertEqual(dismissed.status_code, 200)
        self.assertFalse(dismissed.get_json()["fall"]["active"])
        self.assertFalse(self.live(point, [point]).get_json()["fall"]["active"])

    def test_pose_samples_feed_live_detection(self):
        pose = SimpleNamespace(samples=lambda: [
            PoseSample(99.5, True, 0.2),
            PoseSample(100, True, 0.6),
        ])
        with patch("ui.app.pose_poller", pose):
            fall = self.live().get_json()["fall"]
        self.assertTrue(fall["active"])
        self.assertEqual(fall["source"], "pose")

    def test_old_watch_history_does_not_trigger_without_fresh_latest(self):
        point = {"time": 99, "accel_g": 3.0}
        self.assertFalse(self.live(None, [point]).get_json()["fall"]["active"])

    def test_simulation_works_without_telemetry_and_dismiss_clears(self):
        database = types.ModuleType("telemetry.db")
        database.load_live_telemetry = lambda: (_ for _ in ()).throw(RuntimeError("offline"))
        with patch.dict(sys.modules, {"telemetry.db": database}):
            simulated = self.client.post("/api/fall/simulate")
            self.assertEqual(simulated.status_code, 200)
            self.assertEqual(simulated.get_json()["fall"]["source"], "simulated")
            with self.assertLogs(app.logger, level="ERROR"):
                live = self.client.get("/api/live")
            self.assertEqual(live.status_code, 503)
            self.assertTrue(live.get_json()["fall"]["active"])
            self.assertFalse(self.client.post("/api/fall/dismiss").get_json()["fall"]["active"])

    def test_simulation_visible_through_live_api(self):
        response = self.client.post("/api/fall/simulate")
        self.assertEqual(response.get_json()["fall"]["reason"], "Simulated fall (demo)")
        self.assertEqual(self.live().get_json()["fall"]["source"], "simulated")
        self.client.post("/api/fall/dismiss")
        self.assertFalse(self.live().get_json()["fall"]["active"])

    def test_pose_detection_remains_available_during_telemetry_failure(self):
        database = types.ModuleType("telemetry.db")
        database.load_live_telemetry = lambda: (_ for _ in ()).throw(RuntimeError("offline"))
        pose = SimpleNamespace(samples=lambda: [
            PoseSample(99.5, True, 0.2),
            PoseSample(100, True, 0.6),
        ])
        with patch.dict(sys.modules, {"telemetry.db": database}):
            with patch("ui.app.pose_poller", pose), self.assertLogs(app.logger, level="ERROR"):
                response = self.client.get("/api/live")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["fall"]["source"], "pose")

    def test_simulation_disabled_by_environment(self):
        with patch.dict("os.environ", {"FALL_SIMULATE_ENABLED": "0"}):
            disabled = FallMonitor(FallConfig.from_env(), clock=lambda: 100.0)
        with patch("ui.app.fall_monitor", disabled):
            self.assertFalse(self.live().get_json()["fall"]["simulate_enabled"])
            self.assertEqual(self.client.post("/api/fall/simulate").status_code, 404)
            self.assertFalse(disabled.state()["active"])


class DashboardStartupTests(unittest.TestCase):
    def test_server_binding_and_debug_flag(self):
        with patch.dict("os.environ", {"FLASK_DEBUG": ""}), patch.object(app, "run") as run:
            run_dashboard()
            run.assert_called_once_with(host="0.0.0.0", port=5000, debug=False)
        with patch.dict("os.environ", {"FLASK_DEBUG": "1"}), patch.object(app, "run") as run:
            run_dashboard()
            run.assert_called_once_with(host="0.0.0.0", port=5000, debug=True)


class VideoStreamTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_live_includes_stream_status_even_when_telemetry_fails(self):
        database = types.ModuleType("telemetry.db")
        database.load_live_telemetry = lambda: (_ for _ in ()).throw(RuntimeError("offline"))
        with patch.dict(state, {"stream_connected": True}), patch.dict(sys.modules, {"telemetry.db": database}):
            with self.assertLogs(app.logger, level="ERROR"):
                response = self.client.get("/api/live")
        self.assertEqual(response.status_code, 503)
        self.assertTrue(response.get_json()["stream_connected"])

    def test_stream_route_proxies_and_closes_response(self):
        upstream = Mock()
        upstream.headers = {"Content-Type": "multipart/x-mixed-replace; boundary=frame"}
        upstream.iter_content.return_value = iter([b"frame-data"])
        with patch.dict(state, {"stream_connected": True}), patch("ui.app.requests.get", return_value=upstream):
            response = self.client.get("/video/stream", buffered=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"frame-data")
        self.assertEqual(response.content_type, "multipart/x-mixed-replace; boundary=frame")
        upstream.close.assert_called_once()

    def test_stream_rejects_upstream_failure(self):
        upstream = Mock()
        upstream.raise_for_status.side_effect = requests.HTTPError("upstream error")
        with patch.dict(state, {"stream_connected": True}), patch("ui.app.requests.get", return_value=upstream):
            with self.assertLogs(app.logger, level="WARNING"):
                response = self.client.get("/video/stream")
        self.assertEqual(response.status_code, 503)
        upstream.close.assert_called_once()

    def test_raw_route_unconfigured_returns_503(self):
        with patch("ui.app.CAMERA_STREAM_URL", ""), patch("ui.app.requests.get") as get:
            response = self.client.get("/video/raw")
        self.assertEqual(response.status_code, 503)
        get.assert_not_called()

    def test_raw_route_proxies_camera_stream(self):
        upstream = Mock()
        upstream.headers = {"Content-Type": "multipart/x-mixed-replace; boundary=frame"}
        upstream.iter_content.return_value = iter([b"raw-frame"])
        url = "http://camera-pi:8081/stream"
        with patch("ui.app.CAMERA_STREAM_URL", url), patch.dict(state, {"camera_connected": True}), \
                patch("ui.app.requests.get", return_value=upstream) as get:
            response = self.client.get("/video/raw", buffered=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"raw-frame")
        self.assertEqual(get.call_args.args[0], url)
        upstream.close.assert_called_once()

    def test_raw_route_marks_camera_down_on_failure(self):
        with patch("ui.app.CAMERA_STREAM_URL", "http://camera-pi:8081/stream"), \
                patch.dict(state, {"camera_connected": True}), \
                patch("ui.app.requests.get", side_effect=requests.ConnectionError("down")):
            with self.assertLogs(app.logger, level="WARNING"):
                response = self.client.get("/video/raw")
            self.assertEqual(response.status_code, 503)
            self.assertFalse(state["camera_connected"])

    def test_pose_api_forwards_only_overlay_fields(self):
        upstream = Mock()
        upstream.json.return_value = {
            "timestamp": 12.5, "enabled": True, "detected": True, "frame_shape": [480, 640],
            "keypoints": {"nose": {"x": 1, "y": 2, "conf": 0.9, "visible": True}},
            "pan": 135, "body": {"center": [1, 2]},
        }
        with patch("ui.app.requests.get", return_value=upstream) as get:
            response = self.client.get("/api/pose")
        self.assertEqual(response.status_code, 200)
        from ui.app import POSE_STREAM_URL
        self.assertEqual(get.call_args.args[0], f"{POSE_STREAM_URL}/pose")
        self.assertEqual(set(response.get_json()),
                         {"timestamp", "enabled", "detected", "frame_shape", "keypoints"})

    def test_pose_api_503_when_pose_server_down(self):
        with patch("ui.app.requests.get", side_effect=requests.ConnectionError("down")):
            self.assertEqual(self.client.get("/api/pose").status_code, 503)
        bad = Mock()
        bad.json.side_effect = ValueError("not json")
        with patch("ui.app.requests.get", return_value=bad):
            self.assertEqual(self.client.get("/api/pose").status_code, 503)

    def test_video_source_prefers_raw_camera(self):
        from ui.app import video_source
        cases = [
            ("http://cam:8081/stream", True, True, "raw"),
            ("http://cam:8081/stream", False, True, "annotated"),
            ("", True, True, "annotated"),
            ("", False, False, None),
        ]
        for url, camera, stream, expected in cases:
            with patch("ui.app.CAMERA_STREAM_URL", url), \
                    patch.dict(state, {"camera_connected": camera, "stream_connected": stream}):
                self.assertEqual(video_source(), expected, (url, camera, stream))

    def test_camera_base_url(self):
        from ui.app import camera_base_url
        self.assertEqual(camera_base_url("http://camera-pi.local:8081/stream"), "http://camera-pi.local:8081")


class SessionControlTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.recorder = Mock()
        self.recorder.status.return_value = {"active": False}
        patcher = patch("ui.app.session_recorder", self.recorder)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = patch.dict(state, {"recording": False, "elapsed": 0.0, "start": time.time()})
        self.state.start()
        self.addCleanup(self.state.stop)

    def test_start_and_stop_drive_state_and_recorder(self):
        from ui.app import on_session_command
        with patch("ui.app.threading.Thread"):
            on_session_command("START")
        self.assertTrue(state["recording"])
        self.recorder.start.assert_called_once()
        on_session_command("STOP")
        self.assertFalse(state["recording"])
        self.recorder.stop.assert_called_once()
        self.assertGreaterEqual(state["elapsed"], 0)

    def test_stop_ends_session_and_next_start_resets_timer(self):
        from ui.app import read_elapsed, set_recording
        with patch("ui.app.threading.Thread"), patch("ui.app.time.time", return_value=100.0):
            set_recording(True)
        with patch("ui.app.time.time", return_value=130.0):
            set_recording(False)
            self.assertEqual(read_elapsed(), 30.0)  # frozen at the session's length
        with patch("ui.app.time.time", return_value=500.0):
            self.assertEqual(read_elapsed(), 30.0)
        with patch("ui.app.threading.Thread"), patch("ui.app.time.time", return_value=600.0):
            set_recording(True)
            self.assertEqual(read_elapsed(), 0.0)   # new session starts from zero
        self.assertEqual(self.recorder.stop.call_count, 1)
        self.assertEqual(self.recorder.start.call_count, 2)

    def test_remote_start_and_stop_are_idempotent(self):
        db = Mock()
        with patch("ui.app.telemetry_db", db), patch("ui.app.threading.Thread"), \
                patch.dict("os.environ", {"RECORDING_API_TOKEN": ""}):
            first = self.client.post("/api/recording/start").get_json()
            second = self.client.post("/api/recording/start").get_json()
            self.assertEqual((first["recording"], first["changed"]), (True, True))
            self.assertEqual((second["recording"], second["changed"]), (True, False))
            self.recorder.start.assert_called_once()
            db.start_session.assert_called_once()
            stopped = self.client.post("/api/recording/stop").get_json()
            again = self.client.post("/api/recording/stop").get_json()
        self.assertEqual((stopped["recording"], stopped["changed"]), (False, True))
        self.assertFalse(again["changed"])
        self.recorder.stop.assert_called_once()
        db.stop_session.assert_called_once()

    def test_remote_calls_require_token_when_configured(self):
        with patch("ui.app.telemetry_db", None), patch("ui.app.control_client", None), \
                patch("ui.app.threading.Thread"), patch.dict("os.environ", {"RECORDING_API_TOKEN": "s3cret"}):
            self.assertEqual(self.client.post("/api/recording/start").status_code, 401)
            wrong = self.client.post("/api/recording/start", headers={"X-ClimbBud-Token": "nope"})
            self.assertEqual(wrong.status_code, 401)
            self.recorder.start.assert_not_called()
            right = self.client.post("/api/recording/start", headers={"X-ClimbBud-Token": "s3cret"})
            self.assertEqual(right.status_code, 200)
            self.recorder.start.assert_called_once()
            # the browser button isn't affected by the token
            self.assertEqual(self.client.post("/api/recording/toggle").status_code, 200)
        self.assertFalse(state["recording"])

    def test_repeated_commands_are_ignored(self):
        from ui.app import on_session_command
        with patch("ui.app.threading.Thread"):
            on_session_command("START")
            on_session_command("START")  # e.g. voice says it again / our own echo
        self.recorder.start.assert_called_once()
        on_session_command("STOP")
        on_session_command("STOP")
        self.recorder.stop.assert_called_once()

    def test_toggle_starts_immediately_with_in_process_telemetry(self):
        db = Mock()
        client = Mock()
        with patch("ui.app.control_client", client), patch("ui.app.telemetry_db", db), \
                patch("ui.app.threading.Thread"):
            response = self.client.post("/api/recording/toggle")
            data = response.get_json()
            self.assertTrue(data["recording"])
            self.assertIn("video_recording", data)
            self.assertTrue(state["recording"])
            db.start_session.assert_called_once()
            self.recorder.start.assert_called_once()
            response = self.client.post("/api/recording/toggle")
        self.assertFalse(response.get_json()["recording"])
        db.stop_session.assert_called_once()
        self.recorder.stop.assert_called_once()
        client.publish.assert_not_called()  # would make the in-process worker start a 2nd session

    def test_toggle_without_broker_or_telemetry_still_records(self):
        with patch("ui.app.control_client", None), patch("ui.app.telemetry_db", None), \
                patch("ui.app.threading.Thread"):
            response = self.client.post("/api/recording/toggle")
        self.assertTrue(response.get_json()["recording"])
        self.recorder.start.assert_called_once()

    def test_toggle_tells_remote_telemetry_over_mqtt(self):
        from ui.app import on_session_command, parse_session_command

        def deliver(topic, payload, qos):
            on_session_command(parse_session_command(payload))  # broker echoes to our subscription
            return SimpleNamespace(rc=0)

        client = Mock()
        client.publish.side_effect = deliver
        connected = threading.Event()
        connected.set()
        with patch("ui.app.control_client", client), patch("ui.app._control_connected", connected), \
                patch("ui.app.telemetry_db", None), patch("ui.app.threading.Thread"):
            response = self.client.post("/api/recording/toggle")
        self.assertTrue(response.get_json()["recording"])
        topic, payload = client.publish.call_args.args[:2]
        self.assertEqual(topic, "climbbud/control/session")
        self.assertEqual(json.loads(payload), {**json.loads(payload), "command": "START", "source": "dashboard"})
        self.recorder.start.assert_called_once()  # the echo didn't restart it

    def test_live_reports_recording_even_when_telemetry_fails(self):
        database = types.ModuleType("telemetry.db")
        database.load_live_telemetry = lambda: (_ for _ in ()).throw(RuntimeError("offline"))
        state["recording"] = True
        with patch.dict(sys.modules, {"telemetry.db": database}):
            with self.assertLogs(app.logger, level="ERROR"):
                response = self.client.get("/api/live")
        self.assertEqual(response.status_code, 503)
        self.assertTrue(response.get_json()["recording"])
        self.assertIn("elapsed", response.get_json())


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

    def test_live_api_returns_database_telemetry(self):
        live_data = {
            "session": {"id": 8, "status": "ACTIVE"},
            "latest": {
                "time": "2026-01-01T00:00:02+00:00",
                "bpm": 126,
                "accel_g": 1.1,
                "speed": 0.7,
                "fall_detected": False,
            },
            "history": [{"bpm": 120}, {"bpm": 126}],
        }
        database = types.ModuleType("telemetry.db")
        database.load_live_telemetry = lambda: live_data

        with patch.dict(sys.modules, {"telemetry.db": database}):
            response = self.client.get("/api/live")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["bpm"], 126)
        self.assertEqual(payload["intensity"], 0.7)
        self.assertEqual(payload["session"]["id"], 8)
        self.assertEqual(payload["history"], live_data["history"])
        self.assertIsInstance(payload["stream_connected"], bool)

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
