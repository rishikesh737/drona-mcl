"""
core.config_loader — Loads and exposes the typed configuration object.

All modules import `load_config()` and call it once. The result is a plain
dataclass so callers get IDE-friendly attribute access and no magic strings.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

# tomllib is stdlib in Python 3.11+; fall back to the third-party tomli.
if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "Python < 3.11 detected. Install 'tomli': pip install tomli"
        ) from exc

# ---------------------------------------------------------------------------
# Typed config dataclasses
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path(__file__).parent.parent / "config" / "config.toml"


@dataclass(frozen=True)
class OllamaConfig:
    host: str
    model: str
    request_timeout: int
    think: bool


@dataclass(frozen=True)
class AgentConfig:
    max_iterations: int
    system_prompt: str


@dataclass(frozen=True)
class PathsConfig:
    ai_workspace: Path
    log_file: Path


@dataclass(frozen=True)
class ValidatorConfig:
    network_operation_patterns: list[str]
    ping_sentinel: str


@dataclass(frozen=True)
class DronaConfig:
    ollama: OllamaConfig
    agent: AgentConfig
    paths: PathsConfig
    validator: ValidatorConfig


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_config(path: Path = _CONFIG_PATH) -> DronaConfig:
    """Parse config.toml and return a typed DronaConfig instance.

    Args:
        path: Absolute path to the TOML config file. Defaults to the
              project-standard location.

    Returns:
        A fully populated, immutable DronaConfig dataclass.

    Raises:
        FileNotFoundError: If the config file does not exist.
        tomllib.TOMLDecodeError: If the config file is malformed TOML.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found at '{path}'. "
            "Run setup.sh to initialise the project."
        )

    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    ollama_raw = raw["ollama"]
    agent_raw = raw["agent"]
    paths_raw = raw["paths"]
    validator_raw = raw["validator"]

    return DronaConfig(
        ollama=OllamaConfig(
            host=ollama_raw["host"],
            model=ollama_raw["model"],
            request_timeout=int(ollama_raw["request_timeout"]),
            think=bool(ollama_raw.get("think", False)),
        ),
        agent=AgentConfig(
            max_iterations=int(agent_raw["max_iterations"]),
            system_prompt=agent_raw["system_prompt"],
        ),
        paths=PathsConfig(
            ai_workspace=Path(paths_raw["ai_workspace"]),
            log_file=Path(paths_raw["log_file"]),
        ),
        validator=ValidatorConfig(
            network_operation_patterns=list(
                validator_raw["network_operation_patterns"]
            ),
            ping_sentinel=validator_raw["ping_sentinel"],
        ),
    )
