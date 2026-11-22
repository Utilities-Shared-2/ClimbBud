import cv2
import time
import signal
import argparse
import os
import threading


from recognition import PoseDetector
from camera import open_camera, IS_QNX
import utils
import framebox as app
from stream_server import FrameBroadcaster, start_stream_server, stop_stream_server

io = None
running = True
args = None
# Set = model + servos active. Flipped by the /toggle HTTP route; the main loop
# reacts to changes so all GPIO calls stay on the main thread.
tracking_enabled = threading.Event()
tracking_enabled.set()
_toggle_lock = threading.Lock()


def toggle_tracking(state=None):
    """Set tracking on (True), off (False) or flip it (None). Returns the new state."""
    with _toggle_lock:
        if state is None:
            state = not tracking_enabled.is_set()
        if state:
            tracking_enabled.set()
        else:
            tracking_enabled.clear()
        return state
FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "model", "public",
                    "yolov11n", "yolo11n-pose.onnx")

def signal_handler(signum, frame):
    """Handle shutdown signals gracefully."""
    global running
    print("\n[INFO] Shutdown signal received, cleaning up...")
    running = False

# Register signal handlers for graceful shutdown
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

class PIDController:
    def __init__(self, kp, ki, kd, MAX_PIXEL_ERROR=None, output_limits=1, d_alpha=0.3):
        # MAX_PIXEL_ERROR is set per frame in center() to half the frame size on that axis
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.output_limits = output_limits
        # Low-pass coefficient for the derivative term (0 = ignore D, 1 = no filtering)
        self.d_alpha = d_alpha
        self.MAX_PIXEL_ERROR = MAX_PIXEL_ERROR
        self.reset()

    def reset(self):
        self._prev_error = 0
        self._integral = 0
        self._derivative = 0
        self._last_time = time.time()

    def compute(self, setpoint, measured_value):
        #Calculate time delta
        current_time = time.time()
        dt = current_time - self._last_time
        if dt <= 0:
            dt = 0.02
        #Calculate error
        raw_error = setpoint - measured_value
        #Normalize error
        """
        Note :
        - We need to normalized because the error is delta in pixels, meaning each delta is on the magnitude few 100.
        - we normalize by dividing the error with the half of the resolution in respect to the axis.
        """
        normalized_error = max(-1.0, min(1.0, raw_error / self.MAX_PIXEL_ERROR))

        self._integral += normalized_error * dt
        # Filtered derivative: dampens spikes when dt is small/erratic (frame stalls, pauses)
        raw_derivative = (normalized_error - self._prev_error) / dt
        self._derivative += self.d_alpha * (raw_derivative - self._derivative)

        output = (self.kp * normalized_error) + (self.ki * self._integral) + (self.kd * self._derivative)
        print(f"output:{output}")
        output = output*self.output_limits
        if abs(output) > 0.099:
            #clamps the movement limit to roughly > 0.1
            self._prev_error = normalized_error
        else:
            output = 0
        self._last_time = current_time
        return output

# Initialize PID controllers for pan (x-axis) and tilt (y-axis)
# kp: proportional gain, ki: integral gain, kd: derivative gain
# These values may need tuning based on your specific servo characteristics
pan_pid = PIDController(kp=0.65, ki=0.001, kd=0.5, output_limits=15)
tilt_pid = PIDController(kp=0.5, ki=0.001, kd=0.3, output_limits=10)

def pick_target(person_kpts, box, kpt_thr, frame_shape):
    """
    Choose the point the servos should centre in the frame.
    Order: body centre (torso visible) -> head -> body centre from any keypoints / box.
    :return: (target (x, y), source str, body dict, head point or None)
    """
    body = PoseDetector.body_position(person_kpts, box, kpt_thr, frame_shape)
    head = PoseDetector.head_point(person_kpts, kpt_thr)
    if body is not None and body["source"] == "torso":
        return body["center"], "body", body, head
    if head is not None:
        return head, "head", body, head
    if body is not None:
        return body["center"], f"body ({body['source']})", body, head
    return None, None, body, head

