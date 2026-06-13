"""Curated city guides (highlights, hidden gems, food, seasons) + food costs."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.services.fares import _normalize  # shared name normalization


class PlacesRepository:
    def __init__(self, places_path: Path, food_path: Path):
        places = json.loads(places_path.read_text(encoding="utf-8"))
        self.version: str = places["version"]
        self.disclaimer: str = places["disclaimer"]
        self._cities: dict[str, dict[str, Any]] = places["cities"]

        food = json.loads(food_path.read_text(encoding="utf-8"))
        self.food_version: str = food["version"]
        self.food_disclaimer: str = food["disclaimer"]
        self._meal_reference: list[dict[str, Any]] = food["meal_reference"]
        self._daily_tiers: list[dict[str, Any]] = food["daily_tiers"]
        self._multipliers: dict[str, float] = food["city_multipliers"]

    def _city_key(self, city: str) -> str | None:
        q = _normalize(city)
        if q in self._cities:
            return q
        for key, data in self._cities.items():
            names = (key, _normalize(data["name"]), _normalize(data["name_ja"]))
            if any(q == n or q in n or n in q for n in names if n):
                return key
        return None

    def city_guide(self, city: str) -> dict[str, Any]:
        key = self._city_key(city)
        if key is None:
            return {
                "ok": False,
                "error": f"No curated guide for '{city}'. Use web_search to find highlights, "
                         "hidden gems and typical food prices for it.",
            }
        return {"ok": True, "basis": "curated", **self._cities[key]}

    def city_coords(self, city: str) -> tuple[float, float] | None:
        key = self._city_key(city)
        if key is None:
            return None
        data = self._cities[key]
        return (data["lat"], data["lon"])

    def food_reference(self, city: str) -> dict[str, Any]:
        key = self._city_key(city)
        mult = self._multipliers.get(key or "", self._multipliers["default"])
        tiers = [
            {**t, "jpy_per_day": int(round(t["jpy_per_day"] * mult / 100) * 100)}
            for t in self._daily_tiers
        ]
        guide = self._cities.get(key or "")
        return {
            "ok": True,
            "city": guide["name"] if guide else city,
            "city_price_level": mult,
            "daily_tiers": tiers,
            "meal_reference": self._meal_reference,
            "local_specialties": guide["food_specialties"] if guide else [],
            "note": "Pick the tier matching the document's style; adjust jpy_per_day within ±20% if the plan implies it (e.g. one splurge meal).",
        }

    def daily_tier_jpy(self, tier: str, city: str) -> int:
        key = self._city_key(city)
        mult = self._multipliers.get(key or "", self._multipliers["default"])
        for t in self._daily_tiers:
            if t["tier"] == tier:
                return int(round(t["jpy_per_day"] * mult / 100) * 100)
        return int(round(6500 * mult / 100) * 100)  # standard fallback
