"""Live public-transit fares via the google-maps-scraper `gmaps-fares` tool.

Shells out to the Dockerized scraper (built from the fork at
github.com/derekvawdrey/google-maps-scraper), which opens Google Maps
directions for an origin->destination pair in transit mode and returns the
fare, duration and route. Best-effort: any failure returns a structured notice
rather than raising, so the agent can fall back to curated/estimated fares.

Results are cached per (origin, destination, lang). `prefetch()` warms that
cache for many pairs in a SINGLE concurrent container run, so the rail phase's
per-segment lookups hit the cache instead of launching a browser each time.

Build the image once:  docker build -t gmaps-scraper:fares <fork checkout>
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import re
import shutil
from typing import Any

from app.core.config import settings

log = logging.getLogger("sakura.gmaps_fares")

_DIGITS = re.compile(r"\d+")


class GmapsFareService:
    """Runs the `gmaps-fares` container; caches results; can batch-prefetch."""

    def __init__(self, *, enabled: bool, image: str, lang: str, timeout: float, concurrency: int):
        self._enabled = enabled
        self._image = image
        self._lang = lang
        self._timeout = timeout
        self._concurrency = max(1, concurrency)
        self._cache: dict[tuple[str, str, str], dict[str, Any]] = {}

    @classmethod
    def from_settings(cls) -> "GmapsFareService":
        return cls(
            enabled=settings.gmaps_fares_enabled,
            image=settings.gmaps_fares_image,
            lang=settings.gmaps_fares_lang,
            timeout=settings.gmaps_fares_timeout_seconds,
            concurrency=settings.gmaps_fares_concurrency,
        )

    async def transit_fare(self, origin: str, destination: str) -> dict[str, Any]:
        origin, destination = origin.strip(), destination.strip()

        if not self._enabled:
            return {"ok": False, "error": "Live Google Maps fare lookup is disabled (SAKURA_GMAPS_FARES_ENABLED)."}
        if not origin or not destination:
            return {"ok": False, "error": "Both origin and destination are required."}

        cached = self._cache.get((origin, destination, self._lang))
        if cached is not None:
            return dict(cached)

        if shutil.which("docker") is None:
            return {"ok": False, "error": "Docker is unavailable; cannot run the Google Maps scraper."}

        rows, err = await self._run(f"{origin} -> {destination}\n", concurrency=1)
        if err is not None:
            return {"ok": False, "error": err}

        row = _match_row(rows, origin, destination)
        if row is None:
            return {"ok": False, "error": "Google Maps returned no result for this route."}
        return self._build_result(origin, destination, row)

    async def prefetch(self, pairs: list[tuple[str, str]]) -> None:
        """Warm the cache for many pairs in one concurrent run. Best-effort, never raises."""
        if not self._enabled or shutil.which("docker") is None:
            return

        todo: list[tuple[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for origin, destination in pairs:
            origin, destination = origin.strip(), destination.strip()
            key = (origin, destination, self._lang)
            if not origin or not destination or key in self._cache or key in seen:
                continue
            seen.add(key)
            todo.append((origin, destination))

        if not todo:
            return

        input_text = "".join(f"{o} -> {d}\n" for o, d in todo)
        rows, err = await self._run(input_text, concurrency=min(len(todo), self._concurrency))
        if err is not None or not rows:
            return  # leave uncached; transit_fare will retry per-pair / fall back

        for origin, destination in todo:
            row = _match_row(rows, origin, destination)
            if row is not None:
                self._build_result(origin, destination, row)  # caches on success

    async def _run(self, input_text: str, concurrency: int) -> tuple[list[dict[str, str]] | None, str | None]:
        cmd = [
            "docker", "run", "--rm", "-i", "--entrypoint", "gmaps-fares",
            self._image,
            "-input", "stdin", "-results", "stdout", "-lang", self._lang, "-c", str(concurrency),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return None, f"Could not start Docker: {exc}"

        try:
            out, err = await asyncio.wait_for(proc.communicate(input_text.encode()), timeout=self._timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return None, f"Google Maps lookup timed out after {self._timeout:.0f}s."

        if proc.returncode != 0:
            tail = _last_line(err) or f"exit {proc.returncode}"
            log.warning("gmaps-fares failed (image=%s): %s", self._image, tail)
            return None, f"Scraper failed ({tail}). Is the '{self._image}' image built?"

        return _csv_rows(out.decode(errors="replace")), None

    def _build_result(self, origin: str, destination: str, row: dict[str, str]) -> dict[str, Any]:
        fare_jpy = _yen_to_int(row.get("fare", ""))
        result: dict[str, Any] = {
            "ok": True,
            "source": "Google Maps (live transit directions)",
            "from": origin,
            "to": destination,
            "mode": row.get("travel_mode") or "transit",
            "fare_jpy": fare_jpy,
            "fare_text": row.get("fare", ""),
            "duration": row.get("duration", ""),
            "route": row.get("route_summary", ""),
            "url": row.get("url", ""),
        }
        if fare_jpy is None:
            result["note"] = (
                row.get("note")
                or "Google Maps did not display a fare for this route (common for some buses/rural lines)."
            )
        else:
            self._cache[(origin, destination, self._lang)] = result
        return result


def _csv_rows(stdout: str) -> list[dict[str, str]]:
    """Return every data row under the scraper's CSV header.

    The container logs to stderr, but we locate the header defensively in case
    anything leaks onto stdout.
    """
    lines = stdout.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("origin,destination,travel_mode"):
            return list(csv.DictReader(io.StringIO("\n".join(lines[i:]))))
    return []


def _match_row(rows: list[dict[str, str]] | None, origin: str, destination: str) -> dict[str, str] | None:
    if not rows:
        return None
    for row in rows:
        if row.get("origin", "").strip() == origin and row.get("destination", "").strip() == destination:
            return row
    return rows[0]


def _yen_to_int(fare_text: str) -> int | None:
    """'¥1,290' -> 1290; '' -> None."""
    digits = "".join(_DIGITS.findall(fare_text))
    return int(digits) if digits else None


def _last_line(data: bytes) -> str:
    lines = [ln for ln in data.decode(errors="replace").splitlines() if ln.strip()]
    return lines[-1].strip() if lines else ""
