# 桜旅 Sakura Fare — Japan Travel Analyzer

Upload a travel-plan document (PDF / DOCX / TXT / MD) and an **agentic pipeline
running on your local LLM** (Qwen3.6-27B via llama.cpp on `localhost:8020`)
produces a full cost-and-experience analysis:

- **Rail costs** for every segment, priced against real published JR fares
- **Food budget** per city (tiered: budget / standard / premium, city-adjusted)
- **Local transit** recommendations (day passes vs single rides)
- **City guides**: top highlights + hidden gems with admission fees, day-by-day
  plans, seasonal notes — curated data enriched by **live web search (self-hosted SearXNG)**
- **Interactive route map** (Leaflet + OpenStreetMap, open source) with the rail
  route, numbered city stops, and every recommended place pinned
- An executive summary written by the model

Flights and lodging are out of scope by design.

## Run

```bash
cd sakura-fare
uv run uvicorn app.main:app --port 8400
# open http://localhost:8400
```

Requires the llama.cpp server from your opencode config on
`http://localhost:8020/v1` (override with `SAKURA_LLM_BASE_URL` / `SAKURA_LLM_MODEL`).
Web search and geocoding degrade gracefully when offline — analysis falls back
to the curated datasets.

Sample PDF to test with: `uv run python scripts/make_sample_pdf.py` →
`samples/japan-trip.pdf`. Choose **Quick** (rail only, ~3-5 min) or **Full**
(everything, ~10-20 min on local hardware; the UI streams agent progress live).

## How it works

```
frontend (static, sakura-themed, Leaflet map)
   │  POST /api/analyses (file/text, travelers?, depth=quick|full) → 202 {id}
   │  GET  /api/analyses/{id} ← status + live agent events + partial results
   ▼
FastAPI ── JobStore (serializes runs; the local model fits one at a time)
   ▼
AnalysisPipeline — 4 phases, each a focused tool-calling agent run:
   1. EXTRACT   document → structured stays + travel segments
   2. RAIL      price every segment   tools: search_station, lookup_route_fare,
                                              web_search (verify estimates)
   3. CITY ×N   per-city enrichment   tools: city_guide, food_cost_reference,
                (full depth only)             city_transit_info, web_search,
                                              fetch_page
   4. ASSEMBLE  code: totals, confidence, map payload, geocoding;
                + one LLM call for the executive summary
   ▼
Data layer
   fares.json   ~45 routes, published JR fares w/ sources; distance-based
                estimator fallback (always flagged `estimated`)
   places.json  curated guides for 16 cities: highlights & hidden gems with
                fees + coordinates, food specialties, seasonal notes, tips
   food.json    typical meal prices + daily budget tiers, city multipliers
   web          search via a provider chain (SearXNG primary, DuckDuckGo
                fallback) with per-provider circuit breakers; readable-page
                fetch; Nominatim geocoding (rate-limited, cached) for POIs
```

Reliability choices for a 27B local model: each phase has a **terminal tool**
whose payload is pydantic-validated (errors are fed back for self-correction),
totals are **recomputed in code** (the LLM never does arithmetic), prompts
forbid quoting fares from memory, and a failed city enrichment degrades to a
curated fallback instead of failing the job. Every fare/POI carries its
`basis`: `published` / `curated` / `estimated` / `web`.

## Layout

```
app/
  core/config.py      settings (SAKURA_* env vars, per-phase iteration caps)
  api/                routes + pydantic schemas (incl. terminal-tool payloads)
  services/           documents, fares, places+food, search, geocode, jobs
  agent/              LLM client, generic tool loop, per-phase tools/prompts,
                      pipeline orchestrator
  data/               fares.json · places.json · food.json (versioned, sourced)
frontend/             static SPA: petals, phase strip, timeline, map, city cards
samples/              example itinerary (md + generated pdf)
```

## Notes

- Dataset version `2025-10`; fares are regular-season reserved unless noted.
  Seasonal surcharges and revisions apply — verify before purchase.
- Web search prefers a **self-hosted SearXNG** container (Docker) — it
  aggregates Google/Bing/Brave/DDG, so one engine rate-limiting never blanks a
  search. The app **auto-starts the container when it launches** (best-effort,
  via `scripts/searxng.sh`) — no boot/systemd involvement; the container uses
  `--restart no` so it does not resurrect on reboot. If SearXNG or Docker is
  unavailable, search falls back to DuckDuckGo scraping automatically. Manage
  it directly with `scripts/searxng.sh {up,down,logs,status}`; disable
  auto-start with `SAKURA_SEARXNG_AUTOSTART=false`, or point at an existing
  instance with `SAKURA_SEARXNG_URL=...`.
- Nominatim is called ≤1 req/s per its usage policy with a proper User-Agent.
- Attraction admission fees are shown per sight but not added to trip totals.
```
