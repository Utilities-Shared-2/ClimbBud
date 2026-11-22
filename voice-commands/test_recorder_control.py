"""handle_phrase -> MQTT session commands (no mic, no Vosk, no broker needed)."""
import importlib.util
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


def load_module():
    path = Path(__file__).resolve().parent / "recorder_control.py"
    spec = importlib.util.spec_from_file_location("recorder_control", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # must not open the mic or load the model
    return module


class HandlePhraseTests(unittest.TestCase):
    def setUp(self):
        self.rc = load_module()
        self.client = Mock()
        self.client.publish.return_value = SimpleNamespace(rc=0)
        self.rc.mqtt_client = self.client
        self.rc.print = lambda *a, **k: None

    def commands(self):
        return [json.loads(c.args[1])["command"] for c in self.client.publish.call_args_list]

    def test_start_once_then_stop(self):
        self.rc.handle_phrase("start recording")
        self.rc.handle_phrase(" Start Recording ")  # repeated: not re-sent
        self.rc.handle_phrase("end recording")
        self.rc.handle_phrase("end recording")      # already stopped: not re-sent
        self.assertEqual(self.commands(), ["START", "STOP"])
        topic, payload = self.client.publish.call_args_list[0].args[:2]
        self.assertEqual(topic, "climbbud/control/session")
        self.assertEqual(json.loads(payload)["source"], "voice")

    def test_other_phrases_ignored(self):
        self.rc.handle_phrase("[unk]")
        self.rc.handle_phrase("")
        self.client.publish.assert_not_called()

    def test_failed_publish_allows_retry(self):
        self.client.publish.return_value = SimpleNamespace(rc=4)
        self.rc.handle_phrase("start recording")
        self.assertFalse(self.rc.session_active)
        self.client.publish.return_value = SimpleNamespace(rc=0)
        self.rc.handle_phrase("start recording")
        self.assertTrue(self.rc.session_active)
        self.assertEqual(self.commands(), ["START", "START"])

    def test_no_client_does_not_crash(self):
        self.rc.mqtt_client = None
        self.rc.handle_phrase("start recording")
        self.assertFalse(self.rc.session_active)


if __name__ == "__main__":
    unittest.main()
