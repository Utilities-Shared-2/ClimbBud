"""
Camera sources for the tracker.

- opencv: a local webcam via cv2.VideoCapture (desktop / Raspberry Pi OS).
- mjpeg:  frames pulled over HTTP from camera_streamer.py running on another
          device (e.g. a Raspberry Pi OS board with the USB webcam). This is how
          the QNX Pi gets its frames: QNX has no usable local camera path, and this
          needs nothing beyond the Python stdlib + cv2.

Every source's read() returns a BGR uint8 (H, W, 3) frame, which is what
PoseDetector and the drawing code expect.
"""
import sys
import threading
import time
import urllib.request

import cv2
import numpy as np

IS_QNX = sys.platform.startswith("qnx")

JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"


class Camera:
    """Thin wrapper giving every source the same read()/isOpened()/release() API."""

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
        frame = to_bgr(np.asarray(frame))
        if not self._logged:
            print(f"[INFO] camera ({self.backend}) frame: shape={frame.shape}")
            self._logged = True
        return True, frame


def to_bgr(frame):
    """Normalise grey / BGRA frames to 3-channel BGR uint8."""
    if frame.dtype != np.uint8:
        frame = cv2.convertScaleAbs(frame)
    if frame.ndim == 2 or (frame.ndim == 3 and frame.shape[2] == 1):
        return cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    if frame.ndim == 3 and frame.shape[2] == 4:
        return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
    return frame


# ---------------------------------------------------------------------------
# MJPEG over HTTP
# ---------------------------------------------------------------------------

def iter_mjpeg(stream, chunk_size=65536):
    """
    Yield raw JPEG bytes from a multipart/x-mixed-replace MJPEG byte stream.

    Uses the part's Content-Length when present (exact, works even if the JPEG
    data contains marker-like bytes); otherwise scans for the JPEG start/end
    markers, which also copes with servers that send no part headers at all.
    """
    # read1 returns whatever has arrived; read(n) would block until n bytes (= latency)
    read = getattr(stream, "read1", stream.read)
    buf = b""
    while True:
        # find the start of the next JPEG, reading part headers on the way
        soi = buf.find(JPEG_SOI)
        if soi < 0:
            chunk = read(chunk_size)
            if not chunk:
                return
            # keep recent bytes: they hold the part headers (Content-Length) and
            # possibly the first half of a split marker; cap so junk can't grow forever
            buf = buf[-4096:] + chunk
            continue

        length = _content_length(buf[:soi])
        buf = buf[soi:]
        if length is not None:
            while len(buf) < length:
                chunk = read(chunk_size)
                if not chunk:
                    return
                buf += chunk
            yield buf[:length]
            buf = buf[length:]
        else:
            while True:
                eoi = buf.find(JPEG_EOI, 2)
                if eoi >= 0:
                    yield buf[:eoi + 2]
                    buf = buf[eoi + 2:]
                    break
                chunk = read(chunk_size)
                if not chunk:
                    return
                buf += chunk


def _content_length(header_bytes):
    """Content-Length from the part headers just before a JPEG, or None."""
    for line in header_bytes.split(b"\r\n")[::-1]:
        name, sep, value = line.partition(b":")
        if sep and name.strip().lower() == b"content-length":
            try:
                return int(value.strip())
            except ValueError:
                return None
    return None


class MjpegCamera:
    """
    cv2.VideoCapture-like reader for an MJPEG HTTP stream.

    A background thread keeps only the newest decoded frame, so a slow
    consumer never builds up latency. Reconnects automatically if the
    stream drops (e.g. the camera device reboots).
    """

    def __init__(self, url, retry_s=2.0, connect_timeout=5.0):
        self.url = url
        self.retry_s = retry_s
        self.connect_timeout = connect_timeout
        self._cond = threading.Condition()
        self._frame = None
        self._frame_id = 0
        self._last_read_id = 0
        self._running = True
        self._response = None
        self._thread = threading.Thread(target=self._run, name="mjpeg-reader", daemon=True)
        self._thread.start()

    def _run(self):
        connected = None  # None = never tried, so the first state is always logged
        while self._running:
            try:
                self._response = urllib.request.urlopen(self.url, timeout=self.connect_timeout)
                if connected is not True:
                    print(f"[INFO] camera stream connected: {self.url}")
                connected = True
                for jpg in iter_mjpeg(self._response):
                    if not self._running:
                        break
                    frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
                    if frame is None:
                        continue
                    with self._cond:
                        self._frame = frame
                        self._frame_id += 1
                        self._cond.notify_all()
                if self._running:
                    raise ConnectionError("stream ended")
            except Exception as e:
                if not self._running:
                    break
                if connected is not False:
                    print(f"[WARN] camera stream unavailable ({e}); retrying every {self.retry_s:.0f}s")
                connected = False
                time.sleep(self.retry_s)
            finally:
                self._close_response()

    def _close_response(self):
        resp, self._response = self._response, None
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass

    def isOpened(self):
        return self._running

    def read(self, timeout=1.0):
        with self._cond:
            self._cond.wait_for(lambda: self._frame_id != self._last_read_id, timeout)
            if self._frame_id == self._last_read_id:
                return False, None
            self._last_read_id = self._frame_id
            return True, self._frame

    def release(self):
        self._running = False
        self._close_response()


def open_camera(index=0, backend="auto", url=None):
    """
    :param backend: "opencv", "mjpeg" or "auto" (mjpeg when a url is given, else opencv)
    :param url: MJPEG stream URL, e.g. http://camera-pi.local:8081/stream
    """
    if backend not in ("auto", "opencv", "mjpeg"):
        raise ValueError(f"unknown camera backend {backend!r}")
    if backend == "mjpeg" or (backend == "auto" and url):
        if not url:
            raise ValueError("the mjpeg camera backend needs --camera-url")
        return Camera(MjpegCamera(url), "mjpeg")
    if IS_QNX:
        raise RuntimeError("QNX has no local camera support here; stream from the camera Pi with "
                           "--camera-url http://<camera-pi>:8081/stream (see README)")
    return Camera(cv2.VideoCapture(index), "opencv")
