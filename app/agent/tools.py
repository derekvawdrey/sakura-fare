"""Per-phase tool registries: OpenAI-style definitions + async executors.

Data tools are backed by the curated repositories (fares, places, food) and
the live web (DuckDuckGo search, page fetch). Each phase gets only the tools
it needs, keeping the local model focused.
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from app.agent.client import LLMClient
from app.agent.loop import EventSink
from app.agent.research import run_research
from app.services.fares import FareRepository
from app.services.gmaps_fares import GmapsFareService
from app.services.places import PlacesRepository
from app.services.search import WebSearchService

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[str]]


def _fn(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


# ---- shared tool definitions -------------------------------------------------

LOOKUP_ROUTE_FARE = _fn(
    "lookup_route_fare",
    "Train fare between two stations/places. Returns published fare options when available, otherwise a distance-based estimate (basis='estimated'). Call once per travel segment.",
    {
        "from_station": {"type": "string"},
        "to_station": {"type": "string"},
    },
    ["from_station", "to_station"],
)

LOOKUP_TRANSIT_FARE = _fn(
    "lookup_transit_fare",
    "Live public-transit fare from Google Maps between ANY two places (stations, cities, addresses, landmarks) — the real fare for trains (incl. Shinkansen, surcharge included), subways, buses, ferries and mixed routes. Returns fare_jpy, duration and the route. This is the primary fare tool; call once per origin->destination.",
    {
        "origin": {"type": "string", "description": "start place, e.g. 'Gion, Kyoto' or 'Kyoto Station'"},
        "destination": {"type": "string", "description": "end place, e.g. 'Kinkaku-ji Temple'"},
    },
    ["origin", "destination"],
)

WEB_SEARCH = _fn(
    "web_search",
    "Search the live web (DuckDuckGo). Use for details not covered by curated tools: current prices, events, or guides for places without a curated entry. Keep queries short and specific.",
    {
        "query": {"type": "string"},
        "max_results": {"type": "integer", "minimum": 1, "maximum": 8},
    },
    ["query"],
)

FETCH_PAGE = _fn(
    "fetch_page",
    "Fetch a web page from a web_search result and return its readable text (truncated). Use sparingly — at most a couple of pages per task.",
    {"url": {"type": "string"}},
    ["url"],
)

RESEARCH = _fn(
    "research",
    "Delegate ONE focused web-research question to a research subagent: it searches the live web, reads pages, and returns a concise, cited answer. Use for current info the curated guide lacks — an event, a venue's hours/price, a seasonal detail. Ask one specific question per call.",
    {"question": {"type": "string", "description": "one specific question, e.g. 'teamLab Planets Tokyo admission price and hours 2026'"}},
    ["question"],
)

SUBMIT_FINDINGS = _fn(
    "submit_findings",
    "Submit your research answer. Call exactly once.",
    {
        "answer": {"type": "string", "description": "1-3 sentences with concrete facts"},
        "sources": {"type": "array", "items": {"type": "string"}, "description": "URLs you used"},
    },
    ["answer"],
)

CITY_GUIDE = _fn(
    "city_guide",
    "Curated guide for a Japanese city: top highlights and hidden gems (with approx admission fees and coordinates), food specialties, seasonal notes, practical tips. Try this BEFORE web_search.",
    {"city": {"type": "string"}},
    ["city"],
)

FOOD_REFERENCE = _fn(
    "food_cost_reference",
    "Typical food costs for a city: daily budget tiers (budget/standard/premium, city-adjusted) and a reference list of typical meal prices.",
    {"city": {"type": "string"}},
    ["city"],
)

CITY_TRANSIT = _fn(
    "city_transit_info",
    "Typical local transit costs for a Japanese city: single-ride and day-pass prices.",
    {"city": {"type": "string"}},
    ["city"],
)


# ---- terminal tool definitions -------------------------------------------------

SUBMIT_ITINERARY = _fn(
    "submit_itinerary",
    "Submit the structured itinerary extracted from the document. Call exactly once.",
    {
        "trip_summary": {"type": "string", "description": "1-2 sentences"},
        "travelers": {"type": "integer", "minimum": 1},
        "season": {"type": "string", "description": "e.g. 'spring (early April)' if stated or inferable, else omit"},
        "stays": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "city": {"type": "string"},
                    "days": {"type": "integer", "minimum": 1, "description": "days spent in/around this city incl. day trips based there"},
                    "activities": {"type": "array", "items": {"type": "string"}, "description": "activities the document mentions there"},
                },
                "required": ["city", "days"],
            },
        },
        "segments": {
            "type": "array",
            "description": "EVERY intercity/airport rail leg in travel order, including return legs of day trips",
            "items": {
                "type": "object",
                "properties": {
                    "day": {"type": "string"},
                    "from_place": {"type": "string"},
                    "to_place": {"type": "string"},
                    "note": {"type": "string", "description": "e.g. 'document says Nozomi reserved' or 'ferry to Miyajima'"},
                },
                "required": ["from_place", "to_place"],
            },
        },
        "assumptions": {"type": "array", "items": {"type": "string"}},
    },
    ["trip_summary", "travelers", "stays", "segments"],
)

SUBMIT_RAIL = _fn(
    "submit_rail_costs",
    "Submit the priced rail segments. Call exactly once, after pricing every segment with lookup_route_fare.",
    {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "day": {"type": "string"},
                    "from": {"type": "string"},
                    "to": {"type": "string"},
                    "line": {"type": "string"},
                    "train": {"type": "string"},
                    "fare_jpy": {"type": "integer", "description": "one-way per person, copied from the tool result"},
                    "basis": {"type": "string", "enum": ["published", "estimated"]},
                    "source": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["from", "to", "line", "train", "fare_jpy", "basis", "source"],
            },
        },
        "assumptions": {"type": "array", "items": {"type": "string"}},
    },
    ["segments"],
)

SUBMIT_CITY_PLAN = _fn(
    "submit_city_plan",
    "Submit the completed city plan. Call exactly once per task.",
    {
        "city": {"type": "string"},
        "days": {"type": "integer", "minimum": 1},
        "food_tier": {"type": "string", "enum": ["budget", "standard", "premium"], "description": "matched to the document's style"},
        "food_daily_jpy": {"type": "integer", "description": "per person per day, from food_cost_reference (±20% adjustment allowed)"},
        "food_notes": {"type": "array", "items": {"type": "string"}, "description": "specialties to try with typical prices"},
        "transit_recommendation": {"type": "string", "description": "e.g. 'Kyoto bus+subway 1-day pass (1,100) both days'"},
        "transit_total_jpy": {"type": "integer", "description": "local transit total per person for ALL days here"},
        "transit_basis": {"type": "string", "enum": ["published", "estimated", "curated"]},
        "day_plan": {
            "type": "array",
            "description": "one entry per day, concrete and tied to the document's activities",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "e.g. 'Day 6'"},
                    "morning": {"type": "string"},
                    "afternoon": {"type": "string"},
                    "evening": {"type": "string"},
                },
                "required": ["label", "morning", "afternoon", "evening"],
            },
        },
        "highlights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "why": {"type": "string", "description": "one punchy sentence"},
                    "cost_jpy": {"type": "integer", "description": "admission fee, 0 if free"},
                    "lat": {"type": "number"}, "lon": {"type": "number"},
                    "basis": {"type": "string", "enum": ["curated", "web"]},
                },
                "required": ["name", "why", "basis"],
            },
        },
        "hidden_gems": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "why": {"type": "string"},
                    "cost_jpy": {"type": "integer"},
                    "lat": {"type": "number"}, "lon": {"type": "number"},
                    "basis": {"type": "string", "enum": ["curated", "web"]},
                },
                "required": ["name", "why", "basis"],
            },
        },
        "seasonal_note": {"type": "string"},
        "sources": {"type": "array", "items": {"type": "string"}, "description": "URLs of any web sources used"},
    },
    ["city", "days", "food_tier", "food_daily_jpy", "transit_recommendation",
     "transit_total_jpy", "transit_basis", "highlights", "hidden_gems"],
)


# ---- executors -------------------------------------------------------------------

class ToolBox:
    """Bundles the data services and dispatches tool calls for any phase."""

    def __init__(self, fares: FareRepository, places: PlacesRepository, search: WebSearchService,
                 llm: LLMClient | None = None, gmaps: GmapsFareService | None = None):
        self._fares = fares
        self._places = places
        self._search = search
        self._llm = llm
        self._gmaps = gmaps or GmapsFareService.from_settings()
        self.emit: EventSink = lambda *_args: None

    async def execute(self, name: str, args: dict[str, Any]) -> str:
        try:
            return _dump(await self._dispatch(name, args))
        except Exception as exc:  # tool bugs must not kill the run
            return _dump({"ok": False, "error": f"Tool failed: {exc}"})

    async def _dispatch(self, name: str, args: dict[str, Any]) -> Any:
        if name == "lookup_route_fare":
            return self._fares.route_fare(
                str(args.get("from_station", "")), str(args.get("to_station", "")))
        if name == "lookup_transit_fare":
            return await self._transit_fare(
                str(args.get("origin", "")), str(args.get("destination", "")))
        if name == "city_transit_info":
            return self._fares.city_transit(str(args.get("city", "")))
        if name == "city_guide":
            return self._places.city_guide(str(args.get("city", "")))
        if name == "food_cost_reference":
            return self._places.food_reference(str(args.get("city", "")))
        if name == "web_search":
            return await self._search.search(
                str(args.get("query", "")), int(args.get("max_results", 5)))
        if name == "fetch_page":
            return await self._search.fetch_page(str(args.get("url", "")))
        if name == "research":
            return await self._research(str(args.get("question", "")))
        return {"ok": False, "error": f"Unknown tool '{name}'"}

    async def _transit_fare(self, origin: str, destination: str) -> dict[str, Any]:
        """Primary fare path: live Google Maps fare with a silent curated fallback."""
        result = await self._gmaps.transit_fare(origin, destination)
        if result.get("ok") and result.get("fare_jpy") is not None:
            result.setdefault("basis", "published")  # a real, live fare
            return result
        # Maps showed no fare (or the scraper failed) — fall back to the curated
        # dataset/estimate so a segment is never left unpriced.
        curated = _normalize_curated(self._fares.route_fare(origin, destination), origin, destination)
        if curated is not None:
            reason = result.get("error") or result.get("note") or "Google Maps showed no fare."
            curated["note"] = f"{curated.get('note', '')} [fallback: {reason}]".strip()
            return curated
        return {
            "ok": False, "from": origin, "to": destination,
            "error": result.get("error") or "No fare from Google Maps and no curated match.",
        }

    async def _research(self, question: str) -> dict[str, Any]:
        """Delegate a web-research question to a focused subagent."""
        question = question.strip()
        if not question:
            return {"ok": False, "error": "A research question is required."}
        if self._llm is None:
            return {"ok": False, "error": "Research subagent unavailable (no LLM bound)."}
        self.emit("tool_call", "research subagent", question[:200])
        return await run_research(
            self._llm,
            question,
            tool_definitions=[WEB_SEARCH, FETCH_PAGE, SUBMIT_FINDINGS],
            execute_tool=self.execute,
            emit=lambda kind, title, detail=None: self.emit(kind, f"↳ {title}", detail),
        )


def _normalize_curated(curated: dict[str, Any], origin: str, destination: str) -> dict[str, Any] | None:
    """Reshape FareRepository.route_fare output into the transit-fare result shape."""
    if not curated.get("ok"):
        return None
    options = curated.get("options") or []
    if not options or options[0].get("fare_jpy") is None:
        return None
    opt = options[0]
    frm, to = curated.get("from"), curated.get("to")
    return {
        "ok": True,
        "source": opt.get("source") or "Curated JR fare dataset",
        "from": frm.get("name") if isinstance(frm, dict) else origin,
        "to": to.get("name") if isinstance(to, dict) else destination,
        "mode": "train",
        "fare_jpy": opt.get("fare_jpy"),
        "fare_text": f"¥{opt['fare_jpy']:,}",
        "duration": f"{opt['duration_min']} min" if opt.get("duration_min") else "",
        "route": " ".join(x for x in (opt.get("line"), opt.get("train")) if x),
        "basis": curated.get("basis", "estimated"),
        "note": curated.get("note", ""),
    }


# Phase tool sets
EXTRACT_TOOLS = [SUBMIT_ITINERARY]
RAIL_TOOLS = [LOOKUP_TRANSIT_FARE, SUBMIT_RAIL]
CITY_TOOLS = [CITY_GUIDE, FOOD_REFERENCE, CITY_TRANSIT, LOOKUP_TRANSIT_FARE, RESEARCH, SUBMIT_CITY_PLAN]
