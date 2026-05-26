# Drona-MCL: Core Engine & Engineering Implementation Report

---

## 1. Project Overview & Problem Statement

### 1.1 What Drona Is

Drona is a **100% local, offline, autonomous Linux SysAdmin agent**. It accepts a natural-language task from the user, drives a locally-running LLM (served by [Ollama](https://ollama.com)) through a multi-turn agentic tool-calling loop, executes real system commands on the host machine, and returns a verified final answer — all without any external API call, cloud dependency, or data egress.

The entire inference pipeline, from prompt to tool execution to final answer, runs on the operator's hardware. This makes Drona suitable for air-gapped servers, regulated environments, and scenarios where sending system state to a cloud provider is unacceptable.

### 1.2 The Core Problem: Small Models Break Tool Calling

The standard agentic architecture — give the model a list of tool schemas, receive a structured `tool_calls` response, execute, repeat — works reliably with large frontier models (GPT-4, Claude 3, Gemini 1.5 Pro). It fails unpredictably with the 3B–7B parameter models that are practical to run locally.

**Observed failure modes in 3B–7B models:**

| Failure Mode | Description |
|---|---|
| Schema non-compliance | Model ignores `tool_calls` field; outputs JSON inside prose or markdown fences |
| Schema drift | Uses `{"tool": …, "parameters": …}` instead of `{"name": …, "arguments": …}` |
| Thinking bleed | Chain-of-thought scratchpad leaks into `message.content`, corrupting JSON parsing |
| Silent null | `message.tool_calls` is `None` even when the model clearly intends a tool call |
| Fence wrapping | Tool call JSON wrapped in ` ```json ``` `, `{{{ }}}`, or plain backticks |

Any one of these causes a naive agent loop to either crash, loop infinitely, or silently return a wrong answer.

### 1.3 The Solution: Model Compliance Layer (MCL)

The **Model Compliance Layer** is a routing middleware that sits between the raw Ollama API response and the tool execution engine. It makes unreliable small models behave reliably by:

1. **Path A** — When the model *does* populate `message.tool_calls` correctly, extract and dispatch directly.
2. **Path B** — When the model outputs a freeform text response, attempt to parse a tool call from the text using a multi-strategy extraction pipeline (fence stripping → JSON parsing → schema normalisation).
3. **Crash isolation** — Wrap every `dispatch()` call in `try/except Exception` so a tool-level failure becomes an observation the model can reason about, not a traceback that kills the loop.
4. **Iteration guard** — Hard-cap the loop at `max_iterations` to prevent infinite loops regardless of model behaviour.

The MCL is the defining engineering contribution of this project.

---

## 2. Low-Level Directory Tree & Module Mapping

```
drona-mcl/
├── main.py                  # CLI entry point, argument parsing, KeyboardInterrupt guard
├── requirements.txt         # Runtime + test Python dependencies
├── setup.sh                 # Bootstrap: venv, deps, model pull, test run
├── Dockerfile               # Container image for the Drona agent process
├── docker-compose.yml       # Orchestrates Ollama + Drona services
├── drona.log                # Session log file (appended per run, gitignored)
├── debug_model.py           # Ad-hoc script for probing raw Ollama responses
├── patch_config.py          # One-off utility for patching config.toml values
│
├── config/
│   └── config.toml          # All runtime tuneable values (model, timeouts, paths, validator patterns)
│
├── core/
│   ├── __init__.py          # Package marker
│   ├── agent.py             # Recursive agentic loop, Ollama calls, Rich UI rendering
│   ├── mcl.py               # Model Compliance Layer: Path A/B routing, tool dispatch
│   ├── parser.py            # MCL Path B: fence stripping, JSON extraction, schema normalisation
│   ├── validator.py         # bash -n syntax check + ping-c network safety heuristic
│   └── config_loader.py     # Typed config dataclasses, TOML loader, lazy sentinel
│
├── tools/
│   ├── __init__.py          # TOOL_REGISTRY, TOOL_SCHEMAS, dispatch() function
│   ├── system.py            # journalctl, systemctl, df, free wrappers
│   ├── network.py           # ss, ping, curl wrappers
│   └── scripts.py           # create/execute/rollback/list script lifecycle
│
├── tests/
│   ├── __init__.py          # Package marker
│   ├── test_parser.py       # 30+ tests: all fence variants, alt schemas, is_final_answer
│   ├── test_validator.py    # 20+ tests: bash -n (real subprocess), all network patterns
│   └── test_tools.py        # 30+ tests: system/network/scripts tools (mocked)
│
├── ai_workspace/            # Runtime: agent-generated .sh scripts live here
├── docs/
│   └── architecture.md      # Public architecture overview
└── .venv/                   # Python virtual environment (gitignored)
```

### 2.1 `main.py` — CLI Entry Point

**Role:** The sole user-facing surface. Contains zero business logic.

**Key behaviours:**
- `_build_parser()` builds an `argparse.ArgumentParser` accepting a task as a positional argument or `--task` named flag, plus `--verbose/-v`.
- `_configure_logging()` sets `logging.DEBUG` on verbose, `logging.WARNING` otherwise.
- `from core.agent import run_agent` is deferred inside `main()` — *after* `logging.basicConfig()` — so module-level config reads in `core/` see the correct log level at import time.
- The `run_agent()` call is wrapped in `KeyboardInterrupt` → exit code 130, matching POSIX convention for Ctrl-C.

### 2.2 `core/agent.py` — Recursive Agentic Loop

**Role:** Owns the conversation history, drives the Ollama API, routes responses through the MCL, renders Rich UI panels, and enforces the iteration guard.

**Key functions:**

| Function | Purpose |
|---|---|
| `run_agent(task)` | Public entry point. Loads config, builds initial messages, runs the loop. |
| `_build_initial_messages()` | Injects system prompt as `role=user` (Qwen3 bypass — see §3.2) |
| `_call_ollama()` | Single Ollama API call with `think=cfg.ollama.think` |
| `_append_assistant_message()` | Appends assistant turn (with optional `tool_calls` key) to history |
| `_render_tool_call()` | Rich Panel showing Path A/B badge, tool name, arguments, output preview |
| `_render_final_answer()` | Green Rich Panel for the final answer |

**Crash-proofing inside `with Live:`:**
The spinner block catches three tiers:
1. `ollama.ResponseError` — API-level model errors
2. `Exception` — connection errors (Ollama not running)
3. `BaseException` — OS-level uncaught failures

All three log the error and return a clean string — the `with Live:` block never crashes silently.

### 2.3 `core/mcl.py` — Model Compliance Layer

**Role:** The reliability engine. Routes every `ChatResponse` to Path A, Path B, or final-answer. Isolates tool crashes.

**`route(response)` decision tree:**
```
response.message.tool_calls non-empty?
  YES → Path A: _dispatch_tool_calls()
  NO  →
    text_for_path_b = content or thinking (fallback)
    is_final_answer(text_for_path_b)?
      NO  → Path B: _dispatch_from_text()
               → result is None? → fall through to final
      YES → final answer
```

**`_dispatch_tool_calls()` (Path A):**
Iterates `tool_calls`, extracts `tc.function.name` and `dict(tc.function.arguments)`, calls `dispatch()`. Wrapped in `try/except Exception` — tool crashes become `[ERROR]` strings in `ToolResult.output`.

**`_dispatch_from_text()` (Path B):**
Calls `extract_tool_call(text)`. If `None`, returns `None` (causes fall-through to final). Otherwise dispatches. Also wrapped in `try/except Exception`.

**`build_tool_result_messages():`**
Path A → `role=tool` messages (Ollama structured protocol).
Path B → `role=user` messages prefixed `[Tool result for {name}]` (model didn't use structured interface).

### 2.4 `core/parser.py` — MCL Path B Extraction Pipeline

**Role:** Pure-stdlib fence stripper and JSON normaliser. Dependency-free for isolated unit testing.

**`strip_fences(text)` — priority order:**
1. ` ```json … ``` ` or ` ```JSON … ``` ` via `_BACKTICK_WITH_LANG` regex
2. ` ``` … ``` ` via `_BACKTICK_NO_LANG` regex
3. `{{{ … }}}` via plain-string sentinel search (`rfind` on close to handle embedded `}}`)
4. Raw text returned unchanged (raw JSON or prose-wrapped)

**`_extract_json_object(text)` — brace counter:**
Walks the string character-by-character tracking `depth` and `in_string`/`escape_next` state. Handles prose-wrapped JSON where the object is embedded mid-sentence.

**`_normalise_tool_call(raw)` — schema variants handled:**

| Input schema | Canonical output |
|---|---|
| `{"name": …, "arguments": …}` | identity |
| `{"tool": …, "parameters": …}` | remapped |
| `{"function": …, "arguments": …}` | remapped |
| `{"function_name": …, "arguments": …}` | remapped |
| `{"function": …, "parameters": …}` | remapped |
| `{"action": …, "action_input": …}` | remapped; `action_input` string → `json.loads` |

**`is_final_answer(text)`:** Returns `True` if text has no `{`, or if `extract_tool_call` returns `None`. Gates Path B entry in `route()`.

### 2.5 `core/validator.py` — Script Safety Gate

**Role:** Two-stage validation pipeline run before any script is written to disk.

**Stage 1 — `validate_bash(content)`:**
- Writes content to a `NamedTemporaryFile` inside `ai_workspace` (temp never escapes project root).
- Runs `bash -n <tmpfile>` with `timeout=10`.
- `bash -n` parses without executing — catches all syntax errors with zero side effects.
- Temp file always removed in `finally` block.
- Returns `(True, "")` or `(False, "Syntax error: <stderr>")`.

**Stage 2 — `check_network_safety(content)`:**
- Searches content for each `network_operation_patterns` regex (from config).
- Records character offset of the **first** match.
- Searches for `ping_sentinel` (`"ping -c"`) and records its offset.
- **Pass:** no network ops found, OR ping sentinel exists AND appears before the first network op.
- **Fail (no sentinel):** returns error naming the offending operation.
- **Fail (wrong order):** returns error with both offsets so the model can fix placement.

**Lazy config loading:**
`_cfg = None` sentinel at module level. `_get_config()` populates on first call. Prevents `FileNotFoundError` at import time in CI environments without `config.toml`.

### 2.6 `core/config_loader.py` — Typed Configuration

Parses `config/config.toml` using `tomllib` (stdlib, Python ≥ 3.11) or `tomli` (backport). Returns a frozen `DronaConfig` dataclass tree. All four subsections (`ollama`, `agent`, `paths`, `validator`) are typed; callers get IDE autocomplete and no string-keyed dict access.

### 2.7 `tools/__init__.py` — Registry & Dispatcher

`TOOL_REGISTRY`: explicit `name → callable` dict. No dynamic discovery — the contract is always visible in one place.

`TOOL_SCHEMAS`: OpenAI-compatible JSON schema list passed verbatim to `ollama.chat(tools=…)`.

`dispatch(name, arguments)`: resolves callable, calls with `**arguments`. Unknown names return a self-correcting error string listing all registered tools.

### 2.8 `tools/system.py` — System Introspection (5 tools)

| Tool | Command | Notes |
|---|---|---|
| `get_journal_logs(unit, lines)` | `journalctl --no-pager -n <lines>` | lines clamped 1–500 |
| `get_service_status(unit)` | `systemctl status --no-pager --full <unit>` | requires non-empty unit |
| `list_failed_services()` | `systemctl list-units --state=failed` | returns "No failed units" message if empty |
| `get_disk_usage(path)` | `df -h [path]` | omit path for all filesystems |
| `get_memory_usage()` | `free -h` | no parameters |

All use `subprocess.run` in list form, `capture_output=True`, `text=True`, explicit `timeout`. Errors returned as strings.

### 2.9 `tools/network.py` — Network Diagnostics (3 tools)

| Tool | Command | Notes |
|---|---|---|
| `list_network_sockets()` | `ss -tulnp` | timeout 15s |
| `ping_host(host, count)` | `ping -c <count> -W 3 <host>` | count clamped 1–10 |
| `curl_health_check(url, timeout)` | `curl -s -w "\n%{http_code}" <url>` | single atomic call; body + status split on last `\n`; body truncated 2 KB |

### 2.10 `tools/scripts.py` — Script Lifecycle (4 tools)

**`create_script(filename, content, description)`:**
1. `_sanitise_filename`: rejects `/`, `\`, null bytes; appends `.sh`; 128-char limit.
2. `validate_script(content)`: `bash -n` + ping heuristic. Rejects before any disk write.
3. If `target.exists()`: copies current file to `target.with_suffix(".sh.bak")`.
4. `target.write_text(content)` + `target.chmod(0o755)`.
5. Returns `[OK]` message with backup note only if `bak_created` is `True`.

**`execute_script(filename, args)`:**
Renders script via `rich.syntax.Syntax` (Monokai theme, line numbers) in a yellow warning Panel. Blocks on `input("Type yes to execute…")`. EOF/KeyboardInterrupt → cancelled. Only runs on exact `"yes"`. Captures stdout+stderr, truncates to 8 KB each.

**`rollback_script(filename)`:** Reads `.sh.bak`, overwrites current file, re-applies `chmod 0o755`.

**`list_scripts()`:** Globs `ai_workspace/*.sh`, shows name (40-char padded), size, and `[has backup]` indicator.

### 2.11 `config/config.toml` — Runtime Configuration

```toml
[ollama]
host             = "http://localhost:11434"
model            = "qwen3:4b-thinking"
request_timeout  = 120
think            = true          # separates scratchpad → message.thinking

[agent]
max_iterations   = 10
system_prompt    = "..."         # injected as role=user (Qwen3 bypass)

[paths]
ai_workspace     = "/mnt/fedora-partition/drona-mcl/ai_workspace"
log_file         = "/mnt/fedora-partition/drona-mcl/drona.log"

[validator]
ping_sentinel              = "ping -c"
network_operation_patterns = ["\\bmount\\b", "\\bcurl\\b", "\\bwget\\b",
                               "\\brsync\\b.*@", "\\bssh\\b", "\\bnfs\\b",
                               "\\bcifs\\b", "\\bsmb\\b"]
```

### 2.12 Test Suites

**`tests/test_parser.py`** — 30+ pure unit tests, zero external dependencies:
- All 5 fence variants (backtick+lang, backtick bare, triple-brace, raw, prose-wrapped)
- All 6 alt schema normalisations including `action_input` as JSON string
- `is_final_answer` edge cases (empty, no-JSON, non-tool JSON)
- Malformed JSON inside fence → `None`, not exception
- Multiline nested arguments

**`tests/test_validator.py`** — 20+ tests using real `bash -n` subprocess:
- Valid scripts (simple, functions, heredoc, empty, shebang-only)
- Syntax errors (unclosed `if`, unmatched quote)
- All 6 network patterns without ping check → fail
- Ping check present but after network op → fail with offset info
- Clean scripts → pass both stages

**`tests/test_tools.py`** — 30+ tests with mocked subprocess + `tmp_path`:
- All system tools: output format, clamping, `TimeoutExpired`, `FileNotFoundError`
- All network tools: success, failure, empty-param guard, count clamping, body truncation
- Scripts: path traversal rejection, validation failure blocks write, backup creation, rollback, list

---

## 3. Request Lifecycle Walkthrough

### Phase A — CLI Ingestion (`main.py`)

```
$ python main.py "Why is nginx failing and how do I fix it?"
```

1. `argparse` resolves task from positional arg or `--task` flag.
2. `_configure_logging(verbose=False)` sets root logger to `WARNING`.
3. `from core.agent import run_agent` is imported **now** (after logging setup).
4. `run_agent(task)` is called inside a `try/except KeyboardInterrupt`.

### Phase B — System Prompt Injection (Qwen3 Bypass)

`_build_initial_messages()` constructs:
```python
[{"role": "user", "content": f"{system_prompt}\n\nTask: {user_task}"}]
```

The system prompt is embedded in the **first user message**, not as `role=system`. Qwen3 and several other small models have observed behaviour of partially ignoring `role=system` instructions when tool schemas are also present. Injecting the instruction as a user turn ensures the model always processes the constraint ("interact ONLY through tool calls").

### Phase C — Ollama Call & MCL Routing

```
client.chat(model=…, messages=…, tools=TOOL_SCHEMAS, think=True)
```

Returns a `ChatResponse`. With `think=True`:
- `message.thinking` contains the chain-of-thought scratchpad
- `message.content` contains only the tool call JSON or final answer text
- `message.tool_calls` is populated if the model used the structured interface

**Path A triggered when:** `len(message.tool_calls) > 0`
- `_dispatch_tool_calls` iterates the list, calls `dispatch(name, args)` for each, collects `ToolResult` objects.

**Path B triggered when:** `tool_calls` is empty/None AND `is_final_answer(text) == False`
- `text_for_path_b = message.content or message.thinking`
- `extract_tool_call(text)` runs the full fence-strip → JSON-parse → normalise pipeline.
- On success: dispatches the extracted call.
- On failure (returns `None`): falls through to final answer.

**Path C (final) triggered when:** neither A nor B matched.
- `final_text = raw_content or thinking_content`
- Loop exits, answer returned to caller.

### Phase D — Stateful History Accumulation & Termination

After each tool dispatch, two things are appended to `messages`:

1. **Assistant turn:** `{"role": "assistant", "content": raw_content, "tool_calls": […]}` — gives the model context of what it just said.
2. **Tool result messages:** Either `role=tool` (Path A) or `role=user` with prefix (Path B).

This accumulated history is the model's working memory. The model sees its own prior tool calls and their results on every subsequent iteration.

**Termination conditions:**
- `path == "final"` → clean return with the answer string.
- `iteration > max_iterations` → `[TIMEOUT]` message; operator is advised to increase the limit or break the task into steps.
- Ollama connection error → `[ERROR]` string returned immediately; loop does not continue.

---

## 4. Operational Safety & Execution Guards

### 4.1 `bash -n` Syntax Validation Pipeline

Every script submitted by the model goes through `validate_bash()` before a single byte is written to disk:

```python
with tempfile.NamedTemporaryFile(mode="w", suffix=".sh",
        dir=workspace, delete=False, encoding="utf-8") as tmp:
    tmp.write(content)
    tmp_path = Path(tmp.name)

result = subprocess.run(["bash", "-n", str(tmp_path)],
    capture_output=True, text=True, timeout=10)
```

Key design decisions:
- **`delete=False` + `finally` cleanup** — file persists long enough for `bash -n`, always deleted regardless of outcome.
- **`dir=workspace`** — temp files stay inside `ai_workspace`; no `/tmp` pollution.
- **`timeout=10`** — prevents a pathological script from hanging the validator.
- **Never raises** — all `subprocess.TimeoutExpired` and `OSError` branches return `(False, reason)`.

`bash -n` catches unclosed `if`/`for`/`while` blocks, unmatched quotes, invalid redirections, and malformed function definitions — all without executing a single command.

### 4.2 Network Safety Heuristic — `ping -c` Enforcement

Scripts that issue network or mount operations against unreachable hosts cause `subprocess.run` to hang until timeout, blocking the agent loop. The `check_network_safety()` heuristic prevents this.

**Detection:** Eight regex patterns compiled from `config.toml` at first call:
```
\bmount\b   \bcurl\b    \bwget\b    \brsync\b.*@
\bssh\b     \bnfs\b     \bcifs\b    \bsmb\b
```

**Enforcement algorithm:**
1. Find character offset of the **earliest** pattern match across all regexes.
2. Find character offset of `"ping -c"` (the sentinel string).
3. Three outcomes:
   - No network op found → **pass**
   - Network op found, no sentinel → **fail**: error names the offending command
   - Sentinel found but `ping_offset > net_offset` → **fail**: reports exact offsets

**Why character offsets, not line numbers?** The script is a raw string at validation time. Offsets are exact and O(n) to compute. The error message translates them into actionable guidance: *"Move the ping check to the top of the script, before any mount/curl/wget/ssh/rsync call."*

### 4.3 Script Execution Preview & Manual Confirmation

`execute_script` enforces a mandatory human-in-the-loop gate. The script is rendered with `rich.syntax.Syntax` (Monokai theme, line numbers) inside a yellow warning `Panel` before any execution:

```
┌─ ⚠ Script ready to execute: fix_nginx.sh ─────────────────┐
│  1  #!/bin/bash                                            │
│  2  set -euo pipefail                                      │
│  3  if ! ping -c 2 -W 3 nginx-host &>/dev/null; then      │
│  4      echo "host unreachable" >&2; exit 1               │
│  5  fi                                                     │
│  6  systemctl restart nginx                                │
└────────────────────────────────────────────────────────────┘
Type yes to execute, or anything else to cancel:
```

- `input()` is wrapped in `try/except (EOFError, KeyboardInterrupt)` → `[CANCELLED]`.
- Exact string `"yes"` (lowercase, stripped) required. Any other input cancels.
- Output capped at 8 KB stdout + 8 KB stderr with truncation notice.

---

## 5. Local Setup & Infrastructure Quickstart

### 5.1 Prerequisites

| Requirement | Minimum | Purpose |
|---|---|---|
| Python | 3.10+ | Runtime (3.11+ for stdlib `tomllib`) |
| bash | Any | `bash -n` syntax validation |
| Ollama | Latest | Local LLM inference server |
| Docker + Compose v2 | Latest | Container deployment (optional) |
| `iproute2`, `iputils-ping`, `curl` | Any | Tool subprocess dependencies |

### 5.2 Host Setup via `setup.sh`

```bash
chmod +x setup.sh && ./setup.sh
```

**Seven automated steps:**

1. **Prerequisite check** — verifies Python ≥ 3.10, warns if `ollama` CLI absent.
2. **Virtual environment** — creates `.venv` via `python3 -m venv`; skips if exists (idempotent).
3. **tomli backport** — installs `tomli>=2.0.0` automatically if Python < 3.11.
4. **Dependencies** — `pip install -r requirements.txt` (ollama, rich, pytest, pytest-mock).
5. **Model pull** — reads model name from `config.toml` via Python, runs `ollama pull`.
6. **Workspace** — creates `ai_workspace/` if absent.
7. **Test run** — `pytest tests/ -v --tb=short`; exit code captured but does not abort setup.

**Post-setup usage:**
```bash
source .venv/bin/activate
python main.py "Check which services are failing and fix them"
python main.py --verbose "Show disk usage on all mounts"
```

### 5.3 Docker / Docker Compose Deployment

```bash
# Start Ollama
docker compose up -d ollama

# Pull the model (first time only, ~2–4 GB)
docker compose exec ollama ollama pull qwen3:4b-thinking

# Run a one-shot task
docker compose run --rm drona "Why is nginx failing?"
```

**Service design:**

`ollama`: `ollama/ollama:latest`, port `11434:11434`, volume `ollama_data` persists models, healthcheck via `curl -f http://localhost:11434/`.

`drona`: built from local `Dockerfile`, `network_mode: host` (reaches Ollama at `localhost:11434`), `depends_on: ollama: condition: service_healthy`, bind-mounts `./ai_workspace`, `./config` (read-only), `./drona.log`, `stdin_open: true` + `tty: true` for confirmation prompt.

**Dockerfile summary:**
```dockerfile
FROM python:3.11-slim
RUN apt-get install -y iproute2 iputils-ping curl bash
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY core/ tools/ config/ tests/ main.py ./
RUN mkdir -p ai_workspace
RUN useradd --create-home --shell /bin/bash drona
USER drona
ENTRYPOINT ["python", "main.py"]
CMD ["--help"]
```

Non-root `drona` user; system tools (`ss`, `ping`, `curl`, `bash`) installed at build time.

---

## 6. Enterprise GitHub CI/CD Automation Pipeline

Save the following as `.github/workflows/ci.yml`:

```yaml
name: CI

on:
  push:
    branches: [main, develop]
  pull_request:
    branches: [main]

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:

  # ── 1. Code formatting & linting ────────────────────────────────────────
  lint:
    name: Lint & Format (ruff)
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: pip

      - name: Install ruff
        run: pip install ruff

      - name: Check formatting
        run: ruff format --check core/ tools/ tests/ main.py

      - name: Check linting
        run: ruff check core/ tools/ tests/ main.py

  # ── 2. Full test suite (matrix) ─────────────────────────────────────────
  test:
    name: Tests (pytest) — Python ${{ matrix.python-version }}
    runs-on: ubuntu-latest
    needs: lint

    strategy:
      fail-fast: false
      matrix:
        python-version: ["3.10", "3.11", "3.12"]

    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
          cache: pip

      - name: Install tomli backport (Python < 3.11)
        if: matrix.python-version == '3.10'
        run: pip install "tomli>=2.0.0"

      - name: Install dependencies
        run: |
          pip install --upgrade pip
          pip install -r requirements.txt

      - name: Patch config paths for CI
        run: |
          python patch_config.py \
            --ai-workspace "$(pwd)/ai_workspace" \
            --log-file "$(pwd)/drona.log"

      - name: Run test suite
        run: |
          python -m pytest tests/ \
            -v \
            --tb=short \
            --strict-markers \
            -x

      - name: Upload pytest results
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: pytest-results-py${{ matrix.python-version }}
          path: .pytest_cache/

  # ── 3. Docker image build validation ────────────────────────────────────
  docker-build:
    name: Docker Build
    runs-on: ubuntu-latest
    needs: test

    steps:
      - uses: actions/checkout@v4

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Build image (no push)
        uses: docker/build-push-action@v5
        with:
          context: .
          push: false
          tags: drona-mcl:ci-${{ github.sha }}
          cache-from: type=gha
          cache-to: type=gha,mode=max

  # ── 4. Dependency security audit ────────────────────────────────────────
  security:
    name: Security Scan (pip-audit)
    runs-on: ubuntu-latest
    needs: lint

    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install pip-audit
        run: pip install pip-audit

      - name: Audit dependencies
        run: pip-audit -r requirements.txt
```

**Pipeline job summary:**

| Job | Depends on | What it validates |
|---|---|---|
| `lint` | — | `ruff format --check` + `ruff check` across all Python source |
| `test` | `lint` | Full pytest suite on Python 3.10, 3.11, 3.12 in parallel |
| `docker-build` | `test` | Dockerfile builds successfully; GHA layer cache for speed |
| `security` | `lint` | `pip-audit` scans all runtime deps for known CVEs |

**Key CI design decisions:**
- `concurrency` + `cancel-in-progress: true` — cancels stale runs on rapid pushes to the same branch, saving runner minutes.
- `needs: lint` on `test` — pytest never wastes runner time if code is unformatted.
- Matrix 3.10/3.11/3.12 — catches `tomllib` vs `tomli` regressions and 3.12 deprecation warnings simultaneously.
- `fail-fast: false` on matrix — all Python versions complete so you see the full failure picture.
- `patch_config.py` rewrites absolute paths in `config.toml` to CI-relative paths — prevents hardcoded `/mnt/fedora-partition/` from causing `FileNotFoundError` in CI.
- `-x` flag — fails fast on first test failure for quick signal.
- `--strict-markers` — enforces that all `@pytest.mark.*` decorators are declared in `pytest.ini`, preventing silent marker typos.

---

*Report generated from source: `/mnt/fedora-partition/drona-mcl`. All code references are grounded in the live codebase with no placeholders.*
