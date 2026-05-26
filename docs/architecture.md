# Drona-MCL: Architecture Overview

> **Drona** is a 100% local, autonomous Linux SysAdmin agent. It accepts a
> natural-language task, drives a local LLM (Ollama) through a structured
> agentic tool-calling loop, and returns a verified final answer — with zero
> external network calls and zero cloud dependencies.

---

## High-Level Data Flow

```
User (CLI)
    │
    ▼
main.py
  └─ argument parsing (argparse)
  └─ KeyboardInterrupt guard
    │
    ▼
core/agent.py  run_agent()
  ┌─────────────────────────────────────────────────────────┐
  │  for iteration in range(1, max_iterations + 1):         │
  │                                                         │
  │  ① Build / extend conversation messages                 │
  │  ② Call Ollama API  (_call_ollama)                      │
  │       └─ think=True  →  message.thinking  (scratchpad)  │
  │       └─ think=False →  message.content   (direct)      │
  │  ③ MCL route()                                          │
  │       ├─ Path A: message.tool_calls  populated  ──────► │
  │       │         _dispatch_tool_calls()                  │
  │       ├─ Path B: freeform text / thinking fallback ───► │
  │       │         _dispatch_from_text()                   │
  │       │         └─ core/parser.py  extract_tool_call()  │
  │       └─ final:  return answer to caller                │
  │  ④ Append assistant turn to messages                    │
  │  ⑤ Append tool result messages, loop                    │
  └─────────────────────────────────────────────────────────┘
    │
    ▼
tools/dispatch()   (tools/__init__.py)
  ├─ tools/system.py    (journalctl, systemctl, df, free)
  ├─ tools/network.py   (ping, ss, curl)
  └─ tools/scripts.py   (create / execute / rollback / list)
         └─ core/validator.py  (bash -n + ping-c heuristic)
```

---

## Component Descriptions

### `main.py` — CLI Entry Point
Handles argument parsing via `argparse`. Accepts a task as a positional
argument or via `--task`. Configures logging with `--verbose`. Wraps
`run_agent()` in a `KeyboardInterrupt` guard (exit code 130). Defers
`from core.agent import run_agent` until after `logging.basicConfig` so
module-level config reads see the correct log level.

---

### `core/agent.py` — Recursive Agentic Loop
The loop iterates up to `cfg.agent.max_iterations` times. Each iteration:

1. Renders a spinner via `rich.live.Live` while waiting for Ollama.
2. Calls `_call_ollama()` with `think=cfg.ollama.think`. The `think` flag
   separates chain-of-thought into `message.thinking` for thinking-model
   variants (qwen3, deepseek-r1), keeping `message.content` clean.
3. Passes the raw `ChatResponse` to `route()`.
4. On Path A or B: appends the assistant turn + tool result messages and
   continues.
5. On `final`: renders the answer panel and returns.
6. If `max_iterations` is exhausted: returns a `[TIMEOUT]` message.

**Crash-proofing:** `_call_ollama` is wrapped in three exception branches —
`ollama.ResponseError`, `Exception` (connection errors), and `BaseException`
(uncaught OS-level failures). All three return a clean error string instead
of propagating a traceback.

**System-prompt injection:** The system prompt is injected as a `role=user`
message rather than `role=system`. This bypasses Qwen3's tendency to ignore
system-role content and ensures the instruction is always processed.

---

### `core/mcl.py` — Model Compliance Layer
The MCL is the reliability engine. It routes every Ollama response through
one of three paths:

| Path | Trigger | Mechanism |
|------|---------|-----------|
| **A** | `message.tool_calls` is non-empty | Extracts `tc.function.name` + `tc.function.arguments` directly |
| **B** | Content or thinking contains a parseable tool-call JSON | Delegates to `core/parser.py` |
| **final** | Neither A nor B matched | Returns `raw_content or thinking_content` as the answer |

Both `_dispatch_tool_calls` (Path A) and `_dispatch_from_text` (Path B)
wrap `tools.dispatch()` in `try/except Exception`, converting any tool-level
crash into a `[ERROR] Tool execution failed unexpectedly: …` observation
string that the model can reason about in the next iteration.

