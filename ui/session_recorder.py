"""
Record a climbing session's camera video + pose data while a session is active.

- Video: the camera Pi's raw MJPEG stream, saved without re-encoding into an
  MJPEG .avi (plays in VLC / ffmpeg). Frame rate is measured, not assumed.
- Pose: every new /pose update from the pose server (keypoints, target, servo
  angles), appended to timeline.jsonl together with one line per video frame,
  all stamped with this machine's clock so playback can sync skeleton to video.

Output: <out_root>/session_<YYYYmmdd-HHMMSS>[_<label>]/
          video.avi (video_002.avi, ... past ~1 GB), timeline.jsonl, meta.json

Stdlib + requests only.
"""
import json
import struct
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"
MAX_AVI_BYTES = 1_000_000_000  # stay well under the 2/4 GB RIFF limits


# ---------------------------------------------------------------------------
# MJPEG parsing (copied from pose-landmarks/src/camera.py iter_mjpeg; that
# package isn't importable because of the hyphen in its directory name)
# ---------------------------------------------------------------------------

def iter_mjpeg(stream, chunk_size=65536):
    """Yield raw JPEG bytes from a multipart/x-mixed-replace MJPEG byte stream."""
    read = getattr(stream, "read1", stream.read)
    buf = b""
    while True:
        soi = buf.find(JPEG_SOI)
        if soi < 0:
            chunk = read(chunk_size)
            if not chunk:
                return
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
    for line in header_bytes.split(b"\r\n")[::-1]:
        name, sep, value = line.partition(b":")
        if sep and name.strip().lower() == b"content-length":
            try:
                return int(value.strip())
            except ValueError:
                return None
    return None


