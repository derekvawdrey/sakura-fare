"""Thin async client for the local llama.cpp OpenAI-compatible endpoint."""
from __future__ import annotations

from typing import Any

import httpx

from app.core.config import settings


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=settings.llm_base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            timeout=httpx.Timeout(settings.llm_timeout_seconds, connect=10.0),
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def healthy(self) -> bool:
        try:
            resp = await self._http.get("/models", timeout=5.0)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """One chat-completion turn; returns the assistant message dict."""
        payload: dict[str, Any] = {
            "model": settings.llm_model,
            "messages": messages,
            "temperature": settings.llm_temperature,
            "max_tokens": settings.llm_max_tokens,
        }
        if tools:
            payload["tools"] = tools

        try:
            resp = await self._http.post("/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise LLMError(f"Local LLM unreachable at {settings.llm_base_url}: {exc}") from exc
        if resp.status_code != 200:
            raise LLMError(f"LLM returned HTTP {resp.status_code}: {resp.text[:500]}")

        body = resp.json()
        try:
            return body["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"Malformed LLM response: {body}") from exc
