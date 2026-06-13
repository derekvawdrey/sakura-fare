"""Multi-phase analysis pipeline.

    extract itinerary ──► price rail ──► enrich each city ──► assemble
        (phase 1)          (phase 2)        (phase 3)         (phase 4)

Phases 1-3 are tool-calling agent runs on the local model; phase 4 is code
(totals, map, confidence) plus one short LLM call for the executive summary.
A failed city enrichment degrades to a curated fallback instead of failing
the whole job.
"""
from __future__ import annotations

import logging
from typing import Callable

from app.agent.client import LLMClient
from app.agent.loop import AgentError, plain_completion, run_tool_loop
from app.agent import prompts
from app.agent.tools import (
    CITY_TOOLS, EXTRACT_TOOLS, RAIL_TOOLS, SUBMIT_CITY_PLAN, SUBMIT_ITINERARY,
    SUBMIT_RAIL, ToolBox,
)
from app.api.schemas import (
    CityPlan, ItineraryExtract, JobPartial, MapCity, MapPayload, MapPoi, Poi,
    RailCosts, StayExtract, Totals, TripAnalysis,
)
from app.core.config import settings
from app.services.fares import FareRepository
from app.services.geocode import GeocodeService
from app.services.places import PlacesRepository
from app.services.search import WebSearchService

log = logging.getLogger("sakura.pipeline")

EventSink = Callable[[str, str, str | None], None]


