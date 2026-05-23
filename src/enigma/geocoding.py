"""Address geocoding via the OpenStreetMap Nominatim service.

Nominatim is free and requires no API key, but it has a strict usage policy:
identify yourself with a real User-Agent and respect a max of ~1 request per
second. We enforce that rate limit client-side.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

NOMINATIM_BASE = "https://nominatim.openstreetmap.org"
DEFAULT_USER_AGENT = "Enigma/0.1 (https://github.com/zofefiyiw245-png/enigma)"


class Geocoder:
    """Rate-limited Nominatim client."""

    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        min_interval_s: float = 1.0,
        timeout_s: float = 10.0,
    ):
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": user_agent, "Accept-Language": "en"})
        self._min_interval = min_interval_s
        self._timeout = timeout_s
        self._lock = threading.Lock()
        self._last_request_at: float = 0.0

    def _throttle(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._min_interval - (now - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()

    def search(self, query: str, limit: int = 5) -> list[dict]:
        """Forward-geocode a free-form query (e.g. ``"Eiffel Tower, Paris"``).

        Returns a list of ``{name, lat, lon, type}`` dicts. Empty list on no
        results or transport errors (the caller decides how to surface that).
        """
        query = (query or "").strip()
        if not query:
            return []

        self._throttle()
        try:
            response = self._session.get(
                f"{NOMINATIM_BASE}/search",
                params={
                    "q": query,
                    "format": "jsonv2",
                    "limit": max(1, min(limit, 20)),
                    "addressdetails": 0,
                },
                timeout=self._timeout,
            )
            response.raise_for_status()
        except requests.RequestException:
            logger.exception("Nominatim search failed for %r", query)
            return []

        try:
            payload = response.json()
        except ValueError:
            logger.exception("Nominatim returned non-JSON for %r", query)
            return []

        results: list[dict] = []
        for entry in payload:
            try:
                results.append(
                    {
                        "name": entry.get("display_name", ""),
                        "lat": float(entry["lat"]),
                        "lon": float(entry["lon"]),
                        "type": entry.get("type", ""),
                        "category": entry.get("category", ""),
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
        return results

    def reverse(self, lat: float, lon: float) -> Optional[dict]:
        """Reverse-geocode a coordinate to a human-readable address."""
        self._throttle()
        try:
            response = self._session.get(
                f"{NOMINATIM_BASE}/reverse",
                params={"lat": lat, "lon": lon, "format": "jsonv2"},
                timeout=self._timeout,
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError):
            logger.exception("Nominatim reverse failed for %s,%s", lat, lon)
            return None

        if not isinstance(data, dict) or "display_name" not in data:
            return None
        return {"name": data["display_name"], "lat": lat, "lon": lon}
