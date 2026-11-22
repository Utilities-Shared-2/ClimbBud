"""
Camera streamer: runs on the device that has the USB webcam (e.g. a Raspberry Pi
running Raspberry Pi OS) and serves it as MJPEG over HTTP. The QNX Pi pulls it
with:  python3 src/main.py --camera-url http://<this-device>:8081/stream

Only capture + JPEG encoding happens here; pose inference, PID and servos all
run on the QNX side. Frames are only encoded while someone is connected.

    python3 src/camera_streamer.py [--camera 0] [--port 8081] [--fps 15]

Endpoints: /stream (MJPEG), /snapshot.jpg, /health, / (viewer page)
"""
import argparse
import signal
import time

import cv2

from stream_server import FrameBroadcaster, start_stream_server, stop_stream_server

running = True


def _stop(signum, frame):
    global running
    running = False


def main():
    parser = argparse.ArgumentParser(description="Serve a webcam as MJPEG for the QNX tracker")
    parser.add_argument("--camera", type=int, default=0, help="Webcam index (default 0)")
    parser.add_argument("--width", type=int, default=640, help="Capture width (default 640)")
    parser.add_argument("--height", type=int, default=480, help="Capture height (default 480)")
    parser.add_argument("--fps", type=float, default=15.0, help="Max frames per second sent (default 15)")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8081, help="HTTP port (default 8081)")
    parser.add_argument("--quality", type=int, default=80, help="JPEG quality 1-100 (default 80)")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    cap = cv2.VideoCapture(args.camera)
    if not cap.isOpened():
        print(f"[ERROR] Could not open camera {args.camera}")
        return
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)

    broadcaster = FrameBroadcaster(quality=args.quality)
    server = start_stream_server(broadcaster, args.host, args.port)
    period = 1.0 / args.fps if args.fps > 0 else 0

    try:
        while running:
            start = time.perf_counter()
            ok, frame = cap.read()  # keep reading even with no clients so frames stay fresh
            if not ok:
                print("[WARN] camera read failed, retrying")
                time.sleep(0.5)
                continue
            if broadcaster.has_clients:
                broadcaster.publish(frame)
            remaining = period - (time.perf_counter() - start)
            if remaining > 0:
                time.sleep(remaining)
    finally:
        print("[INFO] Stopping camera streamer...")
        stop_stream_server(server)
        cap.release()


if __name__ == "__main__":
    main()
