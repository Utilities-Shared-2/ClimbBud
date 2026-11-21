"""ClimbBot – Flask GUI.

Run:  pip install flask
      python app.py   ->  http://localhost:5000

All sensor values are simulated in `read_sensors()`. Replace that one
function with your real heart-rate / camera / pose-tracking code.
"""
import math
import random
import time

from flask import Flask, jsonify, render_template

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


def read_sensors():
    """Swap this for real data."""
    t = state["elapsed"]
    if state["recording"]:
        t += time.time() - state["start"]
    bpm = int(132 + 12 * math.sin(t / 4) + random.randint(-2, 2))
    intensity = round(0.5 + 0.35 * math.sin(t / 2.2) + 0.1 * math.sin(t * 1.3), 3)
    return {"bpm": bpm, "intensity": intensity, "elapsed": t}


@app.route("/")
def index():
    return render_template("index.html", route=ROUTE, history=HISTORY)


@app.route("/api/live")
def live():
    data = read_sensors()
    return jsonify(**data, pr=state["pr"], battery=state["battery"],
                   recording=state["recording"])


@app.route("/api/recording/toggle", methods=["POST"])
def toggle():
    now = time.time()
    if state["recording"]:
        state["elapsed"] += now - state["start"]
    state["recording"] = not state["recording"]
    state["start"] = now
    return jsonify(recording=state["recording"], elapsed=read_sensors()["elapsed"])


@app.route("/api/stats")
def stats():
    times = [h["time"] for h in HISTORY]
    if not times:
        return jsonify(sessions=0, best=0, avg=0, avg_bpm=0)
    return jsonify(sessions=len(HISTORY), best=min(times),
                   avg=round(sum(times) / len(times), 1),
                   avg_bpm=round(sum(h["bpm"] for h in HISTORY) / len(HISTORY)))


if __name__ == "__main__":
    app.run(debug=True)
