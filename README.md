# ClimbBud

A climbing training system with pose tracking, health telemetry, voice commands, and a web dashboard.

## Project Overview

ClimbBud consists of multiple components that work together:

- **Pose Tracking** (`pose-landmarks/`) - Head/body tracking with servo-controlled camera (runs on QNX Pi)
- **Web Dashboard** (`ui/`) - Flask-based UI for live monitoring and session control
- **Telemetry** (`telemetry/`) - Health stats database with MQTT integration
- **Voice Commands** (`voice-commands/`) - Voice-activated recording control (runs on separate subnet Pi)

## Architecture

```
┌─────────────────────────────────┐         ┌─────────────────┐
│  Voice/Camera Pi               │         │  QNX Pi         │
│  (Subnet 192.168.50.x)         │         │  (Auto-start)   │
│                                 │         │                 │
│  - Voice Recorder              │ MQTT    │  - Pose         │
│  - Mic                         │────────▶│    Tracker      │
│  - Camera Streamer             │         │  - Servos       │
│  - USB Webcam                  │ MJPEG   │                 │
└─────────────────────────────────┘         └─────────────────┘
       │ (via Tailscale)                          │
       │                                           │ HTTP
       └───────────────────────────────────────────┤
                                                   ▼
                                          ┌─────────────────┐
                                          │  Dashboard      │
                                          │  (Flask UI)     │
                                          │                 │
                                          │  - Live View    │
                                          │  - Session Rec  │
                                          │  - Health Stats │
                                          └─────────────────┘
```

**Network Setup:**
- Voice/Camera Pi: Subnet `192.168.50.x` with Tailscale for remote access
- QNX Pi: Connects to Voice/Camera Pi via Tailscale
- Dashboard: Can run on any machine with network access to the Pi(s)

## Quick Start

### Prerequisites

- Python 3.8+
- MQTT broker (e.g., mosquitto)
- Raspberry Pi devices (or desktop for development)

### Installation

1. **Clone the repository:**
```bash
git clone <repository-url>
cd ClimbBud
```

2. **Create and activate virtual environment:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

3. **Install dependencies:**
```bash
pip install -r Requirements.txt
```

### Starting the Dashboard

The main web dashboard is started from the repository root:

```bash
# Using the provided script
./start.sh

# Or manually
source .venv/bin/activate
python ui/app.py
```

The dashboard will be available at `http://localhost:5000`

## Component Setup

### 1. QNX Pi - Pose Tracking (Auto-start Service)

The QNX Pi runs the pose tracking service with automatic restart via systemd.

**Setup:**
```bash
cd pose-landmarks
# Install QNX-specific dependencies
apk update
apk add python3-numpy python3-opencv
```

**Systemd Service Configuration:**
Create a systemd service file for auto-start on boot:

```bash
sudo nano /etc/systemd/system/climbbud-pose.service
```

Add the following content (adjust paths as needed):

```ini
[Unit]
Description=ClimbBud Pose Tracking Service
After=network.target

[Service]
Type=simple
User=qnxuser
WorkingDirectory=/data/home/qnxuser/ClimbBud/pose-landmarks
Environment="CAMERA_URL=http://192.168.50.1:8081/stream"
ExecStart=/data/home/qnxuser/ClimbBud/pose-landmarks/start_wrapper.sh
Restart=always
RestartSec=5
StandardOutput=append:/data/home/qnxuser/ClimbBud/pose-landmarks/start.log
StandardError=append:/data/home/qnxuser/ClimbBud/pose-landmarks/start.log

[Install]
WantedBy=multi-user.target
```

Enable and start the service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable climbbud-pose.service
sudo systemctl start climbbud-pose.service
```

**Service wrapper (`start_wrapper.sh`):**
- Waits for required devices (`/dev/snd`, `/dev/sensor`)
- Runs the pose tracker via `start.sh`
- Automatically restarts on crash (with 5s delay)
- Logs to `/data/home/qnxuser/ClimbBud/pose-landmarks/start.log`

**Manual testing (without systemd):**
```bash
CAMERA_URL=http://192.168.50.1:8081/stream ./start.sh
```

**Stream endpoints:**
- `http://<qnx-pi>:8080/stream` - Annotated MJPEG feed
- `http://<qnx-pi>:8080/pose` - JSON pose data
- `http://<qnx-pi>:8080/snapshot.jpg` - Latest frame

