import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import paho.mqtt.client as mqtt
import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel

load_dotenv(Path(__file__).resolve().parent / ".env")

TIGER_DATA_URL = os.getenv("TIGER_DATA_URL")
BROKER_IP = os.getenv("MQTT_BROKER_IP", "100.73.37.89")
BROKER_PORT = int(os.getenv("MQTT_BROKER_PORT", "1883"))
CONTROL_TOPIC = "climbbud/control/session"

if not TIGER_DATA_URL:
    raise RuntimeError("TIGER_DATA_URL missing in telemetry/.env")

app = FastAPI(title="ClimbBud Telemetry & Coaching API", version="1.0.0")

# Allow React frontend (localhost:5173, localhost:3000, etc.) to call the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SessionControlResponse(BaseModel):
    command: Literal["START", "STOP"]
    topic: str
    status: str


def publish_session_command(command: Literal["START", "STOP"]) -> None:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    try:
        client.connect(BROKER_IP, BROKER_PORT, keepalive=10)
        client.loop_start()
        info = client.publish(CONTROL_TOPIC, command, qos=1)
        info.wait_for_publish(timeout=5.0)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Failed to reach MQTT broker at {BROKER_IP}:{BROKER_PORT} ({exc})",
        ) from exc
    finally:
        client.loop_stop()
        client.disconnect()


@app.get("/api/health")
def health_check() -> dict:
    return {"status": "ok", "mqtt_broker": f"{BROKER_IP}:{BROKER_PORT}"}


@app.post("/api/sessions/start", response_model=SessionControlResponse)
def start_session() -> SessionControlResponse:
    publish_session_command("START")
    return SessionControlResponse(
        command="START",
        topic=CONTROL_TOPIC,
        status="Start command published to MQTT broker",
    )


@app.post("/api/sessions/stop", response_model=SessionControlResponse)
def stop_session() -> SessionControlResponse:
    publish_session_command("STOP")
    return SessionControlResponse(
        command="STOP",
        topic=CONTROL_TOPIC,
        status="Stop command published to MQTT broker",
    )


@app.get("/api/live")
def get_live_telemetry(limit: int = 60) -> dict:
    """Returns the active (or latest) session, its newest telemetry reading, and recent chart points."""
    with psycopg.connect(TIGER_DATA_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, status, started_at, ended_at
                FROM sessions
                ORDER BY (status = 'ACTIVE') DESC, id DESC
                LIMIT 1;
                """
            )
            session = cur.fetchone()

            if not session:
                return {"session": None, "latest": None, "history": []}

            cur.execute(
                """
                SELECT time, bpm, accel_g, speed, fall_detected
                FROM telemetry_points
                WHERE session_id = %s
                ORDER BY time DESC
                LIMIT %s;
                """,
                (session["id"], limit),
            )
            rows = cur.fetchall()

    # Reverse so timestamps run oldest -> newest for frontend line charts
    history = list(reversed(rows))
    latest = rows[0] if rows else None

    return {
        "session": session,
        "latest": latest,
        "history": history,
    }


@app.get("/api/sessions")
def list_sessions(limit: int = 20) -> list[dict]:
    """Returns past and active sessions with aggregated stats and Gemini's coaching summary."""
    with psycopg.connect(TIGER_DATA_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    s.id,
                    s.status,
                    s.started_at,
                    s.ended_at,
                    EXTRACT(EPOCH FROM (COALESCE(s.ended_at, NOW()) - s.started_at))::int AS duration_sec,
                    COUNT(tp.time) AS total_samples,
                    ROUND(AVG(NULLIF(tp.bpm, 0))::numeric, 1)::float AS avg_bpm,
                    MAX(tp.bpm) AS max_bpm,
                    ROUND(MAX(tp.accel_g)::numeric, 2)::float AS peak_accel_g,
                    ROUND(AVG(tp.speed)::numeric, 2)::float AS avg_speed,
                    ROUND(MAX(tp.speed)::numeric, 2)::float AS max_speed,
                    COUNT(*) FILTER (WHERE tp.fall_detected IS TRUE) AS fall_count,
                    re.risk_level AS coaching_risk_level,
                    re.summary AS coaching_summary
                FROM sessions s
                LEFT JOIN telemetry_points tp ON tp.session_id = s.id
                LEFT JOIN LATERAL (
                    SELECT risk_level, summary
                    FROM risk_events
                    WHERE session_id = s.id AND signal_type = 'COACHING_SUMMARY'
                    ORDER BY created_at DESC
                    LIMIT 1
                ) re ON TRUE
                GROUP BY s.id, s.status, s.started_at, s.ended_at, re.risk_level, re.summary
                ORDER BY s.id DESC
                LIMIT %s;
                """,
                (limit,),
            )
            return cur.fetchall()


@app.get("/api/sessions/{session_id}")
def get_session_detail(session_id: int) -> dict:
    """Returns full telemetry series and all Gemini risk/coaching events for a single session."""
    with psycopg.connect(TIGER_DATA_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    status,
                    started_at,
                    ended_at,
                    EXTRACT(EPOCH FROM (COALESCE(ended_at, NOW()) - started_at))::int AS duration_sec
                FROM sessions
                WHERE id = %s;
                """,
                (session_id,),
            )
            session = cur.fetchone()
            if not session:
                raise HTTPException(status_code=404, detail=f"Session #{session_id} not found")

            cur.execute(
                """
                SELECT time, bpm, accel_g, speed, fall_detected
                FROM telemetry_points
                WHERE session_id = %s
                ORDER BY time ASC;
                """,
                (session_id,),
            )
            points = cur.fetchall()

            cur.execute(
                """
                SELECT id, created_at, risk_level, signal_type, summary
                FROM risk_events
                WHERE session_id = %s
                ORDER BY created_at DESC;
                """,
                (session_id,),
            )
            events = cur.fetchall()

    return {
        "session": session,
        "telemetry": points,
        "events": events,
    }