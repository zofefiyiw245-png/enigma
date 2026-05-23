"""Flask server: HTTP/JSON glue between the browser UI and the device backend."""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from flask import Flask, jsonify, request, send_from_directory

from .device import DeviceController, DeviceError, MockDeviceController
from .geocoding import Geocoder
from .routing import RouteSimulator, SPEED_PRESETS_MPS, kmh_to_mps

logger = logging.getLogger(__name__)


def _err(message: str, status: int = 400):
    response = jsonify({"error": message})
    response.status_code = status
    return response


def create_app(use_mock: bool = False) -> Flask:
    """Application factory. Wires controller, geocoder, simulator, and routes."""
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    app.config["JSON_SORT_KEYS"] = False

    device: DeviceController = MockDeviceController() if use_mock else DeviceController()
    geocoder = Geocoder()
    simulator = RouteSimulator(device.loop, device.aset_location, tick_hz=1.0)

    # Make these available for tests via app.extensions, and for clean shutdown.
    app.extensions["enigma"] = {
        "device": device,
        "geocoder": geocoder,
        "simulator": simulator,
    }

    # ------------------------------------------------------------------
    # Static UI
    # ------------------------------------------------------------------

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True, "mock": use_mock})

    # ------------------------------------------------------------------
    # Device
    # ------------------------------------------------------------------

    @app.get("/api/device/status")
    def device_status():
        return jsonify(device.status())

    @app.post("/api/device/connect")
    def device_connect():
        payload = request.get_json(silent=True) or {}
        udid = payload.get("udid") or None
        try:
            return jsonify(device.connect(udid=udid))
        except DeviceError as exc:
            return _err(str(exc), status=503)
        except Exception as exc:  # noqa: BLE001
            logger.exception("connect failed")
            return _err(f"Unexpected error: {exc}", status=500)

    @app.post("/api/device/disconnect")
    def device_disconnect():
        # Stop any active route first so it doesn't keep firing
        # set_location against a torn-down transport.
        if simulator.is_running():
            try:
                asyncio.run_coroutine_threadsafe(simulator.stop(), device.loop).result()
            except Exception:  # noqa: BLE001
                logger.exception("simulator stop failed during disconnect")
        try:
            device.disconnect()
        except Exception as exc:  # noqa: BLE001
            logger.exception("disconnect failed")
            return _err(str(exc), status=500)
        return jsonify({"ok": True})

    # ------------------------------------------------------------------
    # Location
    # ------------------------------------------------------------------

    @app.post("/api/location/set")
    def location_set():
        payload = request.get_json(silent=True) or {}
        try:
            lat = float(payload["lat"])
            lon = float(payload["lon"])
        except (KeyError, TypeError, ValueError):
            return _err("Body must include numeric 'lat' and 'lon'.")

        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
            return _err("Coordinates out of range.")

        # Stop any active route — manual override wins.
        if simulator.is_running():
            asyncio.run_coroutine_threadsafe(simulator.stop(), device.loop).result()

        try:
            device.set_location(lat, lon)
        except DeviceError as exc:
            return _err(str(exc), status=409)
        except Exception as exc:  # noqa: BLE001
            logger.exception("set_location failed")
            return _err(f"Failed to set location: {exc}", status=500)

        return jsonify({"ok": True, "lat": lat, "lon": lon})

    @app.post("/api/location/reset")
    def location_reset():
        if simulator.is_running():
            asyncio.run_coroutine_threadsafe(simulator.stop(), device.loop).result()
        try:
            device.reset_location()
        except DeviceError as exc:
            return _err(str(exc), status=409)
        except Exception as exc:  # noqa: BLE001
            logger.exception("reset_location failed")
            return _err(f"Failed to reset location: {exc}", status=500)
        return jsonify({"ok": True})

    # ------------------------------------------------------------------
    # Geocoding
    # ------------------------------------------------------------------

    @app.get("/api/geocode")
    def geocode():
        query = request.args.get("q", "").strip()
        if not query:
            return _err("Missing query parameter 'q'.")
        try:
            limit = int(request.args.get("limit", "5"))
        except ValueError:
            limit = 5
        return jsonify({"results": geocoder.search(query, limit=limit)})

    # ------------------------------------------------------------------
    # Route simulation
    # ------------------------------------------------------------------

    @app.get("/api/route/presets")
    def route_presets():
        return jsonify(
            {
                "presets": [
                    {"key": k, "mps": v, "kmh": round(v * 3.6, 2)}
                    for k, v in SPEED_PRESETS_MPS.items()
                ]
            }
        )

    @app.get("/api/route/status")
    def route_status():
        return jsonify(simulator.state().as_dict())

    @app.post("/api/route/start")
    def route_start():
        payload: dict[str, Any] = request.get_json(silent=True) or {}
        raw_waypoints = payload.get("waypoints")
        if not isinstance(raw_waypoints, list) or len(raw_waypoints) < 2:
            return _err("Provide at least two waypoints as [[lat, lon], ...].")

        waypoints: list[tuple[float, float]] = []
        try:
            for entry in raw_waypoints:
                lat, lon = float(entry[0]), float(entry[1])
                if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
                    raise ValueError("coordinate out of range")
                waypoints.append((lat, lon))
        except (TypeError, ValueError, IndexError) as exc:
            return _err(f"Malformed waypoint: {exc}")

        # Resolve speed: either preset, kmh, or mps.
        speed_mps: float
        if "preset" in payload and payload["preset"]:
            key = str(payload["preset"])
            if key not in SPEED_PRESETS_MPS:
                return _err(f"Unknown preset {key!r}. Choices: {list(SPEED_PRESETS_MPS)}.")
            speed_mps = SPEED_PRESETS_MPS[key]
        elif "speedMps" in payload:
            try:
                speed_mps = float(payload["speedMps"])
            except (TypeError, ValueError):
                return _err("'speedMps' must be a number.")
        elif "speedKmh" in payload:
            try:
                speed_mps = kmh_to_mps(float(payload["speedKmh"]))
            except (TypeError, ValueError):
                return _err("'speedKmh' must be a number.")
        else:
            return _err("Provide one of 'preset', 'speedMps', or 'speedKmh'.")

        if not math.isfinite(speed_mps) or speed_mps <= 0:
            return _err("Speed must be a positive, finite number.")

        loop_route = bool(payload.get("loop", False))

        if not device.status()["connected"]:
            return _err("Not connected to a device.", status=409)

        if simulator.is_running():
            asyncio.run_coroutine_threadsafe(simulator.stop(), device.loop).result()

        try:
            asyncio.run_coroutine_threadsafe(
                simulator.start(waypoints, speed_mps, loop_route=loop_route),
                device.loop,
            ).result()
        except DeviceError as exc:
            return _err(str(exc), status=409)
        except Exception as exc:  # noqa: BLE001
            logger.exception("route start failed")
            return _err(f"Failed to start route: {exc}", status=500)

        return jsonify(simulator.state().as_dict())

    @app.post("/api/route/stop")
    def route_stop():
        try:
            asyncio.run_coroutine_threadsafe(simulator.stop(), device.loop).result()
        except Exception as exc:  # noqa: BLE001
            logger.exception("route stop failed")
            return _err(str(exc), status=500)
        return jsonify(simulator.state().as_dict())

    return app
