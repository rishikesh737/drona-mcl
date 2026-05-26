"""
tools.scripts — Script lifecycle management tools.

This module handles the complete lifecycle of agent-generated bash scripts:
  create_script   : validate → backup → write to ai_workspace
  execute_script  : rich confirmation prompt → subprocess execution
  rollback_script : restore from .bak backup
  list_scripts    : enumerate files in ai_workspace

Security invariants enforced here:
  1. No script executes without explicit user confirmation ("yes" at the prompt).
  2. Every script passes validate_script() (bash -n + ping-check) before write.
  3. The ai_workspace path is always resolved from config — never from user input.
  4. Script filenames are sanitised: path separators and null bytes are rejected.
  5. Script arguments passed to execute_script are validated as plain strings.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

from core.config_loader import load_config
from core.validator import validate_script

_cfg = load_config()
_WORKSPACE: Path = _cfg.paths.ai_workspace
_console = Console()

# Maximum number of bytes read back from a script execution for the result.
_MAX_OUTPUT_BYTES = 8192


# ---------------------------------------------------------------------------
# Filename sanitisation
# ---------------------------------------------------------------------------


def _sanitise_filename(filename: str) -> tuple[bool, str]:
    """Ensure *filename* is a safe, flat filename with no path components.

    Returns:
        (True, clean_filename) on success.
        (False, error_message) if the filename is rejected.
    """
    if not filename:
        return False, "Filename must not be empty."
    if "/" in filename or "\\" in filename:
        return False, f"Filename '{filename}' must not contain path separators."
    if "\x00" in filename:
        return False, "Filename must not contain null bytes."
    if not filename.endswith(".sh"):
        filename = filename + ".sh"
    if len(filename) > 128:
        return False, "Filename exceeds 128 characters."
    return True, filename


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def create_script(
    filename: str,
    content: str,
    description: str,
) -> str:
    """Validate and write a bash script to ai_workspace.

    Steps:
      1. Sanitise the filename.
      2. Run validate_script() (bash -n + network-safety heuristic).
      3. If the target file already exists, write a .bak backup.
      4. Write the script with executable permissions (0o755).

    Args:
        filename:    Destination filename (e.g. 'fix_nginx.sh').
        content:     Complete bash script text including shebang.
        description: One-sentence description logged alongside the script.

    Returns:
        A success message or a detailed error string.
    """
    ok, filename = _sanitise_filename(filename)
    if not ok:
        return f"[ERROR] Invalid filename: {filename}"

    # Ensure workspace exists
    _WORKSPACE.mkdir(parents=True, exist_ok=True)

    # Validate before touching disk
    valid, validation_msg = validate_script(content)
    if not valid:
        return (
            f"[ERROR] Script validation failed for '{filename}'.\n"
            f"Reason: {validation_msg}\n"
            "The script was NOT written to disk. Fix the issue and try again."
        )

    target: Path = _WORKSPACE / filename

    # Backup existing file if present
    bak_created = False
    if target.exists():
        bak_path = target.with_suffix(".sh.bak")
        try:
            bak_path.write_bytes(target.read_bytes())
            bak_created = True
        except OSError as exc:
            return f"[ERROR] Could not create backup of existing '{filename}': {exc}"

    # Write the validated script
    try:
        target.write_text(content, encoding="utf-8")
        target.chmod(0o755)
    except OSError as exc:
        return f"[ERROR] Could not write script '{filename}': {exc}"

    bak_note = f" (backup saved as {filename}.bak)" if bak_created else ""
    return (
        f"[OK] Script '{filename}' written to ai_workspace{bak_note}.\n"
        f"Description: {description}\n"
        f"Size: {len(content)} bytes\n"
        "Use execute_script to run it after reviewing."
    )


def execute_script(
    filename: str,
    args: list[str] | None = None,
) -> str:
    """Execute a script from ai_workspace after user confirmation.

    A rich-rendered preview of the script is shown to the user before
    they are prompted to approve execution. The script does NOT run if
    the user does not type "yes".

    Args:
        filename: Name of the script file in ai_workspace.
        args:     Optional positional arguments to pass to the script.

    Returns:
        Execution output (stdout + stderr, truncated to 8 KB) or an
        error/cancellation message.
    """
    ok, filename = _sanitise_filename(filename)
    if not ok:
        return f"[ERROR] Invalid filename: {filename}"

    args = [str(a) for a in (args or [])]

    script_path: Path = _WORKSPACE / filename
    if not script_path.exists():
        return (
            f"[ERROR] Script '{filename}' not found in ai_workspace. "
            "Use create_script first."
        )

    # Show the script to the user for review
    content = script_path.read_text(encoding="utf-8")
    _console.print()
    _console.print(
        Panel(
            Syntax(content, "bash", theme="monokai", line_numbers=True),
            title=f"[bold yellow]⚠ Script ready to execute: {filename}[/bold yellow]",
            border_style="yellow",
        )
    )
    if args:
        _console.print(f"[bold]Arguments:[/bold] {' '.join(args)}")
    _console.print()

    # Explicit user approval — required by Assumption D
    try:
        answer = input(
            "Type [bold]yes[/bold] to execute, or anything else to cancel: "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "[CANCELLED] Execution cancelled (EOF/interrupt)."

    if answer != "yes":
        return f"[CANCELLED] Execution of '{filename}' was cancelled by the user."

    cmd = [str(script_path)] + args
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,  # 5-minute upper bound for sysadmin scripts
        )
    except subprocess.TimeoutExpired:
        return f"[ERROR] Script '{filename}' timed out after 300 seconds."
    except PermissionError:
        return f"[ERROR] Could not execute '{filename}': Permission denied. Ensure the script is executable."
    except OSError as exc:
        return f"[ERROR] Could not execute '{filename}': {exc}"

    stdout = result.stdout[:_MAX_OUTPUT_BYTES]
    stderr = result.stderr[:_MAX_OUTPUT_BYTES]
    truncation_note = ""
    if len(result.stdout) > _MAX_OUTPUT_BYTES or len(result.stderr) > _MAX_OUTPUT_BYTES:
        truncation_note = "\n[output truncated to 8 KB]"

    lines = [f"=== Execution result: {filename} (exit code {result.returncode}) ==="]
    if stdout:
        lines.append(f"stdout:\n{stdout}")
    if stderr:
        lines.append(f"stderr:\n{stderr}")
    if truncation_note:
        lines.append(truncation_note)

    return "\n".join(lines)


def rollback_script(filename: str) -> str:
    """Restore a script from its .bak backup.

    This undoes the most recent create_script call for *filename* by
    overwriting the current file with the backup.

    Args:
        filename: Name of the script file to roll back.

    Returns:
        A success message or an error string.
    """
    ok, filename = _sanitise_filename(filename)
    if not ok:
        return f"[ERROR] Invalid filename: {filename}"

    script_path: Path = _WORKSPACE / filename
    bak_path = script_path.with_suffix(".sh.bak")

    if not bak_path.exists():
        return (
            f"[ERROR] No backup found for '{filename}'. "
            "A .bak file is only created when create_script overwrites an "
            "existing script."
        )

    try:
        script_path.write_bytes(bak_path.read_bytes())
        script_path.chmod(0o755)
    except OSError as exc:
        return f"[ERROR] Rollback failed for '{filename}': {exc}"

    return (
        f"[OK] '{filename}' has been restored from its backup. "
        f"The backup file '{filename}.bak' is still present."
    )


def list_scripts() -> str:
    """List all scripts currently in ai_workspace.

    Returns:
        A formatted listing of script files with sizes, or a message
        if ai_workspace is empty.
    """
    _WORKSPACE.mkdir(parents=True, exist_ok=True)

    scripts = sorted(_WORKSPACE.glob("*.sh"))
    if not scripts:
        return "=== ai_workspace ===\n(no scripts found)"

    lines = ["=== Scripts in ai_workspace ==="]
    for path in scripts:
        size = path.stat().st_size
        bak = " [has backup]" if path.with_suffix(".sh.bak").exists() else ""
        lines.append(f"  {path.name:<40} {size:>8} bytes{bak}")

    return "\n".join(lines)
