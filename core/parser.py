"""
core.parser — MCL Path B: fence stripping, JSON extraction, and tool-call
normalisation for non-compliant small-model outputs.

This module is intentionally dependency-free (stdlib only) so it can be
tested in total isolation with no Ollama or filesystem interaction.

Supported fence variants (all observed in real small-model output):
  1. Standard backtick with language tag  : ```json { ... } ```
  2. No-language backtick                 : ``` { ... } ```
  3. Triple-brace                         : {{{ { ... } }}}
  4. No fence (raw JSON)                  : { "name": ..., "arguments": ... }
  5. Prose-wrapped JSON                   : "Sure! Here is the call: { ... }"

Normalised output schema (canonical ToolCall dict):
  {
      "name":      str,          # tool function name
      "arguments": dict[str, *]  # keyword arguments for the tool
  }
"""
from __future__ import annotations

import json
import re
from typing import Any

# ---------------------------------------------------------------------------
# Type alias
# ---------------------------------------------------------------------------

ToolCall = dict[str, Any]

# ---------------------------------------------------------------------------
# Fence patterns — tried in priority order
# ---------------------------------------------------------------------------

# Matches: ```json ... ``` or ```JSON ... ```
_BACKTICK_WITH_LANG = re.compile(
    r"```[a-zA-Z]*\s*(\{.*?\})\s*```", re.DOTALL
)

# Matches: ``` ... ``` (no language tag)
_BACKTICK_NO_LANG = re.compile(
    r"```\s*(\{.*?\})\s*```", re.DOTALL
)

# Triple-brace prefix and suffix sentinels.
# We do NOT use a single regex to simultaneously delimit AND capture the
# inner JSON because the inner content can end with '}' characters that
# collide with the '}}}' fence suffix under lazy matching.
_TRIPLE_BRACE_OPEN  = "{{{"
_TRIPLE_BRACE_CLOSE = "}}}"

# ---------------------------------------------------------------------------
# Alternative key-name schemas emitted by various small models
# ---------------------------------------------------------------------------

# Some models use {"tool": ..., "parameters": ...} instead of
# {"name": ..., "arguments": ...}
_ALT_SCHEMAS: list[tuple[str, str]] = [
    ("tool", "parameters"),
    ("function", "arguments"),
    ("function_name", "arguments"),
    ("function", "parameters"),
    ("action", "action_input"),
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _try_parse_json(text: str) -> dict[str, Any] | None:
    """Attempt to parse *text* as a JSON object.

    Returns the parsed dict on success, None on failure.
    The input is stripped of leading/trailing whitespace before parsing.
    """
    text = text.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    return None


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Find the first balanced JSON object in *text* using brace counting.

    This handles prose-wrapped JSON where the object is embedded inside
    a sentence (e.g., "Sure, I'll call: {\"name\": ...}").

    Returns the parsed dict or None if no valid JSON object is found.
    """
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape_next = False

    for i, ch in enumerate(text[start:], start=start):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                return _try_parse_json(candidate)

    return None


def _normalise_tool_call(raw: dict[str, Any]) -> ToolCall | None:
    """Convert any known tool-call schema variant into the canonical form.

    Canonical form: {"name": str, "arguments": dict}

    Handles:
      - {"name": ..., "arguments": ...}       (OpenAI / Ollama native)
      - {"tool": ..., "parameters": ...}
      - {"function": ..., "arguments": ...}
      - {"function_name": ..., "arguments": ...}
      - {"function": ..., "parameters": ...}
      - {"action": ..., "action_input": ...}

    Returns None if the dict cannot be mapped to a tool call.
    """
    # Primary schema — already canonical
    if "name" in raw and "arguments" in raw:
        name = raw["name"]
        args = raw["arguments"]
        if isinstance(name, str) and isinstance(args, dict):
            return {"name": name, "arguments": args}

    # Alternative schemas
    for name_key, args_key in _ALT_SCHEMAS:
        if name_key in raw and args_key in raw:
            name = raw[name_key]
            args = raw[args_key]
            # action_input is sometimes a string (e.g. ReAct style)
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"input": args}
            if isinstance(name, str) and isinstance(args, dict):
                return {"name": name, "arguments": args}

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def strip_fences(text: str) -> str:
    """Remove all known fence wrappers from *text* and return the inner body.

    Tries fence patterns in priority order. If no fence is matched, returns
    the original text (raw JSON or prose).

    This function does NOT parse JSON; it only strips structural wrappers.
    """
    # 1. Backtick with language tag (most common in instruction-tuned models)
    match = _BACKTICK_WITH_LANG.search(text)
    if match:
        return match.group(1).strip()

    # 2. Backtick without language tag
    match = _BACKTICK_NO_LANG.search(text)
    if match:
        return match.group(1).strip()

    # 3. Triple-brace: find {{{ and }}} as plain-string sentinels.
    # We use rfind for the closing sentinel because the inner JSON content
    # can itself end with '}}', creating an ambiguous sequence of '}' chars.
    # Since {{{ }}} fences do not nest, the inner content is everything
    # between the FIRST '{{{' and the LAST '}}}' in the string.
    tb_start = text.find(_TRIPLE_BRACE_OPEN)
    if tb_start != -1:
        inner_start = tb_start + len(_TRIPLE_BRACE_OPEN)
        tb_end = text.rfind(_TRIPLE_BRACE_CLOSE)
        if tb_end != -1 and tb_end >= inner_start:
            return text[inner_start:tb_end].strip()

    # 4 & 5: Raw JSON or prose-wrapped — return as-is for downstream extraction
    return text.strip()


def extract_tool_call(text: str) -> ToolCall | None:
    """Parse a non-compliant model text response into a canonical ToolCall.

    Execution order:
      1. Strip any recognised fence wrappers.
      2. Attempt a direct JSON parse of the stripped text.
      3. If that fails, attempt brace-counting extraction from the raw text
         (handles prose-wrapped JSON).
      4. Normalise the parsed dict to the canonical {"name", "arguments"} form.

    Args:
        text: The raw text content from the model's message.

    Returns:
        A canonical ToolCall dict on success, or None if no tool call could
        be extracted.
    """
    # Step 1 — strip fences
    stripped = strip_fences(text)

    # Step 2 — try direct parse of stripped content
    parsed = _try_parse_json(stripped)

    # Step 3 — fallback: brace-counting on original text (prose-wrapped case)
    if parsed is None:
        parsed = _extract_json_object(text)

    if parsed is None:
        return None

    # Step 4 — normalise to canonical schema
    return _normalise_tool_call(parsed)


def is_final_answer(text: str) -> bool:
    """Heuristic: return True if the model response looks like a final answer
    rather than a tool call.

    A response is considered a final answer when:
      - It contains no JSON object (no '{'), OR
      - It contains a JSON object but it cannot be normalised to a tool call.

    This keeps the agent loop from mistakenly treating every response that
    mentions JSON as a tool invocation.
    """
    if "{" not in text:
        return True
    tool_call = extract_tool_call(text)
    return tool_call is None
