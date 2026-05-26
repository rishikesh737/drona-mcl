"""
tools.network — Network diagnostic tools.

All functions use subprocess list form, set explicit timeouts, capture
output, and return errors as strings. No shell=True anywhere.

Available tools:
  list_network_sockets()           → ss -tulnp output
  ping_host(host, count)           → ICMP reachability check
  curl_health_check(url, timeout)  → HTTP GET status + truncated body
"""
from __future__ import annotations

import subprocess


# ---------------------------------------------------------------------------
# Internal helper (mirrors the one in system.py — kept separate to avoid
# a cross-tool import dependency)
# ---------------------------------------------------------------------------


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """Run *cmd* and return (returncode, stdout, stderr).

    On subprocess exceptions the return code is set to -1 and the
    exception message is placed in stderr.
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", f"Command timed out after {timeout} seconds."
    except FileNotFoundError:
        return -1, "", f"Command not found: '{cmd[0]}'. Is it installed?"
    except OSError as exc:
        return -1, "", f"OS error: {exc}"


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def list_network_sockets() -> str:
    """List all listening TCP/UDP sockets using ``ss -tulnp``.

    Returns:
        Formatted socket table, or an error string.
    """
    rc, stdout, stderr = _run(["ss", "-tulnp"], timeout=15)
    if rc != 0:
        return f"[ERROR] ss failed (exit {rc}): {stderr}"
    output = stdout if stdout else "(no listening sockets found)"
    return f"=== Listening network sockets (ss -tulnp) ===\n{output}"


def ping_host(host: str, count: int = 3) -> str:
    """Check if *host* is reachable via ICMP ping.

    Args:
        host:  Hostname or IP address to ping.
        count: Number of packets to send (clamped to 1–10).

    Returns:
        Ping output including latency statistics, or an error string.
    """
    if not host:
        return "[ERROR] 'host' parameter is required for ping_host."

    count = max(1, min(int(count), 10))  # clamp for safety

    rc, stdout, stderr = _run(
        ["ping", "-c", str(count), "-W", "3", host],
        timeout=count * 5 + 5,
    )

    header = f"=== ping {host} (count={count}) ==="
    if rc == 0:
        return f"{header}\n{stdout}"

    detail = stdout or stderr or f"ping exited with code {rc}"
    return f"{header}\n[UNREACHABLE] {detail}"


def curl_health_check(url: str, timeout: int = 10) -> str:
    """Perform an HTTP GET against *url* using curl.

    The response body is truncated to 2 048 characters to avoid flooding
    the model context window.

    Args:
        url:     Full URL to check (e.g. 'http://localhost:8080/health').
        timeout: Request timeout in seconds (clamped to 1–60).

    Returns:
        HTTP status code, headers summary, and truncated response body.
    """
    if not url:
        return "[ERROR] 'url' parameter is required for curl_health_check."

    timeout = max(1, min(int(timeout), 60))

    # -s: silent, -o /dev/null suppressed, -w: write-out format for status
    # We collect both the body and status code in one call by appending
    # the status code to the end of stdout.
    rc, stdout, stderr = _run(
        [
            "curl",
            "-s",
            "-w", "\n%{http_code}",
            "--max-time", str(timeout),
            url,
        ],
        timeout=timeout + 5,
    )

    if rc != 0:
        return (
            f"[ERROR] curl failed (exit {rc}): "
            f"{stderr or 'unknown error'}\nURL: {url}"
        )

    # Output might be empty, or have body then \n then status code.
    # Split from the right.
    parts = stdout.rsplit("\n", 1)
    if len(parts) == 2:
        body, http_code = parts
    else:
        body = ""
        http_code = parts[0] if parts else "000"

    truncated = body[:2048]
    if len(body) > 2048:
        truncated += f"\n... [truncated — {len(body) - 2048} bytes omitted]"

    return (
        f"=== HTTP health check: {url} ===\n"
        f"Status: {http_code}\n"
        f"Body:\n{truncated}"
    )