`build_tool_result_messages()` formats results as `role=tool` for Path A
(Ollama structured protocol) and `role=user` for Path B (the model didn't
use the structured interface and won't parse `role=tool`).

---

### `core/parser.py` — MCL Path B: Fence Stripper & Normaliser
Handles every observed non-compliant output format from 3B–7B models:

| Variant | Example |
|---------|---------|
| Backtick + language tag | ` ```json { … } ``` ` |
| Backtick, no tag | ` ``` { … } ``` ` |
| Triple-brace sentinels | `{{{ { … } }}}` |
| Raw JSON | `{ "name": …, "arguments": … }` |
| Prose-wrapped | `Sure! Here is the call: { … }` |

After fence stripping, the JSON is normalised from any of six observed
schema variants (`name/arguments`, `tool/parameters`, `function/arguments`,
`function_name/arguments`, `function/parameters`, `action/action_input`)
into the canonical `{"name": str, "arguments": dict}` form.

`is_final_answer()` gates Path B entry: if the text contains no `{`, or if
`extract_tool_call()` returns `None`, the response is treated as a final answer.

---

### `core/validator.py` — Script Safety Gate
Two independent checks run before any script touches disk:

**1. Syntax validation (`validate_bash`):**
Writes the script to a temp file inside `ai_workspace` and runs `bash -n`.
`bash -n` parses without executing — catches all structural syntax errors.
The temp file is always cleaned up in a `finally` block.

**2. Network-safety heuristic (`check_network_safety`):**
Scans the script content with the regex patterns from `config.toml`
`[validator].network_operation_patterns` (mount, curl, wget, rsync@, ssh,
nfs, cifs, smb). If any pattern matches:
- The character offset of the first match is recorded.
- The `ping_sentinel` string (`ping -c`) is searched for.
- If the sentinel is absent, or appears **after** the first network
  operation, the script is rejected with a precise character-offset error.

This prevents agent-generated scripts from issuing network or mount calls
against unreachable hosts, which would cause indefinite terminal hangs.

The config is loaded lazily via `_get_config()` using a module-level
`_cfg = None` sentinel — prevents `FileNotFoundError` on import in CI
environments without `config.toml`.

---

### `core/config_loader.py` — Typed Configuration
Parses `config/config.toml` using `tomllib` (stdlib ≥ 3.11) or `tomli`
(backport for 3.10). Returns an immutable `DronaConfig` dataclass tree:
`OllamaConfig`, `AgentConfig`, `PathsConfig`, `ValidatorConfig`.
All callers use attribute access; no magic strings.

---

### `tools/__init__.py` — Tool Registry & Dispatcher
Single source of truth for `TOOL_REGISTRY` (name → callable) and
`TOOL_SCHEMAS` (OpenAI-compatible JSON schema list passed to `ollama.chat(tools=…)`).

`dispatch(name, arguments)` resolves the callable and calls it with
`**arguments`. Unknown tool names return a `[ERROR] Unknown tool` string
listing all registered names — the model can self-correct on the next turn.

---

### `tools/system.py` — System Introspection
Wraps `journalctl`, `systemctl`, `df`, and `free` via `subprocess.run`
in list form (never `shell=True`). All calls have explicit `timeout=`.
Errors are returned as strings, never raised. Output is clamped (e.g.,
journal lines: 1–500) to protect the context window.

### `tools/network.py` — Network Diagnostics
Three tools: `list_network_sockets` (`ss -tulnp`), `ping_host` (ICMP,
count clamped 1–10), and `curl_health_check`. The health check issues a
single atomic `curl -s -w "\n%{http_code}"` call, splitting stdout on the
last newline to extract body and status code atomically — avoiding the
two-request race condition of separate calls.

### `tools/scripts.py` — Script Lifecycle Manager
Full lifecycle: `create_script` → `execute_script` → `rollback_script`
→ `list_scripts`.

- **`create_script`**: Sanitises filename (rejects `/`, `\`, null bytes,
  enforces `.sh`, 128-char limit), runs `validate_script()`, backs up any
  existing file to `.sh.bak`, writes with `chmod 0o755`.
- **`execute_script`**: Renders a syntax-highlighted Rich panel of the
  script, then blocks on `input("yes to execute…")`. The script does not
  run unless the user explicitly types `yes`.
- **`rollback_script`**: Overwrites the current script with its `.bak`.
- **`list_scripts`**: Enumerates `*.sh` in `ai_workspace` with sizes and
  backup indicators.

---

### `config/config.toml` — Runtime Configuration
All tuneable values. Key sections:

```toml
[ollama]
model = "qwen3:4b-thinking"
think = true           # enables thinking scratchpad separation
request_timeout = 120

[agent]
max_iterations = 10    # hard loop guard

[validator]
ping_sentinel = "ping -c"
network_operation_patterns = ["\\bmount\\b", "\\bcurl\\b", ...]
```

---

## Security Model

| Layer | Mechanism |
|-------|-----------|
| No external calls | Ollama runs 100% locally; no data leaves the machine |
| Script syntax gate | `bash -n` rejects malformed scripts before disk write |
| Network safety gate | `ping -c` heuristic blocks unsafe network scripts |
| Mandatory confirmation | `execute_script` requires interactive `yes` input |
| Filename sanitisation | Path separators, null bytes, and length enforced |
| Workspace isolation | All scripts confined to `ai_workspace`; path never from user input |
| Tool crash isolation | `try/except Exception` in MCL wraps every `dispatch()` call |
| Iteration guard | `max_iterations` terminates runaway loops unconditionally |

---

## Test Coverage Map

| Test file | Covers |
|-----------|--------|
| `tests/test_parser.py` | All 5 fence variants, 6 alt schemas, `is_final_answer`, edge cases |
| `tests/test_validator.py` | `bash -n` (real subprocess), all network patterns, ping ordering |
| `tests/test_tools.py` | system/network/scripts tools (mocked subprocess + tmp_path) |
