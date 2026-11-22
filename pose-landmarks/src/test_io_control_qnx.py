"""Tests for io_control_qnx using a fake rpi_gpio module (runs anywhere, no QNX needed)."""
import importlib
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class FakePWM:
    MODE_MS = "ms"
    MODE_PWM = "pwm"

    def __init__(self, pin, freq, mode=None):
        self.pin, self.freq, self.mode = pin, freq, mode
        self.duty = None
        FakeGPIO.pwms[pin] = self

    def ChangeDutyCycle(self, dc):
        self.duty = dc


class FakeGPIO(types.ModuleType):
    pwms = {}


def make_fake_gpio():
    g = FakeGPIO("rpi_gpio")
    FakeGPIO.pwms = {}
    g.OUT, g.IN, g.PUD_UP, g.BOTH, g.LOW, g.HIGH = "out", "in", "pud_up", "both", 0, 1
    g.PWM = FakePWM
    g.level = {26: 1}
    g.setups, g.events = {}, {}
    g.setup = lambda pin, mode, pud=None: g.setups.__setitem__(pin, (mode, pud))
    g.input = lambda pin: g.level[pin]
    g.add_event_detect = lambda pin, edge, callback=None: g.events.__setitem__(pin, (edge, callback))
    return g


@pytest.fixture
def qnx(monkeypatch):
    gpio = make_fake_gpio()
    monkeypatch.setitem(sys.modules, "rpi_gpio", gpio)
    sys.modules.pop("io_control_qnx", None)
    mod = importlib.import_module("io_control_qnx")
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    yield mod, gpio
    sys.modules.pop("io_control_qnx", None)


def test_setup_uses_hw_pwm_pins_at_50hz_and_centres(qnx):
    mod, gpio = qnx
    io = mod.IoControl()
    assert set(gpio.pwms) == {12, 13}
    for p in gpio.pwms.values():
        assert p.freq == 50 and p.mode == FakePWM.MODE_MS
        assert p.duty == pytest.approx(7.5)  # 135 deg -> 1500 us -> 7.5 %
    assert gpio.setups[26] == ("in", "pud_up")
    assert gpio.events[26][0] == "both"
    assert io.get_base_angle() == 135 and io.get_camera_angle() == 135


def test_pulse_to_duty_range():
    # independent of the fixture: pure maths
    assert 500 / 20000 * 100 == pytest.approx(2.5)
    assert 2500 / 20000 * 100 == pytest.approx(12.5)


def test_rotation_moves_servo_and_clamps(qnx):
    mod, gpio = qnx
    io = mod.IoControl()
    io.rotate_right(inc=1000)
    assert io.get_base_angle() == io.PAN_MAX
    assert gpio.pwms[12].duty == pytest.approx(12.5)
    io.rotate_left(inc=1000)
    assert io.get_base_angle() == io.PAN_MIN
    assert gpio.pwms[12].duty == pytest.approx(2.5)
    io.rotate_up(inc=1000)
    assert io.get_camera_angle() == io.PITCH_MAX
    io.rotate_down(inc=1000)
    assert io.get_camera_angle() == io.PITCH_MIN
    assert gpio.pwms[13].duty == pytest.approx(io.pulsewidth_to_duty(io.degrees_to_pulsewidth(io.PITCH_MIN)))


def test_switch_press_centres_and_release_clears(qnx):
    mod, gpio = qnx
    io = mod.IoControl()
    io.rotate_right(inc=50)
    callback = gpio.events[26][1]
    gpio.level[26] = 0
    callback(26)
    assert io.resetToggled is True and io.get_base_angle() == 135
    gpio.level[26] = 1
    callback(26)
    assert io.resetToggled is False


def test_stop_all_zeroes_duty(qnx):
    mod, gpio = qnx
    io = mod.IoControl()
    io.stop_all()
    assert all(p.duty == 0 for p in gpio.pwms.values())


def test_release_zeroes_duty_and_center_restores(qnx):
    mod, gpio = qnx
    io = mod.IoControl()
    io.release()
    assert all(p.duty == 0 for p in gpio.pwms.values())
    io.center()
    assert all(p.duty == pytest.approx(7.5) for p in gpio.pwms.values())
