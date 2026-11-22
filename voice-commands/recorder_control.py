"""
Voice control for ClimbBud recording sessions (runs on its own Pi with a mic).

Listens (Vosk, offline) for "start recording" / "end recording" and sends two
HTTP requests:
    POST <DASHBOARD_URL>/api/recording/start          (or /stop)
        dashboard starts/stops the video + pose recording and the health-stats
        (telemetry) session, exactly like its Record button
    POST <POSE_TRACKER_URL>/toggle?state=on           (or state=off)
        the QNX pose tracker turns tracking (model + servos) on/off

Settings come from environment variables, or from voice-commands/.env (copy
.env.example). Real environment variables win over the file:
  DASHBOARD_URL        dashboard base URL (default http://127.0.0.1:5000)
  POSE_TRACKER_URL     QNX pose tracker base URL, e.g. http://192.168.50.2:8080
                       (empty = don't control tracking)
  RECORDING_API_TOKEN  optional; must match the dashboard's RECORDING_API_TOKEN
  VOICE_RECORD_AUDIO   1 = also save the mic audio to recordings/*.wav while a
                       session is active (default 0)

Test the network call without a mic: python simulate_command.py start
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

DASHBOARD_URL = os.getenv("DASHBOARD_URL", "http://127.0.0.1:5000").strip().rstrip("/")
POSE_TRACKER_URL = os.getenv("POSE_TRACKER_URL", "").strip().rstrip("/")
RECORDING_API_TOKEN = os.getenv("RECORDING_API_TOKEN", "").strip()
HTTP_TIMEOUT_SEC = 8  # dashboard start/stop also opens/closes the DB session and video files

session_active = False
last_response = None  # dashboard's JSON reply to the most recent command
last_tracking = None  # QNX tracker's JSON reply ({"enabled": bool}) to the most recent toggle
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


# ---- session control over HTTP ----

def send_session(command: str) -> bool:
    """POST START/STOP to the dashboard. True if it answered 200 (recording state applied)."""
    global last_response
    import requests

    url = f"{DASHBOARD_URL}/api/recording/{'start' if command == 'START' else 'stop'}"
    headers = {"X-ClimbBud-Token": RECORDING_API_TOKEN} if RECORDING_API_TOKEN else {}
    try:
        response = requests.post(url, headers=headers, timeout=HTTP_TIMEOUT_SEC)
    except requests.RequestException as exc:  # never let a network error kill the listener
        print(f"[HTTP] Could not reach dashboard at {url}: {exc}")
        return False
    if response.status_code != 200:
        print(f"[HTTP] Dashboard refused {command} ({response.status_code}): {response.text[:200]}")
        return False
    try:
        last_response = response.json()
    except ValueError:
        last_response = None
    recording = last_response.get("recording") if isinstance(last_response, dict) else "?"
    print(f"[HTTP] {command} -> recording={recording}")
    return True


def send_tracking(on: bool) -> bool:
    """POST <POSE_TRACKER_URL>/toggle?state=on|off to the QNX tracker. True if it answered 200."""
    global last_tracking
    if not POSE_TRACKER_URL:
        return False  # tracking control not configured
    import requests

    url = f"{POSE_TRACKER_URL}/toggle"
    state = "on" if on else "off"
    try:
        response = requests.post(url, params={"state": state}, timeout=HTTP_TIMEOUT_SEC)
    except requests.RequestException as exc:  # never let a network error kill the listener
        print(f"[HTTP] Could not reach pose tracker at {url}: {exc}")
        return False
    if response.status_code != 200:
        print(f"[HTTP] Pose tracker refused state={state} ({response.status_code}): {response.text[:200]}")
        return False
    try:
        last_tracking = response.json()
    except ValueError:
        last_tracking = None
    enabled = last_tracking.get("enabled") if isinstance(last_tracking, dict) else "?"
    print(f"[HTTP] tracking {state} -> enabled={enabled}")
    return True


def start_session() -> None:
    global session_active
    # Tracker first, so pose data is flowing when the recording starts. state=on is
    # safe to repeat, so it's sent every time and doesn't depend on the dashboard.
    send_tracking(True)
    if session_active:
        return
    if send_session("START"):
        session_active = True
    start_recording()


def end_session() -> None:
    global session_active
    if session_active and send_session("STOP"):
        session_active = False
    send_tracking(False)  # always: tracking goes off even if the dashboard was unreachable
    end_recording()


# ---- optional local audio recording ----

def start_recording() -> None:
    global is_currently_recording, current_file
    print("Recording Started")
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
    print("Recording Ended")
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
    import sounddevice as sd
    from vosk import KaldiRecognizer, Model

    model = Model(str(MODEL_PATH))
    rec = KaldiRecognizer(model, SAMPLE_RATE, GRAMMAR)
    rec.SetWords(False)
    print(f"Dashboard: {DASHBOARD_URL}")
    print(f"Pose tracker: {POSE_TRACKER_URL or '(not configured: tracking is not controlled)'}")

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
        end_session()  # don't leave the dashboard recording when the listener exits


if __name__ == "__main__":
    main()
