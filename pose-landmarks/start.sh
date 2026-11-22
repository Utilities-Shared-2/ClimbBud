#!/bin/sh
# QNX Pi: pose tracker + servos. Frames come from the camera Pi (start_camera.sh).
# Usage: CAMERA_URL=http://<camera-pi-ip>:8081/stream ./start.sh
exec python3 src/main.py --headless --stream --camera-url "${CAMERA_URL:-http://192.168.50.1:8081/stream}"
