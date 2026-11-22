"""
Camera abstraction so the tracker runs on both QNX and desktop/Linux.

- QNX: cv2.VideoCapture has no camera backend there, so use qnx_apis.VideoCapture
  (https://github.com/qnx/python3-qnx-apis, a cv2-style wrapper around libcamapi).
  A USB webcam also needs the QNX camera service packages (qnx-sf-camapi, qnx-sf-camera-usb).
- Everywhere else: plain cv2.VideoCapture.

Both are wrapped so read() always returns a BGR uint8 (H, W, 3) frame, which is
what PoseDetector and the drawing code expect.
"""
import sys

import cv2
import numpy as np

IS_QNX = sys.platform.startswith("qnx")


class Camera:
    def __init__(self, cap, backend):
        self._cap = cap
        self.backend = backend
        self._logged = False

    def isOpened(self):
        return self._cap.isOpened()

    def release(self):
        self._cap.release()

    def read(self):
        ok, frame = self._cap.read()
        if not ok or frame is None or getattr(frame, "size", 0) == 0:
            return False, None
        frame = np.asarray(frame)
        if not self._logged:
            # The qnx_apis frame format isn't documented; log it once so it's easy to check
            print(f"[INFO] camera ({self.backend}) frame: shape={frame.shape} dtype={frame.dtype}")
            self._logged = True
        return True, to_bgr(frame)


def to_bgr(frame):
    """Normalise grey / BGRA frames to 3-channel BGR uint8."""
    if frame.dtype != np.uint8:
        frame = cv2.convertScaleAbs(frame)
    if frame.ndim == 2 or (frame.ndim == 3 and frame.shape[2] == 1):
        return cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    if frame.ndim == 3 and frame.shape[2] == 4:
        return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
    return frame


def open_camera(index=0, backend="auto"):
    """
    :param backend: "qnx", "opencv" or "auto" (qnx on QNX, opencv elsewhere)
    """
    if backend not in ("auto", "qnx", "opencv"):
        raise ValueError(f"unknown camera backend {backend!r}")
    if backend == "qnx" or (backend == "auto" and IS_QNX):
        try:
            from qnx_apis import VideoCapture
        except ImportError as e:
            raise ImportError(
                "qnx_apis not installed - build/install https://github.com/qnx/python3-qnx-apis "
                "(see README 'Running on QNX')") from e
        return Camera(VideoCapture(index), "qnx")
    return Camera(cv2.VideoCapture(index), "opencv")