### 2. Voice/Camera Pi - Webcam Streamer & Voice Commands

This Pi runs both the camera streamer and voice command service on the `192.168.50.x` subnet. It also has Tailscale configured for remote access.

**Setup:**
```bash
# Install camera streamer dependencies
cd pose-landmarks
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-camera.txt

# Install voice command dependencies
cd ../voice-commands
python3 -m venv .venv
source .venv/bin/activate
pip install -r Requirements.txt

# Configure MQTT broker
cp .env.example .env
# Edit .env to set MQTT_BROKER_IP to your broker address
```

**Starting the camera streamer:**
```bash
cd pose-landmarks
./start_camera.sh
# Or with custom options
python3 src/camera_streamer.py --port 8081 --fps 30 --quality 80
```

The stream is available at `http://192.168.50.1:8081/stream`

**Starting voice commands:**
```bash
cd voice-commands
python recorder_control.py
```

**Voice commands:**
- "start recording" - Starts the session (publishes START to MQTT)
- "end recording" - Stops the session (publishes STOP to MQTT)

**Optional audio recording:**
Set `VOICE_RECORD_AUDIO=1` in `.env` to also save microphone audio to `voice-commands/recordings/`.

**Tailscale Setup:**
The QNX Pi connects to this Pi via Tailscale. Ensure Tailscale is installed and running on both devices.

See the "Network Configuration" section below for detailed subnet routing setup.

### 4. Dashboard - Web UI

The Flask dashboard provides the main user interface.

**Environment variables (optional):**
Set these in `telemetry/.env` or as environment variables:

```bash
# Pose stream configuration
POSE_STREAM_HOST=localhost          # QNX Pi IP for pose stream
POSE_STREAM_PORT=8080
POSE_STREAM_SCHEME=http

# Raw camera stream (optional, for higher frame rate)
CAMERA_STREAM_URL=http://<camera-pi>:8081/stream

# MQTT broker for session control
MQTT_BROKER_IP=127.0.0.1
MQTT_BROKER_PORT=1883

# Fall detection settings
POSE_URL=http://<qnx-pi>:8080/pose
FALL_ACCEL_G=2.2
FALL_INACTIVITY_SEC=10
FALL_DROP_FRAC=0.25
POSE_STILL_SEC=5
```

**Starting the dashboard:**
```bash
cd <repository-root>
source .venv/bin/activate
python ui/app.py
```

Access at `http://localhost:5000`

## MQTT Session Control

All components coordinate via the MQTT topic `climbbud/control/session`:

- **Voice/Camera Pi** publishes `START`/`STOP` when voice commands are detected
- **Dashboard button** publishes `START`/`STOP` on user click
- **Telemetry worker** subscribes and starts/stops health-stats database sessions
- **Dashboard** subscribes and starts/stops video + pose recording

**MQTT Broker:**
Install and run mosquitto on a machine accessible to all components (could be the dashboard machine, one of the Pis, or a dedicated server):

```bash
# Install mosquitto (Ubuntu/Debian)
sudo apt install mosquitto mosquitto-clients

# Start the service
sudo systemctl start mosquitto
sudo systemctl enable mosquitto
```

Ensure all components use the same `MQTT_BROKER_IP` and `MQTT_BROKER_PORT` in their respective `.env` files.

## Testing Locally (Single Machine)

For development without multiple Pis:

```bash
# Terminal 1: Simulated camera streamer
cd pose-landmarks
python src/camera_streamer.py --port 8081

# Terminal 2: Simulated pose tracker
cd pose-landmarks
python src/main.py --mock --inference opencv --camera-url http://localhost:8081/stream --stream

# Terminal 3: Dashboard
cd <repository-root>
python ui/app.py

# Terminal 4: Voice commands (optional - requires microphone)
cd voice-commands
python recorder_control.py
```

## Network Configuration

This section details the network setup for the QNX Pi and Subnet Pi (Voice/Camera Pi) to communicate via the `192.168.50.x` subnet with Tailscale access.

### On QNX Pi

Configure the network interface and routing:

