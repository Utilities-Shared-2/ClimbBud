"""ClimbBud Flask dashboard.

Run from the repository root: python ui/app.py
The dashboard listens on 0.0.0.0:5000; open http://localhost:5000.

Fall detection settings can be supplied as environment variables or in
telemetry/.env before starting the dashboard (environment takes precedence):
  POSE_URL                       Full pose service /pose URL; empty disables it.
  FALL_ACCEL_G                   Impact threshold in g (default 2.2).
  FALL_INACTIVITY_SEC            Watch stillness duration in seconds (10).
  FALL_DROP_FRAC                 Downward drop as fraction of frame height (0.25).
  POSE_STILL_SEC                 Camera stillness duration in seconds (5).
  FALL_COOLDOWN_SEC              Cooldown after dismissal in seconds (5).
  FALL_INACTIVITY_REQUIRES_EVENT Require a preceding moderate event (default 1).
  FALL_SIMULATE_ENABLED          Enable the demo endpoint (default 1; 0 disables).
  FLASK_DEBUG                    Enable Flask debug mode (default off).
  POSE_STREAM_HOST               Video stream server host (default localhost).
  POSE_STREAM_PORT               Video stream server port (default 8080).
  POSE_STREAM_SCHEME             Video stream scheme (default http).
  CAMERA_STREAM_URL              Raw camera MJPEG URL, e.g.
                                 http://camera-pi.local:8081/stream. When set and
                                 reachable, the Live tab plays this feed at the
                                 camera's frame rate and draws the pose skeleton
                                 client-side, interpolated between the (slow) pose
                                 updates. Empty = show the annotated pose stream.
                                 Also the source the session video is recorded from
                                 (unset: the annotated pose stream is recorded).
  RECORDINGS_DIR                 Where session video + pose logs are saved
                                 (default <repo>/recordings).
  MQTT_BROKER_IP / _PORT         Broker carrying climbbud/control/session. START/STOP
                                 there (voice Pi, dashboard button) starts/stops the
                                 health-stats session and the video recording.
  RECORDING_API_TOKEN            Optional shared secret for POST /api/recording/start
                                 and /stop (used by the voice Pi). When set, callers
                                 must send it in the X-ClimbBud-Token header.
  UI_TELEMETRY_WORKER            Run the telemetry DB worker inside the dashboard
                                 (default 1). Set 0 when telemetry/db.py runs as its
                                 own process on the same broker, or every START
                                 creates two DB sessions.

On the Live tab, enable sound, click Simulate Fall, then Dismiss. Browsers
require a user gesture to enable audio. Simulation exercises the alert UI but
does not test sensor detection. The simulate and dismiss endpoints have no
authentication; disable simulation outside the demo and do not expose the
debugger through a public tunnel.

Alerts are evaluated when /api/live is requested and are stored only in process
memory. A telemetry error returns HTTP 503 with both error and fall fields.
Detection is experimental and must not be relied on as a safety system.
"""
import atexit
import hmac
import json
import math
import os
import random
import sys
import threading
import time
from collections import deque
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, Response, stream_with_context

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from telemetry.fall_detection import FallConfig, FallMonitor, PoseSample, pose_sample_from_payload
from telemetry.db import CONTROL_TOPIC, parse_session_command  # stdlib-only at import time
from ui.session_recorder import SessionRecorder

# Load environment variables from telemetry/.env
load_dotenv(REPO_ROOT / "telemetry" / ".env")

app = Flask(__name__)

