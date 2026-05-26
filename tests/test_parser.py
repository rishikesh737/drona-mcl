"""
tests.test_parser — Exhaustive tests for core.parser (MCL Path B).

Tests cover every fence variant documented in the spec, plus normalisation
of alternative tool-call schemas emitted by different small models.

No Ollama, no filesystem, no subprocess — pure unit tests.
"""
from __future__ import annotations

import pytest

from core.parser import (
    extract_tool_call,
    is_final_answer,
    strip_fences,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CANONICAL_CALL = {
    "name": "get_journal_logs",
    "arguments": {"unit": "nginx.service", "lines": 20},
}

RAW_JSON = '{"name": "get_journal_logs", "arguments": {"unit": "nginx.service", "lines": 20}}'


# ---------------------------------------------------------------------------
# strip_fences — fence removal only
# ---------------------------------------------------------------------------


class TestStripFences:
    def test_backtick_json_tag(self) -> None:
        """Fence variant 1: ```json ... ```"""
        text = f"```json\n{RAW_JSON}\n```"
        result = strip_fences(text)
        assert result == RAW_JSON

    def test_backtick_json_tag_uppercase(self) -> None:
        """Fence variant 1a: ```JSON ... ``` (case insensitive tag)"""
        text = f"```JSON\n{RAW_JSON}\n```"
        result = strip_fences(text)
        assert result == RAW_JSON

    def test_backtick_no_language(self) -> None:
        """Fence variant 2: ``` ... ``` (no language tag)"""
        text = f"```\n{RAW_JSON}\n```"
        result = strip_fences(text)
        assert result == RAW_JSON

    def test_triple_brace(self) -> None:
        """Fence variant 3: {{{ ... }}}"""
        text = f"{{{{\n{RAW_JSON}\n}}}}"
        # Note: Python requires doubling braces in f-strings for literals
        text = "{{{" + RAW_JSON + "}}}"
        result = strip_fences(text)
        assert result == RAW_JSON

    def test_no_fence_raw_json(self) -> None:
        """Fence variant 4: raw JSON with no wrapper"""
        result = strip_fences(RAW_JSON)
        assert result == RAW_JSON

    def test_prose_wrapped_no_fence_stripped(self) -> None:
        """Fence variant 5: prose-wrapped JSON — strip_fences returns full text.

        strip_fences does NOT extract from prose; extract_tool_call does.
        """
        text = f"Sure, I will call the tool now: {RAW_JSON} to help you."
        result = strip_fences(text)
        # Should return the input unchanged (no fence to strip)
        assert result == text.strip()

    def test_whitespace_inside_fences_trimmed(self) -> None:
        """Extra whitespace inside fences is stripped from the result."""
        text = f"```json\n\n  {RAW_JSON}  \n\n```"
        result = strip_fences(text)
        assert result == RAW_JSON


# ---------------------------------------------------------------------------
# extract_tool_call — full extraction pipeline
# ---------------------------------------------------------------------------


class TestExtractToolCall:
    def test_fence_variant_1_backtick_json(self) -> None:
        """Full extraction: ```json ... ```"""
        text = f"```json\n{RAW_JSON}\n```"
        result = extract_tool_call(text)
        assert result == CANONICAL_CALL

    def test_fence_variant_2_backtick_no_lang(self) -> None:
        """Full extraction: ``` ... ```"""
        text = f"```\n{RAW_JSON}\n```"
        result = extract_tool_call(text)
        assert result == CANONICAL_CALL

    def test_fence_variant_3_triple_brace(self) -> None:
        """Full extraction: {{{ ... }}}"""
        text = "{{{" + RAW_JSON + "}}}"
        result = extract_tool_call(text)
        assert result == CANONICAL_CALL

    def test_fence_variant_4_raw_json(self) -> None:
        """Full extraction: raw JSON, no fence"""
        result = extract_tool_call(RAW_JSON)
        assert result == CANONICAL_CALL

    def test_fence_variant_5_prose_wrapped(self) -> None:
        """Full extraction: JSON embedded in prose"""
        text = (
            "Sure! I will help you. Here is the tool call you need: "
            + RAW_JSON
            + " Let me know if you need anything else."
        )
        result = extract_tool_call(text)
        assert result == CANONICAL_CALL

    def test_returns_none_for_plain_text(self) -> None:
        """Pure text with no JSON → None"""
        result = extract_tool_call("I cannot help with that request.")
        assert result is None

    def test_returns_none_for_empty_string(self) -> None:
        """Empty string → None"""
        result = extract_tool_call("")
        assert result is None

    def test_returns_none_for_non_tool_json(self) -> None:
        """JSON that isn't a tool call (e.g. a config dump) → None"""
        result = extract_tool_call('{"status": "ok", "code": 200}')
        assert result is None

    def test_multiline_arguments(self) -> None:
        """Tool call with nested/multiline arguments."""
        text = """`\`\`json
{
    "name": "create_script",
    "arguments": {
        "filename": "check_nginx.sh",
        "content": "#!/bin/bash\\necho hi",
        "description": "A test script"
    }
}
`\`\`""".replace("`\\`\\`", "``" + "`")
        # Build the text cleanly without escape confusion
        raw = (
            '```json\n'
            '{\n'
            '    "name": "create_script",\n'
            '    "arguments": {\n'
            '        "filename": "check_nginx.sh",\n'
            '        "content": "#!/bin/bash\\necho hi",\n'
            '        "description": "A test script"\n'
            '    }\n'
            '}\n'
            '```'
        )
        result = extract_tool_call(raw)
        assert result is not None
        assert result["name"] == "create_script"
        assert result["arguments"]["filename"] == "check_nginx.sh"

    # ── Alternative schema normalisation ───────────────────────────────────

    def test_alt_schema_tool_parameters(self) -> None:
        """{"tool": ..., "parameters": ...} is normalised to canonical form."""
        text = '{"tool": "get_disk_usage", "parameters": {"path": "/var"}}'
        result = extract_tool_call(text)
        assert result == {"name": "get_disk_usage", "arguments": {"path": "/var"}}

    def test_alt_schema_function_arguments(self) -> None:
        """{"function": ..., "arguments": ...} is normalised."""
        text = '{"function": "get_memory_usage", "arguments": {}}'
        result = extract_tool_call(text)
        assert result == {"name": "get_memory_usage", "arguments": {}}

    def test_alt_schema_function_name(self) -> None:
        """{"function_name": ..., "arguments": ...} is normalised."""
        text = '{"function_name": "list_failed_services", "arguments": {}}'
        result = extract_tool_call(text)
        assert result == {"name": "list_failed_services", "arguments": {}}

    def test_alt_schema_action_action_input(self) -> None:
        """{"action": ..., "action_input": {...}} (ReAct style) is normalised."""
        text = '{"action": "ping_host", "action_input": {"host": "8.8.8.8"}}'
        result = extract_tool_call(text)
        assert result == {"name": "ping_host", "arguments": {"host": "8.8.8.8"}}

    def test_alt_schema_action_input_as_json_string(self) -> None:
        """action_input as a JSON string is parsed into a dict."""
        text = '{"action": "ping_host", "action_input": "{\\"host\\": \\"1.1.1.1\\"}"}'
        result = extract_tool_call(text)
        assert result is not None
        assert result["name"] == "ping_host"
        assert result["arguments"]["host"] == "1.1.1.1"

    def test_prose_with_backtick_fence_embedded(self) -> None:
        """Prose before and after a fenced block — the JSON is still extracted."""
        text = (
            "Of course! I'll call the tool for you:\n"
            "```json\n"
            '{"name": "list_network_sockets", "arguments": {}}\n'
            "```\n"
            "This will list all listening ports."
        )
        result = extract_tool_call(text)
        assert result == {"name": "list_network_sockets", "arguments": {}}

    def test_invalid_json_inside_fence_returns_none(self) -> None:
        """Malformed JSON inside a fence → None (not an exception)."""
        text = "```json\n{name: get_disk_usage, arguments: {}}\n```"
        result = extract_tool_call(text)
        assert result is None


# ---------------------------------------------------------------------------
# is_final_answer
# ---------------------------------------------------------------------------


class TestIsFinalAnswer:
    def test_plain_text_is_final(self) -> None:
        assert is_final_answer("The disk usage looks healthy.") is True

    def test_json_tool_call_is_not_final(self) -> None:
        assert is_final_answer(RAW_JSON) is False

    def test_fenced_tool_call_is_not_final(self) -> None:
        assert is_final_answer(f"```json\n{RAW_JSON}\n```") is False

    def test_prose_with_tool_call_is_not_final(self) -> None:
        text = f"Let me check: {RAW_JSON}"
        assert is_final_answer(text) is False

    def test_json_without_tool_shape_is_final(self) -> None:
        """JSON that can't be parsed as a tool call → treated as final answer."""
        assert is_final_answer('{"result": "done", "code": 0}') is True

    def test_empty_string_is_final(self) -> None:
        assert is_final_answer("") is True
