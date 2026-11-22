"""
Servo + limit-switch control for QNX 8.0 on the Raspberry Pi, via QNX's rpi_gpio
Python module (talks to the rpi_gpio resource manager at /dev/gpio).

Same public interface as io_control_pigio.IoControl / io_control_mock.IoControl.

Wiring (GPIO / Broadcom numbers, not physical pins):
  QNX rpi_gpio only drives hardware PWM on GPIO 12, 13, 18 and 19
  (channel 1 = 12/18, channel 2 = 13/19), so the servos must be on two
  different channels:
    pan / base servo : GPIO 12  (physical pin 32)
    tilt servo       : GPIO 13  (physical pin 33)
    limit switch     : GPIO 26  (physical pin 37), to ground, internal pull-up
"""
import time

try:
    import rpi_gpio as GPIO
except ImportError as e:
    raise ImportError(
        "rpi_gpio not available - this backend only runs on QNX for Raspberry Pi. "
        "Check the resource manager is running with: pidin -p rpi_gpio") from e

PITCH_MIN = 110
PITCH_MAX = 190
PAN_MIN = 0
PAN_MAX = 270
HOME = 135

SERVO_FREQ_HZ = 50
PERIOD_US = 1_000_000 / SERVO_FREQ_HZ  # 20000 us


class IoControl:
    defaultInc = 2
    PITCH_MIN = PITCH_MIN
    PITCH_MAX = PITCH_MAX
    PAN_MIN = PAN_MIN
    PAN_MAX = PAN_MAX

    # GPIO pin numbers (BOARD → BCM conversion if needed)
    # BOARD 11 → BCM 17
    # BOARD 13 → BCM 27
    # BOARD 37 → BCM 26
    def __init__(self, base_pin=17, camera_pin=27, switch_pin=26):
        self.base_pin = base_pin
        self.camera_pin = camera_pin
        self.switch_pin = switch_pin
        self.resetToggled = False

        # M/S mode gives a fixed-period pulse, which is what hobby servos need
        self._pwm = {
            pin: GPIO.PWM(pin, SERVO_FREQ_HZ, mode=GPIO.PWM.MODE_MS)
            for pin in (base_pin, camera_pin)
        }

        GPIO.setup(switch_pin, GPIO.IN, GPIO.PUD_UP)
        GPIO.add_event_detect(switch_pin, GPIO.BOTH, callback=self.switch_callback)

        print("[INFO] initalizing servo position")
        self.center()

    @staticmethod
    def degrees_to_pulsewidth(degrees):
        """Convert degrees (0-270) to pulse width (500-2500 us)."""
        degrees = max(0, min(270, degrees))
        return 500 + degrees * ((2500 - 500) / 270)

    @staticmethod
    def pulsewidth_to_duty(pulsewidth_us):
        """Pulse width in us -> duty cycle percent at 50 Hz (500 us = 2.5 %, 2500 us = 12.5 %)."""
        return pulsewidth_us / PERIOD_US * 100.0

    def set_servo_pulsewidth(self, pin, pulsewidth):
        """Pulse width in us; 0 stops the pulses (servo goes limp), matching pigpio."""
        self._pwm[pin].ChangeDutyCycle(self.pulsewidth_to_duty(pulsewidth))

    def _apply(self):
        self.set_servo_pulsewidth(self.base_pin, self.degrees_to_pulsewidth(self.base_angle))
        self.set_servo_pulsewidth(self.camera_pin, self.degrees_to_pulsewidth(self.camera_angle))

    def rotate_right(self, inc=defaultInc):
        if self.base_angle < self.PAN_MAX:
            self.base_angle = min(self.PAN_MAX, self.base_angle + inc)
            self.set_servo_pulsewidth(self.base_pin, self.degrees_to_pulsewidth(self.base_angle))

    def rotate_left(self, inc=defaultInc):
        if self.base_angle > self.PAN_MIN:
            self.base_angle = max(self.PAN_MIN, self.base_angle - inc)
            self.set_servo_pulsewidth(self.base_pin, self.degrees_to_pulsewidth(self.base_angle))

    def rotate_up(self, inc=defaultInc):
        if self.camera_angle < self.PITCH_MAX:
            self.camera_angle = min(self.PITCH_MAX, self.camera_angle + inc)
            self.set_servo_pulsewidth(self.camera_pin, self.degrees_to_pulsewidth(self.camera_angle))

    def rotate_down(self, inc=defaultInc):
        if self.camera_angle > self.PITCH_MIN:
            self.camera_angle = max(self.PITCH_MIN, self.camera_angle - inc)
            self.set_servo_pulsewidth(self.camera_pin, self.degrees_to_pulsewidth(self.camera_angle))

    def move_to(self, pan, tilt):
        """Drive both servos to absolute angles (clamped). Used by gimbal.SmoothGimbal."""
        self.base_angle = max(self.PAN_MIN, min(self.PAN_MAX, pan))
        self.camera_angle = max(self.PITCH_MIN, min(self.PITCH_MAX, tilt))
        self._apply()

    def get_base_angle(self):
        return self.base_angle

    def get_camera_angle(self):
        return self.camera_angle

    def switch_callback(self, pin, *_):
        """Edge callback (rpi_gpio passes the pin number). LOW = pressed (pull-up)."""
        if GPIO.input(self.switch_pin) == GPIO.LOW:
            print("--- Limit Switch Pressed! ---")
            self.resetToggled = True
            self.center()
        else:
            print("--- Limit Switch Released! ---")
            self.resetToggled = False

    def read_switch(self):
        return GPIO.input(self.switch_pin)

    def center(self):
        self.base_angle = HOME
        self.camera_angle = HOME
        self._apply()
        time.sleep(1)

    def release(self):
        """Stop the servo pulses (servos go limp). Reversible: center() drives them again."""
        for pin in self._pwm:
            self.set_servo_pulsewidth(pin, 0)

    def stop_all(self):
        self.release()