class PosePoller:
    """Collect recent, distinct pose frames without blocking dashboard requests."""

    def __init__(self, url: str, history_sec: float, stale_sec: float = 5.0):
        self.url = url
        self.history_sec = history_sec
        self.stale_sec = stale_sec
        self._samples: deque[PoseSample] = deque(maxlen=1024)
        self._lock = threading.Lock()
        self._last_source_timestamp: float | None = None
        self._last_new_at: float | None = None
        self._unavailable = False

    def poll_once(self):
        try:
            with urlopen(self.url, timeout=1.0) as response:
                data = response.read(65537)
            if len(data) > 65536:
                raise ValueError("pose response is too large")
            payload = json.loads(data)
            if not isinstance(payload, dict):
                raise ValueError("pose response must be a JSON object")
            source_timestamp = payload.get("timestamp")
            if (
                isinstance(source_timestamp, bool)
                or not isinstance(source_timestamp, (int, float))
                or not math.isfinite(source_timestamp)
            ):
                raise ValueError("pose response needs a finite timestamp")
            now = time.time()
            sample = pose_sample_from_payload(payload, now)
            if sample.y is not None and not math.isfinite(sample.y):
                raise ValueError("pose response has an invalid body position")
            with self._lock:
                if self._last_source_timestamp is not None:
                    if source_timestamp == self._last_source_timestamp:
                        if self._last_new_at is not None and now - self._last_new_at > self.stale_sec:
                            raise ValueError("pose feed stopped producing new frames")
                        return
                    if source_timestamp < self._last_source_timestamp:
                        self._samples.clear()
                self._last_source_timestamp = source_timestamp
                self._last_new_at = now
                self._samples.append(sample)
                self._prune(now)
            if self._unavailable:
                app.logger.info("Pose feed recovered")
                self._unavailable = False
        except (URLError, OSError, ValueError, UnicodeError) as exc:
            with self._lock:
                self._samples.clear()
            if not self._unavailable:
                app.logger.warning("Pose feed unavailable: %s", exc)
                self._unavailable = True

    def _prune(self, now):
        while self._samples and self._samples[0].ts < now - self.history_sec:
            self._samples.popleft()

    def samples(self):
        with self._lock:
            self._prune(time.time())
            return list(self._samples)

    def run(self):
        while True:
            self.poll_once()
            time.sleep(0.5)


pose_url = os.getenv("POSE_URL", "").strip()
fall_config = FallConfig.from_env()
fall_monitor = FallMonitor(fall_config)
pose_poller = (
    PosePoller(pose_url, fall_config.pose_history_sec, fall_config.stale_sec)
    if pose_url else None
)
if pose_poller is not None:
    threading.Thread(target=pose_poller.run, daemon=True, name="pose-poller").start()

# Pose stream server configuration
# Set these environment variables to configure where the video stream comes from:
# - POSE_STREAM_HOST: IP address or hostname of the pose stream server (default: localhost)
# - POSE_STREAM_PORT: Port of the pose stream server (default: 8080)
# - POSE_STREAM_SCHEME: http or https (default: http)
# Example for QNX on subnet: POSE_STREAM_HOST=192.168.50.2 POSE_STREAM_PORT=8080
POSE_STREAM_HOST = os.getenv("POSE_STREAM_HOST", "localhost")
POSE_STREAM_PORT = int(os.getenv("POSE_STREAM_PORT", "8080"))
POSE_STREAM_SCHEME = os.getenv("POSE_STREAM_SCHEME", "http")
POSE_STREAM_URL = f"{POSE_STREAM_SCHEME}://{POSE_STREAM_HOST}:{POSE_STREAM_PORT}"
CAMERA_STREAM_URL = os.getenv("CAMERA_STREAM_URL", "").strip()

RECORDINGS_DIR = Path(os.getenv("RECORDINGS_DIR", "").strip() or REPO_ROOT / "recordings")
MQTT_BROKER_IP = os.getenv("MQTT_BROKER_IP", "127.0.0.1")
MQTT_BROKER_PORT = int(os.getenv("MQTT_BROKER_PORT", "1883"))
UI_TELEMETRY_WORKER = os.getenv("UI_TELEMETRY_WORKER", "1").strip().lower() not in {"0", "false", "no", "off"}

