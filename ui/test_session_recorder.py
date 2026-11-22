"""Tests for ui/session_recorder.py: AVI writer and the session recorder (local HTTP fakes)."""
import json
import struct
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from ui.session_recorder import MjpegAviWriter, SessionRecorder, iter_mjpeg, jpeg_size


def fake_jpeg(width, height, payload=b"x" * 31):
    """Minimal JPEG-shaped bytes: SOI, APP0, SOF0 with the size, payload, EOI."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof0 + payload + b"\xff\xd9"


def read_chunks(data, start, end):
    """Yield (fourcc, payload_start, size) for RIFF chunks between start and end."""
    i = start
    while i + 8 <= end:
        fourcc, size = data[i:i + 4], struct.unpack("<I", data[i + 4:i + 8])[0]
        yield fourcc, i + 8, size
        i += 8 + size + (size & 1)


class JpegTests(unittest.TestCase):
    def test_jpeg_size_reads_sof(self):
        self.assertEqual(jpeg_size(fake_jpeg(640, 480)), (640, 480))
        self.assertIsNone(jpeg_size(b"\xff\xd8not a jpeg"))

    def test_iter_mjpeg_with_and_without_content_length(self):
        a, b = fake_jpeg(4, 4), fake_jpeg(8, 8, payload=b"y" * 10)
        body = (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(a) + a
                + b"\r\n--frame\r\n\r\n" + b + b"\r\n")

        class Stream:
            def __init__(self, data):
                self.data = data

            def read(self, n):
                chunk, self.data = self.data[:7], self.data[7:]  # tiny reads stress the parser
                return chunk

        self.assertEqual(list(iter_mjpeg(Stream(body))), [a, b])


class AviWriterTests(unittest.TestCase):
    def test_writes_valid_mjpeg_avi(self):
        frames = [fake_jpeg(320, 240), fake_jpeg(320, 240, payload=b"odd"), fake_jpeg(320, 240)]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "v.avi"
            w = MjpegAviWriter(path)
            for f in frames:
                self.assertTrue(w.write_frame(f))
            w.close(fps=12.5)
            data = path.read_bytes()

        self.assertEqual(data[:4], b"RIFF")
        self.assertEqual(struct.unpack("<I", data[4:8])[0], len(data) - 8)
        self.assertEqual(data[8:12], b"AVI ")
        top = {fourcc if fourcc != b"LIST" else data[p:p + 4]: (p, s)
               for fourcc, p, s in read_chunks(data, 12, len(data))}
        self.assertEqual(set(top), {b"hdrl", b"movi", b"idx1"})

        hp, hs = top[b"hdrl"]
        hdrl = {fourcc: (p, s) for fourcc, p, s in read_chunks(data, hp + 4, hp + hs)}
        avih = struct.unpack("<IIIIIIIIII", data[hdrl[b"avih"][0]:hdrl[b"avih"][0] + 40])
        self.assertEqual(avih[0], 80000)          # 1e6 / 12.5 us per frame
        self.assertEqual(avih[4], 3)              # total frames
        self.assertEqual(avih[8:10], (320, 240))  # width, height

        sp = hdrl[b"LIST"][0]
        strl = {fourcc: p for fourcc, p, _ in read_chunks(data, sp + 4, sp + hdrl[b"LIST"][1])}
        strh = struct.unpack("<4s4sIHHIIIIII", data[strl[b"strh"]:strl[b"strh"] + 40])
        self.assertEqual(strh[:2], (b"vids", b"MJPG"))
        # fields: type, handler, flags, priority, language, initial, scale, rate, start, length, ...
        self.assertEqual(strh[7] / strh[6], 12.5)  # rate / scale
        self.assertEqual(strh[9], 3)               # length in frames

        mp, ms = top[b"movi"]
        movi = list(read_chunks(data, mp + 4, mp + ms))
        self.assertEqual([data[p:p + s] for _, p, s in movi], frames)

        ip, isz = top[b"idx1"]
        entries = [struct.unpack("<4sIII", data[ip + i:ip + i + 16]) for i in range(0, isz, 16)]
        self.assertEqual(len(entries), 3)
        for (ckid, flags, offset, size), frame in zip(entries, frames):
            self.assertEqual((ckid, flags, size), (b"00dc", 0x10, len(frame)))
            # offsets are relative to the 'movi' fourcc
            self.assertEqual(data[mp + offset + 8:mp + offset + 8 + size], frame)

    def test_skips_non_jpeg_until_first_frame(self):
        with TemporaryDirectory() as tmp:
            w = MjpegAviWriter(Path(tmp) / "v.avi")
            self.assertFalse(w.write_frame(b"garbage"))
            self.assertEqual(w.frames, 0)
            w.close(10)  # no file opened: no-op
            self.assertFalse((Path(tmp) / "v.avi").exists())


class FakeServers:
    """Local camera (MJPEG) + pose (/pose JSON) servers."""

    def __init__(self, camera=True):
        self.pose_ts = 0.0
        self.toggles = []  # states received on /toggle, like the QNX tracker's endpoint
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                path, _, query = self.path.partition("?")
                if path != "/toggle":
                    self.send_response(404)
                    self.end_headers()
                    return
                state = query.partition("state=")[2].split("&")[0]
                outer.toggles.append(state)
                body = json.dumps({"enabled": state != "off"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/pose":
                    outer.pose_ts += 1
                    body = json.dumps({"timestamp": outer.pose_ts, "detected": True,
                                       "frame_shape": [240, 320], "keypoints": {}, "pan": 135}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path == "/stream" and camera:
                    self.send_response(200)
                    self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                    self.end_headers()
                    jpg = fake_jpeg(320, 240)
                    try:
                        while not outer.stopping:
                            self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n"
                                             % len(jpg) + jpg + b"\r\n")
                            self.wfile.flush()
                            time.sleep(0.02)
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        pass
                else:
                    self.send_response(404)
                    self.end_headers()

        self.stopping = False
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.stopping = True
        self.server.shutdown()
        self.server.server_close()


class SessionRecorderTests(unittest.TestCase):
    def run_session(self, camera=True, seconds=1.0):
        servers = FakeServers(camera=camera)
        self.addCleanup(servers.close)
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rec = SessionRecorder(f"{servers.base}/stream", f"{servers.base}/pose", tmp.name,
                              poll_s=0.05, reconnect_s=0.2, log=lambda *_: None)
        out = rec.start(label="7")
        self.assertTrue(rec.status()["active"])
        rec.annotate("db_session_id", 7)
        time.sleep(seconds)
        t0 = time.time()
        rec.stop()
        self.assertLess(time.time() - t0, 2.0)
        self.assertFalse(rec.status()["active"])
        meta = json.loads((out / "meta.json").read_text())
        lines = [json.loads(l) for l in (out / "timeline.jsonl").read_text().splitlines()]
        return out, meta, lines

    def test_records_video_and_pose_timeline(self):
        out, meta, lines = self.run_session()
        self.assertTrue(out.name.startswith("session_") and out.name.endswith("_7"))
        self.assertGreater(meta["frames"], 5)
        self.assertGreater(meta["fps"], 5)
        self.assertGreater(meta["poses"], 3)
        self.assertEqual(meta["video_files"], ["video.avi"])
        self.assertEqual(meta["db_session_id"], 7)
        self.assertIsNone(meta["camera_error"])
        kinds = {l["type"] for l in lines}
        self.assertEqual(kinds, {"frame", "pose"})
        self.assertEqual(sum(l["type"] == "frame" for l in lines), meta["frames"])
        self.assertGreater((out / "video.avi").stat().st_size, 100)

    def test_camera_down_still_logs_pose(self):
        out, meta, lines = self.run_session(camera=False, seconds=0.6)
        self.assertEqual(meta["frames"], 0)
        self.assertGreater(meta["poses"], 2)
        self.assertTrue(meta["camera_error"])
        self.assertFalse((out / "video.avi").exists())

    def test_avi_rolls_over_past_size_limit(self):
        servers = FakeServers()
        self.addCleanup(servers.close)
        with TemporaryDirectory() as tmp:
            rec = SessionRecorder(f"{servers.base}/stream", f"{servers.base}/pose", tmp,
                                  max_avi_bytes=2000, log=lambda *_: None)
            out = rec.start()
            time.sleep(0.8)
            rec.stop()
            meta = json.loads((out / "meta.json").read_text())
            self.assertGreater(len(meta["video_files"]), 1)
            for name in meta["video_files"]:
                data = (out / name).read_bytes()
                self.assertEqual(struct.unpack("<I", data[4:8])[0], len(data) - 8)


if __name__ == "__main__":
    unittest.main()
