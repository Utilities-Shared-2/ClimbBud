"""Tests for gimbal.SmoothGimbal and gimbal.aim (no hardware, no threads)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from gimbal import SmoothGimbal, aim  # noqa: E402


class FakeIo:
    PAN_MIN, PAN_MAX, PITCH_MIN, PITCH_MAX = 0, 270, 110, 190

    def __init__(self):
        self.base_angle = self.camera_angle = 135
        self.moves = []

    def get_base_angle(self):
        return self.base_angle

    def get_camera_angle(self):
        return self.camera_angle

    def move_to(self, pan, tilt):
        self.base_angle, self.camera_angle = pan, tilt
        self.moves.append((pan, tilt))


def run(g, seconds, hz=50):
    for _ in range(int(seconds * hz)):
        g.step(1.0 / hz)


def test_eases_to_target_at_capped_speed():
    io = FakeIo()
    g = SmoothGimbal(io, max_speed=90, servo_hz=50)
    g.set_target(225, 135)  # 90 deg away
    run(g, 0.5)
    assert io.base_angle == pytest.approx(180)  # halfway after 0.5 s
    run(g, 0.6)
    assert io.base_angle == pytest.approx(225)
    pans = [135] + [m[0] for m in io.moves]
    assert max(abs(b - a) for a, b in zip(pans, pans[1:])) <= 90 / 50 + 1e-9


def test_no_writes_once_settled():
    io = FakeIo()
    g = SmoothGimbal(io)
    g.set_target(140, 140)
    run(g, 1)
    n = len(io.moves)
    run(g, 1)
    assert len(io.moves) == n


def test_target_clamped_to_limits():
    io = FakeIo()
    g = SmoothGimbal(io)
    g.set_target(999, -50)
    assert g.targets() == (270, 110)


def test_pause_stops_writes_and_resume_resyncs():
    io = FakeIo()
    g = SmoothGimbal(io)
    g.set_target(200, 135)
    g.pause()
    run(g, 1)
    assert io.moves == []
    io.base_angle = io.camera_angle = 135  # e.g. io.center()
    g.resume()
    assert g.angles() == (135, 135) and g.targets() == (135, 135)
    run(g, 1)
    assert io.moves == []  # target was reset, nothing to do


def test_aim_signs_match_pid_path():
    shape = (480, 640)
    # right of centre -> pan up; above centre -> tilt up
    pan, tilt = aim((640, 0), shape, 135, 135, hfov=64, gain=1.0, deadband=0)
    assert pan == pytest.approx(135 + 32)
    assert tilt == pytest.approx(135 + 24)
    pan, tilt = aim((0, 480), shape, 135, 135, hfov=64, gain=1.0, deadband=0)
    assert pan == pytest.approx(135 - 32) and tilt == pytest.approx(135 - 24)


def test_aim_gain_and_deadband():
    shape = (480, 640)
    pan, tilt = aim((420, 241), shape, 100, 150, hfov=64, gain=0.5, deadband=1.5)
    assert pan == pytest.approx(100 + 0.5 * 10)  # 100 px * 0.1 deg/px
    assert tilt == 150  # 0.1 deg error is inside the deadband
