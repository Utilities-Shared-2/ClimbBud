"""handle_phrase -> HTTP calls to the dashboard (no mic, no Vosk, no network needed)."""
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

HERE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)  # must not open the mic or load the model
    return module


def ok(recording):
    response = Mock(status_code=200)
    response.json.return_value = {"recording": recording, "changed": True}
    return response


class HandlePhraseTests(unittest.TestCase):
    def setUp(self):
        self.rc = load("recorder_control")
        self.rc.DASHBOARD_URL = "http://dash:5000"
        self.rc.POSE_TRACKER_URL = ""
        self.rc.RECORDING_API_TOKEN = ""
        self.rc.print = lambda *a, **k: None
        patcher = patch("requests.post")
        self.post = patcher.start()
        self.addCleanup(patcher.stop)
        self.post.side_effect = self.reply

    @staticmethod
    def reply(url, **kw):
        if url.endswith("/toggle"):
            response = Mock(status_code=200)
            response.json.return_value = {"enabled": kw["params"]["state"] == "on"}
            return response
        return ok(url.endswith("/start"))

    def calls(self):
        return [(c.args[0], c.kwargs.get("params", {}).get("state")) for c in self.post.call_args_list]

    def test_phrases_drive_qnx_tracker_at_its_own_url(self):
        self.rc.POSE_TRACKER_URL = "http://qnx:8080"
        self.rc.handle_phrase("start recording")
        self.assertEqual(self.rc.last_tracking, {"enabled": True})
        self.rc.handle_phrase("end recording")
        self.assertEqual(self.calls(), [
            ("http://qnx:8080/toggle", "on"),
            ("http://dash:5000/api/recording/start", None),
            ("http://dash:5000/api/recording/stop", None),
            ("http://qnx:8080/toggle", "off"),
        ])
        self.assertEqual(self.rc.last_tracking, {"enabled": False})

    def test_tracker_still_toggled_when_dashboard_is_down(self):
        self.rc.POSE_TRACKER_URL = "http://qnx:8080"

        def dashboard_down(url, **kw):
            if url.startswith("http://dash"):
                raise requests.ConnectionError("dashboard down")
            return self.reply(url, **kw)

        self.post.side_effect = dashboard_down
        self.rc.handle_phrase("start recording")
        self.rc.handle_phrase("end recording")
        self.assertFalse(self.rc.session_active)
        self.assertIn(("http://qnx:8080/toggle", "off"), self.calls())

    def test_tracker_down_does_not_block_recording(self):
        self.rc.POSE_TRACKER_URL = "http://qnx:8080"

        def qnx_down(url, **kw):
            if url.startswith("http://qnx"):
                raise requests.ConnectionError("qnx down")
            return self.reply(url, **kw)

        self.post.side_effect = qnx_down
        self.rc.handle_phrase("start recording")
        self.assertTrue(self.rc.session_active)

    def test_no_tracker_url_sends_no_toggle(self):
        self.rc.handle_phrase("start recording")
        self.rc.handle_phrase("end recording")
        self.assertFalse(any(url.endswith("/toggle") for url, _ in self.calls()))

    def urls(self):
        return [c.args[0] for c in self.post.call_args_list]

    def test_start_once_then_stop(self):
        self.rc.handle_phrase("start recording")
        self.rc.handle_phrase(" Start Recording ")  # repeated: not re-sent
        self.rc.handle_phrase("end recording")
        self.rc.handle_phrase("end recording")      # already stopped: not re-sent
        self.assertEqual(self.urls(), ["http://dash:5000/api/recording/start",
                                       "http://dash:5000/api/recording/stop"])
        self.assertEqual(self.post.call_args.kwargs["headers"], {})
        self.assertEqual(self.post.call_args.kwargs["timeout"], self.rc.HTTP_TIMEOUT_SEC)
        self.assertEqual(self.rc.last_response["recording"], False)

    def test_token_header_sent_when_configured(self):
        self.rc.RECORDING_API_TOKEN = "s3cret"
        self.rc.handle_phrase("start recording")
        self.assertEqual(self.post.call_args.kwargs["headers"], {"X-ClimbBud-Token": "s3cret"})

    def test_other_phrases_ignored(self):
        self.rc.handle_phrase("[unk]")
        self.rc.handle_phrase("")
        self.post.assert_not_called()

    def test_unreachable_dashboard_allows_retry(self):
        self.post.side_effect = requests.ConnectionError("no route")
        self.rc.handle_phrase("start recording")
        self.assertFalse(self.rc.session_active)
        self.post.side_effect = lambda url, **kw: ok(True)
        self.rc.handle_phrase("start recording")
        self.assertTrue(self.rc.session_active)
        self.assertEqual(self.post.call_count, 2)

    def test_refused_command_is_not_counted(self):
        self.post.side_effect = lambda url, **kw: Mock(status_code=401, text="bad token")
        self.rc.handle_phrase("start recording")
        self.assertFalse(self.rc.session_active)


class SimulatorTests(unittest.TestCase):
    def setUp(self):
        self.rc = load("recorder_control")
        self.rc.print = lambda *a, **k: None
        self.sim = load("simulate_command")
        self.sim.print = lambda *a, **k: None
        patcher = patch("requests.post")
        self.post = patcher.start()
        self.addCleanup(patcher.stop)

    def test_simulator_hits_tracker_url(self):
        def reply(url, **kw):
            if url.endswith("/toggle"):
                r = Mock(status_code=200)
                r.json.return_value = {"enabled": kw["params"]["state"] == "on"}
                return r
            return ok(True)

        self.post.side_effect = reply
        self.assertEqual(self.sim.main(["start", "--url", "http://10.0.0.5:5000",
                                        "--tracker-url", "http://10.0.0.9:8080/"]), 0)
        urls = [c.args[0] for c in self.post.call_args_list]
        self.assertEqual(urls, ["http://10.0.0.9:8080/toggle", "http://10.0.0.5:5000/api/recording/start"])

    def test_simulator_fails_when_tracker_unreachable(self):
        def reply(url, **kw):
            if url.endswith("/toggle"):
                raise requests.ConnectionError("qnx down")
            return ok(True)

        self.post.side_effect = reply
        self.assertEqual(self.sim.main(["start", "--url", "http://10.0.0.5:5000",
                                        "--tracker-url", "http://10.0.0.9:8080"]), 1)

    def test_start_and_stop_send_real_calls(self):
        self.post.return_value = ok(True)
        self.assertEqual(self.sim.main(["start", "--url", "http://10.0.0.5:5000/", "--token", "t",
                                        "--tracker-url", ""]), 0)
        self.assertEqual(self.post.call_args.args[0], "http://10.0.0.5:5000/api/recording/start")
        self.assertEqual(self.post.call_args.kwargs["headers"], {"X-ClimbBud-Token": "t"})
        self.post.return_value = ok(False)
        self.assertEqual(self.sim.main(["stop", "--url", "http://10.0.0.5:5000", "--tracker-url", ""]), 0)
        self.assertEqual(self.post.call_args.args[0], "http://10.0.0.5:5000/api/recording/stop")

    def test_exit_code_when_dashboard_unreachable(self):
        self.post.side_effect = requests.ConnectionError("down")
        self.assertEqual(self.sim.main(["start", "--url", "http://10.0.0.5:5000", "--tracker-url", ""]), 1)


if __name__ == "__main__":
    unittest.main()
