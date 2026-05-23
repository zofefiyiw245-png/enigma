"""Route math and route simulator.

This module is intentionally framework-agnostic: it has no dependency on
pymobiledevice3 or Flask. It exposes pure helpers for great-circle distance
and polyline interpolation, plus a :class:`RouteSimulator` that drives an
``async def set_location(lat, lon)`` callback at a configurable tick rate.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, Sequence

logger = logging.getLogger(__name__)

EARTH_RADIUS_M = 6_371_008.8  # mean Earth radius in metres


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def haversine(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    """Great-circle distance between two ``(lat, lon)`` points in metres."""
    lat1, lon1 = map(math.radians, p1)
    lat2, lon2 = map(math.radians, p2)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def segment_lengths(waypoints: Sequence[tuple[float, float]]) -> list[float]:
    """Length in metres of each segment in a polyline of waypoints."""
    return [haversine(waypoints[i], waypoints[i + 1]) for i in range(len(waypoints) - 1)]


def total_length(waypoints: Sequence[tuple[float, float]]) -> float:
    return sum(segment_lengths(waypoints))


def position_at_distance(
    waypoints: Sequence[tuple[float, float]],
    distance_m: float,
) -> tuple[float, float]:
    """Position along the polyline at ``distance_m`` from the start.

    Uses linear interpolation in ``(lat, lon)`` space, which is fine for the
    typical city-block distances exercised by a GPS-simulation route. Beyond
    the end of the polyline, returns the final waypoint.
    """
    if not waypoints:
        raise ValueError("waypoints must not be empty")
    if len(waypoints) == 1 or distance_m <= 0:
        return waypoints[0]

    remaining = distance_m
    for i in range(len(waypoints) - 1):
        a = waypoints[i]
        b = waypoints[i + 1]
        seg = haversine(a, b)
        if remaining <= seg or i == len(waypoints) - 2:
            if seg == 0:
                return b
            t = min(1.0, remaining / seg)
            lat = a[0] + (b[0] - a[0]) * t
            lon = a[1] + (b[1] - a[1]) * t
            return (lat, lon)
        remaining -= seg

    return waypoints[-1]


# ---------------------------------------------------------------------------
# Speed presets — metres per second
# ---------------------------------------------------------------------------

SPEED_PRESETS_MPS: dict[str, float] = {
    "walking": 1.4,        # ~5 km/h
    "running": 3.0,        # ~10.8 km/h
    "cycling": 5.5,        # ~20 km/h
    "driving_city": 13.9,  # ~50 km/h
    "driving_hwy": 27.8,   # ~100 km/h
}


def kmh_to_mps(kmh: float) -> float:
    return kmh / 3.6


def mps_to_kmh(mps: float) -> float:
    return mps * 3.6


# ---------------------------------------------------------------------------
# Route simulator
# ---------------------------------------------------------------------------


SetLocationCallback = Callable[[float, float], Awaitable[None]]


@dataclass
class RouteState:
    """Snapshot of the simulator state returned to clients."""

    running: bool = False
    waypoints: list[tuple[float, float]] = field(default_factory=list)
    speed_mps: float = 0.0
    loop: bool = False
    started_at: Optional[float] = None
    elapsed_s: float = 0.0
    total_distance_m: float = 0.0
    travelled_m: float = 0.0
    current: Optional[tuple[float, float]] = None
    finished: bool = False

    def as_dict(self) -> dict:
        return {
            "running": self.running,
            "waypoints": [list(p) for p in self.waypoints],
            "speedMps": self.speed_mps,
            "speedKmh": mps_to_kmh(self.speed_mps),
            "loop": self.loop,
            "elapsedS": self.elapsed_s,
            "totalDistanceM": self.total_distance_m,
            "travelledM": self.travelled_m,
            "current": list(self.current) if self.current else None,
            "finished": self.finished,
        }


class RouteSimulator:
    """Drives a route along a polyline by calling an async location setter.

    The simulator owns a single :class:`asyncio.Task` running on the supplied
    event loop. Calls to :meth:`start` are mutually exclusive with an
    already-running route; the caller is expected to :meth:`stop` first.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        set_location: SetLocationCallback,
        tick_hz: float = 1.0,
    ):
        if tick_hz <= 0:
            raise ValueError("tick_hz must be > 0")
        self._loop = loop
        self._set_location = set_location
        self._tick_interval = 1.0 / tick_hz
        self._task: Optional[asyncio.Task] = None
        self._state = RouteState()
        self._stop_event: Optional[asyncio.Event] = None

    # -- lifecycle ---------------------------------------------------------

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def state(self) -> RouteState:
        return self._state

    async def start(
        self,
        waypoints: Sequence[tuple[float, float]],
        speed_mps: float,
        loop_route: bool = False,
    ) -> None:
        if self.is_running():
            raise RuntimeError("route simulation already running")
        if len(waypoints) < 2:
            raise ValueError("at least two waypoints are required")
        if speed_mps <= 0:
            raise ValueError("speed must be > 0")

        wp = [(float(a), float(b)) for a, b in waypoints]
        total = total_length(wp)
        if total <= 0:
            raise ValueError("waypoints have zero total length")

        # Apply the first point synchronously so any underlying error (e.g.
        # device disconnected) surfaces to the caller before we spin a task.
        await self._set_location(*wp[0])

        self._stop_event = asyncio.Event()
        self._state = RouteState(
            running=True,
            waypoints=wp,
            speed_mps=float(speed_mps),
            loop=bool(loop_route),
            started_at=time.monotonic(),
            total_distance_m=total,
            current=wp[0],
        )
        self._task = self._loop.create_task(self._run(wp, float(speed_mps), bool(loop_route)))

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        task = self._task
        if task is not None and not task.done():
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._state.running = False

    # -- internals ---------------------------------------------------------

    async def _run(
        self,
        waypoints: list[tuple[float, float]],
        speed_mps: float,
        loop_route: bool,
    ) -> None:
        total = total_length(waypoints)
        assert self._stop_event is not None
        stop_event = self._stop_event

        # Initial position was already set by ``start()``; start the clock here.
        start = time.monotonic()
        try:
            while not stop_event.is_set():
                now = time.monotonic()
                elapsed = now - start
                travelled = elapsed * speed_mps

                if travelled >= total:
                    if loop_route:
                        # Restart cleanly so the path replays from the beginning.
                        start = now
                        travelled = 0.0
                    else:
                        # Snap to end and mark finished.
                        final = waypoints[-1]
                        self._state.elapsed_s = elapsed
                        self._state.travelled_m = total
                        self._state.current = final
                        self._state.finished = True
                        try:
                            await self._set_location(*final)
                        except Exception:  # noqa: BLE001
                            logger.exception("final set_location failed")
                        break

                pos = position_at_distance(waypoints, travelled)
                self._state.elapsed_s = elapsed
                self._state.travelled_m = travelled
                self._state.current = pos

                try:
                    await self._set_location(*pos)
                except Exception:  # noqa: BLE001
                    logger.exception("tick set_location failed")

                # Sleep until next tick, but wake early on stop.
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self._tick_interval)
                    # event fired — exit loop
                    break
                except asyncio.TimeoutError:
                    continue
        finally:
            self._state.running = False