# Record the raw camera feed when configured; otherwise the pose server's annotated
# stream (lower frame rate, skeleton drawn in) so there is always video.
RECORD_VIDEO_URL = CAMERA_STREAM_URL or f"{POSE_STREAM_URL}/stream"
session_recorder = SessionRecorder(RECORD_VIDEO_URL, f"{POSE_STREAM_URL}/pose", RECORDINGS_DIR)

# Only these /pose fields are forwarded to the browser overlay.
POSE_OVERLAY_FIELDS = ("timestamp", "enabled", "detected", "frame_shape", "keypoints")

state = {
    "recording": False,
    "start": time.time(),
    "elapsed": 0.0,
    "pr": 12.4,          # personal-record time, seconds
    "battery": 87,
    "stream_connected": False,
    "camera_connected": False,
}

# Initialize telemetry database on startup
telemetry_db = None
mqtt_client = None

def cleanup_telemetry():
    """Cleanup telemetry resources on shutdown."""
    global mqtt_client, telemetry_db
    try:
        session_recorder.stop()  # finalize the AVI header if a session is still recording
    except Exception as exc:
        print(f"[UI] Error stopping session recorder: {exc}")
    if control_client:
        try:
            control_client.loop_stop()
            control_client.disconnect()
        except Exception:
            pass
    if mqtt_client:
        try:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()
            print("[UI] MQTT client disconnected")
        except Exception as exc:
            print(f"[UI] Error disconnecting MQTT: {exc}")
    if telemetry_db:
        try:
            telemetry_db.flush()
            if telemetry_db.pg_conn:
                telemetry_db.pg_conn.close()
            print("[UI] Telemetry database flushed and closed")
        except Exception as exc:
            print(f"[UI] Error closing telemetry: {exc}")

def init_telemetry():
    """Initialize telemetry database and MQTT worker in background thread."""
    global telemetry_db, mqtt_client
    try:
        from telemetry.db import TelemetryDatabase, create_mqtt_worker, FLUSH_INTERVAL_SEC, SQLITE_DB_PATH

        telemetry_db = TelemetryDatabase(
            pg_url=os.getenv("TIGER_DATA_URL"),
            sqlite_path=SQLITE_DB_PATH
        )
        mqtt_client = create_mqtt_worker(telemetry_db)
        mqtt_client.loop_start()

        # Background flush thread
        def flush_loop():
            while True:
                time.sleep(FLUSH_INTERVAL_SEC)
                if telemetry_db:
                    telemetry_db.flush()

        flush_thread = threading.Thread(target=flush_loop, daemon=True)
        flush_thread.start()
        print("[UI] Telemetry database and MQTT worker initialized")
    except ImportError as exc:
        print(f"[UI] Telemetry dependencies unavailable: {exc}")
    except Exception as exc:
        print(f"[UI] Failed to initialize telemetry: {exc}")

# ---------------------------------------------------------------------------
# Session control: MQTT climbbud/control/session is the single source of truth.
# The voice Pi (and the dashboard button) publish START/STOP there; the
# telemetry worker starts the DB session and this subscriber starts the
# dashboard timer + video/pose recording, wherever telemetry happens to run.
# ---------------------------------------------------------------------------
control_client = None
_control_connected = threading.Event()
_session_lock = threading.Lock()


def set_recording(on, drive_db=False):
    """
    The one place a session starts/stops: dashboard timer + video/pose recorder,
    and (drive_db=True) the in-process telemetry DB session.

    Idempotent: asking for the current state does nothing, so the button, MQTT
    (voice Pi) and our own echoed MQTT messages can't restart a running session.
    Returns True if the state changed.
    """
    with _session_lock:
        if bool(on) == state["recording"]:
            return False
        now = time.time()
        if on:
            # A new session: the timer starts from zero (stop ends a session, it doesn't pause it)
            state["elapsed"] = 0.0
            state["recording"] = True
            state["start"] = now
            if drive_db and telemetry_db:
                telemetry_db.start_session()
            session_recorder.start()
            threading.Thread(target=_attach_db_session_id, daemon=True).start()
        else:
            # Freeze the timer at this session's length until the next start
            state["elapsed"] += now - state["start"]
            state["recording"] = False
            state["start"] = now
            session_recorder.stop()
            if drive_db and telemetry_db:
                telemetry_db.stop_session()
        return True