#target point and the size of the frame
def center(midpoint, size):
    """
    How it works:
    - calculate the midpoint of the face box
    - calculate the midpoint of the frame
    - calculate the delta between the midpoint of the face box and the midpoint of the frame
    - runs it to PIDController.compute() to get the change in degree needed for the robot
    :param midpoint: array of the mid-point of the rectangle
    :param size: pixel coordinate of the face box
    """
    x_mid = size[1]//2
    y_mid = size[0]//2
    # Normalise the error by half the frame size, so the frame edge = +/-1 for any resolution
    pan_pid.MAX_PIXEL_ERROR = x_mid
    tilt_pid.MAX_PIXEL_ERROR = y_mid

    x = midpoint[0]
    y = midpoint[1]
    
    # print(f"Center X delta: {x_mid - x}")
    # print(f"Center Y delta: {y_mid - y}")
    # Calculate PID outputs for pan (x-axis) and tilt (y-axis)
    pan_output = pan_pid.compute(x_mid, x)
    tilt_output = tilt_pid.compute(y_mid, y)

    # Apply servo adjustments based on PID output
    if pan_output > 0:
        io.rotate_left(inc=abs(pan_output))
        print(f"rotate left: {abs(pan_output):.2f}")
    else:
        io.rotate_right(inc=abs(pan_output))
        print(f"rotate right: {pan_output:.2f}")

    if tilt_output > 0:
        io.rotate_up(inc=abs(tilt_output))
        print(f"rotate up: {tilt_output:.2f}")
    else:
        io.rotate_down(inc=abs(tilt_output))
        print(f"rotate down: {abs(tilt_output):.2f}")

def render_overlay(frame, scale, rectangle, fps_avg, target, target_src, body, simulated):
    """
    Scale + mirror a copy of the (already pose-annotated) frame and add the text overlay.
    Used for both the local preview window and the network stream.
    """
    if scale != 1:
        frame = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)
    # Mirror the output only (after tracking) so servo direction is unaffected
    frame = cv2.flip(frame, 1)
    if rectangle:
        frame = utils.put_text_on_image(frame, position=(10, 150),
            text=f"target {target} [{target_src}]")
        frame = utils.put_text_on_image(frame, position=(10,50), text='FPS: {:.2f}'.format( fps_avg ))
        frame = utils.put_text_on_image(frame, position=(10, 200),
            text=f"body {body['center']} ({body['source']}) norm {body['normalized']}")
    if simulated:
        frame = utils.put_text_on_image(frame, position=(10, 100),
            text=f"pan {io.get_base_angle():.1f}  tilt {io.get_camera_angle():.1f}", color=(0, 255, 255))
    return frame


def to_jsonable(value):
    """Convert numpy scalars / tuples nested in dicts into plain JSON types."""
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if hasattr(value, "item"):
        return value.item()
    return value


def load_io(backend):
    """Import the IoControl class for a servo backend."""
    if backend == 'qnx':
        from io_control_qnx import IoControl
    elif backend == 'pigpio':
        from io_control_pigio import IoControl
    else:
        from io_control_mock import IoControl
    return IoControl


