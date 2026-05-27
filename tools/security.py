"""
tools.security — Autonomous cybersecurity defense tools.

All functions use subprocess list form (no shell=True), set explicit
timeouts, capture output, and return errors as strings.

Input validation is performed BEFORE any subprocess is spawned:
  - IP addresses are validated against a strict IPv4/IPv6 regex.
  - Service names are validated against a safe-character allowlist.

Available tools:
  read_security_logs(service, lines)   → journalctl auth/security logs
  block_malicious_ip(ip_address)       → firewall-cmd permanent drop rule
"""
from __future__ import annotations

import re
import subprocess


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maximum log lines returnable in a single call (prevents context flooding).
_MAX_LOG_LINES: int = 500

# Firewall reload timeout — slightly longer than the rule addition to allow
# nftables/iptables backend to flush cleanly.
_FW_TIMEOUT: int = 30

# Strict allowlist for service names: alphanumeric, hyphens, underscores,
# dots. Prevents argument injection via a malformed service parameter.
_SERVICE_NAME_RE: re.Pattern = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")

# RFC 791 IPv4 — strict per-octet range check.
_IPV4_RE: re.Pattern = re.compile(
    r"^(?:(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)$"
)

# RFC 4291 IPv6 — accepts full, compressed (::), and mixed notations.
_IPV6_RE: re.Pattern = re.compile(
    r"^("
    # Full 8-group form
    r"([0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}|"
    # Compressed :: forms
    r"([0-9a-fA-F]{1,4}:){1,7}:|"
    r":([0-9a-fA-F]{1,4}:){1,6}[0-9a-fA-F]{1,4}|"  # noqa: E501 — intentional
    r"([0-9a-fA-F]{1,4}:){1,5}(:[0-9a-fA-F]{1,4}){1,2}|"
    r"([0-9a-fA-F]{1,4}:){1,4}(:[0-9a-fA-F]{1,4}){1,3}|"
    r"([0-9a-fA-F]{1,4}:){1,3}(:[0-9a-fA-F]{1,4}){1,4}|"
    r"([0-9a-fA-F]{1,4}:){1,2}(:[0-9a-fA-F]{1,4}){1,5}|"
    r"[0-9a-fA-F]{1,4}:(:[0-9a-fA-F]{1,4}){1,6}|"
    r":(:[0-9a-fA-F]{1,4}){1,7}|::|"
    # IPv4-mapped / IPv4-compatible
    r"::ffff:(25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)"
    r"(\.(25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)){3}"
    r")$"
)


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """Run *cmd* without shell=True and return (returncode, stdout, stderr).

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
    except Exception as exc:  # noqa: BLE001
        return -1, "", f"Unexpected error running {cmd[0]}: {exc}"


def _is_valid_ip(ip: str) -> bool:
    """Return True if *ip* is a valid IPv4 or IPv6 address."""
    return bool(_IPV4_RE.match(ip) or _IPV6_RE.match(ip))


def _detect_ip_family(ip: str) -> str:
    """Return 'ipv4' or 'ipv6' for a validated IP string."""
    return "ipv4" if _IPV4_RE.match(ip) else "ipv6"


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def read_security_logs(service: str = "sshd", lines: int = 100) -> str:
    """Fetch recent journal logs for a security-relevant systemd service.

    Uses ``journalctl -n {lines} -u {service}.service --no-pager``
    without shell=True to prevent argument injection.

    Args:
        service: Systemd service name without the ``.service`` suffix
                 (e.g. ``'sshd'``, ``'firewalld'``, ``'auditd'``).
                 Validated against a strict alphanumeric allowlist.
        lines:   Number of most-recent log lines to return (1–500).

    Returns:
        Formatted log output, or an ``[ERROR]`` string on failure.
    """
    # ── Input validation ────────────────────────────────────────────────
    if not service or not isinstance(service, str):
        return "[ERROR] 'service' must be a non-empty string."

    service = service.strip()
    # Strip trailing .service so callers can pass either form
    if service.endswith(".service"):
        service = service[: -len(".service")]

    if not _SERVICE_NAME_RE.match(service):
        return (
            f"[ERROR] Invalid service name '{service}'. "
            "Only alphanumeric characters, hyphens, underscores, and dots are allowed."
        )

    lines = max(1, min(int(lines), _MAX_LOG_LINES))
    unit = f"{service}.service"

    # ── Execution ───────────────────────────────────────────────────────
    rc, stdout, stderr = _run(
        ["journalctl", "-n", str(lines), "-u", unit, "--no-pager"],
        timeout=20,
    )

    header = f"=== Security logs: {unit} (last {lines} lines) ==="

    if rc != 0:
        detail = stderr or f"journalctl exited with code {rc}."
        return f"{header}\n[ERROR] {detail}"

    output = stdout if stdout else f"(no log entries found for {unit})"
    return f"{header}\n{output}"


def block_malicious_ip(ip_address: str) -> str:
    """Permanently block an IP address using firewalld rich rules.

    Performs two operations in sequence:
      1. ``sudo firewall-cmd --permanent --add-rich-rule='...'``
      2. ``sudo firewall-cmd --reload``

    The ``--permanent`` flag writes the rule to persistent storage so it
    survives reboots. The reload activates it in the running firewall.

    Args:
        ip_address: A valid IPv4 or IPv6 address string. Validated by
                    regex before any subprocess is spawned; invalid
                    input is rejected immediately.

    Returns:
        A success confirmation string, or an ``[ERROR]`` string
        describing the failure point (validation / rule-add / reload).
    """
    # ── Input validation ────────────────────────────────────────────────
    if not ip_address or not isinstance(ip_address, str):
        return "[ERROR] 'ip_address' must be a non-empty string."

    ip_address = ip_address.strip()

    if not _is_valid_ip(ip_address):
        return (
            f"[ERROR] '{ip_address}' is not a valid IPv4 or IPv6 address. "
            "Provide a bare IP without port or CIDR suffix."
        )

    family = _detect_ip_family(ip_address)

    # ── Build rich-rule string (no shell interpolation — passed as a
    #    single list element to subprocess) ────────────────────────────
    rich_rule = (
        f'rule family="{family}" source address="{ip_address}" drop'
    )

    # ── Step 1: add permanent rule ──────────────────────────────────────
    rc_add, stdout_add, stderr_add = _run(
        [
            "sudo", "firewall-cmd",
            "--permanent",
            f"--add-rich-rule={rich_rule}",
        ],
        timeout=_FW_TIMEOUT,
    )

    if rc_add != 0:
        detail = stderr_add or stdout_add or f"exit code {rc_add}"
        return (
            f"[ERROR] firewall-cmd --add-rich-rule failed for {ip_address}: "
            f"{detail}"
        )

    # ── Step 2: reload to activate the rule ────────────────────────────
    rc_reload, stdout_reload, stderr_reload = _run(
        ["sudo", "firewall-cmd", "--reload"],
        timeout=_FW_TIMEOUT,
    )

    if rc_reload != 0:
        detail = stderr_reload or stdout_reload or f"exit code {rc_reload}"
        return (
            f"[WARNING] Rule for {ip_address} was written permanently but "
            f"firewall-cmd --reload failed: {detail}\n"
            "The rule will take effect after the next service restart."
        )

    return (
        f"[OK] IP {ip_address} ({family}) blocked permanently.\n"
        f"Rule: {rich_rule}\n"
        f"Firewall reloaded successfully. "
        f"Rule is active and will persist across reboots."
    )
