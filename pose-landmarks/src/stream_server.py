"""
MJPEG stream of the annotated tracker frames, served over plain HTTP so another
device on the network can watch it in a browser / VLC / <img> tag.

Stdlib only (http.server) so nothing extra has to be installed on the Pi.

Routes:
    /              small viewer page (video + live pose JSON)
    /stream        multipart/x-mixed-replace MJPEG stream
    /snapshot.jpg  latest frame as a single JPEG
    /pose          latest pose / body / servo data as JSON
    /health        {"status": "ok"}
"""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2

BOUNDARY = "frame"


class FrameBroadcaster:
    """Holds the latest encoded frame; the tracking loop publishes, HTTP threads read."""

    def __init__(self, quality=70):
        self.quality = int(quality)
        self._cond = threading.Condition()
        self._jpeg = None
        self._frame_id = 0
        self._data = {}
        self._clients = 0

    @property
    def has_clients(self):
        return self._clients > 0

    def client_connected(self):
        with self._cond:
            self._clients += 1

    def client_disconnected(self):
        with self._cond:
            self._clients = max(0, self._clients - 1)

    def publish(self, frame_bgr):
        """Encode and store a frame. Never blocks on the network."""
        ok, jpg = cv2.imencode(".jpg", frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), self.quality])
        if not ok:
            return
        with self._cond:
            self._jpeg = jpg.tobytes()
            self._frame_id += 1
            self._cond.notify_all()

    def publish_data(self, data):
        with self._cond:
            self._data = data

    def latest_frame(self):
        with self._cond:
            return self._frame_id, self._jpeg

    def latest_data(self):
        with self._cond:
            return self._data

    def wait_frame(self, last_id, timeout=1.0):
        """Block until a frame newer than last_id exists. Returns (id, jpeg) or (last_id, None) on timeout."""
        with self._cond:
            self._cond.wait_for(lambda: self._frame_id != last_id and self._jpeg is not None, timeout)
            if self._frame_id == last_id or self._jpeg is None:
                return last_id, None
            return self._frame_id, self._jpeg


INDEX_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pose tracker</title>
<style>
 body{margin:0;background:#111;color:#ddd;font:14px system-ui,sans-serif}
 img{display:block;max-width:100%;margin:0 auto;background:#000}
 pre{margin:0;padding:12px;white-space:pre-wrap;max-height:40vh;overflow:auto}
</style></head>
<body>
<img src="/stream" alt="live stream">
<pre id="pose">waiting for data...</pre>
<script>
async function poll(){
  try{const r=await fetch('/pose');document.getElementById('pose').textContent=JSON.stringify(await r.json(),null,1);}
  catch(e){}
  setTimeout(poll,500);
}
poll();
</script>
</body></html>
"""


def _make_handler(broadcaster):
    class StreamHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # keep the tracker console quiet
            pass

        def _send(self, code, body, content_type):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            try:
                if path in ("/", "/index.html"):
                    self._send(200, INDEX_HTML.encode(), "text/html; charset=utf-8")
                elif path == "/stream":
                    self._stream()
                elif path == "/snapshot.jpg":
                    self._snapshot()
                elif path == "/pose":
                    self._send(200, json.dumps(broadcaster.latest_data()).encode(), "application/json")
                elif path == "/health":
                    self._send(200, b'{"status": "ok"}', "application/json")
                else:
                    self._send(404, b"not found", "text/plain")
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def _snapshot(self):
            broadcaster.client_connected()
            try:
                _, jpeg = broadcaster.latest_frame()
                if jpeg is None:
                    # nothing encoded yet (no client was connected) - wait for the next one
                    _, jpeg = broadcaster.wait_frame(0, timeout=3.0)
                if jpeg is None:
                    self._send(503, b"no frame yet", "text/plain")
                else:
                    self._send(200, jpeg, "image/jpeg")
            finally:
                broadcaster.client_disconnected()

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Pragma", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            broadcaster.client_connected()
            last_id = 0
            try:
                while not getattr(self.server, "stopping", False):
                    last_id, jpeg = broadcaster.wait_frame(last_id, timeout=1.0)
                    if jpeg is None:
                        continue
                    self.wfile.write(
                        f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                        f"Content-Length: {len(jpeg)}\r\n\r\n".encode())
                    self.wfile.write(jpeg)
                    self.wfile.write(b"\r\n")
            finally:
                broadcaster.client_disconnected()

    return StreamHandler


def _lan_ip():
    """Best-effort LAN address (no packets are actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def start_stream_server(broadcaster, host="0.0.0.0", port=8080):
    """Start the HTTP server on a daemon thread. Call stop_stream_server() on exit."""
    server = ThreadingHTTPServer((host, port), _make_handler(broadcaster))
    server.daemon_threads = True
    server.stopping = False
    threading.Thread(target=server.serve_forever, name="stream-server", daemon=True).start()
    shown = _lan_ip() if host in ("0.0.0.0", "") else host
    print(f"[INFO] Streaming at http://{shown}:{port}/  (raw MJPEG: /stream, data: /pose)")
    return server


def stop_stream_server(server):
    server.stopping = True
    server.shutdown()
    server.server_close()