def jpeg_size(jpg):
    """(width, height) from a JPEG's SOF marker, or None."""
    i = 2
    n = len(jpg)
    while i + 9 < n:
        if jpg[i] != 0xFF:
            i += 1
            continue
        marker = jpg[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:  # no length field
            i += 2
            continue
        seg_len = struct.unpack(">H", jpg[i + 2:i + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", jpg[i + 5:i + 9])
            return width, height
        i += 2 + seg_len
    return None


# ---------------------------------------------------------------------------
# Minimal MJPEG AVI writer
# ---------------------------------------------------------------------------

class MjpegAviWriter:
    """
    Writes JPEG frames into an AVI (RIFF) container with an MJPG video stream.
    The header is written with the first frame (its size sets the dimensions)
    and patched with the frame count and measured frame rate in close().
    """

    def __init__(self, path):
        self.path = Path(path)
        self._f = None
        self._index = []  # (offset from 'movi' fourcc, size)
        self.frames = 0
        self.width = self.height = 0
        self._max_frame = 0

    @property
    def bytes_written(self):
        return self._f.tell() if self._f else 0

    def write_frame(self, jpg):
        if self._f is None:
            size = jpeg_size(jpg)
            if size is None:
                return False  # not a usable JPEG; skip until one is
            self.width, self.height = size
            self._open()
        pos = self._f.tell()
        self._f.write(b"00dc" + struct.pack("<I", len(jpg)) + jpg)
        if len(jpg) % 2:
            self._f.write(b"\x00")
        self._index.append((pos - self._movi_fourcc, len(jpg)))
        self._max_frame = max(self._max_frame, len(jpg))
        self.frames += 1
        return True

    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = self._f = open(self.path, "wb")
        f.write(b"RIFF" + b"\0\0\0\0" + b"AVI ")
        f.write(b"LIST")
        hdrl_size_pos = f.tell()
        f.write(b"\0\0\0\0" + b"hdrl")
        hdrl_start = f.tell() - 4

        f.write(b"avih" + struct.pack("<I", 56))
        self._avih = f.tell()
        f.write(self._avih_data(fps=15.0))

        f.write(b"LIST")
        strl_size_pos = f.tell()
        f.write(b"\0\0\0\0" + b"strl")
        strl_start = f.tell() - 4
        f.write(b"strh" + struct.pack("<I", 56))
        self._strh = f.tell()
        f.write(self._strh_data(fps=15.0))
        f.write(b"strf" + struct.pack("<I", 40))
        f.write(struct.pack("<IiiHH4sIiiII", 40, self.width, self.height, 1, 24, b"MJPG",
                            self.width * self.height * 3, 0, 0, 0, 0))
        end = f.tell()
        self._patch(hdrl_size_pos, end - hdrl_start)
        self._patch(strl_size_pos, end - strl_start)
        f.seek(end)

        f.write(b"LIST")
        self._movi_size_pos = f.tell()
        f.write(b"\0\0\0\0")
        self._movi_fourcc = f.tell()
        f.write(b"movi")

    def _avih_data(self, fps):
        return struct.pack("<IIIIIIIIII16x",
                           int(round(1_000_000 / fps)),        # dwMicroSecPerFrame
                           int(self._max_frame * fps),         # dwMaxBytesPerSec
                           0, 0x10,                            # padding, AVIF_HASINDEX
                           self.frames, 0, 1,                  # total frames, initial, streams
                           self._max_frame, self.width, self.height)

    def _strh_data(self, fps):
        return struct.pack("<4s4sIHHIIIIIIIIhhhh",
                           b"vids", b"MJPG", 0, 0, 0, 0,
                           1000, int(round(fps * 1000)),       # dwScale, dwRate -> fps
                           0, self.frames, self._max_frame,    # start, length, buffer size
                           0xFFFFFFFF, 0,                      # quality (default), sample size
                           0, 0, self.width, self.height)

    def _patch(self, pos, value):
        self._f.seek(pos)
        self._f.write(struct.pack("<I", value))

    def close(self, fps):
        """Finish the file. fps = measured average frame rate of what was written."""
        if self._f is None:
            return
        f = self._f
        fps = fps if fps and fps > 0 else 15.0
        f.seek(0, 2)
        movi_end = f.tell()
        f.write(b"idx1" + struct.pack("<I", 16 * len(self._index)))
        for offset, size in self._index:
            f.write(b"00dc" + struct.pack("<III", 0x10, offset, size))  # AVIIF_KEYFRAME
        end = f.tell()
        self._patch(4, end - 8)
        self._patch(self._movi_size_pos, movi_end - self._movi_fourcc)
        f.seek(self._avih)
        f.write(self._avih_data(fps))
        f.seek(self._strh)
        f.write(self._strh_data(fps))
        f.close()
        self._f = None


# ---------------------------------------------------------------------------
# Session recorder
# ---------------------------------------------------------------------------

POSE_FIELDS = ("detected", "frame_shape", "keypoints", "target", "target_source", "pan", "tilt")


class SessionRecorder:
    def __init__(self, camera_url, pose_url, out_root, http=requests,
                 poll_s=0.1, reconnect_s=2.0, max_avi_bytes=MAX_AVI_BYTES, log=print):
        self.camera_url = camera_url
        self.pose_url = pose_url
        self.out_root = Path(out_root)
        self.http = http
        self.poll_s = poll_s
        self.reconnect_s = reconnect_s
        self.max_avi_bytes = max_avi_bytes
        self.log = log
        self._lock = threading.Lock()  # guards start/stop
        self._tl_lock = threading.Lock()  # guards timeline writes + counters
        self._stop = threading.Event()
        self._threads = []
        self._resp = None
        self.dir = None
        self._reset_counters()

    def _reset_counters(self):
        self.frames = 0
        self.poses = 0
        self._first_frame_t = self._last_frame_t = None
        self._files = []
        self._camera_error = None
        self._extra = {}

    @property
    def active(self):
        return bool(self._threads)

    def status(self):
        with self._tl_lock:
            return {"active": self.active, "dir": self.dir.name if self.dir else None,
                    "frames": self.frames, "poses": self.poses}

    def annotate(self, key, value):
        """Add a field to the current recording's meta.json (e.g. the DB session id)."""
        with self._tl_lock:
            if self.active:
                self._extra[key] = value

    def start(self, label=None):
        with self._lock:
            if self.active:
                self._stop_unlocked()
            self._reset_counters()
            self._started = time.time()
            stamp = datetime.fromtimestamp(self._started).strftime("%Y%m%d-%H%M%S")
            name = f"session_{stamp}" + (f"_{label}" if label not in (None, "") else "")
            self.dir = self.out_root / name
            self.dir.mkdir(parents=True, exist_ok=True)
            self._label = label
            self._timeline = open(self.dir / "timeline.jsonl", "a", encoding="utf-8")
            self._stop.clear()
            self._threads = [threading.Thread(target=self._pose_loop, name="rec-pose", daemon=True)]
            if self.camera_url:
                self._threads.append(threading.Thread(target=self._video_loop, name="rec-video", daemon=True))
            else:
                self._camera_error = "no camera URL configured"
            for t in self._threads:
                t.start()
            self.log(f"[REC] Recording session to {self.dir}")
            return self.dir

    def stop(self):
        with self._lock:
            self._stop_unlocked()

    def _stop_unlocked(self):
        if not self.active:
            return
        self._stop.set()
        resp = self._resp
        if resp is not None:
            try:
                resp.close()  # unblock a read waiting on the camera
            except Exception:
                pass
        for t in self._threads:
            t.join(timeout=5)
        self._threads = []
        with self._tl_lock:
            self._timeline.close()
            meta = {
                "label": self._label,
                "started_at": self._started,
                "ended_at": time.time(),
                "frames": self.frames,
                "fps": round(self._fps(), 3),
                "poses": self.poses,
                "video_files": self._files,
                "camera_url": self.camera_url,
                "pose_url": self.pose_url,
                "camera_error": self._camera_error if self.frames == 0 else None,
                **self._extra,
            }
        (self.dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        self.log(f"[REC] Stopped: {self.frames} frames ({meta['fps']} fps), {self.poses} poses -> {self.dir}")

    def _fps(self):
        if self.frames < 2 or self._last_frame_t == self._first_frame_t:
            return 0.0
        return (self.frames - 1) / (self._last_frame_t - self._first_frame_t)

    def _write_line(self, record):
        self._timeline.write(json.dumps(record) + "\n")

    # ---- video ----
    def _video_loop(self):
        writer = None
        warned = False
        try:
            while not self._stop.is_set():
                resp = None
                try:
                    resp = self.http.get(self.camera_url, stream=True, timeout=(3, 5))
                    self._resp = resp
                    resp.raise_for_status()
                    if warned:
                        self.log("[REC] Camera stream reconnected")
                        warned = False
                    for jpg in iter_mjpeg(resp.raw):
                        if self._stop.is_set():
                            break
                        if writer is not None and writer.bytes_written > self.max_avi_bytes:
                            writer.close(self._fps())
                            writer = None
                        if writer is None:
                            name = "video.avi" if not self._files else f"video_{len(self._files) + 1:03d}.avi"
                            writer = MjpegAviWriter(self.dir / name)
                            self._files.append(name)
                        if not writer.write_frame(jpg):
                            continue
                        t = time.time()
                        with self._tl_lock:
                            if self._first_frame_t is None:
                                self._first_frame_t = t
                            self._last_frame_t = t
                            self._write_line({"type": "frame", "i": self.frames, "t": t,
                                              "file": self._files[-1]})
                            self.frames += 1
                except Exception as exc:
                    if self._stop.is_set():
                        break
                    self._camera_error = str(exc)
                    if not warned:
                        self.log(f"[REC] Camera stream unavailable ({exc}); retrying every {self.reconnect_s:g}s")
                        warned = True
                finally:
                    self._resp = None
                    if resp is not None:
                        try:
                            resp.close()
                        except Exception:
                            pass
                self._stop.wait(self.reconnect_s)
        finally:
            if writer is not None:
                writer.close(self._fps())

    # ---- pose ----
    def _pose_loop(self):
        last_ts = None
        warned = False
        while not self._stop.is_set():
            try:
                resp = self.http.get(self.pose_url, timeout=1)
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, dict) and data.get("timestamp") not in (None, last_ts):
                    last_ts = data["timestamp"]
                    record = {"type": "pose", "t": time.time(), "src_ts": last_ts}
                    record.update({k: data.get(k) for k in POSE_FIELDS})
                    with self._tl_lock:
                        self._write_line(record)
                        self.poses += 1
                warned = False
            except Exception as exc:
                if not warned:
                    self.log(f"[REC] Pose data unavailable ({exc})")
                    warned = True
            self._stop.wait(self.poll_s)