class AnalysisPipeline:
    def __init__(
        self,
        llm: LLMClient,
        fares: FareRepository,
        places: PlacesRepository,
        search: WebSearchService,
        geocode: GeocodeService,
    ):
        self._llm = llm
        self._fares = fares
        self._places = places
        self._toolbox = ToolBox(fares, places, search, llm)
        self._geocode = geocode

    async def run(
        self,
        document_text: str,
        travelers_hint: int | None,
        depth: str,
        emit: EventSink,
        partial: JobPartial,
    ) -> TripAnalysis:
        text = document_text[: settings.document_max_chars]
        if len(document_text) > settings.document_max_chars:
            emit("info", "Document truncated", f"Using first {settings.document_max_chars} chars.")

        self._toolbox.emit = emit  # let the research subagent stream sub-events

        # -- phase 1: extract ---------------------------------------------------
        emit("phase", "Extracting itinerary", "Reading the document and structuring stays + travel segments")
        itinerary = await run_tool_loop(
            self._llm,
            system_prompt=prompts.EXTRACT_SYSTEM,
            user_prompt=prompts.build_extract_user(text, travelers_hint),
            tool_definitions=EXTRACT_TOOLS,
            execute_tool=self._toolbox.execute,
            terminal_tool=SUBMIT_ITINERARY["function"]["name"],
            result_model=ItineraryExtract,
            emit=emit,
            max_iterations=settings.extract_max_iterations,
        )
        if travelers_hint:
            itinerary.travelers = travelers_hint
        if not itinerary.segments:
            raise AgentError("No travel segments found in the document.")
        partial.itinerary = itinerary
        emit("info", "Itinerary extracted",
             f"{len(itinerary.stays)} stays · {len(itinerary.segments)} segments · {itinerary.travelers} traveler(s)")

        # -- phase 2: rail costing -----------------------------------------------
        emit("phase", "Pricing rail segments", f"{len(itinerary.segments)} segments against published fares")
        seg_lines = [
            f"{s.day or ''} {s.from_place} -> {s.to_place}"
            + (f" (note: {s.note})" if s.note else "")
            for s in itinerary.segments
        ]
        rail = await run_tool_loop(
            self._llm,
            system_prompt=prompts.RAIL_SYSTEM,
            user_prompt=prompts.build_rail_user(seg_lines, itinerary.travelers),
            tool_definitions=RAIL_TOOLS,
            execute_tool=self._toolbox.execute,
            terminal_tool=SUBMIT_RAIL["function"]["name"],
            result_model=RailCosts,
            emit=emit,
            max_iterations=settings.rail_max_iterations,
        )
        partial.rail_segments = rail.segments
        rail_total = sum(s.fare_jpy for s in rail.segments)
        emit("info", "Rail priced", f"{len(rail.segments)} segments · ¥{rail_total:,}/person")

        # -- phase 3: city enrichment ---------------------------------------------
        city_plans: list[CityPlan] = []
        if depth == "full":
            for i, stay in enumerate(itinerary.stays, 1):
                emit("phase", f"City guide {i}/{len(itinerary.stays)}: {stay.city}",
                     f"{stay.days} day(s) — food, highlights, hidden gems, day plan")
                plan = await self._enrich_city(stay, itinerary, emit)
                city_plans.append(plan)
                partial.city_plans.append(plan)

        # -- phase 4: assemble -----------------------------------------------------
        emit("phase", "Finalizing", "Totals, map and summary")
        analysis = await self._assemble(depth, itinerary, rail, city_plans, emit)
        emit("done", "Analysis complete",
             f"Total ¥{analysis.totals.total_group_jpy:,} for {analysis.travelers} traveler(s)")
        return analysis

    # ---- city phase -----------------------------------------------------------

    async def _enrich_city(self, stay: StayExtract, itin: ItineraryExtract, emit: EventSink) -> CityPlan:
        try:
            plan = await run_tool_loop(
                self._llm,
                system_prompt=prompts.CITY_SYSTEM,
                user_prompt=prompts.build_city_user(
                    stay.city, stay.days, itin.travelers, stay.activities, itin.season),
                tool_definitions=CITY_TOOLS,
                execute_tool=self._toolbox.execute,
                terminal_tool=SUBMIT_CITY_PLAN["function"]["name"],
                result_model=CityPlan,
                emit=emit,
                max_iterations=settings.city_max_iterations,
            )
            plan.days = stay.days  # the extract is authoritative
        except AgentError as exc:
            emit("error", f"{stay.city} guide failed — using curated fallback", str(exc)[:300])
            plan = self._fallback_city_plan(stay)

        plan.food_total_jpy = plan.food_daily_jpy * plan.days
        coords = self._places.city_coords(plan.city) or self._station_coords(plan.city)
        if coords is None:
            coords = await self._geocode.locate(plan.city)
        plan.lat, plan.lon = coords if coords else (None, None)
        await self._fill_poi_coords(plan)
        return plan

    def _fallback_city_plan(self, stay: StayExtract) -> CityPlan:
        guide = self._places.city_guide(stay.city)
        transit = self._fares.city_transit(stay.city)
        day_pass = int(transit.get("day_pass_jpy", 800))
        plan = CityPlan(
            city=stay.city,
            days=stay.days,
            food_tier="standard",
            food_daily_jpy=self._places.daily_tier_jpy("standard", stay.city),
            food_notes=guide.get("food_specialties", [])[:4] if guide.get("ok") else [],
            transit_recommendation=f"{transit.get('day_pass_name', 'local day pass')} × {stay.days} day(s)",
            transit_total_jpy=day_pass * stay.days,
            transit_basis="curated" if transit.get("basis") == "published" else "estimated",
            seasonal_note=None,
        )
        if guide.get("ok"):
            plan.highlights = [Poi(**h, basis="curated") for h in guide.get("highlights", [])]
            plan.hidden_gems = [Poi(**g, basis="curated") for g in guide.get("hidden_gems", [])]
        return plan

    def _station_coords(self, city: str) -> tuple[float, float] | None:
        station = self._fares.resolve(city)
        return (station.lat, station.lon) if station else None

    async def _fill_poi_coords(self, plan: CityPlan) -> None:
        missing = [p for p in (*plan.highlights, *plan.hidden_gems) if p.lat is None or p.lon is None]
        for poi in missing[: settings.geocode_max_per_city]:
            coords = await self._geocode.locate(poi.name, plan.city)
            if coords:
                poi.lat, poi.lon = coords

    # ---- assembly ---------------------------------------------------------------

    async def _assemble(
        self,
        depth: str,
        itin: ItineraryExtract,
        rail: RailCosts,
        city_plans: list[CityPlan],
        emit: EventSink,
    ) -> TripAnalysis:
        travelers = itin.travelers
        rail_total = sum(s.fare_jpy for s in rail.segments)

        if depth == "full":
            transit_total = sum(p.transit_total_jpy for p in city_plans)
            food_total = sum(p.food_total_jpy for p in city_plans)
        else:
            transit_total, food_total = self._quick_transit_total(itin.stays), 0

        per_person = rail_total + transit_total + food_total
        totals = Totals(
            rail_jpy=rail_total,
            local_transit_jpy=transit_total,
            food_jpy=food_total,
            total_per_person_jpy=per_person,
            total_group_jpy=per_person * travelers,
        )

        published = sum(1 for s in rail.segments if s.basis == "published")
        estimated = len(rail.segments) - published
        confidence = "high" if estimated == 0 else ("medium" if estimated <= 2 else "low")

        assumptions = list(dict.fromkeys(  # dedupe, keep order
            itin.assumptions
            + rail.assumptions
            + ["Fares are one-way per person, regular season.",
               "Attraction admission fees are listed per sight but NOT included in the totals.",
               "Flights and lodging are out of scope."]
        ))
        if depth == "full":
            assumptions.append("Food totals use the per-city tier × days shown in each city plan.")
        else:
            assumptions.append("Quick mode: local transit budgeted as one day pass per city day; food not included.")

        analysis = TripAnalysis(
            depth=depth, trip_summary=itin.trip_summary, travelers=travelers,
            season=itin.season, rail_segments=rail.segments, city_plans=city_plans,
            totals=totals, published_fare_count=published, estimated_fare_count=estimated,
            confidence=confidence, assumptions=assumptions,
            map=self._build_map(itin, rail, city_plans),
            dataset_version=self._fares.version,
            disclaimers=[self._fares.disclaimer, self._places.disclaimer,
                         self._places.food_disclaimer],
        )

        if depth == "full":
            try:
                picks = "; ".join(
                    f"{p.hidden_gems[0].name} ({p.city})" for p in city_plans if p.hidden_gems
                )[:300] or "—"
                analysis.executive_summary = await plain_completion(
                    self._llm, prompts.SUMMARY_SYSTEM,
                    prompts.SUMMARY_USER.format(
                        trip_summary=itin.trip_summary, travelers=travelers,
                        season=itin.season or "unspecified",
                        cities=", ".join(s.city for s in itin.stays),
                        rail=rail_total, transit=transit_total, food=food_total,
                        total_pp=per_person, total_group=per_person * travelers,
                        picks=picks,
                    ),
                ) or None
            except AgentError as exc:
                emit("info", "Summary skipped", str(exc)[:200])
        return analysis

    def _quick_transit_total(self, stays: list[StayExtract]) -> int:
        total = 0
        for stay in stays:
            info = self._fares.city_transit(stay.city)
            total += int(info.get("day_pass_jpy", 800)) * stay.days
        return total

    def _build_map(self, itin: ItineraryExtract, rail: RailCosts, plans: list[CityPlan]) -> MapPayload:
        payload = MapPayload()

        plan_coords = {p.city.lower(): (p.lat, p.lon) for p in plans if p.lat is not None}
        for order, stay in enumerate(itin.stays, 1):
            coords = (
                plan_coords.get(stay.city.lower())
                or self._places.city_coords(stay.city)
                or self._station_coords(stay.city)
            )
            if coords:
                payload.cities.append(MapCity(
                    name=stay.city, lat=coords[0], lon=coords[1], order=order, days=stay.days))

        route: list[list[float]] = []
        for seg in rail.segments:
            for place in (seg.from_, seg.to):
                station = self._fares.resolve(place)
                if station:
                    point = [station.lat, station.lon]
                    if not route or route[-1] != point:
                        route.append(point)
        payload.route = route

        for plan in plans:
            for kind, pois in (("highlight", plan.highlights), ("gem", plan.hidden_gems)):
                for poi in pois:
                    if poi.lat is not None and poi.lon is not None:
                        payload.pois.append(MapPoi(
                            city=plan.city, name=poi.name, kind=kind,
                            lat=poi.lat, lon=poi.lon, why=poi.why, cost_jpy=poi.cost_jpy))
        return payload
