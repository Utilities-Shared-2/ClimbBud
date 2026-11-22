"""
Simulate the voice Pi saying "start recording" / "end recording" — no mic needed.

Runs the exact code path a recognised phrase takes in recorder_control.py
(handle_phrase -> start_session/end_session -> HTTP POST to the dashboard), so
it tests the real network call from whatever machine you run it on.

    python simulate_command.py start
    python simulate_command.py stop
    python simulate_command.py start --url http://192.168.50.10:5000 --token secret

--url / --token default to DASHBOARD_URL / RECORDING_API_TOKEN from the
environment or voice-commands/.env.

--tracker-url defaults to POSE_TRACKER_URL (the QNX pose tracker, e.g.
http://192.168.50.2:8080); pass --tracker-url "" to skip it.

Equivalent raw calls, no Python needed:
    curl -X POST "http://<qnx>:8080/toggle?state=on"
    curl -X POST http://<dashboard>:5000/api/recording/start
    curl -X POST http://<dashboard>:5000/api/recording/stop
    curl -X POST "http://<qnx>:8080/toggle?state=off"
"""
import argparse
import json
import sys

import recorder_control as rc

PHRASES = {"start": "start recording", "stop": "end recording"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Send a simulated voice command to the dashboard")
    parser.add_argument("command", choices=sorted(PHRASES), help="start or stop recording")
    parser.add_argument("--url", default=rc.DASHBOARD_URL, help=f"dashboard URL (default {rc.DASHBOARD_URL})")
    parser.add_argument("--tracker-url", default=rc.POSE_TRACKER_URL,
                        help=f"QNX pose tracker URL, empty to skip (default {rc.POSE_TRACKER_URL or 'unset'})")
    parser.add_argument("--token", default=rc.RECORDING_API_TOKEN, help="X-ClimbBud-Token, if the dashboard needs one")
    args = parser.parse_args(argv)

    rc.DASHBOARD_URL = args.url.strip().rstrip("/")
    rc.POSE_TRACKER_URL = args.tracker_url.strip().rstrip("/")
    rc.RECORDING_API_TOKEN = args.token.strip()
    # Each run is a fresh process, so assume the opposite state: "stop" must actually send STOP.
    rc.session_active = args.command == "stop"

    print(f"Simulating '{PHRASES[args.command]}' -> dashboard {rc.DASHBOARD_URL}, "
          f"tracker {rc.POSE_TRACKER_URL or '(skipped)'}")
    rc.handle_phrase(PHRASES[args.command])

    failed = False
    if rc.session_active != (args.command == "start"):
        print("FAILED: the dashboard did not accept the command (see message above).")
        failed = True
    elif rc.last_response is not None:
        print("dashboard:", json.dumps(rc.last_response, indent=2))
    if rc.POSE_TRACKER_URL:
        if rc.last_tracking is None:
            print("FAILED: the pose tracker did not accept the command (see message above).")
            failed = True
        else:
            print("tracker:", json.dumps(rc.last_tracking))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
