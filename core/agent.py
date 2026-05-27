"""
core.agent — Recursive agentic loop with max_iterations guard.

The agent maintains a conversation history (a list of message dicts) and
iterates as follows:

  1. Call Ollama with the current conversation + tool schemas.
  2. Pass the response to the MCL router.
  3. If the MCL returns a tool result → append the tool messages and loop.
  4. If the MCL returns a final answer → return it to the caller.
  5. If a duplicate tool call is detected (same name + identical args as the
     immediately preceding iteration) → intercept and force graceful exit.
  6. If max_iterations is reached → force-stop and return a timeout notice.

The agent loop has no knowledge of specific tools; all tool dispatch is
delegated to the MCL and the tool registry.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncGenerator

import ollama
from rich.console import Console
from rich.panel import Panel
from rich.spinner import Spinner
from rich.live import Live
from rich.text import Text

from core.config_loader import load_config, DronaConfig
from core.mcl import MCLResult, build_tool_result_messages, route
from tools import TOOL_SCHEMAS

logger = logging.getLogger(__name__)

_console = Console()


# ---------------------------------------------------------------------------
# Session logger (appends to drona.log)
# ---------------------------------------------------------------------------


def _get_file_logger(cfg: DronaConfig) -> logging.Logger:
    """Configure and return a file logger for this session."""
    log_path = cfg.paths.log_file
    log_path.parent.mkdir(parents=True, exist_ok=True)

    file_logger = logging.getLogger("drona.session")
    if not file_logger.handlers:
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        file_logger.addHandler(handler)
        file_logger.setLevel(logging.DEBUG)

    return file_logger


# ---------------------------------------------------------------------------
# Rich rendering helpers
# ---------------------------------------------------------------------------


def _render_tool_call(path: str, result: Any) -> None:
    """Print a compact panel describing a dispatched tool call."""
    path_badge = (
        "[bold green]Path A[/bold green]"
        if path == "A"
        else "[bold yellow]Path B[/bold yellow]"
    )
    _console.print(
        Panel(
            f"[bold]{result.tool_name}[/bold]({result.arguments})\n\n"
            f"[dim]{result.output[:400]}{'...' if len(result.output) > 400 else ''}[/dim]",
            title=f"🔧 Tool call ({path_badge})",
            border_style="blue",
            expand=False,
        )
    )


def _render_final_answer(text: str) -> None:
    """Print the model's final answer in a styled panel."""
    _console.print(
        Panel(
            text,
            title="[bold green]✓ Drona[/bold green]",
            border_style="green",
        )
    )


def _render_iteration_header(iteration: int, max_iter: int) -> None:
    """Print a subtle iteration counter."""
    _console.print(
        f"[dim]  Iteration {iteration}/{max_iter}[/dim]",
        justify="right",
    )


# ---------------------------------------------------------------------------
# Core agentic loop
# ---------------------------------------------------------------------------


def _build_initial_messages(
    system_prompt: str,
    user_task: str,
) -> list[dict[str, Any]]:
    """Construct the initial conversation history."""
    return [
        {"role": "user", "content": f"{system_prompt}\n\nTask: {user_task}"},
    ]


def _call_ollama(
    client: ollama.Client,
    model: str,
    messages: list[dict[str, Any]],
    request_timeout: int,
    think: bool = False,
) -> ollama.ChatResponse:
    return client.chat(
        model=model,
        messages=messages,
        tools=TOOL_SCHEMAS,
        think=think,
    )