def on_session_command(command):
    """START/STOP received over MQTT (voice Pi, other dashboards). The telemetry
    worker handles the DB session for that same message, so only local state here."""
    if command in ("START", "STOP"):
        set_recording(command == "START")


def _attach_db_session_id(timeout=3.0):
    """If telemetry runs in this process, note its session id in the recording's meta.json."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        db = telemetry_db
        if db is not None and db.active_session_id is not None:
            session_recorder.annotate("db_session_id", db.active_session_id)
            return
        time.sleep(0.1)


def start_control_client():
    """Subscribe to the session control topic (independent of the telemetry worker)."""
    global control_client
    try:
        import paho.mqtt.client as mqtt
    except ImportError as exc:
        print(f"[UI] paho-mqtt unavailable, session control only via the button: {exc}")
        return None

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"climbbud-ui-control-{os.getpid()}")

    def on_connect(c, _userdata, _flags, reason_code, _properties=None):
        c.subscribe(CONTROL_TOPIC, qos=1)
        _control_connected.set()
        print(f"[UI] Session control subscribed on {MQTT_BROKER_IP}:{MQTT_BROKER_PORT} (rc={reason_code})")

    def on_disconnect(_c, _userdata, _flags, _reason_code, _properties=None):
        _control_connected.clear()

    def on_message(_c, _userdata, msg):
        command = parse_session_command(msg.payload.decode("utf-8", errors="replace"))
        if command:
            on_session_command(command)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=10)
    try:
        client.connect_async(MQTT_BROKER_IP, MQTT_BROKER_PORT, keepalive=30)
        client.loop_start()
    except Exception as exc:
        print(f"[UI] Session control MQTT setup failed: {exc}")
        return None
    control_client = client
    return client


def publish_session_command(command):
    """Publish START/STOP for everyone (telemetry + this dashboard). False if the broker is unreachable."""
    if control_client is None or not _control_connected.is_set():
        return False
    payload = json.dumps({"command": command, "source": "dashboard", "ts": time.time()})
    try:
        info = control_client.publish(CONTROL_TOPIC, payload, qos=1)
    except Exception as exc:
        app.logger.warning("Unable to publish session command: %s", exc)
        return False
    return info.rc == 0


# Register cleanup handler
atexit.register(cleanup_telemetry)

# Start telemetry in background (skip when telemetry/db.py runs as its own worker)
if UI_TELEMETRY_WORKER:
    init_thread = threading.Thread(target=init_telemetry, daemon=True)
    init_thread.start()
start_control_client()


def check_stream_connection():
    """Check if the pose stream server is available."""
    global state
    try:
        response = requests.get(f"{POSE_STREAM_URL}/health", timeout=2)
        state["stream_connected"] = response.status_code == 200
    except Exception:
        state["stream_connected"] = False
    state["camera_connected"] = check_camera_connection()


def camera_base_url(stream_url):
    """http://host:8081/stream -> http://host:8081 (camera_streamer serves /health there)."""
    parts = urlsplit(stream_url)
    return f"{parts.scheme}://{parts.netloc}"


def check_camera_connection():
    if not CAMERA_STREAM_URL:
        return False
    try:
        response = requests.get(f"{camera_base_url(CAMERA_STREAM_URL)}/health", timeout=2)
        return response.status_code == 200
    except Exception:
        return False


def video_source():
    """Which feed the Live tab should play: raw camera + overlay, annotated pose stream, or none."""
    if CAMERA_STREAM_URL and state["camera_connected"]:
        return "raw"
    if state["stream_connected"]:
        return "annotated"
    return None


# Periodically check stream connection
def stream_check_loop():
    while True:
        check_stream_connection()
        time.sleep(5)


