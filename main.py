"""
main.py — Drona CLI entry point.

Usage:
    python main.py "Check which services are failing and why"
    python main.py --task "Show disk usage on all mounts"
    python main.py --config /path/to/custom/config.toml "..."

This file contains ONLY argument parsing and a call into core.run_agent.
No business logic lives here.
"""
from __future__ import annotations

import argparse
import logging
import sys

from rich.console import Console

_console = Console()


def _build_parser() -> argparse.ArgumentParser:
    """Build and return the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="drona",
        description=(
            "Drona — a fully local Linux SysAdmin agent powered by Ollama.\n"
            "All processing stays on your machine. No data leaves."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            '  python main.py "Why is nginx failing?"\n'
            '  python main.py --task "Check disk usage"\n'
            '  python main.py --verbose "List all failed services"\n'
        ),
    )
    parser.add_argument(
        "task",
        nargs="?",
        help="The sysadmin task to perform (positional).",
    )
    parser.add_argument(
        "--task",
        dest="task_flag",
        metavar="TASK",
        help="The sysadmin task to perform (named flag; alternative to positional).",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose debug logging to the console.",
    )
    return parser


def _configure_logging(verbose: bool) -> None:
    """Set up root logger based on verbosity flag."""
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(name)s [%(levelname)s] %(message)s",
    )


def main() -> int:
    """Parse arguments, run the agent, return an exit code."""
    parser = _build_parser()
    args = parser.parse_args()

    # Resolve task from positional or named flag
    task: str | None = args.task or args.task_flag
    if not task:
        parser.print_help()
        _console.print(
            "\n[bold red]Error:[/bold red] Please provide a task. "
            'Example: python main.py "Check disk usage"'
        )
        return 1

    task = task.strip()
    if not task:
        _console.print("[bold red]Error:[/bold red] Task must not be empty.")
        return 1

    _configure_logging(args.verbose)

    # Import here so logging is configured before module-level code runs
    from core.agent import run_agent  # noqa: PLC0415

    try:
        run_agent(task)
        return 0
    except KeyboardInterrupt:
        _console.print("\n[yellow]Interrupted by user.[/yellow]")
        return 130


if __name__ == "__main__":
    sys.exit(main())
