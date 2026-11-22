import asyncio
import json
import os
import struct
import time
from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError
import paho.mqtt.client as mqtt
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

# Standard BLE Heart Rate Measurement & InfiniTime Motion Service UUIDs
HR_CHAR_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
MOTION_CHAR_UUID = "00030002-78fc-48fe-8e23-433b3a1942d0"

DEVICE_NAME_FILTER = os.getenv("WATCH_NAME", "InfiniTime")
BROKER_IP = os.getenv("MQTT_BROKER_IP", "127.0.0.1")
BROKER_PORT = int(os.getenv("MQTT_BROKER_PORT", "1883"))
MQTT_TOPIC = os.getenv("WATCH_TOPIC", "climbbud/telemetry/watch")
PUBLISH_INTERVAL = float(os.getenv("WATCH_INTERVAL_SEC", "0.5"))
OFFLINE_TEST = os.getenv("OFFLINE_TEST", "1") == "1"

latest_telemetry = {
    "bpm": 0,
    "xyz": [0, 0, 0],
}


def handle_heart_rate(_sender, data: bytearray) -> None:
    # Byte 0 bit 0 indicates uint8 (0) or uint16 (1) BPM format
    flags = data[0]
    if (flags & 0x01) == 0:
        bpm = data[1]
    else:
        bpm = int.from_bytes(data[1:3], byteorder="little")
    latest_telemetry["bpm"] = bpm


def handle_motion(_sender, data: bytearray) -> None:
    # InfiniTime broadcasts 3 signed 16-bit integers (X, Y, Z), where ~1024 = 1g
    if len(data) >= 6:
        x, y, z = struct.unpack("<3h", data[:6])
        latest_telemetry["xyz"] = [x, y, z]


def create_mqtt_client() -> mqtt.Client | None:
    if OFFLINE_TEST:
        print("[MODE] OFFLINE_TEST=1 -> Printing locally without MQTT broker.")
        return None

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.connect(BROKER_IP, BROKER_PORT, keepalive=60)
    client.loop_start()
    print(f"[MQTT] Connected to {BROKER_IP}:{BROKER_PORT} -> topic '{MQTT_TOPIC}'")
    return client


async def stream_from_watch(mqtt_client: mqtt.Client | None) -> None:
    while True:
        print(f"[BLE] Scanning for '{DEVICE_NAME_FILTER}'...")
        device = await BleakScanner.find_device_by_filter(
            lambda d, _adv: bool(d.name and DEVICE_NAME_FILTER.lower() in d.name.lower()),
            timeout=15.0,
        )

        if not device:
            print("[BLE] Watch not found. Make sure it's awake and disconnected from your phone. Retrying in 3s...")
            await asyncio.sleep(3.0)
            continue

        disconnected_event = asyncio.Event()

        def on_disconnect(_client: BleakClient) -> None:
            print("[BLE] Watch disconnected!")
            disconnected_event.set()

        try:
            async with BleakClient(device, disconnected_callback=on_disconnect) as client:
                print(f"[BLE] Connected to {device.name} ({device.address})")
                await client.start_notify(HR_CHAR_UUID, handle_heart_rate)
                await client.start_notify(MOTION_CHAR_UUID, handle_motion)

                while client.is_connected and not disconnected_event.is_set():
                    payload = {
                        "ts": round(time.time(), 3),
                        "source": "watch",
                        "bpm": latest_telemetry["bpm"],
                        "xyz": latest_telemetry["xyz"],
                    }
                    msg = json.dumps(payload)

                    if mqtt_client:
                        mqtt_client.publish(MQTT_TOPIC, msg)
                    print(msg)

                    await asyncio.sleep(PUBLISH_INTERVAL)

        except (BleakError, TimeoutError, OSError) as exc:
            print(f"[BLE] Connection error: {exc}. Retrying in 3s...")
            await asyncio.sleep(3.0)


async def main() -> None:
    mqtt_client = create_mqtt_client()
    try:
        await stream_from_watch(mqtt_client)
    finally:
        if mqtt_client:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped watch bridge.")