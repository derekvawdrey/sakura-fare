"""Best-effort startup helpers.

ensure_searxng() tries to bring up the local SearXNG container if it's
configured but not answering — so a bare `uvicorn app.main:app` also works
without the systemd unit. It is strictly best-effort: any failure just leaves
the DuckDuckGo fallback in place and is logged, never raised.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from urllib.parse import urlparse

import httpx

from app.core.config import REPO_DIR, settings

log = logging.getLogger("sakura.bootstrap")

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


async def ensure_searxng() -> None:
    url = (settings.searxng_url or "").strip()
    if not url or not settings.searxng_autostart:
        return

    host = urlparse(url).hostname
    if host not in _LOCAL_HOSTS:
        return  # a remote instance is not ours to manage

    if await _healthy(url):
        return

    script = REPO_DIR / "scripts" / "searxng.sh"
    if not shutil.which("docker") or not script.exists():
        log.warning(
            "SearXNG unreachable at %s and cannot auto-start (docker/script missing); "
            "using DuckDuckGo fallback. Start it with: %s up", url, script)
        return

    log.info("SearXNG unreachable; attempting auto-start via %s up", script)
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", str(script), "up",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
        log.info("searxng.sh up: %s", (out or b"").decode(errors="replace").strip()[:200])
    except (asyncio.TimeoutError, OSError) as exc:
        log.warning("SearXNG auto-start failed (%s); using DuckDuckGo fallback.", exc)
        return

    # Give the container a moment, then confirm.
    for _ in range(10):
        if await _healthy(url):
            log.info("SearXNG is up at %s", url)
            return
        await asyncio.sleep(1.5)
    log.warning("SearXNG did not become healthy in time; using DuckDuckGo fallback for now.")


async def _healthy(url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=4.0) as http:
            resp = await http.get(f"{url.rstrip('/')}/healthz")
            return resp.status_code == 200
    except httpx.HTTPError:
        return False


async def ensure_gmaps_image() -> None:
    """Warn (never fail) if the live-fare scraper image is missing, so it's clear
    rail fares will fall back to the curated dataset."""
    if not settings.gmaps_fares_enabled:
        return
    if not shutil.which("docker"):
        log.warning("Docker not found — live Google Maps fares disabled; using the curated JR dataset.")
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "image", "inspect", settings.gmaps_fares_image,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        code = await asyncio.wait_for(proc.wait(), timeout=15)
    except (OSError, asyncio.TimeoutError):
        return
    if code != 0:
        log.warning(
            "Live-fare image '%s' not found — rail fares will fall back to the curated dataset. "
            "Build it: docker build -t %s <google-maps-scraper checkout>",
            settings.gmaps_fares_image, settings.gmaps_fares_image,
        )
