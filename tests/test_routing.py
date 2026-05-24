"""Pure-Python tests for the route math and simulator.

These tests run on any platform without an iPhone or pymobiledevice3 actually
talking to a device — the simulator's only dependency is an asyncio loop and
the async ``set_location`` callback we pass in.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from enigma.routing import (
    RouteSimulator,
    haversine,
    kmh_to_mps,
    mps_to_kmh,
    position_at_distance,
    segment_lengths,
    total_length,
)


def test_haversine_zero():
    assert haversine((0.0, 0.0), (0.0, 0.0)) == pytest.approx(0.0)


def test_haversine_known_distance():
    # Empire State Building -> Statue of Liberty, ~8.3 km
    a = (40.7484, -73.9857)
    b = (40.6892, -74.0445)
    d = haversine(a, b)
    assert 7800 < d < 8700  # generous bound, this is a known ~8.3 km hop


def test_segment_and_total_length():
    pts = [(0.0, 0.0), (0.0, 1.0), (0.0, 2.0)]
    segs = segment_lengths(pts)
    assert len(segs) == 2
    assert segs[0] == pytest.approx(segs[1], rel=1e-6)
    assert total_length(pts) == pytest.approx(segs[0] + segs[1])


def test_position_at_distance_start_and_end():
    pts = [(0.0, 0.0), (0.0, 1.0)]
    assert position_at_distance(pts, 0.0) == pts[0]
    # Far past the end — clamps to last waypoint
    end = position_at_distance(pts, 10_000_000)
    assert end[0] == pytest.approx(pts[1][0])
    assert end[1] == pytest.approx(pts[1][1])


def test_position_at_distance_midpoint():
    pts = [(0.0, 0.0), (0.0, 1.0)]
    total = total_length(pts)
    mid = position_at_distance(pts, total / 2)
    assert mid[0] == pytest.approx(0.0)
    assert mid[1] == pytest.approx(0.5, abs=1e-3)


def test_speed_unit_helpers():
    assert kmh_to_mps(3.6) == pytest.approx(1.0)
    assert mps_to_kmh(1.0) == pytest.approx(3.6)


# ---------------------------------------------------------------------------
# RouteSimulator
# ---------------------------------------------------------------------------


def _spawn_loop() -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    return loop, thread


def _stop_loop(loop: asyncio.AbstractEventLoop, thread: threading.Thread) -> None:
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=2)


def test_route_simulator_finishes_short_route():
    loop, thread = _spawn_loop()
    try:
        seen: list[tuple[float, float]] = []

        async def set_location(lat: float, lon: float) -> None:
            seen.append((lat, lon))

        sim = RouteSimulator(loop, set_location, tick_hz=20.0)

        # Tiny route — two points ~111 m apart at 0.001° latitude.
        waypoints = [(0.0, 0.0), (0.001, 0.0)]
        speed_mps = 50.0  # ~2.2s to traverse — well under test timeout

        asyncio.run_coroutine_threadsafe(
            sim.start(waypoints, speed_mps), loop
        ).result(timeout=5)

        # Wait for completion.
        deadline = time.monotonic() + 10
        while sim.is_running() and time.monotonic() < deadline:
            time.sleep(0.05)

        assert not sim.is_running()
        state = sim.state()
        assert state.finished is True
        assert state.current == pytest.approx(waypoints[-1])
        assert len(seen) >= 2
        # First point should be the start.
        assert seen[0] == pytest.approx(waypoints[0])
        # Last point should be the end.
        assert seen[-1] == pytest.approx(waypoints[-1])
    finally:
        _stop_loop(loop, thread)


def test_route_simulator_can_be_stopped_mid_route():
    loop, thread = _spawn_loop()
    try:
        async def set_location(lat: float, lon: float) -> None:
            pass

        sim = RouteSimulator(loop, set_location, tick_hz=5.0)
        # Big route that would take many seconds at this speed.
        waypoints = [(0.0, 0.0), (1.0, 0.0)]  # ~111 km
        asyncio.run_coroutine_threadsafe(
            sim.start(waypoints, 5.0), loop
        ).result(timeout=5)
        assert sim.is_running()

        # Stop quickly.
        asyncio.run_coroutine_threadsafe(sim.stop(), loop).result(timeout=5)
        assert not sim.is_running()
        assert sim.state().finished is False
    finally:
        _stop_loop(loop, thread)


def test_route_simulator_rejects_bad_inputs():
    loop, thread = _spawn_loop()
    try:
        async def set_location(lat: float, lon: float) -> None:
            pass

        sim = RouteSimulator(loop, set_location)
        with pytest.raises(ValueError):
            asyncio.run_coroutine_threadsafe(sim.start([(0.0, 0.0)], 1.0), loop).result(timeout=2)
        with pytest.raises(ValueError):
            asyncio.run_coroutine_threadsafe(
                sim.start([(0.0, 0.0), (0.0, 1.0)], 0.0), loop
            ).result(timeout=2)
    finally:
        _stop_loop(loop, thread)
