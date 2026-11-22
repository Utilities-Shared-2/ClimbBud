"""
Voice control for ClimbBud recording sessions (runs on its own Pi with a mic).

Listens (Vosk, offline) for "start recording" / "end recording" and publishes
START / STOP to the MQTT topic climbbud/control/session. The telemetry worker
starts/stops the health-stats session and the dashboard starts/stops the
video + pose recording from that same message.

Settings come from environment variables, or from voice-commands/.env (copy
.env.example). Real environment variables win over the file:
  MQTT_BROKER_IP      broker the telemetry service uses (default 127.0.0.1)
  MQTT_BROKER_PORT    default 1883
  VOICE_RECORD_AUDIO  1 = also save the mic audio to recordings/*.wav while a
                      session is active (default 0)
"""
import json
import os
import queue
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")  # does not override variables already set
except ImportError:
    pass  # python-dotenv not installed: plain environment variables still work

MODEL_PATH = BASE_DIR / "models" / "vosk-model-small-en-us-0.15"
RECORDINGS_DIR = BASE_DIR / "recordings"
IsVoiceRecorded = os.getenv("VOICE_RECORD_AUDIO", "0").strip().lower() in {"1", "true", "yes", "on"}

SAMPLE_RATE = 16000
BLOCKSIZE = 4000
GRAMMAR = json.dumps(["start recording", "end recording", "[unk]"])

CONTROL_TOPIC = "climbbud/control/session"
MQTT_BROKER_IP = os.getenv("MQTT_BROKER_IP", "127.0.0.1")
MQTT_BROKER_PORT = int(os.getenv("MQTT_BROKER_PORT", "1883"))

session_active = False
mqtt_client = None
is_currently_recording = False
audio_queue: queue.Queue[bytes] = queue.Queue()
current_file = None


def setIsVoiceRecorded(isRecorded) -> None:
    """
    Define wether voice is recording during the video taping
    by default this value is false
    """
    global IsVoiceRecorded
    IsVoiceRecorded = isRecorded


# ---- session control over MQTT ----

def connect_mqtt():
    """Connect to the broker in the background; paho keeps reconnecting on its own."""
    import paho.mqtt.client as mqtt

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"climbbud-voice-{os.getpid()}")
    client.on_connect = lambda c, u, f, rc, p=None: print(
        f"[MQTT] Connected to {MQTT_BROKER_IP}:{MQTT_BROKER_PORT} (rc={rc})")
    client.on_disconnect = lambda c, u, f, rc, p=None: print(f"[MQTT] Disconnected (rc={rc}), retrying...")
    client.reconnect_delay_set(min_delay=1, max_delay=10)
    client.connect_async(MQTT_BROKER_IP, MQTT_BROKER_PORT, keepalive=30)
    client.loop_start()
    return client


def publish_session(command: str) -> bool:
    """Publish START/STOP to the control topic. Returns True if handed to the client."""
    if mqtt_client is None:
        print(f"[MQTT] Not connected; could not send {command}")
        return False
    # payload = json.dumps({"command": command, "source": "voice", "ts": time.time()})
    # try:
    #     info = mqtt_client.publish(CONTROL_TOPIC, payload, qos=1)
    # except Exception as exc:  # never let a network error kill the listener
    #     print(f"[MQTT] Failed to send {command}: {exc}")
    #     return False
    # if info.rc != 0:
    #     print(f"[MQTT] Failed to send {command} (rc={info.rc})")
    #     return False
    # print(f"[MQTT] Sent {command} -> {CONTROL_TOPIC}")
    return True


def start_session() -> None:
    global session_active
    if session_active:
        return
    if publish_session("START"):
        session_active = True
    start_recording()


def end_session() -> None:
    global session_active
    if session_active and publish_session("STOP"):
        session_active = False
    end_recording()


# ---- optional local audio recording ----

def start_recording() -> None:
    global is_currently_recording, current_file
    if not is_currently_recording and IsVoiceRecorded:
        import soundfile as sf

        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        filepath = RECORDINGS_DIR / f"rec_{int(time.time())}.wav"
        current_file = sf.SoundFile(
            str(filepath),
            mode="w",
            samplerate=SAMPLE_RATE,
            channels=1,
            subtype="PCM_16",
        )
        is_currently_recording = True
        print(f"[ACTION] Started recording -> {filepath.relative_to(BASE_DIR)}")


def end_recording() -> None:
    global is_currently_recording, current_file
    if is_currently_recording:
        is_currently_recording = False
        if current_file is not None:
            current_file.close()
            current_file = None
        print("[ACTION] Stopped recording.")


def handle_phrase(text: str) -> None:
    cleaned = text.strip().lower()
    if cleaned == "start recording":
        start_session()
    elif cleaned == "end recording":
        end_session()


def audio_callback(indata, frames, time_info, status) -> None:
    if status:
        print(f"[AUDIO STATUS] {status}")
    raw_bytes = bytes(indata)
    audio_queue.put(raw_bytes)
    if is_currently_recording and current_file is not None:
        import numpy as np

        samples = np.frombuffer(raw_bytes, dtype=np.int16)
        current_file.write(samples)


def main() -> None:
    global mqtt_client
    import sounddevice as sd
    from vosk import KaldiRecognizer, Model

    model = Model(str(MODEL_PATH))
    rec = KaldiRecognizer(model, SAMPLE_RATE, GRAMMAR)
    rec.SetWords(False)
    mqtt_client = connect_mqtt()

    try:
        with sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=BLOCKSIZE,
            dtype="int16",
            channels=1,
            callback=audio_callback,
        ):
            print("Listening for 'start recording' and 'end recording'...")
            while True:
                data = audio_queue.get()
                if rec.AcceptWaveform(data):
                    result = json.loads(rec.Result())
                    handle_phrase(result.get("text", ""))
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        end_session()
        if mqtt_client is not None:
            time.sleep(0.5)  # let a final STOP go out
            mqtt_client.loop_stop()
            mqtt_client.disconnect()


if __name__ == "__main__":
    main()
