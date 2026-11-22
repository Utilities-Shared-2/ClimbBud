import os
import time
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from google import genai
from google.genai import types
import psycopg
from pydantic import BaseModel, Field

load_dotenv(Path(__file__).resolve().parent / ".env")

TIGER_DATA_URL = os.getenv("TIGER_DATA_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
POLL_INTERVAL_SEC = float(os.getenv("GEMINI_POLL_SEC", "3.0"))


class SessionCoachingReport(BaseModel):
    risk_level: Literal["LOW", "MEDIUM", "HIGH"] = Field(
        description="Overall session exertion/safety rating based on heart rate, peak G-force, and fall count."
    )
    signal_type: Literal["COACHING_SUMMARY"] = Field(
        default="COACHING_SUMMARY",
        description="Event category stored in risk_events.",
    )
    summary: str = Field(
        description=(
            "2-3 sentence post-climb coaching debrief comparing this session's heart rate, "
            "climbing velocity/speed, and peak G-force against previous sessions."
        )
    )


def get_gemini_client() -> genai.Client:
    if not GEMINI_API_KEY or "your-gemini-api-key" in GEMINI_API_KEY:
        raise RuntimeError("Set a valid GEMINI_API_KEY in telemetry/.env first.")
    return genai.Client(api_key=GEMINI_API_KEY)


def generate_field_report(
    client: genai.Client, session_id: int, metrics: dict
) -> SessionCoachingReport:
    prompt = f"""
You are ClimbBud's AI climbing coach. Write a concise, data-backed field report for Session #{session_id}.

Computed session metrics:
{metrics}

Use the average and peak heart rate, duration, count and values of acceleration spikes,
hold-shift count, and terrain stability to describe the climber's effort and movement.
Set risk_level to HIGH if peak_accel_g > 2.2, MEDIUM if peak_bpm > 175, otherwise LOW.
Keep the report to 2-3 encouraging sentences and do not invent missing measurements.
"""
    response = client.models.generate_content(
        model="gemini-3.8-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=SessionCoachingReport,
            temperature=0.3,
        ),
    )
    return SessionCoachingReport.model_validate_json(response.text)


def fetch_session_metrics(conn: psycopg.Connection, session_id: int) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                s.id,
                EXTRACT(EPOCH FROM (COALESCE(s.ended_at, NOW()) - s.started_at))::int AS duration_sec,
                COUNT(tp.time) AS total_samples,
                ROUND(AVG(NULLIF(tp.bpm, 0))::numeric, 1) AS avg_bpm,
                MAX(tp.bpm) AS max_bpm,
                ROUND(MAX(tp.accel_g)::numeric, 2) AS peak_accel_g,
                ROUND(AVG(tp.speed)::numeric, 2) AS avg_speed,
                ROUND(MAX(tp.speed)::numeric, 2) AS max_speed,
                COUNT(*) FILTER (WHERE tp.fall_detected IS TRUE) AS fall_count
            FROM sessions s
            LEFT JOIN telemetry_points tp ON tp.session_id = s.id
            WHERE s.id = %s
            GROUP BY s.id, s.started_at, s.ended_at;
            """,
            (session_id,),
        )
        row = cur.fetchone()

    if not row or row[2] == 0:
        return None

    return {
        "session_id": row[0],
        "duration_sec": row[1],
        "total_samples": row[2],
        "avg_bpm": float(row[3]) if row[3] is not None else None,
        "max_bpm": row[4],
        "peak_accel_g": float(row[5]) if row[5] is not None else None,
        "avg_speed": float(row[6]) if row[6] is not None else None,
        "max_speed": float(row[7]) if row[7] is not None else None,
        "fall_count": row[8],
    }


def fetch_previous_sessions(
    conn: psycopg.Connection, current_session_id: int, limit: int = 4
) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id
            FROM sessions
            WHERE status = 'COMPLETED' AND id < %s
            ORDER BY id DESC
            LIMIT %s;
            """,
            (current_session_id, limit),
        )
        prev_ids = [r[0] for r in cur.fetchall()]

    history = []
    for sid in prev_ids:
        metrics = fetch_session_metrics(conn, sid)
        if metrics:
            history.append(metrics)
    return history


def has_existing_summary(conn: psycopg.Connection, session_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM risk_events
            WHERE session_id = %s AND signal_type = 'COACHING_SUMMARY'
            LIMIT 1;
            """,
            (session_id,),
        )
        return cur.fetchone() is not None


def generate_and_save_summary(
    conn: psycopg.Connection, client: genai.Client, session_id: int
) -> None:
    current_metrics = fetch_session_metrics(conn, session_id)
    if not current_metrics:
        print(f"[GEMINI] Session #{session_id} has no telemetry rows yet; skipping.")
        return

    previous_metrics = fetch_previous_sessions(conn, session_id, limit=4)

    prompt = f"""
You are ClimbBud's AI climbing coach. Write a concise, data-backed post-climb summary for Session #{session_id}.

Current Session Metrics:
{current_metrics}

Previous Completed Sessions (most recent first):
{previous_metrics if previous_metrics else "None (this is the climber's first recorded session)."}

Instructions:
- Compare current average/max heart rate, climbing velocity/speed (avg_speed, max_speed), and peak G-force against previous sessions (if available) to highlight pacing, endurance, or speed progression.
- If speed metrics are None (camera/vision stream wasn't active during this climb), focus on heart rate, duration, and G-force stability without mentioning database nulls.
- Set risk_level to HIGH if fall_count > 0 or peak_accel_g > 2.2, MEDIUM if max_bpm > 175, otherwise LOW.
- Keep the summary to 2-3 punchy, encouraging sentences suitable for a climber's dashboard.
"""

    response = client.models.generate_content(
        model="gemini-3.8-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=SessionCoachingReport,
            temperature=0.3,
        ),
    )
    report = SessionCoachingReport.model_validate_json(response.text)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO risk_events (session_id, risk_level, signal_type, summary)
            VALUES (%s, %s, %s, %s);
            """,
            (session_id, report.risk_level, report.signal_type, report.summary),
        )

    print(f"[GEMINI SAVED] Session #{session_id} ({report.risk_level}): {report.summary}")


def main() -> None:
    if not TIGER_DATA_URL:
        raise RuntimeError("TIGER_DATA_URL missing in telemetry/.env")

    client = get_gemini_client()
    print("[GEMINI] Post-session coaching worker started. Watching for completed climbs...")

    with psycopg.connect(TIGER_DATA_URL, autocommit=True) as conn:
        while True:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id
                        FROM sessions
                        WHERE status = 'COMPLETED'
                        ORDER BY id DESC
                        LIMIT 5;
                        """
                    )
                    completed_ids = [r[0] for r in cur.fetchall()]

                for sid in reversed(completed_ids):
                    if not has_existing_summary(conn, sid):
                        generate_and_save_summary(conn, client, sid)

            except Exception as exc:
                print(f"[GEMINI ERROR] {exc}")

            time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped Gemini coaching worker.")