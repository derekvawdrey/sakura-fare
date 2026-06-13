"""Live public-transit fares via the google-maps-scraper `gmaps-fares` tool.

Shells out to the Dockerized scraper (built from the fork at
github.com/derekvawdrey/google-maps-scraper), which opens Google Maps
directions for an origin->destination pair in transit mode and returns the
fare, duration and route. Best-effort: any failure returns a structured notice
rather than raising, so the agent can fall back to curated/estimated fares.

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
    """Runs the `gmaps-fares` container for a single origin->destination pair."""

    def __init__(self, *, enabled: bool, image: str, lang: str, timeout: float):
        self._enabled = enabled
        self._image = image
        self._lang = lang
        self._timeout = timeout

    @classmethod
    def from_settings(cls) -> "GmapsFareService":
        return cls(
            enabled=settings.gmaps_fares_enabled,
            image=settings.gmaps_fares_image,
            lang=settings.gmaps_fares_lang,
            timeout=settings.gmaps_fares_timeout_seconds,
        )

    async def transit_fare(self, origin: str, destination: str) -> dict[str, Any]:
        origin, destination = origin.strip(), destination.strip()

        if not self._enabled:
            return {"ok": False, "error": "Live Google Maps fare lookup is disabled (SAKURA_GMAPS_FARES_ENABLED)."}
        if not origin or not destination:
            return {"ok": False, "error": "Both origin and destination are required."}
        if shutil.which("docker") is None:
            return {"ok": False, "error": "Docker is unavailable; cannot run the Google Maps scraper."}

        cmd = [
            "docker", "run", "--rm", "-i", "--entrypoint", "gmaps-fares",
            self._image,
            "-input", "stdin", "-results", "stdout", "-lang", self._lang,
        ]
        stdin = f"{origin} -> {destination}\n".encode()

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return {"ok": False, "error": f"Could not start Docker: {exc}"}

        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=self._timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"ok": False, "error": f"Google Maps lookup timed out after {self._timeout:.0f}s."}

        if proc.returncode != 0:
            tail = _last_line(err) or f"exit {proc.returncode}"
            log.warning("gmaps-fares failed (image=%s): %s", self._image, tail)
            return {"ok": False, "error": f"Scraper failed ({tail}). Is the '{self._image}' image built?"}

        row = _first_csv_row(out.decode(errors="replace"))
        if not row:
            return {"ok": False, "error": "Google Maps returned no result for this route."}

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
        return result


def _first_csv_row(stdout: str) -> dict[str, str] | None:
    """Find the CSV header the scraper prints and return the first data row.

    The container logs to stderr, but we locate the header defensively in case
    anything leaks onto stdout.
    """
    lines = stdout.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("origin,destination,travel_mode"):
            reader = csv.DictReader(io.StringIO("\n".join(lines[i:])))
            for row in reader:
                return row
            return None
    return None


def _yen_to_int(fare_text: str) -> int | None:
    """'¥1,290' -> 1290; '' -> None."""
    digits = "".join(_DIGITS.findall(fare_text))
    return int(digits) if digits else None


def _last_line(data: bytes) -> str:
    lines = [ln for ln in data.decode(errors="replace").splitlines() if ln.strip()]
    return lines[-1].strip() if lines else ""
