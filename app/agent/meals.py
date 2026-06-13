"""Meal-planner subagent.

Plans a representative breakfast/lunch/dinner for one city at a budget tier,
using the curated food reference (venue archetypes + the city's local
specialties). A focused nested tool-loop, invoked per city by the pipeline.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from app.agent.client import LLMClient
from app.agent.loop import AgentError, EventSink, run_tool_loop
from app.agent.prompts import MEAL_PLANNER_SYSTEM
from app.api.schemas import MealPlan
from app.core.config import settings

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[str]]


async def run_meal_planner(
    llm: LLMClient,
    city: str,
    style: str,
    *,
    tool_definitions: list[dict[str, Any]],
    execute_tool: ToolExecutor,
    emit: EventSink,
) -> MealPlan | None:
    """Return a representative daily meal plan, or None if the subagent fails."""
    try:
        return await run_tool_loop(
            llm,
            system_prompt=MEAL_PLANNER_SYSTEM,
            user_prompt=f"Plan a representative day of meals for {city} at a '{style}' budget tier.",
            tool_definitions=tool_definitions,
            execute_tool=execute_tool,
            terminal_tool="submit_meal_plan",
            result_model=MealPlan,
            emit=emit,
            max_iterations=settings.meal_max_iterations,
        )
    except AgentError as exc:
        emit("info", "Meal plan skipped", str(exc)[:200])
        return None
