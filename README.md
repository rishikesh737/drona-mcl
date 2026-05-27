# Drona-MCL

> **A fully local, autonomous Linux SysAdmin & Security Operations agent.**
> Powered by small open-source models via [Ollama](https://ollama.com).
> No data leaves your machine. Ever.

---

## What Drona Does

Drona accepts a natural-language task, drives a locally-running LLM through a
multi-turn agentic tool-calling loop, executes real system commands, and returns
a verified final answer — with zero external API calls and zero cloud dependencies.

**Phase 1 — SysAdmin Core:** Inspect services, read journals, check disk/memory,
diagnose network, generate and execute validated bash scripts.

**Phase 2 — Autonomous Security Operations Center (SOC):** Read host
authentication and firewall logs via `journalctl`, and permanently block
malicious IPs using `firewalld` rich DROP rules — all from a natural-language prompt.

```
$ python main.py "Read sshd logs and block any IP with repeated failures."

╭── 🔧 Tool call (Path A) ──────────────────────────────╮
│ read_security_logs({'service': 'sshd', 'lines': 200}) │
│ === Security logs: sshd.service ===                   │
│ Failed password for root from 203.0.113.42 (x47)     │
╰────────────────────────────────────────────────────────╯
╭── 🔧 Tool call (Path A) ──────────────────────────────╮
│ block_malicious_ip({'ip_address': '203.0.113.42'})    │
│ [OK] IP 203.0.113.42 (ipv4) blocked permanently.     │
╰────────────────────────────────────────────────────────╯
╭── ✓ Drona ─────────────────────────╮
│ Blocked 203.0.113.42 permanently.  │
╰─────────────────────────────────────╯
```

---

## The Model Compliance Layer (MCL)

The defining engineering contribution. Small local models (3B–7B) frequently
ignore the Ollama structured `tool_calls` API and emit raw JSON, markdown-fenced
JSON, or other freeform output. This causes naive agents to silently fail.

The MCL handles **both** output styles:

| Path | Trigger | Action |
|------|---------|--------|
| **Path A** (Compliant) | Model populates `tool_calls` | Extract & dispatch directly |
| **Path B** (Non-Compliant) | Model emits freeform text | Strip fences → extract JSON → normalise schema → dispatch |

**Fence variants handled (Path B):** backtick+language, bare backtick, triple-brace `{{{ }}}`, raw JSON, and prose-wrapped JSON.

**Schema normalisation:** `name/arguments`, `tool/parameters`, `function/arguments`, `function_name/arguments`, `action/action_input` (ReAct) — all normalised to canonical form.

### Loop Stability — Duplicate Tool-Call Guard

After each successful tool execution, the agent fingerprints the call
(`tool_name` + sorted args → `frozenset`). If the model re-issues an
identical call on the very next iteration — the classic infinite-loop
failure mode — the guard intercepts, logs a warning, and exits gracefully
with the prior successful output as a summary. The `[TIMEOUT]` hard cap
becomes a last resort, not the norm.

---

## 5-Minute Podman Install (Fedora)

Drona is packaged for **Podman** — the rootless, daemonless container runtime
native to Fedora. No Docker daemon required.

### Prerequisites

```bash
sudo dnf install podman podman-compose
# Optional: NVIDIA GPU passthrough for faster inference
sudo dnf install nvidia-container-toolkit
```

### Start the stack

```bash
git clone https://github.com/your-org/drona-mcl.git && cd drona-mcl

# Start Ollama (GPU-accelerated if available)
podman-compose up -d ollama

# Pull the model (~2 GB, first time only)
podman-compose exec ollama ollama pull qwen3:4b-thinking

# Run a task
podman-compose run --rm drona "Check sshd logs and block repeated offenders."
```

Drona's container manages the **host's actual firewall** via a DBus socket
passthrough — see [Privileged Container Orchestration](#privileged-container-orchestration).

### Alternative: bare-metal

```bash
chmod +x setup.sh && ./setup.sh
source .venv/bin/activate
python main.py "Why is nginx failing?"
```

---

## Configuration

```toml
# config/config.toml
[ollama]
host            = "http://localhost:11434"
model           = "qwen3:4b-thinking"
request_timeout = 120
think           = true   # separates chain-of-thought into message.thinking

[agent]
max_iterations  = 10     # hard cap; duplicate-call guard exits before this
```

---

## Available Tools

### System (5)
`get_journal_logs` · `get_service_status` · `list_failed_services` · `get_disk_usage` · `get_memory_usage`

### Network (3)
`list_network_sockets` · `ping_host` · `curl_health_check`

### Script Lifecycle (4)
`create_script` · `execute_script` · `rollback_script` · `list_scripts`

### Security / SOC (2) 🆕
| Tool | Description |
|------|-------------|
| `read_security_logs` | `journalctl` for auth/security services (`sshd`, `firewalld`, `auditd`…). Service name validated against strict allowlist — path traversal and injection rejected before any subprocess. |
| `block_malicious_ip` | Permanent `firewalld` DROP rule. Strict RFC 791 IPv4 + RFC 4291 IPv6 regex validation. Two-step: `--permanent` → `--reload`. CIDR, ports, hostnames rejected. |

---

## Script Safety

Every agent-generated bash script passes two checks before touching disk:

1. **`bash -n` syntax check** — real subprocess, zero execution
2. **Network-safety heuristic** — `mount`, `curl`, `wget`, `rsync`, `ssh`, `nfs`, `cifs`, `smb` require a `ping -c` before the first network operation

Failed checks return descriptive errors to the model for self-correction. Script execution always requires explicit `yes` at the prompt.

---

## Privileged Container Orchestration

Drona manages the host firewall from inside its container through a **DBus socket passthrough**. The `podman-compose.yml` bind-mounts `/var/run/dbus/system_bus_socket` and sets `DBUS_SYSTEM_BUS_ADDRESS`. When `firewall-cmd` runs inside the container, its IPC message travels over that socket to the host's `firewalld` daemon — which is unaware it is being managed remotely.

This is the same mechanism used by Cockpit and other system management tools. SELinux `:Z` labels are applied to all volume mounts to preserve the host's MAC policy. The container is **not** run with `--privileged`; it receives exactly the socket it needs and nothing more.

---

## Architecture

```
main.py
  └── core.agent.run_agent()
        ├── ollama.Client.chat()          ← LLM call (think=True)
        ├── core.mcl.route()             ← MCL dispatch
        │     ├── Path A: tool_calls field
        │     │     └── duplicate-call guard (frozenset fingerprint) 🆕
        │     └── Path B: core.parser.extract_tool_call()
        └── tools.dispatch()             ← tool execution
              ├── tools.system   (journalctl, systemctl, df, free)
              ├── tools.network  (ss, ping, curl)
              ├── tools.scripts  (create, validate, execute, rollback)
              │     └── core.validator (bash -n + ping-check)
              └── tools.security 🆕
                    ├── read_security_logs  (journalctl auth reader)
                    └── block_malicious_ip  (firewall-cmd via DBus)
```

---

## Project Structure

```
drona-mcl/
├── core/
│   ├── agent.py          # Agentic loop + duplicate-call guard
│   ├── mcl.py            # Model Compliance Layer
│   ├── parser.py         # Path B fence stripper + normaliser
│   ├── validator.py      # bash -n + ping-check heuristic
│   └── config_loader.py
├── tools/
│   ├── __init__.py       # TOOL_REGISTRY + TOOL_SCHEMAS + dispatch()
│   ├── system.py
│   ├── network.py
│   ├── scripts.py
│   └── security.py       # 🆕 SOC defense tools
├── config/config.toml
├── tests/
├── docs/architecture.md
├── ai_workspace/
├── main.py
├── podman-compose.yml    # Podman + NVIDIA GPU + DBus passthrough
└── Dockerfile
```

---

## Design Decisions

- **No LangChain.** The MCL is the framework — ~200 lines make small models reliable.
- **No `shell=True` anywhere.** Every subprocess uses list form.
- **Errors as strings, not exceptions.** Tools return descriptive strings; the model reasons about failures.
- **Podman, not Docker.** Native Fedora, rootless, SELinux-compatible.
- **DBus passthrough, not `--privileged`.** Least privilege — exactly the socket needed.
- **Config in one place.** `config/config.toml`. No magic constants.

---

## Running Tests

```bash
source .venv/bin/activate && pytest tests/ -v
# No live Ollama required. No root required.
```

---

## License

MIT