```bash
# Step 1: Assign static IP to cgem0 interface
ifconfig cgem0 192.168.50.2 netmask 255.255.255.0 up

# Step 2: Set default route via Subnet Pi
route delete default
route add default 192.168.50.1
```

### On Subnet Pi (Voice/Camera Pi)

#### Step 1: Assign static IP to eth0

```bash
sudo nmcli con add type ethernet ifname eth0 con-name qnx-link ip4 192.168.50.1/24
sudo nmcli con up qnx-link
```

#### Step 2: Enable IP Forwarding & NAT (Internet Sharing)

```bash
# Enable IPv4 forwarding immediately and persistently
echo 'net.ipv4.ip_forward = 1' | sudo tee -a /etc/sysctl.d/99-tailscale.conf
sudo sysctl -p /etc/sysctl.d/99-tailscale.conf
```

#### Step 3: Set up NAT masquerading from eth0 to wlan0

```bash
sudo iptables -t nat -A POSTROUTING -o wlan0 -j MASQUERADE
sudo iptables -A FORWARD -i wlan0 -o eth0 -m state --state RELATED,ESTABLISHED -j ACCEPT
sudo iptables -A FORWARD -i eth0 -o wlan0 -j ACCEPT
```

#### Step 4: Expose the QNX Pi via Tailscale

```bash
sudo tailscale up --advertise-routes=192.168.50.0/24
```

**To undo subnet route advertisement:**
```bash
sudo tailscale set --advertise-routes=
```

### DNS Configuration for QNX via Tailscale

#### Step 1: Install dnsmasq on Subnet Pi

```bash
sudo apt update && sudo apt install -y dnsmasq
```

#### Step 2: Create dnsmasq config file

```bash
sudo nano /etc/dnsmasq.d/qnx-subnet.conf
```

#### Step 3: Add the following configuration (adjust interface names as needed)

```
interface=enxb827eb55b1fe
interface=tailscale0
listen-address=192.168.50.1
listen-address=100.x.y.z
bind-interfaces
no-resolv
server=8.8.8.8
server=1.1.1.1
local-service
address=/qnx-pi.lab.local/192.168.50.2
```

**Note:** Replace `enxb827eb55b1fe` with your actual ethernet interface name and `100.x.y.z` with your Tailscale IP address.

#### Step 4: Restart dnsmasq

```bash
sudo systemctl restart dnsmasq
```

After this setup, the QNX Pi can be accessed via the hostname `qnx-pi.lab.local` from devices connected via Tailscale.

## Hardware Connections

### Servo Wiring (QNX Pi)
QNX `rpi_gpio` only supports hardware PWM on specific pins:

| Servo | GPIO | Physical Pin |
|-------|------|--------------|
| Pan/Base | 12 | 32 |
| Tilt | 13 | 33 |
| Limit Switch | 26 | 37 |

Note: Ground must be shared between Pi and external power supply.

### Camera
- USB webcam connected to Camera Pi
- Webcam must be mounted on the pan/tilt gimbal

## Troubleshooting

**QNX Pi service not starting:**
- Check logs: `tail -f /data/home/qnxuser/ClimbBud/pose-landmarks/start.log`
- Verify GPIO resource manager: `pidin -p rpi_gpio`
- Check camera URL is reachable from QNX Pi

**Voice commands not working:**
- Verify MQTT broker is running and accessible
- Check `voice-commands/.env` has correct `MQTT_BROKER_IP`
- Ensure microphone is connected and permissions are set

**Dashboard can't connect to streams:**
- Verify `POSE_STREAM_HOST` is set to QNX Pi IP
- Check firewall allows ports 8080 (pose) and 8081 (camera)
- Test stream directly in browser: `http://<pi-ip>:8080/stream`

**Fall detection not triggering:**
- Ensure `POSE_URL` is set and reachable
- Check `/api/live` returns pose data
- Adjust thresholds in telemetry/.env (FALL_ACCEL_G, etc.)

## Authors

- [@Ghala-s](https://github.com/Ghala-s)
- [@braydenleung-Git](https://github.com/braydenleung-Git)
- [@RileyWong26](https://github.com/RileyWong26)
- [@heyshonen](https://github.com/heyshonen)

## License

See individual component directories for license information.
