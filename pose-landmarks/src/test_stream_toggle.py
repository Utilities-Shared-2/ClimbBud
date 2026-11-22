"""Tests for the /toggle route on the stream server."""
import json
import os
import sys
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from stream_server import FrameBroadcaster, start_stream_server, stop_stream_server  # noqa: E402


@pytest.fixture
def server():
    state = {"enabled": True, "calls": []}

    def on_toggle(s):
        state["calls"].append(s)
        state["enabled"] = (not state["enabled"]) if s is None else s
        return state["enabled"]

    srv = start_stream_server(FrameBroadcaster(), "127.0.0.1", 0, on_toggle=on_toggle)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield base, state
    stop_stream_server(srv)


def call(url, method="GET"):
    req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.loads(r.read())


def test_toggle_flips(server):
    base, state = server
    assert call(base + "/toggle", "POST") == {"enabled": False}
    assert call(base + "/toggle") == {"enabled": True}
    assert state["calls"] == [None, None]


def test_toggle_explicit_state(server):
    base, _ = server
    assert call(base + "/toggle?state=off") == {"enabled": False}
    assert call(base + "/toggle?state=off") == {"enabled": False}
    assert call(base + "/toggle?state=on", "POST") == {"enabled": True}


def test_toggle_bad_state(server):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError) as e:
        call(base + "/toggle?state=maybe")
    assert e.value.code == 400


def test_post_to_other_routes_rejected(server):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError) as e:
        call(base + "/pose", "POST")
    assert e.value.code == 405
