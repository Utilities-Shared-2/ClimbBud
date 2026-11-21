import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import numpy as np
from io_control_mock import IoControl

def make_io():
    return IoControl(verbose=False)

def test_starts_centered():
    io = make_io()
    assert io.get_base_angle() == 135 and io.get_camera_angle() == 135
    assert io.resetToggled is False

def test_limits_clamp_like_real_hardware():
    io = make_io()
    io.rotate_right(inc=1000); assert io.get_base_angle() == io.PAN_MAX
    io.rotate_left(inc=1000);  assert io.get_base_angle() == io.PAN_MIN
    io.rotate_up(inc=1000);    assert io.get_camera_angle() == io.PITCH_MAX
    io.rotate_down(inc=1000);  assert io.get_camera_angle() == io.PITCH_MIN

def _frame_with_dot(x, y, w=640, h=480):
    f = np.zeros((h, w, 3), np.uint8)
    f[y-3:y+3, x-3:x+3] = 255
    return f

def _dot_pos(view):
    ys, xs = np.nonzero(view[:, :, 0])
    return xs.mean(), ys.mean()

def test_view_is_centered_crop_at_rest():
    io = make_io()
    v = io.view(_frame_with_dot(320, 240))
    x, y = _dot_pos(v)
    assert abs(x - v.shape[1] / 2) < 4 and abs(y - v.shape[0] / 2) < 4

def test_rotate_left_moves_view_toward_left_side():
    # main.py calls rotate_left when the face is LEFT of center,
    # so the virtual camera must pan left, bringing the face toward center
    io = make_io()
    f = _frame_with_dot(250, 240)
    x0, _ = _dot_pos(io.view(f))
    io.rotate_left(inc=3)
    x1, _ = _dot_pos(io.view(f))
    assert x1 > x0

def test_rotate_up_moves_view_toward_top():
    # main.py calls rotate_up when the face is ABOVE center
    io = make_io()
    f = _frame_with_dot(320, 200)
    _, y0 = _dot_pos(io.view(f))
    io.rotate_up(inc=3)
    _, y1 = _dot_pos(io.view(f))
    assert y1 > y0

def test_view_never_leaves_frame():
    io = make_io()
    f = _frame_with_dot(320, 240)
    io.rotate_right(inc=1000); io.rotate_up(inc=1000)
    v = io.view(f)
    assert v.shape == io.view(_frame_with_dot(320, 240)).shape