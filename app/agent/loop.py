"""Generic tool-calling loop, reused by every pipeline phase.

Drives the local model with native OpenAI-style tool calls until it invokes
the phase's terminal tool with a payload that validates; validation errors are
fed back so the model can correct itself.
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, TypeVar

from pydantic import BaseModel, ValidationError

from app.agent.client import LLMClient, LLMError
from app.core.config import settings

T = TypeVar("T", bound=BaseModel)
EventSink = Callable[[str, str, str | None], None]  # (kind, title, detail)
ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[str]]


class AgentError(RuntimeError):
    pass


async def run_tool_loop(
    llm: LLMClient,
    *,
    system_prompt: str,
    user_prompt: str,
    tool_definitions: list[dict[str, Any]],
    execute_tool: ToolExecutor,
    terminal_tool: str,
    result_model: type[T],
    emit: EventSink,
    max_iterations: int,
) -> T:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    nudges = 0
    seen_calls: dict[str, int] = {}  # canonical call -> times seen
    for _ in range(max_iterations):
        try:
            message = await llm.chat(messages, tools=tool_definitions)
        except LLMError as exc:
            raise AgentError(str(exc)) from exc

        tool_calls = message.get("tool_calls") or []
        content = (message.get("content") or "").strip()

        if not tool_calls:
            if content:
                emit("thinking", "Model reasoning", content[:500])
            nudges += 1
            if nudges > 2:
                raise AgentError("Model stopped calling tools before submitting its result.")
            messages.append({"role": "assistant", "content": content})
            messages.append({
                "role": "user",
                "content": f"Continue with tool calls only; finish by calling {terminal_tool}.",
            })
            continue

        messages.append({
            "role": "assistant",
            "content": message.get("content") or "",
            "tool_calls": tool_calls,
        })

        for call in tool_calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError as exc:
                messages.append(_tool_msg(call, json.dumps(
                    {"ok": False, "error": f"Arguments were not valid JSON: {exc}"})))
                continue

            if name == terminal_tool:
                try:
                    result = result_model.model_validate(args)
                except ValidationError as exc:
                    errors = "; ".join(
                        f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
                        for e in exc.errors()[:6]
                    )
                    emit("info", "Submission rejected", errors[:400])
                    messages.append(_tool_msg(call, json.dumps({
                        "ok": False,
                        "error": f"Validation failed — fix these fields and call {terminal_tool} again: {errors}",
                    })))
                    continue
                return result

            # Local models can get stuck repeating one failing call; block the spiral.
            call_key = f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
            seen_calls[call_key] = seen_calls.get(call_key, 0) + 1
            if seen_calls[call_key] > 1:
                emit("info", "Repeated call blocked", f"{name} called again with identical arguments")
                messages.append(_tool_msg(call, json.dumps({
                    "ok": False,
                    "error": f"You already called {name} with these exact arguments and have the "
                             f"result above. Do NOT call it again. Use what you have (or a clearly "
                             f"flagged estimate) and proceed; finish with {terminal_tool}.",
                })))
                if seen_calls[call_key] >= 3:
                    messages.append({
                        "role": "user",
                        "content": f"Stop repeating tool calls. Call {terminal_tool} NOW with your "
                                   "best current data; mark anything uncertain as estimated.",
                    })
                continue

            emit("tool_call", name, json.dumps(args, ensure_ascii=False)[:400])
            output = await execute_tool(name, args)
            emit("tool_result", f"{name} result", output[:700])
            messages.append(_tool_msg(call, output))

    raise AgentError(f"Phase did not finish within {max_iterations} iterations.")


def _tool_msg(call: dict[str, Any], content: str) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": call.get("id", ""),
        "name": call["function"]["name"],
        "content": content,
    }


async def plain_completion(llm: LLMClient, system_prompt: str, user_prompt: str) -> str:
    """Single non-tool completion (used for the executive summary)."""
    try:
        message = await llm.chat([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ])
    except LLMError as exc:
        raise AgentError(str(exc)) from exc
    return (message.get("content") or "").strip()
