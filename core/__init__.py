"""
core — Drona's internal engine.

Exports:
    run_agent   : Top-level entry point for the agentic loop.
    load_config : Config loader used by all modules.
"""
from __future__ import annotations

from core.agent import run_agent
from core.config_loader import load_config

__all__ = ["run_agent", "load_config"]
