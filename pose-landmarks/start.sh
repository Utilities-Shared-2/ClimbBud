#!/bin/sh
# QNX Pi: pose tracker + servos. Frames come from the camera Pi (start_camera.sh).
# Usage: CAMERA_URL=http://<camera-pi-ip>:8081/stream ./start.sh
cd "$(dirname "$0")"
[ -f .venv/bin/activate ] && . .venv/bin/activate
exec python3 src/main.py --headless --stream --camera-url "${CAMERA_URL:-http://camera-pi.local:8081/stream}"
