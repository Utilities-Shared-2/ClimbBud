from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from telemetry.session_metrics import build_sessions, timestamp_epoch

# Load environment variables from telemetry/.env
ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=ENV_PATH)

TIGER_DATA_URL = os.getenv("TIGER_DATA_URL")
MQTT_BROKER_IP = os.getenv("MQTT_BROKER_IP", "127.0.0.1")
MQTT_BROKER_PORT = int(os.getenv("MQTT_BROKER_PORT", "1883"))
SQLITE_DB_PATH = Path(__file__).resolve().parent / "climb_data.db"
WATCH_LOG_PATH = Path(__file__).resolve().parent / "watch_log.jsonl"

FLUSH_INTERVAL_SEC = 1.0
MAX_BATCH_SIZE = 20
LIVE_TELEMETRY_MAX_AGE_SEC = 5.0


class TelemetryDatabase:
    def __init__(self, pg_url: str | None, sqlite_path: Path) -> None:
        self.pg_url = pg_url
        self.sqlite_path = sqlite_path
        self.pg_conn: psycopg.Connection | None = None
        self.lock = threading.Lock()

        self.active_session_id: int | None = None
        self.buffer: list[tuple[Any, ...]] = []

        self._init_sqlite()
        self._connect_pg()
        if self.pg_conn:
            self._init_timescale_schema()

    def _connect_pg(self) -> bool:
        if not self.pg_url:
            print("[DB] TIGER_DATA_URL not set. Running in SQLite fallback mode.")
            return False
        try:
            import psycopg

            # autocommit=True is required for TimescaleDB continuous aggregate DDL
            self.pg_conn = psycopg.connect(
                self.pg_url,
                autocommit=True,
                connect_timeout=5,
            )
            print("[DB] Connected to Tiger Data Cloud (TimescaleDB).")
            return True
        except Exception as exc:
            print(f"[DB] Tiger Data connection failed: {exc}. Falling back to SQLite.")
            self.pg_conn = None
            return False

    def _init_sqlite(self) -> None:
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT,
                    ended_at TEXT,
                    status TEXT
                );

                CREATE TABLE IF NOT EXISTS telemetry_points (
                    time TEXT NOT NULL,
                    session_id INTEGER,
                    source TEXT,
                    bpm INTEGER,
                    accel_g REAL,
                    xyz TEXT,
                    speed REAL,
                    fall_detected INTEGER
                );

                CREATE TABLE IF NOT EXISTS risk_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER,
                    created_at TEXT,
                    risk_level TEXT,
                    signal_type TEXT,
                    summary TEXT
                );
                """
            )
        print(f"[DB] Local SQLite fallback ready at {self.sqlite_path}")

    def _init_timescale_schema(self) -> None:
        assert self.pg_conn is not None
        try:
            with self.pg_conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb;")

                # 1. Sessions table
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        id SERIAL PRIMARY KEY,
                        started_at TIMESTAMPTZ,
                        ended_at TIMESTAMPTZ,
                        status TEXT
                    );
                    """
                )

                # 2. Telemetry hypertable
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS telemetry_points (
                        time TIMESTAMPTZ NOT NULL,
                        session_id INT,
                        source TEXT,
                        bpm INT,
                        accel_g FLOAT,
                        xyz JSONB,
                        speed FLOAT,
                        fall_detected BOOLEAN
                    );
                    """
                )
                cur.execute(
                    """
                    SELECT create_hypertable(
                        'telemetry_points',
                        by_range('time'),
                        if_not_exists => TRUE
                    );
                    """
                )

                # 3. Real-time Continuous Aggregate (1-minute buckets)
                cur.execute(
                    """
                    CREATE MATERIALIZED VIEW IF NOT EXISTS session_telemetry_summary
                    WITH (timescaledb.continuous) AS
                    SELECT
                        session_id,
                        time_bucket('1 minute', time) AS bucket,
                        AVG(bpm) AS avg_bpm,
                        MAX(bpm) AS max_bpm,
                        MAX(accel_g) AS max_accel_g,
                        AVG(speed) AS avg_speed
                    FROM telemetry_points
                    WHERE session_id IS NOT NULL
                    GROUP BY session_id, bucket
                    WITH NO DATA;
                    """
                )
                cur.execute(
                    """
                    ALTER MATERIALIZED VIEW session_telemetry_summary
                    SET (timescaledb.materialized_only = false);
                    """
                )

                # Refresh policy so historical buckets materialize automatically
                cur.execute(
                    """
                    SELECT add_continuous_aggregate_policy(
                        'session_telemetry_summary',
                        start_offset => INTERVAL '1 hour',
                        end_offset => INTERVAL '1 minute',
                        schedule_interval => INTERVAL '1 minute',
                        if_not_exists => TRUE
                    );
                    """
                )

                # 4. Risk events table for Gemini detector & React UI
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS risk_events (
                        id SERIAL PRIMARY KEY,
                        session_id INT,
                        created_at TIMESTAMPTZ,
                        risk_level TEXT,
                        signal_type TEXT,
                        summary TEXT
                    );
                    """
                )

            print("[DB] Tiger Data schema, hypertable, and continuous aggregate verified.")
        except Exception as exc:
            print(f"[DB] Schema initialization error: {exc}")

    def start_session(self) -> int:
        now = datetime.now(timezone.utc)
        with self.lock:
            # Close any dangling active session cleanly
            if self.active_session_id is not None:
                self._stop_session_unlocked(now)

            session_id: int | None = None
            if self.pg_conn or self._connect_pg():
                try:
                    assert self.pg_conn is not None
                    with self.pg_conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO sessions (started_at, status)
                            VALUES (%s, 'ACTIVE')
                            RETURNING id;
                            """,
                            (now,),
                        )
                        row = cur.fetchone()
                        if row:
                            session_id = int(row[0])
                except Exception as exc:
                    print(f"[DB] Tiger Data start_session failed ({exc}), using SQLite.")
                    self.pg_conn = None

            if session_id is None:
                with sqlite3.connect(self.sqlite_path) as conn:
                    cur = conn.execute(
                        "INSERT INTO sessions (started_at, status) VALUES (?, 'ACTIVE');",
                        (now.isoformat(),),
                    )
                    session_id = int(cur.lastrowid)

            self.active_session_id = session_id
            print(f"[SESSION] Started session #{self.active_session_id} at {now.isoformat()}")
            return self.active_session_id

    def stop_session(self) -> None:
        now = datetime.now(timezone.utc)
        with self.lock:
            self._stop_session_unlocked(now)
        self.flush()

    def _stop_session_unlocked(self, ended_at: datetime) -> None:
        if self.active_session_id is None:
            print("[SESSION] STOP received, but no active session is running.")
            return

        sid = self.active_session_id
        self.active_session_id = None

        if self.pg_conn or self._connect_pg():
            try:
                assert self.pg_conn is not None
                with self.pg_conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE sessions
                        SET ended_at = %s, status = 'COMPLETED'
                        WHERE id = %s;
                        """,
                        (ended_at, sid),
                    )
            except Exception as exc:
                print(f"[DB] Tiger Data stop_session failed ({exc}), updating SQLite.")
                self.pg_conn = None

        with sqlite3.connect(self.sqlite_path) as conn:
            conn.execute(
                "UPDATE sessions SET ended_at = ?, status = 'COMPLETED' WHERE id = ?;",
                (ended_at.isoformat(), sid),
            )
        print(f"[SESSION] Stopped session #{sid} at {ended_at.isoformat()}")

    def enqueue_telemetry(self, payload: dict[str, Any], default_source: str) -> None:
        # Only log telemetry if there's an active session
        if self.active_session_id is None:
            return

        ts_raw = payload.get("ts")
        if isinstance(ts_raw, (int, float)):
            dt = datetime.fromtimestamp(float(ts_raw), tz=timezone.utc)
        else:
            dt = datetime.now(timezone.utc)

        source = payload.get("source", default_source)
        bpm = payload.get("bpm")
        accel_g = payload.get("accel_g")
        xyz = payload.get("xyz")
        speed = payload.get("speed")
        fall_detected = payload.get("fall_detected")

        should_flush = False
        with self.lock:
            row = (
                dt,
                self.active_session_id,
                source,
                int(bpm) if bpm is not None else None,
                float(accel_g) if accel_g is not None else None,
                json.dumps(xyz) if xyz is not None else None,
                float(speed) if speed is not None else None,
                bool(fall_detected) if fall_detected is not None else None,
            )
            self.buffer.append(row)
            if len(self.buffer) >= MAX_BATCH_SIZE:
                should_flush = True

        if should_flush:
            self.flush()

    def flush(self) -> None:
        with self.lock:
            if not self.buffer:
                return
            batch = self.buffer[:]
            self.buffer.clear()

        # Attempt primary write to Tiger Data Cloud
        if self.pg_conn or self._connect_pg():
            try:
                assert self.pg_conn is not None
                with self.pg_conn.cursor() as cur:
                    cur.executemany(
                        """
                        INSERT INTO telemetry_points (
                            time, session_id, source, bpm, accel_g, xyz, speed, fall_detected
                        )
                        VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s);
                        """,
                        batch,
                    )
                return
            except Exception as exc:
                print(f"[DB] Tiger Data batch insert failed ({exc}). Writing {len(batch)} rows to SQLite.")
                self.pg_conn = None

        # Fallback write to local SQLite
        sqlite_rows = [
            (
                r[0].isoformat(),
                r[1],
                r[2],
                r[3],
                r[4],
                r[5],
                r[6],
                int(r[7]) if r[7] is not None else None,
            )
            for r in batch
        ]
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.executemany(
                """
                INSERT INTO telemetry_points (
                    time, session_id, source, bpm, accel_g, xyz, speed, fall_detected
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """,
                sqlite_rows,
            )


