import unittest

from telemetry.db import CONTROL_TOPIC, parse_session_command


class ParseSessionCommandTests(unittest.TestCase):
    def test_raw_strings_any_case(self):
        self.assertEqual(parse_session_command("START"), "START")
        self.assertEqual(parse_session_command(" stop \n"), "STOP")

    def test_json_command_or_action(self):
        self.assertEqual(parse_session_command('{"command": "start", "source": "voice"}'), "START")
        self.assertEqual(parse_session_command('{"action": "STOP"}'), "STOP")

    def test_junk_is_none(self):
        for payload in ("", "pause", "{not json", '["START"]', '{"command": 5}', '{"other": "START"}'):
            self.assertIsNone(parse_session_command(payload), payload)

    def test_topic(self):
        self.assertEqual(CONTROL_TOPIC, "climbbud/control/session")


if __name__ == "__main__":
    unittest.main()