def _append_assistant_message(
    messages: list[dict[str, Any]],
    raw_content: str,
    tool_calls: list[Any] | None,
) -> None:
    """Append the assistant's turn to conversation history."""
    msg: dict[str, Any] = {"role": "assistant", "content": raw_content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    messages.append(msg)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _call_ollama_sync(
    client: ollama.Client,
    model: str,
    messages: list[dict[str, Any]],
    think: bool = False,
) -> ollama.ChatResponse:
    return client.chat(
        model=model,
        messages=messages,
        tools=TOOL_SCHEMAS,
        think=think,
    )

class _FakeMessage:
    def __init__(self, content: str, thinking: str | None, tool_calls: list[Any] | None):
        self.role = "assistant"
        self.content = content
        self.thinking = thinking
        self.tool_calls = tool_calls

class _FakeResponse:
    def __init__(self, msg: _FakeMessage):
        self.message = msg

async def run_agent(task: str) -> AsyncGenerator[dict[str, Any], None]:
    """Run the Drona agentic loop for a given sysadmin *task*.

    Yields:
        Dictionaries containing state updates, tokens, and tool results
        for downstream streaming consumers.
    """
    cfg = load_config()
    session_log = _get_file_logger(cfg)

    session_log.info("=== New session | task: %s", task)

    client = ollama.AsyncClient(host=cfg.ollama.host)
    messages = _build_initial_messages(cfg.agent.system_prompt, task)

    _console.print()
    _console.print(
        Panel(
            f"[bold]{task}[/bold]",
            title="[bold cyan]🤖 Drona Agent[/bold cyan]",
            border_style="cyan",
        )
    )

    yield {"type": "start", "task": task}

    # Tracks the canonical fingerprint of the previous iteration's tool calls.
    # A frozenset of (tool_name, frozen_args) tuples; None on the first pass.
    previous_tool_calls: frozenset | None = None

    for iteration in range(1, cfg.agent.max_iterations + 1):
        _render_iteration_header(iteration, cfg.agent.max_iterations)
        yield {"type": "iteration", "iteration": iteration, "max_iterations": cfg.agent.max_iterations}

        # ── LLM call with spinner ────────────────────────────────────────
        raw_content = ""
        thinking_content = ""
        tool_calls = []

        try:
            stream = await client.chat(
                model=cfg.ollama.model,
                messages=messages,
                tools=TOOL_SCHEMAS,
                think=cfg.ollama.think,
                stream=True,
            )
            with Live(
                Spinner("dots", text=Text(" Thinking…", style="dim")),
                console=_console,
                refresh_per_second=10,
            ):
                async for chunk in stream:
                    msg = chunk.message
                    if getattr(msg, "content", None):
                        raw_content += msg.content
                        yield {"type": "token", "content": msg.content}
                    if getattr(msg, "thinking", None):
                        thinking_content += msg.thinking
                        yield {"type": "think_token", "content": msg.thinking}
                    if getattr(msg, "tool_calls", None):
                        tool_calls.extend(msg.tool_calls)
            
            response = _FakeResponse(
                _FakeMessage(
                    content=raw_content,
                    thinking=thinking_content if thinking_content else None,
                    tool_calls=tool_calls if tool_calls else None
                )
            )

        except ollama.ResponseError as exc:
            err = f"[ERROR] Ollama API error: {exc}"
            session_log.error(err)
            _console.print(f"[red]{err}[/red]")
            yield {"type": "error", "content": err}
            return
        except Exception as exc:  # noqa: BLE001
            err = (
                f"[ERROR] Could not connect to Ollama at "
                f"'{cfg.ollama.host}'. Is Ollama running?\nDetail: {exc}"
            )
            session_log.error(err)
            _console.print(f"[red]{err}[/red]")
            yield {"type": "error", "content": err}
            return
        except BaseException as exc:
            err = f"[ERROR] Uncaught exception during LLM call: {exc}"
            session_log.error(err)
            _console.print(f"[red]{err}[/red]")
            yield {"type": "error", "content": err}
            return

        # ── MCL routing ──────────────────────────────────────────────────
        mcl_result: MCLResult = route(response)
        session_log.info(
            "Iteration %d | MCL path: %s | tools: %s",
            iteration,
            mcl_result.path,
            [r.tool_name for r in mcl_result.tool_results],
        )

        # ── Final answer ─────────────────────────────────────────────────
        if mcl_result.path == "final":
            _render_final_answer(mcl_result.final_text)
            session_log.info("Final answer returned after %d iteration(s).", iteration)
            yield {"type": "final_answer", "content": mcl_result.final_text}
            return

        # ── Tool call(s) dispatched ──────────────────────────────────────

        # Build a canonical fingerprint of this iteration's tool calls.
        # Arguments are converted to a sorted tuple of items so that key
        # ordering differences (which the model produces freely) do not
        # produce false negatives.
        current_tool_calls: frozenset = frozenset(
            (
                tr.tool_name,
                tuple(sorted(tr.arguments.items()))
                if isinstance(tr.arguments, dict)
                else repr(tr.arguments),
            )
            for tr in mcl_result.tool_results
        )

        # ── Duplicate tool-call guard ────────────────────────────────────
        if current_tool_calls == previous_tool_calls:
            dup_msg = (
                "Duplicate tool call detected (identical name + args as "
                "previous iteration). Forcing graceful exit."
            )
            session_log.info(dup_msg)
            _console.print(
                f"[bold yellow]⚠ {dup_msg}[/bold yellow]"
            )
            # Surface the last successful tool output as a clean summary.
            last_outputs = "\n".join(
                f"• {tr.tool_name}: {tr.output.splitlines()[0]}"
                for tr in mcl_result.tool_results
            )
            summary = (
                f"Task complete. The following tool(s) executed successfully "
                f"on the previous iteration:\n{last_outputs}"
            )
            _render_final_answer(summary)
            session_log.info(
                "Graceful exit after duplicate detected at iteration %d.",
                iteration,
            )
            yield {"type": "final_answer", "content": summary}
            return

        # Update state for the next iteration.
        previous_tool_calls = current_tool_calls

        # Append assistant turn
        native_tool_calls = getattr(response.message, "tool_calls", None)
        _append_assistant_message(
            messages,
            mcl_result.raw_content,
            native_tool_calls,
        )

        # Render and append tool results
        tool_messages = build_tool_result_messages(mcl_result)
        for tr in mcl_result.tool_results:
            _render_tool_call(mcl_result.path, tr)
            yield {
                "type": "tool_call",
                "path": mcl_result.path,
                "tool_name": tr.tool_name,
                "arguments": tr.arguments,
                "output": tr.output
            }
            session_log.info(
                "Tool '%s' returned: %s",
                tr.tool_name,
                tr.output[:200],
            )

        messages.extend(tool_messages)

    # ── max_iterations reached ───────────────────────────────────────────
    timeout_msg = (
        f"[TIMEOUT] Drona reached the maximum iteration limit "
        f"({cfg.agent.max_iterations}). The task may be incomplete. "
        "Consider increasing max_iterations in config/config.toml or "
        "breaking the task into smaller steps."
    )
    session_log.warning(timeout_msg)
    _console.print(f"[bold yellow]{timeout_msg}[/bold yellow]")
    yield {"type": "error", "content": timeout_msg}
    return
