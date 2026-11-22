"""Tests for the MJPEG stream parser and reader in camera.py (no network needed)."""
import io
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from camera import MjpegCamera, iter_mjpeg


def jpeg(w, h, value):
    img = np.full((h, w, 3), value, np.uint8)
    return cv2.imencode(".jpg", img)[1].tobytes()


def part(data, with_length=True):
    head = b"--frame\r\nContent-Type: image/jpeg\r\n"
    if with_length:
        head += b"Content-Length: %d\r\n" % len(data)
    return head + b"\r\n" + data + b"\r\n"


class Trickle(io.RawIOBase):
    """Byte stream that returns at most n bytes per read, to split headers/markers across reads."""

    def __init__(self, data, n):
        self.data, self.n, self.pos = data, n, 0

    def readable(self):
        return True

    def read1(self, size=-1):
        out = self.data[self.pos:self.pos + min(self.n, size if size > 0 else self.n)]
        self.pos += len(out)
        return out

    read = read1


def decode_all(stream):
    return [cv2.imdecode(np.frombuffer(j, np.uint8), cv2.IMREAD_COLOR) for j in iter_mjpeg(stream)]


def test_parses_with_and_without_content_length():
    a, b, c = jpeg(64, 48, 10), jpeg(32, 24, 200), jpeg(16, 16, 90)
    data = part(a) + part(b, with_length=False) + part(c)
    for chunk in (7, 100, 1 << 20):  # tiny reads split every header and marker
        frames = decode_all(Trickle(data, chunk))
        assert [f.shape for f in frames] == [(48, 64, 3), (24, 32, 3), (16, 16, 3)], chunk


def test_headers_arriving_before_jpeg_start_are_kept():
    a = jpeg(40, 30, 50)
    # first read delivers only the part headers, next read the image
    data = part(a)
    split = data.index(b"\xff\xd8")
    frames = decode_all(Trickle(data, split))
    assert len(frames) == 1 and frames[0].shape == (30, 40, 3)


def test_truncated_stream_stops_cleanly():
    a = jpeg(64, 48, 10)
    data = part(a) + part(jpeg(64, 48, 99))[:-200]  # second frame cut off
    assert len(decode_all(Trickle(data, 512))) == 1


def test_mjpeg_camera_reads_from_http_server():
    frames = [jpeg(64, 48, v) for v in (20, 120, 220)]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            for f in frames:
                self.wfile.write(part(f))
                self.wfile.flush()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cam = MjpegCamera(f"http://127.0.0.1:{srv.server_address[1]}/stream", retry_s=0.2)
    try:
        ok, frame = cam.read(timeout=5)
        assert ok and frame.shape == (48, 64, 3)
        assert cam.isOpened()
    finally:
        cam.release()
        srv.shutdown()
    assert not cam.isOpened()
