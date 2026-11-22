"""Fall detection from watch telemetry and camera pose samples.

The check_* functions are pure. FallMonitor combines them and keeps a latched
alert that stays active until it is dismissed.

Signals, in priority order:
  1. Impact      - watch fall_detected flag, or accel_g >= impact_accel_g.
  2. Pose drop   - camera body centre moves down sharply within drop_window_sec.
  3. Inactivity  - recording, watch acceleration at rest for inactivity_sec.
  4. Pose still  - camera body centre stationary for pose_still_sec.
Inactivity and pose stillness only alert after a preceding soft event (a
moderate acceleration spike or pose drop) unless inactivity_requires_event is
disabled, so a climber resting on a hold does not trigger them.

Thresholds can be overridden with FALL_* environment variables (see
FallConfig.from_env).
"""

from __future__ import annotations

import math
import os
import threading
import time
from dataclasses import dataclass, fields
from typing import Any, Callable, NamedTuple, Sequence

from telemetry.session_metrics import timestamp_epoch

REST_ACCEL_G = 1.0
SIMULATED_REASON = "Simulated fall (demo)"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", ""}


@dataclass(frozen=True)
class FallConfig:
    impact_accel_g: float = 2.2
    soft_accel_g: float = 1.6
    still_band_g: float = 0.05
    inactivity_sec: float = 10.0
    inactivity_requires_event: bool = True
    event_window_sec: float = 20.0
    stale_sec: float = 5.0
    max_sample_age_sec: float = 10.0
    drop_frac: float = 0.25
    soft_drop_frac: float = 0.10
    drop_window_sec: float = 1.0
    pose_still_sec: float = 5.0
    pose_still_band: float = 0.02
    cooldown_sec: float = 5.0
    simulate_enabled: bool = True

    @classmethod
    def from_env(cls) -> "FallConfig":
        env_names = {
            "impact_accel_g": "FALL_ACCEL_G",
            "inactivity_sec": "FALL_INACTIVITY_SEC",
            "drop_frac": "FALL_DROP_FRAC",
            "pose_still_sec": "POSE_STILL_SEC",
            "cooldown_sec": "FALL_COOLDOWN_SEC",
            "inactivity_requires_event": "FALL_INACTIVITY_REQUIRES_EVENT",
            "simulate_enabled": "FALL_SIMULATE_ENABLED",
        }
        defaults = cls()
        overrides: dict[str, Any] = {}
        for field in fields(cls):
            env_name = env_names.get(field.name)
            if env_name is None:
                continue
            default = getattr(defaults, field.name)
            if isinstance(default, bool):
                overrides[field.name] = _env_bool(env_name, default)
            else:
                overrides[field.name] = _env_float(env_name, default)
        return cls(**overrides)

    @property
    def pose_history_sec(self) -> float:
        """How much pose history the caller should retain for the checks."""
        return self.pose_still_sec + self.event_window_sec + self.drop_window_sec + 2.0


class PoseSample(NamedTuple):
    ts: float
    detected: bool
    y: float | None  # body centre as a fraction of frame height, 0 = top


def pose_sample_from_payload(payload: Any, now: float) -> PoseSample:
    """Convert the pose service /pose JSON into a PoseSample stamped with local time."""
    y = None
    if isinstance(payload, dict):
        try:
            y = float(payload["body"]["center"][1]) / float(payload["frame_shape"][0])
        except (TypeError, KeyError, IndexError, ValueError, ZeroDivisionError):
            y = None
    detected = isinstance(payload, dict) and bool(payload.get("detected")) and y is not None
    return PoseSample(now, detected, y if detected else None)


def _watch_points(history: Sequence[dict[str, Any]]) -> list[tuple[float, dict[str, Any]]]:
    points = []
    for point in history or ():
        ts = timestamp_epoch(point.get("time"))
        if ts is not None:
            points.append((ts, point))
    points.sort(key=lambda item: item[0])
    return points


def _accel(point: dict[str, Any]) -> float | None:
    value = point.get("accel_g")
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def check_impact(
    history: Sequence[dict[str, Any]],
    now: float,
    config: FallConfig,
    since_ts: float | None = None,
) -> tuple[str, float] | None:
    """Return (reason, sample_ts) for the newest impact sample newer than since_ts."""
    found = None
    for ts, point in _watch_points(history):
        if ts > now or ts < now - config.max_sample_age_sec or (since_ts is not None and ts <= since_ts):
            continue
        accel = _accel(point)
        if point.get("fall_detected"):
            found = ("Fall detected by watch", ts)
        elif accel is not None and accel >= config.impact_accel_g:
            found = (f"Impact detected ({accel:.1f} g)", ts)
    return found


def check_inactivity(
    history: Sequence[dict[str, Any]],
    now: float,
    recording: bool,
    config: FallConfig,
    ignore_events_before: float = 0.0,
) -> str | None:
    """Return a reason if the watch has been at rest for inactivity_sec while recording."""
    if not recording:
        return None
    points = [(ts, point) for ts, point in _watch_points(history) if ts <= now]
    if not points:
        return None

    window_start = now - config.inactivity_sec
    if points[0][0] > window_start or points[-1][0] < now - config.stale_sec:
        return None
    window = [(ts, point) for ts, point in points if ts >= window_start]
    if len(window) < 2 or window[0][0] - window_start > config.stale_sec:
        return None
    if any(b[0] - a[0] > config.stale_sec for a, b in zip(window, window[1:])):
        return None
    for _, point in window:
        accel = _accel(point)
        if accel is None or abs(accel - REST_ACCEL_G) > config.still_band_g:
            return None

    if config.inactivity_requires_event:
        event_start = max(window_start - config.event_window_sec, ignore_events_before)
        had_event = any(
            event_start < ts < window_start
            and (point.get("fall_detected") or (_accel(point) or 0.0) >= config.soft_accel_g)
            for ts, point in points
        )
        if not had_event:
            return None
    return f"No movement for {config.inactivity_sec:g} s after a possible fall"