def _read_postgres_watch_data() -> tuple[list[dict[str, Any]], dict[int, str]]:
    if not TIGER_DATA_URL:
        return [], {}

    import psycopg

    assert psycopg is not None
    with psycopg.connect(TIGER_DATA_URL, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT time, session_id, bpm, accel_g, xyz
                FROM telemetry_points
                WHERE source = 'watch'
                ORDER BY time ASC;
                """
            )
            points = [
                {
                    "ts": row[0],
                    "session_id": row[1],
                    "bpm": row[2],
                    "accel_g": row[3],
                    "xyz": row[4],
                }
                for row in cur.fetchall()
            ]
            cur.execute(
                """
                SELECT DISTINCT ON (session_id) session_id, summary
                FROM risk_events
                WHERE signal_type = 'COACHING_SUMMARY' AND session_id IS NOT NULL
                ORDER BY session_id, created_at DESC, id DESC;
                """
            )
            summaries = {int(row[0]): row[1] for row in cur.fetchall()}
    return points, summaries


def _read_sqlite_watch_data() -> tuple[list[dict[str, Any]], dict[int, str]]:
    with sqlite3.connect(f"{SQLITE_DB_PATH.resolve().as_uri()}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            """
            SELECT time, session_id, bpm, accel_g, xyz
            FROM telemetry_points
            WHERE source = 'watch'
            ORDER BY time ASC;
            """
        ).fetchall()
        summaries = conn.execute(
            """
            SELECT session_id, summary
            FROM risk_events
            WHERE signal_type = 'COACHING_SUMMARY' AND session_id IS NOT NULL
            ORDER BY created_at DESC, id DESC;
            """
        ).fetchall()

    latest_summaries: dict[int, str] = {}
    for session_id, summary in summaries:
        latest_summaries.setdefault(int(session_id), summary)
    return (
        [
            {
                "ts": row[0],
                "session_id": row[1],
                "bpm": row[2],
                "accel_g": row[3],
                "xyz": json.loads(row[4]) if isinstance(row[4], str) else row[4],
            }
            for row in rows
        ],
        latest_summaries,
    )


def _read_postgres_live_telemetry(limit: int) -> dict[str, Any]:
    import psycopg

    with psycopg.connect(TIGER_DATA_URL, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT time, session_id, bpm, accel_g, speed, fall_detected
                FROM telemetry_points
                WHERE source = 'watch'
                ORDER BY time DESC
                LIMIT %s;
                """,
                (limit,),
            )
            rows = cur.fetchall()

            session = None
            if rows and rows[0][1] is not None:
                cur.execute(
                    """
                    SELECT id, status, started_at, ended_at
                    FROM sessions
                    WHERE id = %s;
                    """,
                    (rows[0][1],),
                )
                session_row = cur.fetchone()
                if session_row:
                    session = {
                        "id": session_row[0],
                        "status": session_row[1],
                        "started_at": session_row[2].isoformat() if session_row[2] else None,
                        "ended_at": session_row[3].isoformat() if session_row[3] else None,
                    }

    history = [
        {
            "time": row[0].isoformat(),
            "bpm": row[2],
            "accel_g": row[3],
            "speed": row[4],
            "fall_detected": row[5],
        }
        for row in reversed(rows)
    ]
    latest = history[-1] if history else None
    if latest and not _is_fresh_live_sample(latest):
        latest = None
    return {
        "session": session,
        "latest": latest,
        "history": history,
    }


