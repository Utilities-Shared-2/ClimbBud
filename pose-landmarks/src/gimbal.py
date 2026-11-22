"""
Servo-side interpolation for slow inference.

Pose inference on the QNX Pi runs at ~2 fps. Instead of jumping the servos once
per detection, each detection sets a target angle (aim()) and SmoothGimbal's
thread eases the commanded angles toward it at a capped speed, ~50 times a
second, so the gimbal moves continuously between detections.

Pure Python + threading; works with every IoControl backend via move_to().
"""
import threading
import time


def aim(point, frame_shape, cap_pan, cap_tilt, hfov, gain=0.7, deadband=1.5):
    """
    Turn a target pixel into absolute (pan, tilt) target angles.

    :param point: (x, y) target in the un-mirrored frame used for tracking
    :param frame_shape: (h, w) of that frame
    :param cap_pan, cap_tilt: commanded angles when the frame was captured
    :param hfov: camera horizontal field of view in degrees
    :param gain: fraction of the error to correct per detection (<1 avoids overshoot)
    :param deadband: ignore errors smaller than this many degrees, per axis
    Sign convention matches the old PID path: target right of centre -> pan up
    (rotate_right), target above centre -> tilt up (rotate_up).
    """
    h, w = frame_shape[:2]
    deg_per_px = hfov / w  # square pixels: same scale vertically
    err_pan = (point[0] - w / 2) * deg_per_px
    err_tilt = (h / 2 - point[1]) * deg_per_px
    if abs(err_pan) < deadband:
        err_pan = 0.0
    if abs(err_tilt) < deadband:
        err_tilt = 0.0
    return cap_pan + gain * err_pan, cap_tilt + gain * err_tilt


def _approach(cur, target, max_step):
    if target > cur:
        return min(target, cur + max_step)
    return max(target, cur - max_step)


class SmoothGimbal:
    def __init__(self, io, max_speed=90.0, servo_hz=50.0):
        """
        :param io: an IoControl (qnx / pigpio / mock) with move_to() and angle limits
        :param max_speed: cap on servo speed in degrees per second
        :param servo_hz: how often the servos are updated
        """
        self.io = io
        self.max_speed = float(max_speed)
        self.servo_hz = float(servo_hz)
        self._lock = threading.Lock()
        self._paused = False
        self._running = False
        self._thread = None
        self._sync_to_io()

    # ---- state ----
    def _sync_to_io(self):
        with self._lock:
            self.cur_pan = self.target_pan = float(self.io.get_base_angle())
            self.cur_tilt = self.target_tilt = float(self.io.get_camera_angle())

    def set_target(self, pan, tilt):
        io = self.io
        with self._lock:
            self.target_pan = max(io.PAN_MIN, min(io.PAN_MAX, pan))
            self.target_tilt = max(io.PITCH_MIN, min(io.PITCH_MAX, tilt))

    def angles(self):
        """Commanded (pan, tilt) right now - where the camera is pointing."""
        with self._lock:
            return self.cur_pan, self.cur_tilt

    def targets(self):
        with self._lock:
            return self.target_pan, self.target_tilt

    # ---- motion ----
    def step(self, dt):
        """Advance one tick: move each axis toward its target by at most max_speed*dt."""
        with self._lock:
            if self._paused:
                return False
            max_step = self.max_speed * dt
            pan = _approach(self.cur_pan, self.target_pan, max_step)
            tilt = _approach(self.cur_tilt, self.target_tilt, max_step)
            moved = pan != self.cur_pan or tilt != self.cur_tilt
            self.cur_pan, self.cur_tilt = pan, tilt
            if moved:
                # inside the lock so pause() can't slip in between deciding and writing
                self.io.move_to(pan, tilt)
            return moved

    def _run(self):
        period = 1.0 / self.servo_hz
        last = time.perf_counter()
        while self._running:
            now = time.perf_counter()
            # cap dt so a stall (GC, scheduling) can't produce one huge jump
            self.step(min(now - last, 4 * period))
            last = now
            time.sleep(max(0.0, period - (time.perf_counter() - now)))

    # ---- lifecycle ----
    def start(self):
        if self._thread is None:
            self._running = True
            self._thread = threading.Thread(target=self._run, name="gimbal", daemon=True)
            self._thread.start()
        return self

    def pause(self):
        """Stop sending servo commands (e.g. tracking toggled off)."""
        with self._lock:
            self._paused = True

    def resume(self):
        """Resync to the IoControl's angles (e.g. after center()) and start moving again."""
        self._sync_to_io()
        with self._lock:
            self._paused = False

    def stop(self):
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
