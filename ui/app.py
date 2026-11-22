"""ClimbBud Flask dashboard.

Run:  pip install flask
      python app.py   ->  http://localhost:5000

Live heart-rate and movement readings come from the telemetry database.
"""
import sys
import time
from pathlib import Path

from flask import Flask, jsonify, render_template

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

app = Flask(__name__)

state = {
    "recording": True,
    "start": time.time(),
    "elapsed": 0.0,
    "pr": 12.4,          # personal-record time, seconds
    "battery": 87,
}

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
    now = time.time()
    if state["recording"]:
        state["elapsed"] += now - state["start"]
    state["recording"] = not state["recording"]
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