def _read_sqlite_live_telemetry(limit: int) -> dict[str, Any]:
    with closing(
        sqlite3.connect(f"{SQLITE_DB_PATH.resolve().as_uri()}?mode=ro", uri=True)
    ) as conn:
        rows = conn.execute(
            """
            SELECT time, session_id, bpm, accel_g, speed, fall_detected
            FROM telemetry_points
            WHERE source = 'watch'
            ORDER BY time DESC
            LIMIT ?;
            """,
            (limit,),
        ).fetchall()
        session_row = None
        if rows and rows[0][1] is not None:
            session_row = conn.execute(
                """
                SELECT id, status, started_at, ended_at
                FROM sessions
                WHERE id = ?;
                """,
                (rows[0][1],),
            ).fetchone()

    history = [
        {
            "time": row[0],
            "bpm": row[2],
            "accel_g": row[3],
            "speed": row[4],
            "fall_detected": bool(row[5]) if row[5] is not None else None,
        }
        for row in reversed(rows)
    ]
    latest = history[-1] if history else None
    if latest and not _is_fresh_live_sample(latest):
        latest = None
    session = (
        {
            "id": session_row[0],
            "status": session_row[1],
            "started_at": session_row[2],
            "ended_at": session_row[3],
        }
        if session_row
        else None
    )
    return {
        "session": session,
        "latest": latest,
        "history": history,
    }


