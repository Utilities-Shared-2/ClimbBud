#!/bin/sh
# Camera Pi (Raspberry Pi OS, USB webcam): serve the webcam as MJPEG on :8081 for the QNX Pi.
cd "$(dirname "$0")"
[ -f .venv/bin/activate ] && . .venv/bin/activate
exec python3 src/camera_streamer.py "$@"
