"""Pure helpers for grouping watch telemetry and calculating session metrics."""

import math
from datetime import datetime, timezone
from typing import Any


def timestamp_epoch(value: Any) -> float | None:
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def _session_metrics(points: list[dict[str, Any]], session_id: Any = None) -> dict[str, Any]:
    points.sort(key=lambda point: point["ts"])
    first_ts = points[0]["ts"]
    last_ts = points[-1]["ts"]

    bpm_values = [
        point["bpm"]
        for point in points
        if isinstance(point.get("bpm"), (int, float)) and point["bpm"] > 0
    ]
    stable_samples = sum(
        1
        for point in points
        if isinstance(point.get("accel_g"), (int, float))
        and 0.85 <= point["accel_g"] <= 1.20
    )

    hold_shifts = 0
    previous_xyz = None
    for point in points:
        xyz = point.get("xyz")
        if not (
            isinstance(xyz, list)
            and len(xyz) == 3
            and all(isinstance(value, (int, float)) for value in xyz)
        ):
            continue
        if previous_xyz is not None:
            distance = math.sqrt(sum((a - b) ** 2 for a, b in zip(xyz, previous_xyz)))
            if distance >= 250:
                hold_shifts += 1
        previous_xyz = xyz

    accel_values = [
        float(point["accel_g"])
        for point in points
        if isinstance(point.get("accel_g"), (int, float))
    ]
    accel_spikes = [
        float(point["accel_g"])
        for point in points
        if isinstance(point.get("accel_g"), (int, float))
        and not 0.85 <= point["accel_g"] <= 1.20
    ]
    key = (
        f"session-{session_id}"
        if session_id is not None
        else f"gap-{round(first_ts * 1000)}"
    )
    return {
        "id": key,
        "session_id": session_id,
        "started_at": datetime.fromtimestamp(first_ts, timezone.utc).isoformat(),
        "ended_at": datetime.fromtimestamp(last_ts, timezone.utc).isoformat(),
        "duration_sec": round(max(0.0, last_ts - first_ts), 1),
        "avg_bpm": round(sum(bpm_values) / len(bpm_values), 1) if bpm_values else None,
        "peak_bpm": max(bpm_values) if bpm_values else None,
        "hold_shifts": hold_shifts,
        "stability_pct": round(100 * stable_samples / len(points), 1),
        "sample_count": len(points),
        "accel_spike_count": len(accel_spikes),
        "peak_accel_g": round(max(accel_values), 3) if accel_values else None,
        "accel_spikes": accel_spikes[:20],
        "_points": points,
    }


def build_sessions(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group rows by explicit session ID, then split unassigned rows at 60s gaps."""
    sessions_by_id: dict[Any, list[dict[str, Any]]] = {}
    unassigned: list[dict[str, Any]] = []

    for point in points:
        ts = timestamp_epoch(point.get("ts"))
        if ts is None:
            continue
        normalized = dict(point)
        normalized["ts"] = ts
        try:
            session_id = int(point["session_id"]) if point.get("session_id") is not None else None
        except (TypeError, ValueError):
            session_id = None
        normalized["session_id"] = session_id
        if session_id is None:
            unassigned.append(normalized)
        else:
            sessions_by_id.setdefault(session_id, []).append(normalized)

    sessions = [
        _session_metrics(group, session_id)
        for session_id, group in sessions_by_id.items()
        if group
    ]

    unassigned.sort(key=lambda point: point["ts"])
    current: list[dict[str, Any]] = []
    for point in unassigned:
        if current and point["ts"] - current[-1]["ts"] > 60:
            sessions.append(_session_metrics(current))
            current = []
        current.append(point)
    if current:
        sessions.append(_session_metrics(current))

    sessions.sort(key=lambda session: session["started_at"], reverse=True)
    return sessions