def _is_fresh_live_sample(point: dict[str, Any]) -> bool:
    sample_time = timestamp_epoch(point.get("time"))
    if sample_time is None:
        return False
    age = time.time() - sample_time
    return 0 <= age <= LIVE_TELEMETRY_MAX_AGE_SEC


def load_live_telemetry(limit: int = 60) -> dict[str, Any]:
    """Load the selected session and its newest telemetry, preferring Postgres."""
    if limit < 1:
        raise ValueError("limit must be at least 1")

    if TIGER_DATA_URL:
        try:
            data = _read_postgres_live_telemetry(limit)
            if data["history"]:
                return data
        except Exception as exc:
            print(f"[DB] Unable to read live Tiger Data telemetry ({exc}); trying local SQLite.")

    return _read_sqlite_live_telemetry(limit)


def _read_watch_log() -> list[dict[str, Any]]:
    try:
        contents = WATCH_LOG_PATH.read_bytes()
    except FileNotFoundError:
        return []

    has_utf16_bom = contents.startswith((b"\xff\xfe", b"\xfe\xff"))
    encoding = "utf-16" if has_utf16_bom else "utf-8-sig"
    lines = contents.decode(encoding).splitlines()
    points = []
    for line_number, line in enumerate(lines, start=1):
        if not line.lstrip().startswith("{"):
            continue
        try:
            point = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"[DB] Ignoring malformed watch log line {line_number}: {exc}")
            continue
        if not isinstance(point, dict) or point.get("source", "watch") != "watch":
            continue
        if timestamp_epoch(point.get("ts")) is None:
            print(f"[DB] Ignoring watch log line {line_number} without a valid timestamp.")
            continue
        point["session_id"] = None
        points.append(point)
    return points


def load_watch_sessions() -> dict[str, Any]:
    """Load database watch telemetry, falling back to SQLite and then the JSONL log."""
    try:
        points, summaries = _read_postgres_watch_data()
    except Exception as exc:
        print(f"[DB] Unable to read Tiger Data telemetry ({exc}); trying local SQLite.")
        points, summaries = [], {}
    source = "postgres"

    if not points:
        try:
            points, summaries = _read_sqlite_watch_data()
            source = "sqlite"
        except sqlite3.Error as exc:
            print(f"[DB] Unable to read local SQLite telemetry ({exc}); trying watch log.")
            points, summaries = [], {}

    if not points:
        points = _read_watch_log()
        summaries = {}
        source = "jsonl"

    sessions = build_sessions(points)
    for session in sessions:
        session["summary"] = (
            summaries.get(int(session["session_id"]))
            if session["session_id"] is not None
            else None
        )
    return {"source": source, "sessions": sessions}


def get_postgres_connection() -> psycopg.Connection:
    if not TIGER_DATA_URL:
        raise RuntimeError("TIGER_DATA_URL is required to save Gemini field reports in Postgres.")
    import psycopg

    return psycopg.connect(TIGER_DATA_URL, connect_timeout=5)


