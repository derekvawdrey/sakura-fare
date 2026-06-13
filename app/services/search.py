"""Web search and page fetching for the agent.

Search runs through a provider chain for reliability:

    SearXNG (self-hosted, multi-engine, no rate limits)  ── primary
        └── DuckDuckGo HTML/lite scraping  ──────────────── fallback

The first provider to return results wins. A provider that gets blocked or
errors trips a short per-provider circuit breaker and the chain moves on, so a
single upstream hiccup never blanks the search. Results are cached with a TTL;
everything degrades to ok=False (the agent then uses curated data).
"""
from __future__ import annotations

import asyncio
import html as html_lib
import ipaddress
import re
import time
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
CACHE_TTL_S = 1800
FAILURE_TTL_S = 240          # remember failed queries briefly to avoid hammering
BREAKER_COOLDOWN_S = 300     # back off a provider once it starts blocking us
MAX_PAGE_CHARS = 2800

_RESULT_LINK = re.compile(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_RESULT_SNIPPET = re.compile(r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', re.S)
_LITE_LINK = re.compile(r'<a[^>]+class="result-link"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_LITE_SNIPPET = re.compile(r'<td class="result-snippet">(.*?)</td>', re.S)
_TAG = re.compile(r"<[^>]+>")
_BLOCK_TAGS = re.compile(r"<(script|style|noscript|svg|header|footer|nav|form)\b.*?</\1>", re.S | re.I)

# Provider outcome statuses.
OK, EMPTY, BLOCKED, ERROR = "ok", "empty", "blocked", "error"


def _strip_tags(fragment: str) -> str:
    return html_lib.unescape(_TAG.sub(" ", fragment))


def _clean_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _decode_ddg_href(href: str) -> str | None:
    if href.startswith("//duckduckgo.com/l/") or href.startswith("/l/"):
        qs = parse_qs(urlparse(href).query)
        href = unquote(qs.get("uddg", [""])[0])
    if not href.startswith(("http://", "https://")):
        return None
    if "duckduckgo.com" in urlparse(href).netloc:  # ads route through y.js
        return None
    return href


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #

class SearchProvider:
    name: str
    min_interval_s: float = 0.0  # politeness throttle between calls to this provider

    async def search(self, http: httpx.AsyncClient, query: str, max_results: int
                     ) -> tuple[list[dict[str, Any]], str]:
        raise NotImplementedError


class SearxngProvider(SearchProvider):
    """Self-hosted SearXNG JSON API — aggregates Google/Bing/Brave/DDG/etc."""
    name = "searxng"

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    async def search(self, http, query, max_results):
        try:
            resp = await http.get(
                f"{self.base_url}/search",
                params={"q": query, "format": "json", "language": "en", "safesearch": 1},
                timeout=httpx.Timeout(15.0, connect=4.0),
            )
        except httpx.HTTPError:
            return [], ERROR
        if resp.status_code in (429, 403):
            return [], BLOCKED
        if resp.status_code != 200:
            return [], ERROR
        try:
            data = resp.json()
        except ValueError:
            return [], ERROR

        results: list[dict[str, Any]] = []
        for item in data.get("results", []):
            url = item.get("url")
            if not url:
                continue
            results.append({
                "title": _clean_ws(item.get("title", "")),
                "url": url,
                "snippet": _clean_ws(item.get("content", "") or "")[:300],
                "engine": item.get("engine"),
            })
            if len(results) >= max_results:
                break
        return results, (OK if results else EMPTY)


class DuckDuckGoProvider(SearchProvider):
    """Scrapes DuckDuckGo's HTML then lite endpoint. No key, but rate-limited."""
    name = "duckduckgo"
    min_interval_s = 3.0

    async def search(self, http, query, max_results):
        results, status = await self._scrape(
            http, "https://html.duckduckgo.com/html/", query, max_results,
            _RESULT_LINK, _RESULT_SNIPPET)
        if status in (OK,):
            return results, status
        # Fall through to the lighter endpoint on block/empty/error.
        lite_results, lite_status = await self._scrape(
            http, "https://lite.duckduckgo.com/lite/", query, max_results,
            _LITE_LINK, _LITE_SNIPPET)
        if lite_results:
            return lite_results, OK
        # Surface "blocked" only if both endpoints were blocked.
        if BLOCKED in (status, lite_status):
            return [], BLOCKED
        return [], status if status != OK else lite_status

    @staticmethod
    async def _scrape(http, url, query, max_results, link_re, snippet_re):
        try:
            resp = await http.get(url, params={"q": query})
        except httpx.HTTPError:
            return [], ERROR
        if resp.status_code != 200 or "anomaly" in resp.text[:4000]:
            return [], BLOCKED
        snippets = snippet_re.findall(resp.text)
        results = []
        for i, (href, title) in enumerate(link_re.findall(resp.text)):
            decoded = _decode_ddg_href(href)
            if decoded is None:
                continue
            results.append({
                "title": _clean_ws(_strip_tags(title)),
                "url": decoded,
                "snippet": _clean_ws(_strip_tags(snippets[i]))[:300] if i < len(snippets) else "",
            })
            if len(results) >= max_results:
                break
        return results, (OK if results else EMPTY)


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #

class WebSearchService:
    def __init__(self, searxng_url: str | None = None) -> None:
        self._http = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=httpx.Timeout(12.0, connect=6.0),
            follow_redirects=True,
        )
        self._providers: list[SearchProvider] = []
        if searxng_url:
            self._providers.append(SearxngProvider(searxng_url))
        self._providers.append(DuckDuckGoProvider())

        self._cache: dict[str, tuple[float, float, Any]] = {}  # key -> (ts, ttl, value)
        self._lock = asyncio.Lock()
        self._breaker_until: dict[str, float] = {}  # provider name -> cooldown end
        self._last_call: dict[str, float] = {}      # provider name -> last call ts

    async def close(self) -> None:
        await self._http.aclose()

    # -- cache --------------------------------------------------------------
    def _cached(self, key: str) -> Any | None:
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < hit[1]:
            return hit[2]
        return None

    def _store(self, key: str, value: Any, ttl: float = CACHE_TTL_S) -> Any:
        self._cache[key] = (time.monotonic(), ttl, value)
        return value

    # -- breaker / throttle -------------------------------------------------
    def _breaker_open(self, name: str) -> bool:
        return time.monotonic() < self._breaker_until.get(name, 0.0)

    def _trip_breaker(self, name: str) -> None:
        self._breaker_until[name] = time.monotonic() + BREAKER_COOLDOWN_S

    async def _throttle(self, provider: SearchProvider) -> None:
        if provider.min_interval_s <= 0:
            return
        wait = provider.min_interval_s - (time.monotonic() - self._last_call.get(provider.name, 0.0))
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call[provider.name] = time.monotonic()

    # -- health -------------------------------------------------------------
    async def status(self) -> dict[str, Any]:
        """Provider availability for /api/health (cached briefly)."""
        if (hit := self._cached("status")) is not None:
            return hit
        searxng_ok = False
        for provider in self._providers:
            if isinstance(provider, SearxngProvider):
                try:
                    resp = await self._http.get(
                        f"{provider.base_url}/healthz", timeout=httpx.Timeout(4.0))
                    searxng_ok = resp.status_code == 200
                except httpx.HTTPError:
                    searxng_ok = False
        active = "searxng" if searxng_ok else "duckduckgo"
        status = {
            "available": True,  # DDG fallback is always attempted
            "active_provider": active,
            "searxng": searxng_ok,
            "providers": [p.name for p in self._providers],
        }
        return self._store("status", status, ttl=30)

    async def available(self) -> bool:
        return (await self.status())["available"]

    # -- search -------------------------------------------------------------
    async def search(self, query: str, max_results: int = 5) -> dict[str, Any]:
        query = query.strip()
        if not query:
            return {"ok": False, "error": "Empty query."}
        key = f"s:{query.lower()}:{max_results}"
        if (hit := self._cached(key)) is not None:
            return hit

        async with self._lock:
            any_blocked = False
            for provider in self._providers:
                if self._breaker_open(provider.name):
                    any_blocked = True
                    continue
                await self._throttle(provider)
                results, outcome = await provider.search(self._http, query, max_results)
                if outcome == OK:
                    return self._store(key, {
                        "ok": True, "query": query,
                        "provider": provider.name, "results": results,
                    })
                if outcome == BLOCKED:
                    self._trip_breaker(provider.name)
                    any_blocked = True

        if any_blocked:
            failure = {"ok": False, "error": "Search is temporarily rate-limited. Do NOT retry; "
                                             "use curated data or your best estimate and continue."}
            return self._store(key, failure, ttl=FAILURE_TTL_S)
        failure = {"ok": False, "error": "No results. Do not repeat this exact query — "
                                         "rephrase once or proceed with what you have."}
        return self._store(key, failure, ttl=FAILURE_TTL_S)

    # -- page fetch ---------------------------------------------------------
    async def fetch_page(self, url: str) -> dict[str, Any]:
        problem = self._unsafe_url(url)
        if problem:
            return {"ok": False, "error": problem}
        key = f"p:{url}"
        if (hit := self._cached(key)) is not None:
            return hit

        try:
            resp = await self._http.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            return {"ok": False, "error": f"Could not fetch page ({exc.__class__.__name__})."}

        ctype = resp.headers.get("content-type", "")
        if "html" not in ctype and "text" not in ctype:
            return {"ok": False, "error": f"Not a text page (content-type {ctype})."}
        body = _BLOCK_TAGS.sub(" ", resp.text)
        text = _clean_ws(_strip_tags(body))[:MAX_PAGE_CHARS]
        if len(text) < 80:
            return {"ok": False, "error": "Page had no readable text."}
        return self._store(key, {"ok": True, "url": url, "text": text})

    @staticmethod
    def _unsafe_url(url: str) -> str | None:
        try:
            parsed = urlparse(url)
        except ValueError:
            return "Invalid URL."
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return "Only http(s) URLs are allowed."
        host = parsed.hostname
        if host in ("localhost",) or host.endswith(".local"):
            return "Local addresses are not allowed."
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                return "Private addresses are not allowed."
        except ValueError:
            pass  # hostname, not an IP literal
        return None


def ddg_search_url(query: str) -> str:
    return f"https://duckduckgo.com/?q={quote_plus(query)}"
