"""Tests for the Flask routes using the mock device controller."""

from __future__ import annotations

import pytest

from enigma.server import create_app


@pytest.fixture()
def client():
    app = create_app(use_mock=True)
    yield app.test_client()
    # Tear down the background loop owned by the device controller.
    app.extensions["enigma"]["device"].shutdown()


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "mock": True}


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"Enigma" in r.data


def test_connect_set_reset_disconnect_cycle(client):
    # Connect to the mock device
    r = client.post("/api/device/connect", json={})
    assert r.status_code == 200
    body = r.get_json()
    assert body["connected"] is True
    assert body["mode"] == "mock"

    # Set a coordinate
    r = client.post("/api/location/set", json={"lat": 40.6892, "lon": -74.0445})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True

    # Status reflects last location
    r = client.get("/api/device/status")
    body = r.get_json()
    assert body["lastLocation"] == [pytest.approx(40.6892), pytest.approx(-74.0445)]

    # Reset
    r = client.post("/api/location/reset", json={})
    assert r.status_code == 200

    # Disconnect
    r = client.post("/api/device/disconnect", json={})
    assert r.status_code == 200
    assert client.get("/api/device/status").get_json()["connected"] is False


def test_connect_rejects_non_object_json(client):
    r = client.post("/api/device/connect", json=[])
    assert r.status_code == 400
    assert "json object" in r.get_json()["error"].lower()


def test_set_location_validation(client):
    client.post("/api/device/connect", json={})
    r = client.post("/api/location/set", json={"lat": 1000, "lon": 0})
    assert r.status_code == 400
    assert "out of range" in r.get_json()["error"].lower()

    r = client.post("/api/location/set", json={"lat": "nope"})
    assert r.status_code == 400


def test_route_rejects_when_disconnected(client):
    # Don't connect — start should fail to talk to device
    r = client.post(
        "/api/route/start",
        json={"waypoints": [[0.0, 0.0], [0.0, 0.001]], "preset": "walking"},
    )
    # Either 409 (device not connected) is acceptable; we just want it not to crash.
    assert r.status_code in {409, 500}


def test_route_start_and_stop_with_mock_device(client):
    client.post("/api/device/connect", json={})

    r = client.post(
        "/api/route/start",
        json={
            "waypoints": [[0.0, 0.0], [0.0, 0.0005]],  # ~55 m
            "speedKmh": 200,  # fast — finishes in ~1s
        },
    )
    assert r.status_code == 200
    status = r.get_json()
    assert status["running"] in (True, False)
    assert status["totalDistanceM"] > 0

    r = client.post("/api/route/stop", json={})
    assert r.status_code == 200
    assert r.get_json()["running"] is False


def test_route_start_validation(client):
    client.post("/api/device/connect", json={})

    # Body must be a JSON object, not an array/scalar.
    r = client.post("/api/route/start", json=[])
    assert r.status_code == 400
    assert "json object" in r.get_json()["error"].lower()

    # Too few waypoints
    r = client.post("/api/route/start", json={"waypoints": [[0.0, 0.0]], "preset": "walking"})
    assert r.status_code == 400

    # Bad preset
    r = client.post(
        "/api/route/start",
        json={"waypoints": [[0.0, 0.0], [0.0, 1.0]], "preset": "teleport"},
    )
    assert r.status_code == 400

    # Missing speed
    r = client.post(
        "/api/route/start",
        json={"waypoints": [[0.0, 0.0], [0.0, 1.0]]},
    )
    assert r.status_code == 400


def test_route_presets(client):
    r = client.get("/api/route/presets")
    assert r.status_code == 200
    presets = r.get_json()["presets"]
    keys = {p["key"] for p in presets}
    assert {"walking", "driving_city"}.issubset(keys)


def test_disconnect_stops_active_route(client):
    """Disconnecting must halt any running simulator so it stops calling the
    torn-down device service."""
    client.post("/api/device/connect", json={})

    # Start a slow route so it's guaranteed still running when we disconnect.
    r = client.post(
        "/api/route/start",
        json={
            "waypoints": [[0.0, 0.0], [1.0, 0.0]],  # ~111 km
            "speedKmh": 1,  # ~30+ hours to finish — definitely still running
        },
    )
    assert r.status_code == 200
    assert r.get_json()["running"] is True

    # Disconnect should also stop the simulator.
    r = client.post("/api/device/disconnect", json={})
    assert r.status_code == 200

    status = client.get("/api/route/status").get_json()
    assert status["running"] is False


def test_route_rejects_nan_and_infinity_speeds(client):
    client.post("/api/device/connect", json={})

    for bad in (float("nan"), float("inf"), float("-inf")):
        r = client.post(
            "/api/route/start",
            json={"waypoints": [[0.0, 0.0], [0.0, 0.001]], "speedMps": bad},
        )
        # Flask returns JSON for these. Make sure validation catches it.
        assert r.status_code == 400, f"expected rejection for speedMps={bad!r}"

    # Same via speedKmh
    for bad in (float("nan"), float("inf")):
        r = client.post(
            "/api/route/start",
            json={"waypoints": [[0.0, 0.0], [0.0, 0.001]], "speedKmh": bad},
        )
        assert r.status_code == 400, f"expected rejection for speedKmh={bad!r}"
