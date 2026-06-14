"""Pydantic models shared by the API and the agent phases' final-answer validation."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Basis = Literal["published", "estimated", "curated", "web"]


# ---- phase 1: itinerary extraction ------------------------------------------

class StayExtract(BaseModel):
    city: str
    days: int = Field(ge=1, le=30, description="full or partial days spent in the city")
    activities: list[str] = []


class SegmentExtract(BaseModel):
    day: str | None = None
    from_place: str
    to_place: str
    note: str | None = None


class ItineraryExtract(BaseModel):
    trip_summary: str
    travelers: int = Field(ge=1, le=50)
    season: str | None = None
    stays: list[StayExtract]
    segments: list[SegmentExtract]
    assumptions: list[str] = []


# ---- phase 2: rail costing ---------------------------------------------------

class RailSegment(BaseModel):
    day: str | None = None
    from_: str = Field(alias="from")
    to: str
    line: str
    train: str
    fare_jpy: int = Field(ge=0)
    basis: Literal["published", "estimated"]
    source: str
    notes: str | None = None

    model_config = {"populate_by_name": True}


class RailCosts(BaseModel):
    segments: list[RailSegment]
    assumptions: list[str] = []


# ---- phase 3: per-city enrichment -------------------------------------------

class Poi(BaseModel):
    name: str
    why: str
    cost_jpy: int | None = None
    transit_fare_jpy: int | None = None  # one-way transit fare to reach it from the city's main station
    lat: float | None = None
    lon: float | None = None
    basis: Basis = "curated"


class DayPlanEntry(BaseModel):
    label: str
    morning: str
    afternoon: str
    evening: str


class Meal(BaseModel):
    slot: Literal["breakfast", "lunch", "dinner"]
    venue: str
    suggestion: str
    cost_jpy: int = Field(ge=0)


class MealPlan(BaseModel):
    daily: list[Meal] = []
    daily_total_jpy: int = Field(ge=0)
    notes: list[str] = []


class CityPlan(BaseModel):
    city: str
    days: int = Field(ge=1)
    food_tier: Literal["budget", "standard", "premium"]
    food_daily_jpy: int = Field(ge=500, le=40_000)
    food_notes: list[str] = []
    meals: MealPlan | None = None
    transit_recommendation: str
    transit_total_jpy: int = Field(ge=0)
    transit_basis: Basis = "curated"
    day_plan: list[DayPlanEntry] = []
    highlights: list[Poi] = []
    hidden_gems: list[Poi] = []
    seasonal_note: str | None = None
    sources: list[str] = []
    # Computed/attached by the pipeline:
    food_total_jpy: int = 0
    lat: float | None = None
    lon: float | None = None


# ---- final assembled analysis ------------------------------------------------

class Totals(BaseModel):
    rail_jpy: int = 0
    local_transit_jpy: int = 0
    food_jpy: int = 0
    total_per_person_jpy: int = 0
    total_group_jpy: int = 0


class MapCity(BaseModel):
    name: str
    lat: float
    lon: float
    order: int
    days: int = 0


class MapPoi(BaseModel):
    city: str
    name: str
    kind: Literal["highlight", "gem"]
    lat: float
    lon: float
    why: str
    cost_jpy: int | None = None


class MapPayload(BaseModel):
    cities: list[MapCity] = []
    route: list[list[float]] = []  # [[lat, lon], ...] in travel order
    pois: list[MapPoi] = []


class TripAnalysis(BaseModel):
    depth: Literal["quick", "full"]
    trip_summary: str
    executive_summary: str | None = None
    travelers: int
    season: str | None = None
    rail_segments: list[RailSegment]
    city_plans: list[CityPlan] = []
    totals: Totals
    published_fare_count: int = 0
    estimated_fare_count: int = 0
    confidence: Literal["high", "medium", "low"] = "medium"
    assumptions: list[str] = []
    map: MapPayload = MapPayload()
    dataset_version: str | None = None
    disclaimers: list[str] = []


# ---- job plumbing --------------------------------------------------------------

class AgentEvent(BaseModel):
    seq: int
    kind: Literal["info", "phase", "thinking", "tool_call", "tool_result", "done", "error"]
    title: str
    detail: str | None = None


class JobPartial(BaseModel):
    itinerary: ItineraryExtract | None = None
    rail_segments: list[RailSegment] | None = None
    city_plans: list[CityPlan] = []


class JobView(BaseModel):
    id: str
    status: Literal["queued", "running", "done", "error"]
    document_name: str
    depth: Literal["quick", "full"]
    created_at: float
    events: list[AgentEvent]
    partial: JobPartial
    result: TripAnalysis | None = None
    error: str | None = None


class CreateJobResponse(BaseModel):
    id: str
