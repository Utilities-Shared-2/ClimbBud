# forhead

rasberry pi 3 powered facial tracking via active rotation


## Authors

- [@ttcdm](https://github.com/ttcdm)
- [@braydenleung-Git](https://github.com/braydenleung-Git)
- [@matthewxiaa](https://github.com/matthewxiaa)


## Step by Step Guide to reproduce

**Step 1**
log on to ras pi either via ssh or VNC or remote desktop solution(i.e [Raspberry Pi Connect](https://www.raspberrypi.com/software/connect/)) or directly

Raspberry Pi Connect or Remote Desktop:
- (setup anything that is necessary for this to work)
- connect to Raspberry Pi via "screen sharing"
- *p.s : just remote into the Raspberry Pi like it is a computer* 

ssh :
- assuming RasPi is connect to your local network or vpn correctly, and have ssh enabled enter the following command
```bash
  ssh [raspi name]@[local ip address]
```
i.e: 
```bash
  ssh myPi@192.168.0.1
```
- you can get the ip address by typing the following command to the console
```bash
  hostname -I
```
- or go to the advance network settings of the raspberrypi, and get the ip address
- note: it is **not** the one that is IPV4, IPV6, or subnet mask
- allow finger printing, and enter the login password of your Pi

VNC : 
- assuming you have the ip address to the Raspberry Pi, and enabled vnc on Raspberry Pi
- connect to Raspberry Pi via that ip address, and enter the login password, if you have setup nothing, check out the default password for the Raspberry Pi, or consider praying you saved the password on some notebook or paper or a password manager. *(at this point, just wipe the old password via directly connecting to it, or consider wiping the SD card if you are really desperate)*

Directly: 
- plug in your display, keyboard and mouse
- power on your Raspberry Pi

**Step 2**
Download and configure python virtual environments
```bash
  git clone https://github.com/Utilities-Shared-2/forhead
  python3 -m venv .venv
  source .venv/bin/activate
  pip install -r requirements.txt
```

**Step 3** 
Setup the electrical circuit
- data cable from the servo(we used 37kg torque, 270 deg servo in our system, please modify the code to adapt to your version of servos) are connected to the pi via pin :
    - swivle base : 11
    - tilt : 13 
- connect the servos to external power source, please make sure the following :
    - all ground are shared(including ras pi)
    - power source is adaqute
- connect the camera via USB port on the ras pi

**Step 4**
Activate the python script 
```bash
  python3 src/main.py
```

**Viewing the stream from another device**

Add `--stream` (on by default in `start.sh`) to serve the annotated feed (pose skeleton, body centre, head point, FPS, pan/tilt) over HTTP:
```bash
  python3 src/main.py --headless --stream            # on the Pi
  python src/main.py --mock --headless --stream      # on a laptop with simulated servos
```
Then, from any device on the same network, open `http://[pi ip]:8080/`.
- `/stream` – raw MJPEG (works in a browser, `<img src>`, or VLC "Open Network Stream")
- `/snapshot.jpg` – the latest single frame
- `/pose` – latest keypoints / body position / servo angles as JSON
- Options: `--stream-port`, `--stream-host`, `--stream-quality` (JPEG, default 70), `--stream-scale` (lower this on a Pi 3 to save CPU/bandwidth)
- Frames are only encoded while someone is watching. There is no authentication, so only use it on a trusted network. On Windows, allow Python through the firewall for the port.

**Using the pose feed for fall warnings**

Start the pose service with `--stream` as shown above and verify
`http://<pi-ip>:8080/pose` returns JSON with a changing `timestamp`. The
dashboard must be able to reach that address. On the dashboard machine, set
`POSE_URL` to the full URL before starting the app. For example, from the
repository root in Windows PowerShell:

```powershell
$env:POSE_URL = "http://<pi-ip>:8080/pose"
python ui\app.py
```

Replace `<pi-ip>` with the Pi's address, or use `127.0.0.1` if both services
run on the same machine. `POSE_URL` can also be set in `telemetry/.env`;
leave it unset to disable pose polling. The dashboard logs pose feed failures
and continues without camera data. The feed is unauthenticated: keep it on a
trusted network rather than exposing it publicly.

The Live tab's **Simulate Fall** button tests the warning and browser sound,
not the camera or watch detection. Enable sound with a click, then dismiss the
warning. Fall alerts are experimental and are not a safety system. See the
[dashboard settings](../ui/app.py) for thresholds and demo configuration.

For a live sensor test, use a fresh sample in an isolated test database.
[seed_demo.py](../telemetry/seed_demo.py) writes to PostgreSQL and its simulated
fall is approximately 25 seconds old when inserted, outside the live detector's
10-second impact window; it is not a live alert test. Do not seed a database
containing important data.

## Running on QNX 8.0 (Raspberry Pi)

OpenVINO and pigpio don't exist on QNX, and `cv2.VideoCapture` has no camera backend there, so the code picks QNX-friendly backends automatically when `sys.platform` is QNX:

| Part | Raspberry Pi OS / desktop | QNX |
|---|---|---|
| Pose model (`--inference`) | OpenVINO | OpenCV DNN (`cv2.dnn`, same ONNX model) |
| Camera (`--camera-backend`) | `cv2.VideoCapture` | `qnx_apis.VideoCapture` (libcamapi) |
| Servos (`--io`) | pigpio (`io_control_pigio.py`) | `rpi_gpio` (`io_control_qnx.py`) |

**Rewire the servos.** QNX `rpi_gpio` only drives hardware PWM on GPIO 12, 13, 18 and 19 (12/18 share channel 1, 13/19 share channel 2), so the servos must move off GPIO 17/27:

| | GPIO | physical pin |
|---|---|---|
| pan / base servo | 12 | 32 |
| tilt servo | 13 | 33 |
| limit switch (unchanged) | 26 | 37 |

**Install packages** (no pip builds needed). See `requirements-qnx.txt`:
```bash
  apk update
  apk add python3-numpy python3-opencv qnx-sf-camapi qnx-sf-camera-usb
  # build deps for the camera wrapper
  apk add opencv-dev python3-numpy-dev python3-dev python3-pybind11-dev python3-setuptools
  git clone --recursive https://github.com/qnx/python3-qnx-apis && cd python3-qnx-apis
  python setup.py build_ext -j$(nproc) && pip install --break-system-packages .
```
Package names can differ between QNX releases; use `apk search <name>` if one isn't found.

**Check and run:**
```bash
  pidin -p rpi_gpio                     # GPIO resource manager must be running
  python3 -c "import cv2; print(cv2.__version__, hasattr(cv2, 'dnn'))"
  python3 src/main.py --headless --stream
```
The startup log prints the backends in use (`[INFO] backends: servos=qnx camera=qnx inference=opencv`) and the camera frame shape. If the servo travel is off, calibrate `degrees_to_pulsewidth` in `io_control_qnx.py` (500–2500 µs maps to 2.5–12.5 % duty at 50 Hz).





    
## Trouble Shooting

I would like to start this section by saying, this is a software as is, meaning we are not going to provide support to it,
if non of the trouble shoot is working
I'd recommend Gemini, ChatGPT, or Perplexity/Comet, or any flavors of Ai

---

**python3 seems to be not working**
- replace python3 with the following
```bash 
    python
``` 
- if it still doesn't work, make sure you googled how to install python on your Raspberry Pi

**How to get dutycycles**
- TL:DR dutycycles is the length of the (signal/maxium pulse width) * 100
- maxium pulse width is usually 20ms, while for reference our servos 270 deg is 2.5ms
- our code uses pigpio, meaning instead of using dutycycles, it uses the pulse width to control
- there should be a short elaboration in the code, representing which part of the math is what.
- if you are into learning more, seriously, consider GPT, googling will not help as much.

## Acknowledgements
- This project would not have success without the vast online community, chat bot(yes, im serious)
  
-[pinout for rasPi](https://pinout.xyz/)

-[Readme Editor](https://readme.so/editor)
