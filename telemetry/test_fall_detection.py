import unittest

from telemetry.fall_detection import (
    FallConfig,
    FallMonitor,
    PoseSample,
    check_impact,
    check_inactivity,
    check_pose_drop,
    check_pose_stillness,
    pose_sample_from_payload,
)


class FallDetectionTests(unittest.TestCase):
    def setUp(self):
        self.config = FallConfig()

    def test_watch_flag_and_acceleration_trigger_impact(self):
        self.assertEqual(
            check_impact([{"time": 99, "fall_detected": True}], 100, self.config),
            ("Fall detected by watch", 99),
        )
        self.assertEqual(
            check_impact([{"time": 99, "accel_g": 2.2}], 100, self.config),
            ("Impact detected (2.2 g)", 99),
        )
        self.assertIsNone(check_impact([{"time": 89, "accel_g": 3}], 100, self.config))
        self.assertIsNone(check_impact([{"time": 101, "accel_g": 3}], 100, self.config))

    def test_inactivity_requires_recording_window_and_prior_event(self):
        history = [{"time": t, "accel_g": 1.0} for t in range(90, 101)]
        self.assertIsNone(check_inactivity(history, 100, True, self.config))
        history.insert(0, {"time": 89, "accel_g": 1.7})
        self.assertIsNone(check_inactivity(history, 99, True, self.config))
        self.assertIsNone(check_inactivity(history, 100, False, self.config))
        self.assertIsNotNone(check_inactivity(history, 100, True, self.config))
        history[-1]["accel_g"] = 1.3
        self.assertIsNone(check_inactivity(history, 100, True, self.config))
        history[-1]["accel_g"] = float("nan")
        self.assertIsNone(check_inactivity(history, 100, True, self.config))

    def test_inactivity_does_not_infer_motion_through_data_gaps(self):
        history = [
            {"time": 89, "accel_g": 1.7},
            {"time": 90, "accel_g": 1.0},
            {"time": 100, "accel_g": 1.0},
        ]
        self.assertIsNone(check_inactivity(history, 100, True, self.config))

    def test_pose_drop_and_jitter(self):
        samples = [PoseSample(99.5, True, 0.3), PoseSample(100, True, 0.56)]
        self.assertIsNotNone(check_pose_drop(samples, 100, self.config))
        samples[-1] = PoseSample(100, True, 0.31)
        self.assertIsNone(check_pose_drop(samples, 100, self.config))
        self.assertIsNone(check_pose_drop(samples, 102, self.config))

    def test_pose_loss_breaks_drop_and_stillness_observations(self):
        samples = [
            PoseSample(94, True, 0.2),
            PoseSample(94.5, True, 0.35),
            PoseSample(95, True, 0.35),
            PoseSample(97, False, None),
            PoseSample(100, True, 0.35),
        ]
        self.assertIsNone(check_pose_stillness(samples, 100, self.config))
        drop = [
            PoseSample(99.2, True, 0.2),
            PoseSample(99.5, False, None),
            PoseSample(100, True, 0.6),
        ]
        self.assertIsNone(check_pose_drop(drop, 100, self.config))

    def test_pose_stillness_after_soft_drop(self):
        samples = [PoseSample(94, True, 0.2), PoseSample(94.5, True, 0.35)]
        samples.extend(PoseSample(t, True, 0.35) for t in range(95, 101))
        self.assertIsNotNone(check_pose_stillness(samples, 100, self.config))
        self.assertIsNone(check_pose_stillness(samples[2:], 100, self.config))

    def test_pose_payload_normalizes_frame_coordinates(self):
        payload = {"detected": True, "body": {"center": [10, 60]}, "frame_shape": [200, 300]}
        self.assertEqual(pose_sample_from_payload(payload, 10), PoseSample(10, True, 0.3))
        self.assertEqual(pose_sample_from_payload({"detected": False}, 10), PoseSample(10, False, None))

    def test_monitor_latches_dismisses_and_accepts_new_impact_after_cooldown(self):
        monitor = FallMonitor(self.config)
        first = [{"time": 100, "accel_g": 3.0}]
        self.assertTrue(monitor.evaluate(first, True, now=100)["active"])
        self.assertTrue(monitor.evaluate([], False, now=101)["active"])
        self.assertFalse(monitor.dismiss(now=101)["active"])
        self.assertFalse(monitor.evaluate(first, True, now=107)["active"])
        second = first + [{"time": 108, "accel_g": 3.2}]
        self.assertTrue(monitor.evaluate(second, True, now=108)["active"])

    def test_pose_drop_not_retriggered_after_dismiss(self):
        monitor = FallMonitor(FallConfig(cooldown_sec=0.1))
        samples = [PoseSample(99.5, True, 0.2), PoseSample(100, True, 0.6)]
        self.assertTrue(monitor.evaluate([], True, samples, now=100)["active"])
        monitor.dismiss(now=100.1)
        self.assertFalse(monitor.evaluate([], True, samples, now=100.3)["active"])

    def test_simulation_can_be_disabled(self):
        monitor = FallMonitor(FallConfig(simulate_enabled=False))
        self.assertFalse(monitor.simulate(now=100))
        self.assertFalse(monitor.state()["active"])


if __name__ == "__main__":
    unittest.main()