def _continuous_pose(
    samples: Sequence[PoseSample], now: float, stale_sec: float
) -> list[PoseSample]:
    continuous: list[PoseSample] = []
    for sample in sorted((s for s in samples if s.ts <= now), key=lambda s: s.ts):
        if not sample.detected or sample.y is None:
            continuous.clear()
        else:
            if continuous and sample.ts - continuous[-1].ts > stale_sec:
                continuous.clear()
            continuous.append(sample)
    return continuous


def _max_drop(samples: Sequence[PoseSample], max_dt: float) -> float:
    """Largest downward move (y increases downward) between two samples within max_dt."""
    largest = 0.0
    for index, later in enumerate(samples):
        for earlier in samples[:index]:
            if later.ts - earlier.ts <= max_dt:
                largest = max(largest, later.y - earlier.y)
    return largest


def check_pose_drop(samples: Sequence[PoseSample], now: float, config: FallConfig) -> str | None:
    """Return a reason if the body centre dropped sharply within drop_window_sec."""
    recent = [
        s for s in _continuous_pose(samples, now, config.stale_sec)
        if s.ts >= now - config.drop_window_sec
    ]
    if _max_drop(recent, config.drop_window_sec) >= config.drop_frac:
        return "Sudden drop detected on camera"
    return None


def check_pose_stillness(
    samples: Sequence[PoseSample],
    now: float,
    config: FallConfig,
    ignore_events_before: float = 0.0,
) -> str | None:
    """Return a reason if the person has not moved for pose_still_sec after a soft drop."""
    detected = _continuous_pose(samples, now, config.stale_sec)
    window_start = now - config.pose_still_sec
    if not detected or detected[0].ts > window_start or detected[-1].ts < now - config.stale_sec:
        return None
    window = [s for s in detected if s.ts >= window_start]
    if len(window) < 2 or window[0].ts - window_start > config.stale_sec:
        return None
    ys = [s.y for s in window]
    if max(ys) - min(ys) > config.pose_still_band:
        return None

    if config.inactivity_requires_event:
        event_start = max(window_start - config.event_window_sec, ignore_events_before)
        before = [s for s in detected if event_start < s.ts <= window_start]
        if _max_drop(before, config.drop_window_sec) < config.soft_drop_frac:
            return None
    return f"Person not moving for {config.pose_still_sec:g} s on camera"


class FallMonitor:
    """Thread-safe latched fall alert fed by watch history and pose samples."""

    def __init__(
        self,
        config: FallConfig | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config or FallConfig.from_env()
        self._clock = clock
        self._lock = threading.Lock()
        self._active = False
        self._reason: str | None = None
        self._source: str | None = None
        self._triggered_at: float | None = None
        self._dismissed_at: float | None = None
        self._last_impact_ts: float | None = None

    def evaluate(
        self,
        history: Sequence[dict[str, Any]],
        recording: bool,
        pose_samples: Sequence[PoseSample] = (),
        now: float | None = None,
    ) -> dict[str, Any]:
        """Run the detectors and return the current alert state."""
        with self._lock:
            now = self._clock() if now is None else now
            config = self.config

            # Always consume impacts so a dismissed one cannot re-trigger after the cooldown.
            impact = check_impact(history, now, config, self._last_impact_ts)
            if impact is not None:
                self._last_impact_ts = impact[1]

            in_cooldown = (
                self._dismissed_at is not None
                and now - self._dismissed_at < config.cooldown_sec
            )
            if self._active or in_cooldown:
                return self._state()

            ignore_before = self._triggered_at or 0.0
            new_pose_samples = [
                sample for sample in pose_samples
                if self._triggered_at is None or sample.ts > self._triggered_at
            ]
            detections = (
                ("watch", impact[0] if impact else None),
                ("pose", check_pose_drop(new_pose_samples, now, config)),
                ("watch", check_inactivity(history, now, recording, config, ignore_before)),
                ("pose", check_pose_stillness(pose_samples, now, config, ignore_before)),
            )
            for source, reason in detections:
                if reason:
                    self._trigger(source, reason, now)
                    break
            return self._state()

    def simulate(self, now: float | None = None) -> bool:
        """Trigger a demo alert. Returns False when simulation is disabled."""
        if not self.config.simulate_enabled:
            return False
        with self._lock:
            self._trigger("simulated", SIMULATED_REASON, self._clock() if now is None else now)
        return True

    def dismiss(self, now: float | None = None) -> dict[str, Any]:
        with self._lock:
            if self._active:
                self._active = False
                self._dismissed_at = self._clock() if now is None else now
            return self._state()

    def state(self) -> dict[str, Any]:
        with self._lock:
            return self._state()

    def _trigger(self, source: str, reason: str, now: float) -> None:
        self._active = True
        self._source = source
        self._reason = reason
        self._triggered_at = now

    def _state(self) -> dict[str, Any]:
        return {
            "active": self._active,
            "reason": self._reason if self._active else None,
            "source": self._source if self._active else None,
            "triggered_at": self._triggered_at if self._active else None,
            "simulate_enabled": self.config.simulate_enabled,
        }
