"""Sakura Fare — Japan rail travel cost analyzer.

Run:  uvicorn app.main:app --port 8400
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.agent.client import LLMClient
from app.agent.pipeline import AnalysisPipeline
from app.api.routes import router
from app.core.bootstrap import ensure_gmaps_image, ensure_searxng
from app.core.config import settings
from app.services.fares import FareRepository
from app.services.geocode import GeocodeService
from app.services.jobs import JobStore
from app.services.places import PlacesRepository
from app.services.search import WebSearchService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Bring up the local SearXNG container if it isn't already running.
    await ensure_searxng()
    await ensure_gmaps_image()
    app.state.fares = FareRepository(settings.fares_path)
    app.state.places = PlacesRepository(settings.places_path, settings.food_path)
    app.state.llm = LLMClient()
    app.state.search = WebSearchService(searxng_url=settings.searxng_url or None)
    app.state.geocode = GeocodeService()
    app.state.jobs = JobStore(AnalysisPipeline(
        app.state.llm, app.state.fares, app.state.places,
        app.state.search, app.state.geocode,
    ))
    yield
    await app.state.llm.close()
    await app.state.search.close()
    await app.state.geocode.close()


app = FastAPI(title="Sakura Fare", version="1.0.0", lifespan=lifespan)
app.include_router(router)
app.mount("/", StaticFiles(directory=settings.frontend_dir, html=True), name="frontend")