def main():
    global io, args
    parser = argparse.ArgumentParser(description='Head tracking (YOLO11n-pose) with servo control')
    parser.add_argument('--headless', action='store_true', help='Run without displaying video feed')
    parser.add_argument('--io', choices=['auto', 'qnx', 'pigpio', 'mock'], default='auto',
                        help='Servo backend: qnx (rpi_gpio on QNX), pigpio (Raspberry Pi OS) or mock. '
                             'auto = qnx on QNX, else pigpio, falling back to mock')
    parser.add_argument('--mock', action='store_true', help='Shortcut for --io mock (simulated servos)')
    parser.add_argument('--camera-url', default=None,
                        help='Pull frames from an MJPEG stream instead of a local webcam, e.g. '
                             'http://camera-pi.local:8081/stream (served by camera_streamer.py). Required on QNX')
    parser.add_argument('--camera-backend', choices=['auto', 'opencv', 'mjpeg'], default='auto',
                        help='auto = mjpeg when --camera-url is given, else the local webcam (cv2.VideoCapture)')
    parser.add_argument('--inference', choices=['auto', 'openvino', 'opencv'], default='auto',
                        help='Pose model runtime; auto = OpenVINO if installed (not on QNX), else OpenCV DNN')
    parser.add_argument('--display-scale', type=float, default=2.0,
                        help='Zoom factor for the preview window (display only, default 2.0)')
    parser.add_argument('--no-pose', action='store_true', help='Hide the body pose landmarks')
    parser.add_argument('--camera', type=int, default=0, help='Webcam index (default 0)')
    parser.add_argument('--max-fps', type=float, default=None,
                        help='Cap the loop rate. Defaults to 8 in --mock to match a Pi 3; the PID gains '
                             'were tuned at that speed and oscillate at desktop frame rates')
    parser.add_argument('--stream', action='store_true',
                        help='Serve the annotated feed over HTTP (MJPEG) for viewing from another device')
    parser.add_argument('--stream-host', default='0.0.0.0', help='Stream bind address (default 0.0.0.0)')
    parser.add_argument('--stream-port', type=int, default=8080, help='Stream port (default 8080)')
    parser.add_argument('--stream-quality', type=int, default=70, help='Stream JPEG quality 1-100 (default 70)')
    parser.add_argument('--stream-scale', type=float, default=1.0,
                        help='Resize factor for the streamed frames (default 1.0)')
    args = parser.parse_args()

    io_backend = 'mock' if args.mock else args.io
    if io_backend == 'auto':
        io_backend = 'qnx' if IS_QNX else 'pigpio'
        try:
            io = load_io(io_backend)()
        except Exception as e:  # missing module, or no daemon / GPIO access
            print(f"[INFO] {io_backend} servos not available ({e}), falling back to simulated servos (--mock)")
            io_backend = 'mock'
            io = load_io(io_backend)()
    else:
        io = load_io(io_backend)()
    simulated = hasattr(io, "view")
    max_fps = args.max_fps if args.max_fps else (8.0 if simulated else None)

    cap = open_camera(args.camera, args.camera_backend, url=args.camera_url)
    if not cap.isOpened():
        print(f"[ERROR] Could not open camera {args.camera}")
        return

    detector = PoseDetector(model=FILE, confidence_thr=0.5, overlap_thr=0.45, backend=args.inference)
    print(f"[INFO] backends: servos={io_backend} camera={cap.backend} inference={detector.backend}")

    broadcaster = stream = None
    if args.stream:
        broadcaster = FrameBroadcaster(quality=args.stream_quality)
        stream = start_stream_server(broadcaster, args.stream_host, args.stream_port,
                                     on_toggle=toggle_tracking)

    if not args.headless:
        # WINDOW_NORMAL lets you drag-resize the preview as well
        cv2.namedWindow('frame', cv2.WINDOW_NORMAL)
    
    n_frames = 0
    fps_cum = 0.0
    last_read_warn = 0.0
    was_enabled = True
    # fps_avg = 0.0

    while running:
        enabled = tracking_enabled.is_set()
        if enabled != was_enabled:
            was_enabled = enabled
            if enabled:
                io.center()
                pan_pid.reset()
                tilt_pid.reset()
                print("[INFO] tracking resumed")
            else:
                io.release()
                print("[INFO] tracking paused (model + servos off)")
                if broadcaster is not None:
                    broadcaster.publish_data({"timestamp": time.time(), "enabled": False,
                                              "detected": False})
        if not enabled:
            if not args.headless:
                k = cv2.waitKey(50) & 0xFF
                if k == 27 or k == ord('q'):
                    break
            else:
                time.sleep(0.05)
            continue

        io.read_switch()
        if not io.resetToggled:
            loop_start = time.perf_counter()
            ret, frame=cap.read()
            if not ret:
                # MjpegCamera already logs disconnects; only nag every 5s here
                if time.time() - last_read_warn > 5:
                    print("[WARN] no camera frame, retrying")
                    last_read_warn = time.time()
                time.sleep(0.2)
                continue
            if simulated:
                # Crop the frame to what the virtual pan/tilt camera would see
                frame = io.view(frame)
            start_time = time.perf_counter()
            bboxes, scores, kpts = detector.inference(frame)
            end_time = time.perf_counter()

            n_frames += 1
            fps = 1.0 / (end_time - start_time)
            fps_cum += fps
            fps_avg = fps_cum / n_frames
            if fps_cum > 5000:
                fps_cum = 0
                n_frames = 0

            # Process face detection regardless of headless mode
            frame, rectangle, idx = app.draw_largest_box(frame, bboxes, scores)
            #if the rectangle has nothing, this will be skipped
            if rectangle:
                # Centre the body (torso); fall back to the head when the torso isn't visible
                target, target_src, body, head = pick_target(kpts[idx], bboxes[idx],
                                                             detector.kpt_thr, frame.shape[:2])
                tracking_body = target_src.startswith("body")
                # Green cross = body centre, red dot = head; the tracked one is drawn larger
                cv2.drawMarker(frame, body["center"], (0, 255, 0), cv2.MARKER_CROSS,
                               22 if tracking_body else 12, 3 if tracking_body else 1)
                if head is not None:
                    cv2.circle(frame, head, 4 if tracking_body else 7, (0, 0, 255), -1)
                center(target, frame.shape[:2])

            if not rectangle:
                target = target_src = body = None

            if broadcaster is not None:
                broadcaster.publish_data(to_jsonable({
                    "timestamp": time.time(),
                    "enabled": True,
                    "fps": fps_avg,
                    "frame_shape": frame.shape[:2],
                    "detected": bool(rectangle),
                    "target": target,
                    "target_source": target_src,
                    "body": body,
                    "keypoints": PoseDetector.keypoint_positions(kpts[idx], detector.kpt_thr)
                                 if rectangle else None,
                    "pan": io.get_base_angle(),
                    "tilt": io.get_camera_angle(),
                }))

            show_window = not args.headless
            streaming = broadcaster is not None and broadcaster.has_clients
            if show_window or streaming:
                if not args.no_pose:
                    app.draw_pose(frame, kpts, detector.kpt_thr, highlight=idx)
                overlay = (rectangle, fps_avg, target, target_src, body, simulated)
                if streaming:
                    broadcaster.publish(render_overlay(frame, args.stream_scale, *overlay))

            if show_window:
                # Enlarge the preview only (tracking above used the original frame)
                frame = render_overlay(frame, args.display_scale, *overlay)
                cv2.imshow('frame', frame)
                k = cv2.waitKey(1) & 0xFF#copied straight from another repo. not sure why they used 0xff. maybe to extract just the ascii bytes
                if k == 27 or k == ord('q'):
                    break
            else:
                # In headless mode, just a small delay to prevent CPU saturation
                # Use Ctrl+C to exit (handled by signal handler)
                time.sleep(0.001)
            if max_fps:
                remaining = (1.0 / max_fps) - (time.perf_counter() - loop_start)
                if remaining > 0:
                    time.sleep(remaining)
    if stream is not None:
        print("[INFO] Stopping stream server...")
        stop_stream_server(stream)
    io.center()
    # Cleanup resources
    print("[INFO] Releasing camera...")
    cap.release()
    if not args.headless:
        print("[INFO] Closing windows...")
        cv2.destroyAllWindows()
    print("[INFO] Stopping servos and GPIO...")
    io.stop_all()
    print("[INFO] Shutdown complete.")



if __name__ == "__main__":
    main()
