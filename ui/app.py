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
from urllib.request import urlopen

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, Response, stream_with_context

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from telemetry.fall_detection import FallConfig, FallMonitor, PoseSample, pose_sample_from_payload

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

state = {
    "recording": False,
    "start": time.time(),
    "elapsed": 0.0,
    "pr": 12.4,          # personal-record time, seconds
    "battery": 87,
    "stream_connected": False,
}

# Initialize telemetry database on startup
telemetry_db = None
mqtt_client = None

def cleanup_telemetry():
    """Cleanup telemetry resources on shutdown."""
    global mqtt_client, telemetry_db
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

# Register cleanup handler
atexit.register(cleanup_telemetry)

# Start telemetry in background
init_thread = threading.Thread(target=init_telemetry, daemon=True)
init_thread.start()


def check_stream_connection():
    """Check if the pose stream server is available."""
    global state
    try:
        response = requests.get(f"{POSE_STREAM_URL}/health", timeout=2)
        state["stream_connected"] = response.status_code == 200
    except Exception:
        state["stream_connected"] = False


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
        ), 503

    try:
        data = load_live_telemetry()
    except Exception:
        app.logger.exception("Unable to load live telemetry from the database")
        return jsonify(
            error="Live telemetry is unavailable.",
            fall=evaluate_fall([]),
            stream_connected=state["stream_connected"],
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
    )


@app.route("/video/stream")
def video_stream():
    """Proxy the MJPEG stream from the pose server if available."""
    if not state["stream_connected"]:
        return Response("Stream disconnected", status=503, mimetype="text/plain")

    req = None
    try:
        req = requests.get(f"{POSE_STREAM_URL}/stream", stream=True, timeout=2)
        req.raise_for_status()
    except requests.RequestException as exc:
        if req is not None:
            req.close()
        app.logger.warning("Unable to connect to video stream: %s", exc)
        state["stream_connected"] = False
        return Response("Stream disconnected", status=503, mimetype="text/plain")

    def generate():
        try:
            for chunk in req.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk
        except requests.RequestException as exc:
            app.logger.warning("Video stream interrupted: %s", exc)
            state["stream_connected"] = False
        finally:
            req.close()

    return Response(
        stream_with_context(generate()),
        content_type=req.headers.get("Content-Type", "multipart/x-mixed-replace"),
    )


@app.route("/api/fall/dismiss", methods=["POST"])
def dismiss_fall():
    return jsonify(fall=fall_monitor.dismiss())


@app.route("/api/fall/simulate", methods=["POST"])
def simulate_fall():
    if not fall_monitor.simulate():
        return jsonify(error="Fall simulation is disabled."), 404
    return jsonify(fall=fall_monitor.state())


@app.route("/api/recording/toggle", methods=["POST"])
def toggle():
    global telemetry_db
    now = time.time()
    
    if state["recording"]:
        # Stop recording
        state["elapsed"] += now - state["start"]
        if telemetry_db:
            telemetry_db.stop_session()
        state["recording"] = False
    else:
        # Start recording
        if telemetry_db:
            telemetry_db.start_session()
        state["recording"] = True
    
    state["start"] = now
    return jsonify(recording=state["recording"], elapsed=read_elapsed())


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

        report = generate_field_report(
            gemini_client,
            database_id,
            {
                "duration_sec": session["duration_sec"],
                "avg_bpm": session["avg_bpm"],
                "peak_bpm": session["peak_bpm"],
                "accel_spike_count": session["accel_spike_count"],
                "accel_spikes": session["accel_spikes"],
                "peak_accel_g": session["peak_accel_g"],
                "hold_shifts": session["hold_shifts"],
                "stability_pct": session["stability_pct"],
            },
        )

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
