import json
import queue
import time
from pathlib import Path
import numpy as np
import sounddevice as sd
import soundfile as sf
from vosk import Model, KaldiRecognizer

BASE_DIR = Path(__file__).resolve().parent
MODEL_PATH = BASE_DIR / "models" / "vosk-model-small-en-us-0.15"
RECORDINGS_DIR = BASE_DIR / "recordings"

SAMPLE_RATE = 16000
BLOCKSIZE = 4000
GRAMMAR = json.dumps(["start recording", "end recording", "[unk]"])

model = Model(str(MODEL_PATH))
rec = KaldiRecognizer(model, SAMPLE_RATE, GRAMMAR)
rec.SetWords(False)

is_recording = False
audio_queue: queue.Queue[bytes] = queue.Queue()
current_file: sf.SoundFile | None = None


def start_recording() -> None:
    global is_recording, current_file
    if not is_recording:
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        filepath = RECORDINGS_DIR / f"rec_{int(time.time())}.wav"
        current_file = sf.SoundFile(
            str(filepath),
            mode="w",
            samplerate=SAMPLE_RATE,
            channels=1,
            subtype="PCM_16",
        )
        is_recording = True
        print(f"[ACTION] Started recording -> {filepath.relative_to(BASE_DIR)}")


def end_recording() -> None:
    global is_recording, current_file
    if is_recording:
        is_recording = False
        if current_file is not None:
            current_file.close()
            current_file = None
        print("[ACTION] Stopped recording.")


def handle_phrase(text: str) -> None:
    cleaned = text.strip().lower()
    if cleaned == "start recording":
        start_recording()
    elif cleaned == "end recording":
        end_recording()


def audio_callback(indata, frames, time_info, status) -> None:
    if status:
        print(f"[AUDIO STATUS] {status}")
    raw_bytes = bytes(indata)
    audio_queue.put(raw_bytes)
    if is_recording and current_file is not None:
        samples = np.frombuffer(raw_bytes, dtype=np.int16)
        current_file.write(samples)


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
    end_recording()