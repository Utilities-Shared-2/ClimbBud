"""
UART bridge for watch telemetry.

Consumes the same telemetry messages as ``watch_bridge.py`` — but over a
one-way UART serial link instead of BLE. Incoming messages are written
straight to the database via ``TelemetryDatabase`` (Tiger Data with SQLite
fallback); no MQTT broker is involved and no ACK/response is sent back to
the device.

Accepted line formats (one message per UART line, '\\n' terminated):

    {"ts": 1730000000.0, "source": "watch", "bpm": 142, "xyz": [12, -3, 980]}
    {"bpm": 142}                  # partial update, merged with last xyz
    {"xyz": [12, -3, 980]}        # partial update, merged with last bpm
    HR:0108e803                   # raw Heart Rate Measurement notification (hex)
    MOTION:...                    # raw InfiniTime motion notification (hex)
    START / STOP                  # session control (same as MQTT control topic)

Environment variables (telemetry/.env):
    UART_PORT    serial device path (e.g. /dev/cu.usbserial-XXXX). If unset
                 or "auto", the first available USB serial device is used.
    UART_BAUD    baud rate (default 115200)
"""

import glob
import json
import os
import sys
import threading
import time

try:
    import serial
except ImportError:
    sys.exit(
        "pyserial is required. Install dependencies with: "
        "pip install -r telemetry/requirements.txt"
    )

from telemetry.db import (
    FLUSH_INTERVAL_SEC,
    SQLITE_DB_PATH,
    TIGER_DATA_URL,
    TelemetryDatabase,
)
from telemetry.watch_bridge import (
    handle_heart_rate,
    handle_motion,
    latest_telemetry,
)

UART_PORT = os.getenv("UART_PORT")
UART_BAUD = int(os.getenv("UART_BAUD", "115200"))

# USB-serial adapters enumerate under these patterns (callout devices first
# on macOS, then dial-in, then generic ACM/USB-serial names).
UART_PORT_GLOBS = (
    "/dev/cu.usb*",
    "/dev/tty.usb*",
    "/dev/cu.SLAB*",
    "/dev/tty.SLAB*",
    "/dev/cu.wchusbserial*",
    "/dev/tty.wchusbserial*",
    "/dev/ttyACM*",
    "/dev/ttyUSB*",
)

MAX_LINE_BYTES = 65536

# ``latest_telemetry`` is imported from watch_bridge so partial updates and
# raw BLE notifications mutate the same state the handlers write to.


def resolve_uart_port() -> str:
    if UART_PORT and UART_PORT.lower() != "auto":
        return UART_PORT
    for pattern in UART_PORT_GLOBS:
        matches = sorted(glob.glob(pattern))
        if matches:
            port = matches[0]
            print(f"[UART] Auto-selected serial port {port}")
            return port
    raise SystemExit(
        "[UART] No UART_PORT configured and no USB serial device found. "
        "Set UART_PORT in telemetry/.env."
    )


def emit_telemetry(db: TelemetryDatabase, payload: dict) -> None:
    payload.setdefault("ts", round(time.time(), 3))
    payload.setdefault("source", "watch")
    db.enqueue_telemetry(payload, default_source="watch")


def handle_raw_notification(line: str, db: TelemetryDatabase) -> bool:
    """Handle 'HR:<hex>' / 'MOTION:<hex>' frames carrying raw BLE payloads."""
    upper = line.upper()
    try:
        if upper.startswith("HR:"):
            data = bytearray.fromhex(line[3:].strip())
            if not data:
                return False
            handle_heart_rate(None, data)
        elif upper.startswith(("MOTION:", "IMU:")):
            data = bytearray.fromhex(line.split(":", 1)[1].strip())
            if len(data) < 6:
                return False
            handle_motion(None, data)
        else:
            return False
    except ValueError:
        return False

    emit_telemetry(
        db,
        {"bpm": latest_telemetry["bpm"], "xyz": list(latest_telemetry["xyz"])},
    )
    return True


def handle_uart_line(line: str, db: TelemetryDatabase) -> None:
    text = line.strip()
    if not text:
        return

    command = text.upper()
    if command == "START":
        db.start_session()
        return
    if command == "STOP":
        db.stop_session()
        return

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        if not handle_raw_notification(text, db):
            print(f"[UART] Ignoring unrecognized line: {text[:120]}")
        return

    if not isinstance(payload, dict):
        return

    if "bpm" in payload:
        latest_telemetry["bpm"] = payload["bpm"]
    if "xyz" in payload:
        latest_telemetry["xyz"] = payload["xyz"]

    has_point = any(
        key in payload
        for key in ("bpm", "xyz", "accel_g", "speed", "fall_detected")
    )
    if not has_point:
        return

    if any(key in payload for key in ("ts", "accel_g", "speed", "fall_detected")):
        # Complete message — forward as-is, same shape the BLE bridge publishes.
        db.enqueue_telemetry(payload, default_source="watch")
    else:
        # Partial BPM/IMU update — emit the merged snapshot like the bridge does.
        emit_telemetry(
            db,
            {
                "bpm": latest_telemetry["bpm"],
                "xyz": list(latest_telemetry["xyz"]),
            },
        )


def uart_reader(port: str, baud: int, db: TelemetryDatabase) -> None:
    """Daemon thread target: read newline-delimited messages, reconnecting on errors."""
    while True:
        try:
            with serial.Serial(port, baudrate=baud, timeout=1) as ser:
                print(f"[UART] Listening on {port} @ {baud} baud (one-way, no ACK).")
                buffer = bytearray()
                while True:
                    chunk = ser.read(256)
                    if not chunk:
                        continue
                    buffer.extend(chunk)
                    while b"\n" in buffer:
                        index = buffer.index(b"\n")
                        line = bytes(buffer[:index])
                        del buffer[: index + 1]
                        handle_uart_line(
                            line.decode("utf-8", errors="replace"), db
                        )
                    if len(buffer) > MAX_LINE_BYTES:
                        print("[UART] Discarding oversized unterminated line.")
                        buffer.clear()
        except (serial.SerialException, OSError) as exc:
            print(f"[UART] Serial error on {port}: {exc}. Retrying in 3s...")
            time.sleep(3.0)


def main() -> None:
    db = TelemetryDatabase(pg_url=TIGER_DATA_URL, sqlite_path=SQLITE_DB_PATH)
    port = resolve_uart_port()
    reader = threading.Thread(
        target=uart_reader, args=(port, UART_BAUD, db), daemon=True
    )
    reader.start()

    try:
        while True:
            time.sleep(FLUSH_INTERVAL_SEC)
            db.flush()
    except KeyboardInterrupt:
        print("\n[UART] Shutting down watch UART bridge...")
    finally:
        db.flush()
        if db.pg_conn:
            db.pg_conn.close()


if __name__ == "__main__":
    main()
