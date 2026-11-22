#!/bin/sh
# run_vision.sh - keep the vision/recorder script alive
LOG=/data/home/qnxuser/ClimbBud/pose-landmarks/start.log
PY=$(which python3)
SCRIPT=/data/home/qnxuser/ClimbBud/pose-landmarks/start.sh

# wait (up to 30 s each) for devices the script depends on
waitfor /dev/snd 30      # audio, for the voice recorder
# waitfor /dev/sensor 30 # camera service, if you use it

cd /data/home/qnxuser/ClimbBud/pose-landmarks
while true; do
    echo "$(date) starting" >> "$LOG"
    sh "$SCRIPT" >> "$LOG" 2>&1
    echo "$(date) exited with $?, restarting in 5s" >> "$LOG"
    sleep 5
done