stream_check_thread = threading.Thread(target=stream_check_loop, daemon=True)
stream_check_thread.start()
check_stream_connection()  # Initial check

# Normalised (0-1) hold positions of the detected route, bottom -> top.
ROUTE = [(0.38, 0.95), (0.52, 0.80), (0.70, 0.65), (0.62, 0.52),
         (0.78, 0.42), (0.70, 0.30), (0.80, 0.18), (0.76, 0.06)]

HISTORY = [
    {"date": "Nov 19", "route": "Overhang A3", "time": 14.8, "bpm": 151},
    {"date": "Nov 17", "route": "Slab B1",     "time": 12.4, "bpm": 138},
    {"date": "Nov 14", "route": "Roof C2",     "time": 21.3, "bpm": 163},
    {"date": "Nov 12", "route": "Slab B1",     "time": 13.1, "bpm": 142},
]


def read_elapsed():
    t = state["elapsed"]
    if state["recording"]:
        t += time.time() - state["start"]
    return t


@app.route("/")
def index():
    return render_template("index.html", route=ROUTE, history=HISTORY)


def evaluate_fall(history):
    pose_samples = pose_poller.samples() if pose_poller is not None else ()
    return fall_monitor.evaluate(history, state["recording"], pose_samples)


@app.route("/api/live")
def live():
    try:
        from telemetry.db import load_live_telemetry
    except ImportError as exc:
        app.logger.exception("Telemetry database dependencies are unavailable")
        return jsonify(
            error=f"Telemetry database dependencies are unavailable: {exc}",
            fall=evaluate_fall([]),
            stream_connected=state["stream_connected"],
            video_source=video_source(),
            recording=state["recording"],
            elapsed=read_elapsed(),
            video_recording=session_recorder.status(),
        ), 503

    try:
        data = load_live_telemetry()
    except Exception:
        app.logger.exception("Unable to load live telemetry from the database")
        return jsonify(
            error="Live telemetry is unavailable.",
            fall=evaluate_fall([]),
            stream_connected=state["stream_connected"],
            video_source=video_source(),
            recording=state["recording"],
            elapsed=read_elapsed(),
            video_recording=session_recorder.status(),
        ), 503

    latest = data["latest"]
    return jsonify(
        bpm=latest["bpm"] if latest else None,
        intensity=latest["speed"] if latest else None,
        elapsed=read_elapsed(),
        pr=state["pr"],
        battery=state["battery"],
        recording=state["recording"],
        session=data["session"],
        latest=latest,
        history=data["history"],
        fall=evaluate_fall(data["history"] if latest is not None else []),
        stream_connected=state["stream_connected"],
        video_source=video_source(),
        video_recording=session_recorder.status(),
    )


def _proxy_mjpeg(url, state_key):
    """Proxy an upstream MJPEG stream; on failure mark state[state_key] disconnected."""
    if not state[state_key]:
        return Response("Stream disconnected", status=503, mimetype="text/plain")

    req = None
    try:
        req = requests.get(url, stream=True, timeout=2)
        req.raise_for_status()
    except requests.RequestException as exc:
        if req is not None:
            req.close()
        app.logger.warning("Unable to connect to video stream: %s", exc)
        state[state_key] = False
        return Response("Stream disconnected", status=503, mimetype="text/plain")

    def generate():
        try:
            for chunk in req.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk
        except requests.RequestException as exc:
            app.logger.warning("Video stream interrupted: %s", exc)
            state[state_key] = False
        finally:
            req.close()

    return Response(
        stream_with_context(generate()),
        content_type=req.headers.get("Content-Type", "multipart/x-mixed-replace"),
    )


@app.route("/video/stream")
def video_stream():
    """Proxy the annotated MJPEG stream from the pose server if available."""
    return _proxy_mjpeg(f"{POSE_STREAM_URL}/stream", "stream_connected")


