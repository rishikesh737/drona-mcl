"""
core.mcl — Model Compliance Layer.

This is the central dispatch brain of Drona. It sits between the raw Ollama
API response and the tool execution engine and handles both conforming and
non-conforming model output.

  Path A (Compliant):
      The model correctly populates response.message.tool_calls.
      The MCL extracts tool calls directly from that field and dispatches them.

  Path B (Non-Compliant):
      The model outputs freeform text that contains a JSON tool-call
      representation (possibly inside markdown fences, triple-brace fences,
      or plain prose). The MCL delegates to core.parser to extract and
      normalise the tool call, then dispatches it.

The MCL's route() function returns a MCLResult dataclass that the agent loop
uses to decide what to feed back into the conversation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import ollama

from core.parser import extract_tool_call, is_final_answer
from tools import dispatch

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class ToolResult:
    """Outcome of a single dispatched tool call."""
    tool_name: str
    arguments: dict[str, Any]
    output: str


@dataclass
class MCLResult:
    """Full output of the MCL routing decision for one model response."""
    path: str                          # "A", "B", or "final"
    tool_results: list[ToolResult] = field(default_factory=list)
    final_text: str = ""               # populated when path == "final"
    raw_content: str = ""              # the model's raw text (for logging)


# ---------------------------------------------------------------------------
# Path A — compliant tool_calls field
# ---------------------------------------------------------------------------


def _dispatch_tool_calls(
    tool_calls: list[Any],
) -> list[ToolResult]:
    """Dispatch a list of tool call objects from the native tool_calls field.

    Each item in *tool_calls* is an ollama ToolCall object with:
        .function.name       : str
        .function.arguments  : dict

    Returns a list of ToolResult, one per call.
    """
    results: list[ToolResult] = []
    for tc in tool_calls:
        name: str = tc.function.name
        arguments: dict[str, Any] = dict(tc.function.arguments)

        logger.debug("Path A dispatch: %s(%s)", name, arguments)
        try:
            output = dispatch(name, arguments)
        except Exception as exc:
            output = f"[ERROR] Tool execution failed unexpectedly: {exc}"
        results.append(ToolResult(tool_name=name, arguments=arguments, output=output))

    return results


# ---------------------------------------------------------------------------
# Path B — non-compliant freeform text
# ---------------------------------------------------------------------------


def _dispatch_from_text(text: str) -> ToolResult | None:
    """Attempt to parse and dispatch a tool call from freeform *text*.

    Returns a ToolResult on success, or None if no valid tool call is found.
    """
    tool_call = extract_tool_call(text)
    if tool_call is None:
        return None

    name: str = tool_call["name"]
    arguments: dict[str, Any] = tool_call["arguments"]

    logger.debug("Path B dispatch: %s(%s)", name, arguments)
    try:
        output = dispatch(name, arguments)
    except Exception as exc:
        output = f"[ERROR] Tool execution failed unexpectedly: {exc}"
    return ToolResult(tool_name=name, arguments=arguments, output=output)


# ---------------------------------------------------------------------------
# Public routing function
# ---------------------------------------------------------------------------


def route(response: ollama.ChatResponse) -> MCLResult:
    """Route an Ollama chat response through the MCL.

    Decision logic:
      1. If response.message.tool_calls is non-empty → Path A.
      2. Else if the message content (or, for thinking models, the thinking
         scratchpad) looks like a tool call (parser succeeds) → Path B.
      3. Otherwise → treat as a final text answer.

    For thinking-model variants (qwen3, deepseek-r1, etc.) the model places
    its chain-of-thought in message.thinking and leaves message.content empty
    when emitting a tool call. We therefore use thinking as a fallback text
    source for Path B — this is safe because Path A fires first and handles
    the structured tool_calls field correctly.

    Args:
        response: The raw ChatResponse object from the Ollama Python client.

    Returns:
        An MCLResult indicating which path was taken and what the tools returned.
    """
    message = response.message

    # Primary content field (empty string when model emits a tool call)
    raw_content: str = message.content or ""

    # Thinking scratchpad — only present for thinking-model variants.
    # Used as a fallback source for Path B extraction when content is empty.
    thinking_content: str = getattr(message, "thinking", None) or ""

    # The text we hand to Path B. Prefer content; fall back to thinking.
    # This handles the case where think=False was set but the model still
    # dumped its scratchpad into message.content (thinking text wrapped in
    # <think>...</think> tags), as well as the case where think=True caused
    # the tool call JSON to be emitted inside the thinking field instead of
    # the content field (observed on some Ollama + qwen3 combinations).
    text_for_path_b: str = raw_content or thinking_content

    # ── Path A ──────────────────────────────────────────────────────────────
    tool_calls = getattr(message, "tool_calls", None) or []
    if tool_calls:
        logger.info("MCL Path A: %d tool call(s) in structured field.", len(tool_calls))
        results = _dispatch_tool_calls(tool_calls)
        return MCLResult(path="A", tool_results=results, raw_content=raw_content)

    # ── Path B ──────────────────────────────────────────────────────────────
    if text_for_path_b and not is_final_answer(text_for_path_b):
        source = "content" if raw_content else "thinking"
        logger.info("MCL Path B: attempting text-based tool-call extraction (source=%s).", source)
        result = _dispatch_from_text(text_for_path_b)
        if result is not None:
            return MCLResult(path="B", tool_results=[result], raw_content=raw_content)

    # ── Final answer ─────────────────────────────────────────────────────────
    logger.info("MCL: treating response as final answer.")
    # Expose thinking content in final_text if content is empty, so the
    # agent loop has something useful to render and log.
    final_text = raw_content or thinking_content
    return MCLResult(path="final", final_text=final_text, raw_content=raw_content)


# ---------------------------------------------------------------------------
# Conversation message builders (used by agent.py)
# ---------------------------------------------------------------------------


def build_tool_result_messages(
    mcl_result: MCLResult,
) -> list[dict[str, Any]]:
    """Convert an MCLResult into Ollama conversation messages to append.

    For Path A, the tool results are formatted as role="tool" messages,
    which is what Ollama's chat API expects after a tool_calls response.

    For Path B, the tool results are formatted as role="user" messages
    containing the tool output, since the model didn't use the structured
    interface and won't recognise a role="tool" message.

    Args:
        mcl_result: The MCLResult to convert.

    Returns:
        A list of message dicts ready to append to the conversation history.
    """
    messages: list[dict[str, Any]] = []

    for tr in mcl_result.tool_results:
        if mcl_result.path == "A":
            messages.append({
                "role": "tool",
                "content": tr.output,
            })
        else:
            # Path B: inject as a user-turn observation so the model has context
            messages.append({
                "role": "user",
                "content": (
                    f"[Tool result for {tr.tool_name}]\n{tr.output}"
                ),
            })

    return messages
