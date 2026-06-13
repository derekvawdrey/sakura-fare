"""Web-research subagent.

A focused, nested tool-loop that answers ONE question from the live web. The
calling (e.g. city) agent delegates via the `research` tool and gets back a
concise, cited answer instead of having to drive search + page-reading itself —
keeping the parent agent's context clean. It reuses the same run_tool_loop and
the parent's web_search / fetch_page executors.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from pydantic import BaseModel, Field

from app.agent.client import LLMClient
from app.agent.loop import AgentError, EventSink, run_tool_loop
from app.agent.prompts import RESEARCH_SYSTEM
from app.core.config import settings

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[str]]


class Findings(BaseModel):
    """Terminal-tool payload for the research subagent."""
    answer: str = Field(min_length=1)
    sources: list[str] = Field(default_factory=list)


async def run_research(
    llm: LLMClient,
    question: str,
    *,
    tool_definitions: list[dict[str, Any]],
    execute_tool: ToolExecutor,
    emit: EventSink,
) -> dict[str, Any]:
    """Run the subagent and return a flat result for the parent tool call."""
    try:
        findings = await run_tool_loop(
            llm,
            system_prompt=RESEARCH_SYSTEM,
            user_prompt=f"Research question: {question}",
            tool_definitions=tool_definitions,
            execute_tool=execute_tool,
            terminal_tool="submit_findings",
            result_model=Findings,
            emit=emit,
            max_iterations=settings.research_max_iterations,
        )
    except AgentError as exc:
        return {"ok": False, "question": question, "error": f"Research did not complete: {exc}"}

    return {
        "ok": True,
        "question": question,
        "answer": findings.answer,
        "sources": findings.sources,
    }
