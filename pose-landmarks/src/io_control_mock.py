"""
Drop-in stand-in for io_control_pigio.IoControl so forhead runs on Windows/macOS
without a Raspberry Pi, pigpio or servos.

Same public interface as the real class (rotate_*, center, read_switch,
stop_all, get_*_angle, resetToggled), with the same angle limits.

It also simulates a "virtual gimbal": view(frame) crops a window out of the
webcam frame and slides it around according to the current pan/tilt angles,
so the PID loop in main.py actually closes and you can watch it centre on
your face the way the real servos would.
"""

PITCH_MIN = 110
PITCH_MAX = 190
PAN_MIN = 0
PAN_MAX = 270
HOME = 135

CROP = 1.0


class IoControl:
    defaultInc = 2
    PITCH_MIN = PITCH_MIN
    PITCH_MAX = PITCH_MAX
    PAN_MIN = PAN_MIN
    PAN_MAX = PAN_MAX

    def __init__(self, verbose=True, crop_fraction=CROP, fov_deg=60.0):
        """
        crop_fraction: size of the virtual camera window relative to the real frame.
        fov_deg: assumed horizontal field of view of the webcam; sets how many
                 pixels one degree of rotation slides the window.
        """
        self.verbose = verbose
        self.crop_fraction = crop_fraction
        self.fov_deg = fov_deg
        self.resetToggled = False
        self.base_angle = HOME
        self.camera_angle = HOME
        self._log("[MOCK] IoControl started (no GPIO, no servos)")
        self.center()

    def _log(self, msg):
        if self.verbose:
            print(msg)

    @staticmethod
    def degrees_to_pulsewidth(degrees):
        degrees = max(0, min(270, degrees))
        return 500 + degrees * ((2500 - 500) / 270)

    def set_servo_pulsewidth(self, pin, pulsewidth):
        self._log(f"[MOCK] pin {pin} -> {pulsewidth:.0f} us")

    def rotate_right(self, inc=defaultInc):
        self.base_angle = min(self.PAN_MAX, self.base_angle + inc)

    def rotate_left(self, inc=defaultInc):
        self.base_angle = max(self.PAN_MIN, self.base_angle - inc)

    def rotate_up(self, inc=defaultInc):
        self.camera_angle = min(self.PITCH_MAX, self.camera_angle + inc)

    def rotate_down(self, inc=defaultInc):
        self.camera_angle = max(self.PITCH_MIN, self.camera_angle - inc)

    def get_base_angle(self):
        return self.base_angle

    def get_camera_angle(self):
        return self.camera_angle

    def center(self):
        self.base_angle = HOME
        self.camera_angle = HOME
        self._log("[MOCK] centred pan/tilt at 135 deg")

    def read_switch(self):
        return 1  # switch never pressed

    def release(self):
        self._log("[MOCK] servos released")

    def stop_all(self):
        self._log(f"[MOCK] stop_all (final pan={self.base_angle:.1f}, tilt={self.camera_angle:.1f})")

    # ---- virtual gimbal ----
    def view(self, frame):
        """Return the part of `frame` the camera would see at the current angles."""
        h, w = frame.shape[:2]
        cw, ch = int(w * self.crop_fraction), int(h * self.crop_fraction)
        px_per_deg = w / self.fov_deg

        # rotate_left lowers base_angle and must slide the window left;
        # rotate_up raises camera_angle and must slide the window up.
        dx = (self.base_angle - HOME) * px_per_deg
        dy = -(self.camera_angle - HOME) * px_per_deg

        x0 = int(round((w - cw) / 2 + dx))
        y0 = int(round((h - ch) / 2 + dy))
        x0 = max(0, min(w - cw, x0))
        y0 = max(0, min(h - ch, y0))
        return frame[y0:y0 + ch, x0:x0 + cw].copy()