@app.route("/video/raw")
def video_raw():
    """Proxy the raw camera MJPEG (full camera frame rate) if CAMERA_STREAM_URL is set."""
    if not CAMERA_STREAM_URL:
        return Response("Camera stream not configured", status=503, mimetype="text/plain")
    return _proxy_mjpeg(CAMERA_STREAM_URL, "camera_connected")


@app.route("/api/pose")
def pose_overlay():
    """Latest keypoints from the pose server, trimmed to what the browser overlay draws."""
    try:
        response = requests.get(f"{POSE_STREAM_URL}/pose", timeout=1)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError):
        return jsonify(error="Pose data unavailable."), 503
    if not isinstance(payload, dict):
        return jsonify(error="Pose data unavailable."), 503
    return jsonify({key: payload.get(key) for key in POSE_OVERLAY_FIELDS})


@app.route("/api/fall/dismiss", methods=["POST"])
def dismiss_fall():
    return jsonify(fall=fall_monitor.dismiss())


@app.route("/api/fall/simulate", methods=["POST"])
def simulate_fall():
    if not fall_monitor.simulate():
        return jsonify(error="Fall simulation is disabled."), 404
    return jsonify(fall=fall_monitor.state())


def _apply_recording(on):
    """Start/stop recording right now: timer, video + pose recorder and health-stats session.
    Shared by the dashboard button and the voice Pi's HTTP calls."""
    # In-process telemetry: drive its DB session directly (publishing would make the
    # in-process worker start a second session).
    changed = set_recording(on, drive_db=True)
    if changed and telemetry_db is None:
        # Telemetry runs elsewhere (or isn't available): tell it over MQTT. Our own
        # subscription receives the echo, which is a no-op because set_recording is idempotent.
        publish_session_command("START" if on else "STOP")
    return jsonify(recording=state["recording"], changed=changed, elapsed=read_elapsed(),
                   video_recording=session_recorder.status())


def _recording_api_authorized():
    """If RECORDING_API_TOKEN is set, remote start/stop calls must send it in X-ClimbBud-Token."""
    token = os.getenv("RECORDING_API_TOKEN", "").strip()
    if not token:
        return True
    return hmac.compare_digest(request.headers.get("X-ClimbBud-Token", ""), token)


@app.route("/api/recording/toggle", methods=["POST"])
def toggle():
    """Dashboard button: flip recording."""
    return _apply_recording(not state["recording"])


@app.route("/api/recording/start", methods=["POST"])
def recording_start():
    """Remote start (voice Pi). Idempotent: starting while recording returns changed=false."""
    if not _recording_api_authorized():
        return jsonify(error="Invalid or missing X-ClimbBud-Token."), 401
    return _apply_recording(True)


@app.route("/api/recording/stop", methods=["POST"])
def recording_stop():
    """Remote stop (voice Pi). Idempotent."""
    if not _recording_api_authorized():
        return jsonify(error="Invalid or missing X-ClimbBud-Token."), 401
    return _apply_recording(False)


@app.route("/api/stats")
def stats():
    try:
        from telemetry.db import load_watch_sessions
    except ImportError as exc:
        app.logger.exception("Telemetry database dependencies are unavailable")
        return jsonify(error=f"Telemetry database dependencies are unavailable: {exc}"), 503

    data = load_watch_sessions()
    sessions = data["sessions"]
    public_sessions = [
        {key: value for key, value in session.items() if not key.startswith("_")}
        for session in sessions
    ]
    peak_values = [
        session["peak_bpm"]
        for session in sessions
        if session["peak_bpm"] is not None
    ]
    durations = [session["duration_sec"] for session in sessions]
    stability = [session["stability_pct"] for session in sessions]
    return jsonify(
        source=data["source"],
        routes_discovered=len(sessions),
        crux_peak=max(peak_values, default=0),
        longest_ascent=max(durations, default=0),
        terrain_stability=round(sum(stability) / len(stability), 1) if stability else 0,
        sessions=public_sessions,
    )


