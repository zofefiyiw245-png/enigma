"""iPhone location-simulation backend.

Wraps :mod:`pymobiledevice3` so the rest of the app can use a synchronous,
thread-safe API. All pmd3 calls happen on a dedicated asyncio event loop
running in a background thread; public methods schedule coroutines onto that
loop and block on the result.

Two underlying paths are supported automatically based on the connected
device's iOS version:

- **iOS < 17** — :class:`pymobiledevice3.services.simulate_location.DtSimulateLocation`,
  reached over USB via plain usbmuxd. No extra setup needed.
- **iOS >= 17** — :class:`pymobiledevice3.services.dvt.instruments.location_simulation.LocationSimulation`,
  reached via DVT through a RemoteServiceDiscovery tunnel. The user must run
  ``sudo pymobiledevice3 remote tunneld`` in a separate shell first; we then
  pick up the device from the local tunneld daemon.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import AsyncExitStack
from typing import Any, Optional

from packaging.version import Version

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers — kept inside functions so the module imports cleanly on systems
# without pymobiledevice3 (e.g. CI for unit tests of routing/geocoding).
# ---------------------------------------------------------------------------


def _import_pmd3() -> dict[str, Any]:
    """Lazily import pymobiledevice3 entry points so missing-package failures
    surface only when the user actually tries to connect to a device.
    """
    from pymobiledevice3.exceptions import (  # noqa: WPS433 — lazy import
        DeviceNotFoundError,
        NoDeviceConnectedError,
    )
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
    from pymobiledevice3.services.dvt.instruments.location_simulation import (
        LocationSimulation,
    )
    from pymobiledevice3.services.simulate_location import DtSimulateLocation
    from pymobiledevice3.tunneld.api import (
        TUNNELD_DEFAULT_ADDRESS,
        get_tunneld_devices,
    )

    return {
        "DeviceNotFoundError": DeviceNotFoundError,
        "NoDeviceConnectedError": NoDeviceConnectedError,
        "create_using_usbmux": create_using_usbmux,
        "DvtProvider": DvtProvider,
        "LocationSimulation": LocationSimulation,
        "DtSimulateLocation": DtSimulateLocation,
        "TUNNELD_DEFAULT_ADDRESS": TUNNELD_DEFAULT_ADDRESS,
        "get_tunneld_devices": get_tunneld_devices,
    }


class DeviceError(Exception):
    """Public, user-facing error raised by :class:`DeviceController`."""


class DeviceController:
    """Synchronous facade over the async pymobiledevice3 location services.

    Instances are safe to share across Flask request threads — every public
    method funnels work through a single background asyncio loop guarded by
    an internal lock.
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            name="enigma-device-loop",
            daemon=True,
        )
        self._thread.start()

        self._lock = threading.Lock()
        self._stack: Optional[AsyncExitStack] = None
        self._location_service: Any = None  # DtSimulateLocation or LocationSimulation
        self._lockdown: Any = None
        self._info: dict[str, Any] = {}
        self._mode: Optional[str] = None  # "legacy" or "dvt"
        self._last_set: Optional[tuple[float, float]] = None

    # -- threading bridge --------------------------------------------------

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    def _run(self, coro):
        """Run ``coro`` on the device loop and return its result."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    # -- public state ------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "connected": self._location_service is not None,
            "mode": self._mode,
            "device": dict(self._info),
            "lastLocation": list(self._last_set) if self._last_set else None,
        }

    # -- connection lifecycle ---------------------------------------------

    def connect(
        self,
        udid: Optional[str] = None,
        tunneld_address: Optional[tuple[str, int]] = None,
    ) -> dict[str, Any]:
        """Discover a device and open the appropriate location service.

        Strategy:

        1. Open lockdown over usbmuxd to learn the iOS version.
        2. If iOS < 17, keep the lockdown client and use ``DtSimulateLocation``.
        3. If iOS >= 17, close the usbmux lockdown and connect via tunneld
           (which must already be running with elevated privileges).
        """
        with self._lock:
            if self._location_service is not None:
                # Already connected — surface current status rather than reconnecting.
                return self.status()

            pmd3 = _import_pmd3()
            self._run(self._connect(pmd3, udid=udid, tunneld_address=tunneld_address))
            return self.status()

    def disconnect(self) -> None:
        with self._lock:
            if self._stack is None and self._location_service is None:
                return
            try:
                self._run(self._disconnect())
            finally:
                self._location_service = None
                self._lockdown = None
                self._mode = None
                self._info = {}
                self._last_set = None

    async def _connect(
        self,
        pmd3: dict[str, Any],
        udid: Optional[str],
        tunneld_address: Optional[tuple[str, int]],
    ) -> None:
        try:
            lockdown = await pmd3["create_using_usbmux"](serial=udid)
        except pmd3["NoDeviceConnectedError"] as exc:
            raise DeviceError(
                "No iPhone detected over USB. Plug the device in, unlock it, and trust this computer."
            ) from exc
        except pmd3["DeviceNotFoundError"] as exc:
            raise DeviceError(f"Device {udid!r} not found over USB.") from exc
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"USB lockdown failed: {exc}") from exc

        version_str = getattr(lockdown, "product_version", "1.0")
        info = {
            "udid": getattr(lockdown, "udid", None) or getattr(lockdown, "identifier", None),
            "name": lockdown.all_values.get("DeviceName") if hasattr(lockdown, "all_values") else None,
            "product_type": lockdown.all_values.get("ProductType") if hasattr(lockdown, "all_values") else None,
            "ios_version": version_str,
        }

        try:
            ios_major = Version(version_str).major
        except Exception:  # noqa: BLE001
            ios_major = 0

        if ios_major < 17:
            # Legacy path — DtSimulateLocation creates a fresh service per call,
            # so we just keep the lockdown client and a stateless wrapper.
            self._lockdown = lockdown
            self._location_service = pmd3["DtSimulateLocation"](lockdown)
            self._mode = "legacy"
            self._info = info
            self._stack = None
            logger.info("Connected to %s (iOS %s) via lockdown DtSimulateLocation", info.get("name"), version_str)
            return

        # iOS 17+: switch to tunneld + DVT.
        try:
            await lockdown.close()
        except Exception:  # noqa: BLE001
            pass

        address = tunneld_address or pmd3["TUNNELD_DEFAULT_ADDRESS"]
        try:
            rsds = await pmd3["get_tunneld_devices"](address)
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(
                "Could not reach tunneld at "
                f"{address[0]}:{address[1]}. Start it in another shell with: "
                "`sudo pymobiledevice3 remote tunneld`."
            ) from exc

        if not rsds:
            raise DeviceError(
                "No devices reported by tunneld. Make sure the iPhone is connected, "
                "unlocked, trusted, and that `sudo pymobiledevice3 remote tunneld` is running."
            )

        if udid:
            rsd = next((r for r in rsds if r.udid == udid), None)
            if rsd is None:
                raise DeviceError(f"Device {udid!r} not present in tunneld.")
        else:
            rsd = rsds[0]

        # Close any sibling RSDs we didn't pick.
        for other in rsds:
            if other is rsd:
                continue
            try:
                await other.close()
            except Exception:  # noqa: BLE001
                pass

        stack = AsyncExitStack()
        try:
            dvt = await stack.enter_async_context(pmd3["DvtProvider"](rsd))
            location = await stack.enter_async_context(pmd3["LocationSimulation"](dvt))
        except Exception as exc:  # noqa: BLE001
            await stack.aclose()
            try:
                await rsd.close()
            except Exception:  # noqa: BLE001
                pass
            raise DeviceError(f"DVT LocationSimulation channel failed: {exc}") from exc

        self._lockdown = rsd
        self._location_service = location
        self._mode = "dvt"
        self._info = info
        self._stack = stack
        logger.info("Connected to %s (iOS %s) via DVT LocationSimulation", info.get("name"), version_str)

    async def _disconnect(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception:  # noqa: BLE001
                logger.exception("Error closing DVT stack")
            self._stack = None
        if self._lockdown is not None:
            try:
                await self._lockdown.close()
            except Exception:  # noqa: BLE001
                logger.exception("Error closing lockdown/RSD")
        self._lockdown = None

    # -- location operations ----------------------------------------------

    async def aset_location(self, latitude: float, longitude: float) -> None:
        """Async setter used by :class:`enigma.routing.RouteSimulator`."""
        if self._location_service is None:
            raise DeviceError("Not connected to a device.")
        await self._location_service.set(float(latitude), float(longitude))
        self._last_set = (float(latitude), float(longitude))

    def set_location(self, latitude: float, longitude: float) -> None:
        if self._location_service is None:
            raise DeviceError("Not connected to a device.")
        self._run(self.aset_location(latitude, longitude))

    async def areset_location(self) -> None:
        if self._location_service is None:
            raise DeviceError("Not connected to a device.")
        await self._location_service.clear()
        self._last_set = None

    def reset_location(self) -> None:
        if self._location_service is None:
            raise DeviceError("Not connected to a device.")
        self._run(self.areset_location())

    # -- teardown ----------------------------------------------------------

    def shutdown(self) -> None:
        """Tear down the controller and stop its background loop."""
        try:
            self.disconnect()
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Mock controller — exposed so the UI is usable without a device for dev/test.
# ---------------------------------------------------------------------------


class MockDeviceController(DeviceController):
    """In-memory stand-in for :class:`DeviceController`.

    Exercises the same async surface (so :class:`RouteSimulator` works) but
    never touches USB or tunneld. Useful for UI development on a machine
    without an iPhone attached.
    """

    def connect(  # type: ignore[override]
        self,
        udid: Optional[str] = None,
        tunneld_address: Optional[tuple[str, int]] = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._location_service = object()  # truthy sentinel
            self._mode = "mock"
            self._info = {
                "udid": udid or "MOCK-0000-0000",
                "name": "Mock iPhone",
                "product_type": "iPhone15,2",
                "ios_version": "17.4.1",
            }
            return self.status()

    def disconnect(self) -> None:  # type: ignore[override]
        with self._lock:
            self._location_service = None
            self._mode = None
            self._info = {}
            self._last_set = None

    async def aset_location(self, latitude: float, longitude: float) -> None:  # type: ignore[override]
        if self._location_service is None:
            raise DeviceError("Not connected to a device.")
        self._last_set = (float(latitude), float(longitude))
        logger.debug("[mock] set location -> %s, %s", latitude, longitude)

    def set_location(self, latitude: float, longitude: float) -> None:  # type: ignore[override]
        self._run(self.aset_location(latitude, longitude))

    async def areset_location(self) -> None:  # type: ignore[override]
        if self._location_service is None:
            raise DeviceError("Not connected to a device.")
        self._last_set = None
        logger.debug("[mock] reset location")

    def reset_location(self) -> None:  # type: ignore[override]
        self._run(self.areset_location())
