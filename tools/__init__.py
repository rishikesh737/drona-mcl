"""
tools — Drona tool registry.

This module is the single source of truth for:
  - TOOL_REGISTRY : dict mapping tool name → callable
  - TOOL_SCHEMAS  : list of OpenAI-compatible JSON schema dicts for Ollama

Ollama's `tools=` parameter accepts a list of dicts following the OpenAI
function-calling schema:
  {
      "type": "function",
      "function": {
          "name":        str,
          "description": str,
          "parameters": {
              "type":       "object",
              "properties": { param_name: { "type": str, "description": str } },
              "required":   [str, ...]
          }
      }
  }

Adding a new tool requires only:
  1. Implementing the function in one of the tool modules.
  2. Adding its entry to _TOOL_DEFINITIONS below.
  3. Importing it here.

No dynamic discovery magic — explicit registration makes the contract clear.
"""
from __future__ import annotations

from typing import Any, Callable

# ---------------------------------------------------------------------------
# Import all tool callables
# ---------------------------------------------------------------------------

from tools.system import (
    get_journal_logs,
    get_service_status,
    list_failed_services,
    get_disk_usage,
    get_memory_usage,
)
from tools.network import (
    list_network_sockets,
    ping_host,
    curl_health_check,
)
from tools.scripts import (
    create_script,
    execute_script,
    rollback_script,
    list_scripts,
)

# ---------------------------------------------------------------------------
# Schema definitions
# ---------------------------------------------------------------------------

_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    # ── system.py ──────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "get_journal_logs",
            "description": (
                "Read recent systemd journal logs. Optionally filter by a "
                "specific service unit name and/or the number of lines to return."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "unit": {
                        "type": "string",
                        "description": (
                            "Systemd unit name to filter by (e.g. 'nginx.service'). "
                            "Omit to read the global journal."
                        ),
                    },
                    "lines": {
                        "type": "integer",
                        "description": "Number of most-recent log lines to return. Default: 50.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_service_status",
            "description": "Check the systemd status of a named service unit.",
            "parameters": {
                "type": "object",
                "properties": {
                    "unit": {
                        "type": "string",
                        "description": "Systemd unit name (e.g. 'sshd.service').",
                    },
                },
                "required": ["unit"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_failed_services",
            "description": "List all systemd services that are currently in a failed state.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_disk_usage",
            "description": "Report disk usage for all mounted filesystems (df -h).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Specific mount point or path to report on. "
                            "Omit for all filesystems."
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_memory_usage",
            "description": "Report current system memory and swap usage (free -h).",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    # ── network.py ─────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "list_network_sockets",
            "description": (
                "List all listening TCP/UDP sockets and the processes bound to them "
                "(ss -tulnp). Useful for auditing open ports."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ping_host",
            "description": (
                "Check if a remote host is reachable via ICMP ping. "
                "Returns latency information on success."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {
                        "type": "string",
                        "description": "Hostname or IP address to ping.",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of ping packets to send. Default: 3.",
                    },
                },
                "required": ["host"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "curl_health_check",
            "description": (
                "Perform an HTTP GET health check against a URL using curl. "
                "Returns the HTTP status code and response body (truncated to 2 KB)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The full URL to check (e.g. 'http://localhost:8080/health').",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Request timeout in seconds. Default: 10.",
                    },
                },
                "required": ["url"],
            },
        },
    },
    # ── scripts.py ─────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "create_script",
            "description": (
                "Write a bash script to the ai_workspace directory after "
                "validating its syntax with 'bash -n' and checking for "
                "mandatory ping checks on any network/mount operations. "
                "If a file with the same name already exists, a .bak backup "
                "is created first to enable rollback."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Filename for the script (e.g. 'fix_nginx.sh'). No path components.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Complete bash script content, including shebang line.",
                    },
                    "description": {
                        "type": "string",
                        "description": "One-sentence description of what this script does.",
                    },
                },
                "required": ["filename", "content", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_script",
            "description": (
                "Execute a previously created script from the ai_workspace. "
                "The user will be prompted to approve execution before the "
                "script runs. Returns stdout and stderr from the script."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Name of the script file in ai_workspace to execute.",
                    },
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional positional arguments to pass to the script.",
                    },
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rollback_script",
            "description": (
                "Restore a script from its .bak backup file, overwriting the "
                "current version. Use this to undo a create_script operation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Name of the script file to roll back.",
                    },
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_scripts",
            "description": "List all scripts currently in the ai_workspace directory.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]

# ---------------------------------------------------------------------------
# Public exports
# ---------------------------------------------------------------------------

#: Maps tool name → callable. Used by the MCL dispatcher.
TOOL_REGISTRY: dict[str, Callable[..., str]] = {
    "get_journal_logs": get_journal_logs,
    "get_service_status": get_service_status,
    "list_failed_services": list_failed_services,
    "get_disk_usage": get_disk_usage,
    "get_memory_usage": get_memory_usage,
    "list_network_sockets": list_network_sockets,
    "ping_host": ping_host,
    "curl_health_check": curl_health_check,
    "create_script": create_script,
    "execute_script": execute_script,
    "rollback_script": rollback_script,
    "list_scripts": list_scripts,
}

#: Full OpenAI-schema list passed to ollama client as ``tools=``.
TOOL_SCHEMAS: list[dict[str, Any]] = _TOOL_DEFINITIONS


def dispatch(name: str, arguments: dict[str, Any]) -> str:
    """Dispatch a tool call by name.

    Args:
        name:      The tool name from the (normalised) tool call.
        arguments: The keyword arguments dict from the tool call.

    Returns:
        The string result from the tool function, or an error string
        if the tool name is not registered.
    """
    fn = TOOL_REGISTRY.get(name)
    if fn is None:
        known = ", ".join(sorted(TOOL_REGISTRY.keys()))
        return (
            f"[ERROR] Unknown tool '{name}'. "
            f"Registered tools are: {known}"
        )
    return fn(**arguments)
