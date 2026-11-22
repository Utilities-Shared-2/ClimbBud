"""ClimbBud Flask dashboard.

Run:  pip install flask
      python app.py   ->  http://localhost:5000

Live heart-rate and movement readings come from the telemetry database.
"""
import atexit
import math
import os
import random
import sys
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Load environment variables from telemetry/.env
load_dotenv(REPO_ROOT / "telemetry" / ".env")

app = Flask(__name__)

state = {
    "recording": False,
    "start": time.time(),
    "elapsed": 0.0,
    "pr": 12.4,          # personal-record time, seconds
    "battery": 87,
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


@app.route("/api/live")
def live():
    try:
        from telemetry.db import load_live_telemetry
    except ImportError as exc:
        app.logger.exception("Telemetry database dependencies are unavailable")
        return jsonify(error=f"Telemetry database dependencies are unavailable: {exc}"), 503

    try:
        data = load_live_telemetry()
    except Exception:
        app.logger.exception("Unable to load live telemetry from the database")
        return jsonify(error="Live telemetry is unavailable."), 503

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
    )


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


if __name__ == "__main__":
    app.run(debug=True)
