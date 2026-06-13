"""Fare repository: curated published fares + distance-based estimation fallback.

Known routes return published fares verbatim (basis="published"). Unknown pairs
of known stations are estimated from great-circle distance with a rail
circuity factor and the JR Honshu fare structure (basis="estimated").
"""
from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

RAIL_CIRCUITY = 1.25  # rail distance vs straight line, typical for Japan trunk lines

# (upper bound km, reserved limited-express fee in JPY) — approximates the
# Tokaido/Sanyo shinkansen fee bands for estimation only.
EXPRESS_FEE_BANDS = [
    (100, 2290), (200, 3060), (300, 4180), (400, 4920),
    (600, 5810), (800, 6500), (1000, 7470), (math.inf, 8140),
]


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower().strip()
    text = re.sub(r"\b(station|sta\.?|eki|駅)\b", "", text)
    text = re.sub(r"[^\w぀-ヿ一-鿿]+", " ", text)
    return text.strip()


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    rad = math.radians
    dlat, dlon = rad(lat2 - lat1), rad(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rad(lat1)) * math.cos(rad(lat2)) * math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def _jr_base_fare(km: float) -> int:
    """Approximate JR Honshu trunk-line base fare (banded per-km rates)."""
    fare = 150.0
    fare += 16.2 * min(km, 300)
    fare += 12.85 * max(0.0, min(km, 600) - 300)
    fare += 7.05 * max(0.0, km - 600)
    return int(round(fare / 10) * 10)


def _express_fee(km: float) -> int:
    for upper, fee in EXPRESS_FEE_BANDS:
        if km <= upper:
            return fee
    raise AssertionError("unreachable")


@dataclass
class Station:
    id: str
    name: str
    name_ja: str
    city: str
    lat: float
    lon: float
    aliases: list[str] = field(default_factory=list)

    def search_keys(self) -> list[str]:
        return [_normalize(k) for k in (self.id, self.name, self.name_ja, self.city, *self.aliases) if k]


class FareRepository:
    def __init__(self, data_path: Path):
        raw = json.loads(data_path.read_text(encoding="utf-8"))
        self.version: str = raw["version"]
        self.disclaimer: str = raw["disclaimer"]
        self.sources: list[str] = raw["sources"]
        self.stations: dict[str, Station] = {
            s["id"]: Station(**s) for s in raw["stations"]
        }
        self._routes: dict[frozenset[str], list[dict[str, Any]]] = {
            frozenset(r["between"]): r["options"] for r in raw["routes"]
        }
        self._city_transit: dict[str, dict[str, Any]] = {
            _normalize(c["city"]): c for c in raw["city_transit"]
        }

    # -- station search ------------------------------------------------------

    def search_stations(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        q = _normalize(query)
        if not q:
            return []
        scored: list[tuple[float, Station]] = []
        for st in self.stations.values():
            best = 0.0
            for key in st.search_keys():
                if not key:
                    continue
                if q == key:
                    best = 1.0
                    break
                if q in key or key in q:
                    best = max(best, 0.9)
                else:
                    best = max(best, SequenceMatcher(None, q, key).ratio())
            scored.append((best, st))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [
            {"id": st.id, "name": st.name, "name_ja": st.name_ja, "city": st.city,
             "match_score": round(score, 2)}
            for score, st in scored[:limit] if score >= 0.55
        ]

    def resolve(self, query: str) -> Station | None:
        matches = self.search_stations(query, limit=1)
        return self.stations[matches[0]["id"]] if matches else None

    # -- fares ---------------------------------------------------------------

    def route_fare(self, from_query: str, to_query: str) -> dict[str, Any]:
        a, b = self.resolve(from_query), self.resolve(to_query)
        unresolved = [q for q, st in ((from_query, a), (to_query, b)) if st is None]
        if unresolved:
            return {
                "ok": False,
                "error": f"Unknown station(s): {', '.join(unresolved)}. "
                         "Try search_station with a city name or a nearby major station.",
            }
        assert a and b
        if a.id == b.id:
            return {"ok": False, "error": f"'{from_query}' and '{to_query}' resolve to the same station ({a.name})."}

        options = self._routes.get(frozenset((a.id, b.id)))
        if options:
            return {
                "ok": True, "basis": "published",
                "from": {"id": a.id, "name": a.name}, "to": {"id": b.id, "name": b.name},
                "options": options,
                "note": "Published regular-season fares; first option is the typical/fastest choice.",
            }
        return self._estimate(a, b)

    def _estimate(self, a: Station, b: Station) -> dict[str, Any]:
        km = _haversine_km(a.lat, a.lon, b.lat, b.lon) * RAIL_CIRCUITY
        base = _jr_base_fare(km)
        if km >= 80:  # long enough that a shinkansen / limited express is the realistic choice
            fare = base + _express_fee(km)
            train = "Shinkansen/limited express (estimated, reserved)"
            speed = 150.0
        else:
            fare = base
            train = "Local/rapid service (estimated)"
            speed = 55.0
        return {
            "ok": True, "basis": "estimated",
            "from": {"id": a.id, "name": a.name}, "to": {"id": b.id, "name": b.name},
            "options": [{
                "line": "Estimated rail route",
                "train": train,
                "fare_jpy": int(round(fare / 10) * 10),
                "duration_min": int(km / speed * 60),
                "source": f"Estimated from ~{km:.0f} rail-km using JR fare structure",
            }],
            "note": "No published fare in dataset for this pair — distance-based estimate. Flag it as estimated.",
        }

    def city_transit(self, city: str) -> dict[str, Any]:
        info = self._city_transit.get(_normalize(city))
        if info:
            return {"ok": True, "basis": "published", **info}
        return {
            "ok": True, "basis": "estimated", "city": city,
            "typical_ride_jpy": 230, "day_pass_jpy": 800, "day_pass_name": "local 1-day pass (typical)",
            "notes": "City not in dataset; figures are typical for mid-size Japanese cities.",
        }