def persist_local_sessions(
    sessions: list[dict[str, Any]], selected_id: str
) -> int:
    """Copy local fallback sessions to Postgres and return the selected database ID."""
    mapping: dict[str, int] = {}
    with get_postgres_connection() as conn:
        with conn.cursor() as cur:
            for session in sessions:
                started_at = datetime.fromtimestamp(
                    timestamp_epoch(session["started_at"]), timezone.utc
                )
                ended_at = datetime.fromtimestamp(
                    timestamp_epoch(session["ended_at"]), timezone.utc
                )
                cur.execute(
                    """
                    INSERT INTO sessions (started_at, ended_at, status)
                    VALUES (%s, %s, 'COMPLETED')
                    RETURNING id;
                    """,
                    (started_at, ended_at),
                )
                row = cur.fetchone()
                if not row:
                    raise RuntimeError("Postgres did not return an ID for an imported session.")
                database_id = int(row[0])
                mapping[session["id"]] = database_id
                cur.executemany(
                    """
                    INSERT INTO telemetry_points (
                        time, session_id, source, bpm, accel_g, xyz
                    )
                    VALUES (%s, %s, 'watch', %s, %s, %s::jsonb);
                    """,
                    [
                        (
                            datetime.fromtimestamp(point["ts"], timezone.utc),
                            database_id,
                            point.get("bpm"),
                            point.get("accel_g"),
                            json.dumps(point.get("xyz")),
                        )
                        for point in session["_points"]
                    ],
                )

    if selected_id not in mapping:
        raise ValueError(f"Unknown local telemetry session: {selected_id}")
    return mapping[selected_id]


def persist_unassigned_postgres_session(session: dict[str, Any]) -> int:
    """Associate one gap-grouped Postgres session with a durable session row."""
    started_at = datetime.fromtimestamp(
        timestamp_epoch(session["started_at"]), timezone.utc
    )
    ended_at = datetime.fromtimestamp(
        timestamp_epoch(session["ended_at"]), timezone.utc
    )
    with get_postgres_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sessions (started_at, ended_at, status)
                VALUES (%s, %s, 'COMPLETED')
                RETURNING id;
                """,
                (started_at, ended_at),
            )
            row = cur.fetchone()
            if not row:
                raise RuntimeError("Postgres did not return an ID for the session.")
            session_id = int(row[0])
            cur.execute(
                """
                UPDATE telemetry_points
                SET session_id = %s
                WHERE source = 'watch' AND session_id IS NULL
                    AND time >= %s AND time <= %s;
                """,
                (session_id, started_at, ended_at),
            )
    return session_id


def create_mqtt_worker(db: TelemetryDatabase) -> mqtt.Client:
    import paho.mqtt.client as mqtt

    def on_connect(client: mqtt.Client, _userdata: Any, _flags: Any, reason_code: Any, _properties: Any = None) -> None:
        print(f"[MQTT] Connected to {MQTT_BROKER_IP}:{MQTT_BROKER_PORT} (rc={reason_code})")
        client.subscribe("climbbud/#")
        print("[MQTT] Subscribed to 'climbbud/#'")

    def on_message(_client: mqtt.Client, _userdata: Any, msg: mqtt.MQTTMessage) -> None:
        topic = msg.topic
        raw_payload = msg.payload.decode("utf-8", errors="replace").strip()

        if topic == "climbbud/control/session":
            command = raw_payload.upper()
            # Support either raw "START"/"STOP" strings or JSON {"command": "START"}
            if command.startswith("{"):
                try:
                    data = json.loads(raw_payload)
                    command = str(data.get("command") or data.get("action") or "").upper()
                except json.JSONDecodeError:
                    pass

            if command == "START":
                db.start_session()
            elif command == "STOP":
                db.stop_session()
            else:
                print(f"[MQTT] Unknown session control command: {raw_payload}")
            return

        if topic in ("climbbud/telemetry/watch", "climbbud/telemetry/vision"):
            default_source = "watch" if topic.endswith("watch") else "vision"
            try:
                payload = json.loads(raw_payload)
                if isinstance(payload, dict):
                    db.enqueue_telemetry(payload, default_source=default_source)
            except json.JSONDecodeError:
                print(f"[MQTT] Invalid JSON on {topic}: {raw_payload}")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_BROKER_IP, MQTT_BROKER_PORT, keepalive=60)
    return client


def main() -> None:
    db = TelemetryDatabase(pg_url=TIGER_DATA_URL, sqlite_path=SQLITE_DB_PATH)
    mqtt_client = create_mqtt_worker(db)
    mqtt_client.loop_start()

    try:
        while True:
            time.sleep(FLUSH_INTERVAL_SEC)
            db.flush()
    except KeyboardInterrupt:
        print("\n[DB] Shutting down telemetry database worker...")
    finally:
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        db.flush()
        if db.pg_conn:
            db.pg_conn.close()


if __name__ == "__main__":
    main()