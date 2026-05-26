"""
tools.system — System introspection tools.

All functions in this module:
  - Use subprocess list form exclusively (no shell=True).
  - Set capture_output=True, text=True, and a timeout on every call.
  - Never raise exceptions to the caller; errors are returned as strings.
  - Return human-readable string output suitable for the LLM context window.

Available tools:
  get_journal_logs(unit, lines)  → recent systemd journal entries
  get_service_status(unit)       → systemctl status for one unit
  list_failed_services()         → all failed systemd units
  get_disk_usage(path)           → df -h output
  get_memory_usage()             → free -h output
"""
from __future__ import annotations

import subprocess


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------


def _run(cmd: list[str], timeout: int = 30) -> str:
    """Run *cmd* and return combined stdout/stderr as a single string.

    On subprocess errors the exception message is returned rather than
    raised, so the agent loop can reason about it.
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout.strip()
        if result.returncode != 0:
            stderr = result.stderr.strip()
            note = f"\n[Process exited with code {result.returncode}]"
            if stderr:
                note += f"\nstderr: {stderr}"
            return output + note if output else note.strip()
        return output if output else "(no output)"
    except subprocess.TimeoutExpired:
        return f"[ERROR] Command {cmd!r} timed out after {timeout} seconds."
    except FileNotFoundError:
        return f"[ERROR] Command not found: '{cmd[0]}'. Is it installed?"
    except OSError as exc:
        return f"[ERROR] OS error running {cmd!r}: {exc}"


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def get_journal_logs(unit: str = "", lines: int = 50) -> str:
    """Read recent systemd journal entries.

    Args:
        unit:  Systemd unit name to filter by (e.g. 'nginx.service').
               Empty string reads the global journal.
        lines: Number of most-recent log lines to return.

    Returns:
        A string containing the requested journal entries, or an error
        message if journalctl is unavailable.
    """
    lines = max(1, min(int(lines), 500))  # clamp to sane bounds

    cmd = ["journalctl", "--no-pager", "-n", str(lines), "--output=short-iso"]
    if unit:
        cmd.extend(["-u", unit])

    header = f"=== Journal logs (last {lines} lines"
    if unit:
        header += f", unit={unit}"
    header += ") ==="

    return f"{header}\n{_run(cmd)}"


def get_service_status(unit: str) -> str:
    """Check the systemd status of a named service unit.

    Args:
        unit: Systemd unit name (e.g. 'sshd.service').

    Returns:
        Full `systemctl status` output for the unit.
    """
    if not unit:
        return "[ERROR] 'unit' parameter is required for get_service_status."

    cmd = ["systemctl", "status", "--no-pager", "--full", unit]
    return f"=== systemctl status {unit} ===\n{_run(cmd)}"


def list_failed_services() -> str:
    """List all systemd units currently in a failed state.

    Returns:
        Output of `systemctl list-units --state=failed`, or a message
        indicating that no failed units were found.
    """
    cmd = [
        "systemctl",
        "list-units",
        "--state=failed",
        "--no-pager",
        "--no-legend",
    ]
    output = _run(cmd)
    if not output or output == "(no output)":
        return "=== Failed services ===\nNo failed systemd units found."
    return f"=== Failed systemd units ===\n{output}"


def get_disk_usage(path: str = "") -> str:
    """Report disk usage via `df -h`.

    Args:
        path: Specific mount point or path to report on.
              Empty string reports all mounted filesystems.

    Returns:
        Human-readable disk usage table.
    """
    cmd = ["df", "-h"]
    if path:
        cmd.append(path)

    header = "=== Disk usage"
    if path:
        header += f" ({path})"
    header += " ==="

    return f"{header}\n{_run(cmd)}"


def get_memory_usage() -> str:
    """Report current memory and swap usage via `free -h`.

    Returns:
        Human-readable memory usage table.
    """
    return f"=== Memory usage ===\n{_run(['free', '-h'])}"
