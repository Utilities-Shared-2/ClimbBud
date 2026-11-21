import json
import os
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")
conn = psycopg.connect(os.getenv("TIGER_DATA_URL"), autocommit=True)

now = datetime.now(timezone.utc)
started_at = now - timedelta(minutes=2)

with conn.cursor() as cur:
    cur.execute(
        "INSERT INTO sessions (started_at, status) VALUES (%s, 'ACTIVE') RETURNING id;",
        (started_at,),
    )
    session_id = cur.fetchone()[0]

    rows = []
    for i in range(120):  # 2 minutes at 1 Hz
        t = started_at + timedelta(seconds=i)
        # Simulate climbing effort + a sudden fall/spike at second 95
        is_fall = i == 95
        bpm = 168 if i >= 90 else random.randint(115, 148)
        accel_g = 3.45 if is_fall else round(random.uniform(0.95, 1.45), 3)
        speed = 0.0 if is_fall else round(random.uniform(0.3, 0.9), 2)

        rows.append(
            (
                t,
                session_id,
                "watch",
                bpm,
                accel_g,
                json.dumps([-320, 140, -980]),
                speed,
                is_fall,
            )
        )

    cur.executemany(
        """
        INSERT INTO telemetry_points (
            time, session_id, source, bpm, accel_g, xyz, speed, fall_detected
        )
        VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s);
        """,
        rows,
    )

    # Verify the real-time Continuous Aggregate immediately reflects the new data
    cur.execute(
        "SELECT * FROM session_telemetry_summary WHERE session_id = %s ORDER BY bucket;",
        (session_id,),
    )
    print(f"Seeded session #{session_id}. Continuous Aggregate buckets:")
    for row in cur.fetchall():
        print(" ", row)

conn.close()