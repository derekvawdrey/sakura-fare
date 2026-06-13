"""Geocoding via OpenStreetMap Nominatim (polite: 1 req/s, cached, UA set).

Used after the city-guide agent runs to pin web-discovered places on the map.
Curated POIs ship with coordinates and never hit the network.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "sakura-fare/1.0 (local travel-plan analyzer)"
MIN_INTERVAL_S = 1.1


class GeocodeService:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=httpx.Timeout(10.0, connect=5.0),
        )
        self._cache: dict[str, tuple[float, float] | None] = {}
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    async def close(self) -> None:
        await self._http.aclose()

    async def locate(self, place: str, city: str | None = None) -> tuple[float, float] | None:
        """Return (lat, lon) for a place, biased to a city in Japan, or None."""
        query = ", ".join(p for p in (place.strip(), city, "Japan") if p)
        key = query.lower()
        if key in self._cache:
            return self._cache[key]

        async with self._lock:  # serialize + rate-limit per Nominatim policy
            wait = MIN_INTERVAL_S - (time.monotonic() - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                resp = await self._http.get(
                    NOMINATIM_URL,
                    params={"q": query, "format": "json", "limit": 1, "countrycodes": "jp"},
                )
                self._last_request = time.monotonic()
                resp.raise_for_status()
                hits: list[dict[str, Any]] = resp.json()
            except (httpx.HTTPError, ValueError):
                # Don't cache failures — they may be transient.
                return None

        coords = (float(hits[0]["lat"]), float(hits[0]["lon"])) if hits else None
        self._cache[key] = coords
        return coords
