"""Tests for the Nominatim geocoder. Uses a stubbed requests Session — no
network access — so the suite runs offline and deterministically."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from enigma.geocoding import Geocoder


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture()
def geocoder():
    g = Geocoder(min_interval_s=0)  # disable throttle for tests
    g._session = MagicMock()
    return g


def test_search_returns_parsed_results(geocoder):
    geocoder._session.get.return_value = _FakeResponse(
        [
            {
                "display_name": "Eiffel Tower, Paris",
                "lat": "48.8584",
                "lon": "2.2945",
                "type": "attraction",
                "category": "tourism",
            },
            {
                "display_name": "Eiffel Bistro",
                "lat": "48.86",
                "lon": "2.29",
            },
        ]
    )
    results = geocoder.search("Eiffel Tower")
    assert len(results) == 2
    assert results[0]["name"] == "Eiffel Tower, Paris"
    assert results[0]["lat"] == pytest.approx(48.8584)
    assert results[0]["lon"] == pytest.approx(2.2945)


def test_search_empty_query_returns_empty_without_network(geocoder):
    assert geocoder.search("") == []
    assert geocoder.search("   ") == []
    geocoder._session.get.assert_not_called()


def test_search_skips_malformed_rows(geocoder):
    geocoder._session.get.return_value = _FakeResponse(
        [
            {"display_name": "Good", "lat": "1.0", "lon": "2.0"},
            {"display_name": "Bad — missing coords"},
            {"display_name": "Bad coords", "lat": "not a number", "lon": "x"},
        ]
    )
    results = geocoder.search("query")
    assert [r["name"] for r in results] == ["Good"]


def test_search_handles_transport_errors(geocoder):
    import requests

    geocoder._session.get.side_effect = requests.ConnectionError("network down")
    # The geocoder swallows transport errors and returns []
    assert geocoder.search("anything") == []


def test_reverse_returns_address(geocoder):
    geocoder._session.get.return_value = _FakeResponse(
        {"display_name": "Somewhere, Earth"}
    )
    result = geocoder.reverse(1.0, 2.0)
    assert result == {"name": "Somewhere, Earth", "lat": 1.0, "lon": 2.0}


def test_reverse_returns_none_on_empty(geocoder):
    geocoder._session.get.return_value = _FakeResponse({})
    assert geocoder.reverse(1.0, 2.0) is None
