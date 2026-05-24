# Enigma

> Desktop tool to spoof the GPS location of a **non-jailbroken iPhone** for personal privacy. Cross-platform (macOS / Windows / Linux), driven over USB through Apple's developer services using [`pymobiledevice3`](https://github.com/doronz88/pymobiledevice3).

![status](https://img.shields.io/badge/status-alpha-orange)
![python](https://img.shields.io/badge/python-3.10%2B-blue)

## Features

- **Set coordinates** — pick a point on an OpenStreetMap-backed map, paste lat/lon, or search by address.
- **Search by address** — free-form geocoding via Nominatim (no API key).
- **Follow a route** — shift-click to add waypoints, then have the device walk/drive the polyline at a configurable speed (presets for walking, running, cycling, city driving, highway driving, or a custom km/h).
- **Reset** — clear the simulation and restore the device's real GPS.
- **Works on iOS 14 → 17+** — automatically picks the legacy `DtSimulateLocation` path for iOS < 17 and the new DVT `LocationSimulation` path (via `tunneld`) for iOS ≥ 17.
- **Mock mode** — run the full UI without an iPhone connected for development.

## Architecture

```
┌────────────┐    HTTP/JSON     ┌───────────┐   pymobiledevice3   ┌─────────┐
│  Browser   │ ◄──────────────► │  Flask    │ ◄─────────────────► │ iPhone  │
│  (Leaflet) │                  │  + asyncio│   (USB / tunneld)   │  USB    │
└────────────┘                  └───────────┘                     └─────────┘
```

- `src/enigma/device.py` — sync facade over the async pymobiledevice3 services. Owns a dedicated asyncio loop in a background thread.
- `src/enigma/routing.py` — haversine, polyline interpolation, async `RouteSimulator`.
- `src/enigma/geocoding.py` — rate-limited Nominatim client.
- `src/enigma/server.py` — Flask app factory and REST API.
- `src/enigma/static/` — single-page Leaflet UI.
- `src/enigma/__main__.py` — CLI entry point (`enigma` console script).

## Install

```bash
# Clone and create a virtual env
git clone https://github.com/zofefiyiw245-png/enigma.git
cd enigma
python3 -m venv .venv && source .venv/bin/activate  # Windows: .venv\Scripts\activate

# Install
pip install -e .
```

You'll also need libusbmuxd available on Linux (`sudo apt install usbmuxd libimobiledevice6` on Debian/Ubuntu); macOS and Windows ship the necessary plumbing.

## Run

### iOS < 17 (anything from iOS 14–16)

```bash
# Plug in iPhone, unlock it, tap "Trust this computer" if prompted.
enigma
```

The browser opens to <http://127.0.0.1:5037>. Click **Connect iPhone**.

### iOS 17+

Apple removed the lockdown `simulatelocation` service on iOS 17. The new path goes through DVT over a RemoteServiceDiscovery tunnel, which requires `pymobiledevice3 remote tunneld` to be running with elevated privileges.

```bash
# Terminal 1 — start the tunnel daemon (needs sudo / admin)
sudo pymobiledevice3 remote tunneld

# Terminal 2 — start Enigma
enigma
```

Then click **Connect iPhone** in the browser. Enigma auto-detects the iOS version and picks the correct backend.

### Mock mode (no device required)

```bash
enigma --mock
```

Lets you explore the UI and verify the route math without a real iPhone.

### CLI options

```
enigma --help

  --host HOST        Bind address (default 127.0.0.1)
  --port PORT        Listen port (default 5037)
  --no-browser       Do not auto-open the browser
  --mock             Run with a mock device
  --debug            Verbose logs / Flask debug
```

## Using the UI

1. **Connect** — top-right "Connect iPhone" button.
2. **Search by address** — type a query (e.g. *"Eiffel Tower, Paris"*), click a result.
3. **Set location** — either click a result, click the map, paste coords, then press **Set location**.
4. **Build a route** — shift-click the map to drop waypoints in order. Pick a speed preset (or custom km/h). Tick "Loop route" to replay forever. Hit **Start route**. The orange dot tracks the device's simulated position.
5. **Reset** — restores real GPS.

## REST API

All endpoints are JSON. Useful for scripting from the shell or another language.

| Method | Path                       | Body / Query                                    |
|-------:|----------------------------|-------------------------------------------------|
| GET    | `/api/device/status`       | —                                               |
| POST   | `/api/device/connect`      | `{ "udid": "..." }` (optional)                  |
| POST   | `/api/device/disconnect`   | —                                               |
| POST   | `/api/location/set`        | `{ "lat": 40.69, "lon": -74.045 }`              |
| POST   | `/api/location/reset`      | —                                               |
| GET    | `/api/geocode`             | `?q=Eiffel+Tower&limit=5`                       |
| GET    | `/api/route/presets`       | —                                               |
| GET    | `/api/route/status`        | —                                               |
| POST   | `/api/route/start`         | `{ waypoints: [[lat,lon],…], preset|speedKmh|speedMps, loop?: bool }` |
| POST   | `/api/route/stop`          | —                                               |

Example — start a 30 km/h drive through three waypoints:

```bash
curl -s -X POST http://127.0.0.1:5037/api/route/start \
  -H 'Content-Type: application/json' \
  -d '{
        "waypoints": [
          [37.7749, -122.4194],
          [37.7849, -122.4094],
          [37.7949, -122.3994]
        ],
        "speedKmh": 30
      }'
```

## Tests

```bash
pip install -e ".[dev]"
pytest
```

`tests/test_routing.py` covers the haversine + interpolation + simulator. The geocoder is exercised against fake responses so the suite stays offline.

## Troubleshooting

- **"No iPhone detected over USB"** — make sure the cable is data-capable, the phone is unlocked, and you've tapped *Trust this computer*. On Linux, `usbmuxd` must be running (`systemctl status usbmuxd`).
- **iOS 17+: "Could not reach tunneld"** — start `sudo pymobiledevice3 remote tunneld` in another shell first.
- **Location doesn't update in Apple Maps** — open Apple Maps after starting the simulation. Some apps cache the last fix for a few seconds.
- **Apps that don't trust simulated GPS** — anti-cheat / banking apps may detect Apple's developer location simulation and ignore it. That's a deliberate Apple/app-vendor choice, not a bug here.

## Legal notice

This software is provided for legitimate personal use — privacy, app development, and testing your own location-based features. Spoofing GPS may violate the terms of service of some apps and platforms (notably Pokémon Go and similar games), and may be illegal for certain purposes (e.g. fraud, evading court orders). You are responsible for using it lawfully and ethically.

## License

MIT.
