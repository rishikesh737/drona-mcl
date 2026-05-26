"""
core.validator — Bash script validation and safety heuristics.

Two independent checks are applied to every script before it is written
to disk:

  1. Syntax check  : `bash -n` — fast, no execution, catches parse errors.
  2. Safety check  : Regex-based scan for network operations (mount, curl,
                     wget, rsync, ssh, nfs, cifs, smb) that lack a preceding
                     ping-based connectivity check. This enforces the
                     project-wide scripting standard.

The public interface is intentionally narrow:
  validate_bash(content)    -> tuple[bool, str]   (syntax only)
  check_network_safety(content) -> tuple[bool, str] (heuristic)
  validate_script(content)  -> tuple[bool, str]   (both, in order)
"""
from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

from core.config_loader import load_config

# ---------------------------------------------------------------------------
# Module-level config (loaded once)
# ---------------------------------------------------------------------------

_cfg = None
_NETWORK_PATTERNS: list[re.Pattern[str]] = []
_PING_SENTINEL: str = ""

def _get_config():
    global _cfg, _NETWORK_PATTERNS, _PING_SENTINEL
    if _cfg is None:
        _cfg = load_config()
        _NETWORK_PATTERNS = [
            re.compile(p, re.IGNORECASE | re.MULTILINE)
            for p in _cfg.validator.network_operation_patterns
        ]
        _PING_SENTINEL = _cfg.validator.ping_sentinel


# ---------------------------------------------------------------------------
# 1. Syntax validation via `bash -n`
# ---------------------------------------------------------------------------


def validate_bash(content: str) -> tuple[bool, str]:
    """Validate *content* as a bash script using ``bash -n``.

    ``bash -n`` reads commands but does not execute them. It is the
    standard tool for catching syntax errors without side effects.

    The script is written to a temporary file inside the project's
    ai_workspace directory so that no temp files escape the project root.

    Args:
        content: The full text of the bash script to validate.

    Returns:
        ``(True, "")`` if the script is syntactically valid.
        ``(False, stderr_message)`` if ``bash -n`` returns non-zero.

    Note:
        This function never raises. All subprocess exceptions are caught
        and returned as error strings.
    """
    _get_config()
    workspace: Path = _cfg.paths.ai_workspace
    workspace.mkdir(parents=True, exist_ok=True)

    tmp_path: Path | None = None
    try:
        # Use a NamedTemporaryFile kept inside ai_workspace
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".sh",
            dir=workspace,
            delete=False,
            encoding="utf-8",
        ) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)

        result = subprocess.run(
            ["bash", "-n", str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode == 0:
            return True, ""

        stderr = result.stderr.strip() or "bash -n returned non-zero exit code."
        return False, f"Syntax error: {stderr}"

    except subprocess.TimeoutExpired:
        return False, "Syntax check timed out after 10 seconds."
    except OSError as exc:
        return False, f"OS error during syntax check: {exc}"
    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink()


# ---------------------------------------------------------------------------
# 2. Network-safety heuristic
# ---------------------------------------------------------------------------


def _find_first_network_op(content: str) -> int | None:
    """Return the character offset of the first network operation in *content*.

    Returns None if no network operation is found.
    """
    first: int | None = None
    for pattern in _NETWORK_PATTERNS:
        match = pattern.search(content)
        if match:
            if first is None or match.start() < first:
                first = match.start()
    return first


def _find_ping_check(content: str) -> int | None:
    """Return the character offset of the ping sentinel in *content*.

    Returns None if the sentinel is not present.
    """
    idx = content.find(_PING_SENTINEL)
    return idx if idx != -1 else None


def check_network_safety(content: str) -> tuple[bool, str]:
    """Verify that any network operations in *content* are preceded by a
    ping-based connectivity check.

    The check is:
      1. If no network operations are detected → pass (True, "").
      2. If network operations are found but no ping sentinel exists → fail.
      3. If the ping sentinel appears *after* the first network operation → fail.
      4. Otherwise → pass.

    Args:
        content: The full text of the bash script.

    Returns:
        ``(True, "")`` if the script is safe or contains no network ops.
        ``(False, reason)`` describing which network pattern was found and
        where the ping check is missing.
    """
    _get_config()
    net_offset = _find_first_network_op(content)
    if net_offset is None:
        # No network operations detected; heuristic passes.
        return True, ""

    ping_offset = _find_ping_check(content)

    if ping_offset is None:
        # Identify which pattern was found for a clear error message.
        for pattern in _NETWORK_PATTERNS:
            match = pattern.search(content)
            if match:
                op = match.group(0)
                break
        else:
            op = "unknown network operation"
        return (
            False,
            f"Safety violation: script contains '{op}' but is missing a "
            f"ping-based connectivity check ('{_PING_SENTINEL}' not found). "
            "Add a ping check before any network or mount operation to "
            "prevent indefinite hangs on unreachable hosts.",
        )

    if ping_offset > net_offset:
        return (
            False,
            f"Safety violation: ping check ('{_PING_SENTINEL}') appears "
            f"at offset {ping_offset}, which is AFTER the first network "
            f"operation at offset {net_offset}. Move the ping check to "
            "the top of the script, before any mount/curl/wget/ssh/rsync call.",
        )

    return True, ""


# ---------------------------------------------------------------------------
# 3. Combined validator (public primary interface)
# ---------------------------------------------------------------------------


def validate_script(content: str) -> tuple[bool, str]:
    """Run both syntax validation and network-safety checks on *content*.

    Checks are applied in order:
      1. Syntax (bash -n)    — fast, fails early on parse errors.
      2. Network safety      — heuristic, applied only if syntax passes.

    Args:
        content: The full text of the bash script to validate.

    Returns:
        ``(True, "")`` if all checks pass.
        ``(False, reason)`` with a human-readable explanation of the first
        failure encountered.
    """
    ok, msg = validate_bash(content)
    if not ok:
        return False, msg

    ok, msg = check_network_safety(content)
    if not ok:
        return False, msg

    return True, ""