_FALLBACK_REPORTS = {
    "HIGH": (
        "Big effort out there! Your peak acceleration showed some sharp, dynamic moves, "
        "so focus on controlled foot placement. "
        "Your heart rate shows you pushed hard. "
        "Verdict: you can go again; stay controlled and keep your footwork precise."
    ),
    "MEDIUM": (
        "Strong session! Your heart rate climbed high during the hardest sections, "
        "so work on breathing and shaking out between cruxes. "
        "Verdict: you can climb again once you've had a short rest to catch your breath and recover."
    ),
    "LOW": (
        "Smooth, controlled climbing! Your heart rate stayed comfortable and your movement was stable. "
        "Verdict: don't start another climb right now; call it a good session and rest."
    ),
}


def _fallback_report(session: dict):
    import random
    import time
    from types import SimpleNamespace

    time.sleep(random.uniform(3, 5))
    if (session.get("peak_accel_g") or 0) > 2.2:
        level = "HIGH"
    elif (session.get("peak_bpm") or 0) > 175:
        level = "MEDIUM"
    else:
        level = "LOW"
    return SimpleNamespace(risk_level=level, summary=_FALLBACK_REPORTS[level])


@app.route("/api/sessions/<session_key>/summary", methods=["POST"])
def generate_session_summary(session_key: str):
    try:
        import psycopg
        from telemetry.db import (
            get_postgres_connection,
            load_watch_sessions,
            persist_local_sessions,
            persist_unassigned_postgres_session,
        )
    except ImportError as exc:
        app.logger.exception("Telemetry database dependencies are unavailable")
        return jsonify(error=f"Telemetry database dependencies are unavailable: {exc}"), 503

    data = load_watch_sessions()
    session = next(
        (item for item in data["sessions"] if item["id"] == session_key),
        None,
    )
    if session is None:
        return jsonify(error="Session not found."), 404
    if session["summary"]:
        return jsonify(session_id=session_key, summary=session["summary"])

    try:
        from google.genai.errors import APIError
        from telemetry.gemini_detector import generate_field_report, get_gemini_client

        gemini_client = get_gemini_client()
    except (ImportError, RuntimeError) as exc:
        app.logger.exception("Gemini field report dependencies or configuration are unavailable")
        return jsonify(error=f"Gemini field report dependencies or configuration are unavailable: {exc}"), 503

    try:
        if data["source"] != "postgres":
            database_id = persist_local_sessions(data["sessions"], session_key)
        elif session["session_id"] is None:
            database_id = persist_unassigned_postgres_session(session)
        else:
            database_id = int(session["session_id"])

        metrics = {
            "duration_sec": session["duration_sec"],
            "avg_bpm": session["avg_bpm"],
            "peak_bpm": session["peak_bpm"],
            "accel_spike_count": session["accel_spike_count"],
            "accel_spikes": session["accel_spikes"],
            "peak_accel_g": session["peak_accel_g"],
            "hold_shifts": session["hold_shifts"],
            "stability_pct": session["stability_pct"],
        }
        try:
            report = generate_field_report(gemini_client, database_id, metrics)
        except Exception:
            app.logger.exception("Gemini unavailable; using hardcoded fallback report")
            report = _fallback_report(session)

        with get_postgres_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO risk_events (
                        session_id, created_at, risk_level, signal_type, summary
                    )
                    VALUES (%s, NOW(), %s, 'COACHING_SUMMARY', %s);
                    """,
                    (database_id, report.risk_level, report.summary),
                )
        return jsonify(session_id=session_key, summary=report.summary)
    except (RuntimeError, ValueError, psycopg.Error, APIError) as exc:
        app.logger.exception("Unable to create the session field report")
        return jsonify(error=str(exc)), 503
    except ImportError as exc:
        app.logger.exception("Field report dependencies are unavailable")
        return jsonify(error=f"Field report dependencies are unavailable: {exc}"), 503


def run_dashboard():
    debug = os.getenv("FLASK_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}
    app.run(host="0.0.0.0", port=5000, debug=debug)


if __name__ == "__main__":
    run_dashboard()
