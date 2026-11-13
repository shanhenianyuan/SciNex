"""intel - SciNex Reader Frontier Intel module (前沿情报追踪).

A dependency-injected, stdlib-only package that tracks the latest progress in
LLMs / agents across arXiv (papers), HuggingFace (models) and GitHub (code),
analyzes them with the internal LLM into structured intel, accumulates a
cross-day knowledge network, and schedules a daily 00:30 (Beijing) run with
network-failure recording + auto-retry.

Exposed API (used by serve.py):
    load_config()                    -> Config
    Store(cfg) / NetworkMonitor(...) / Engine(...) / Scheduler(...)
"""
from __future__ import annotations

from .config import Config, load_config, now_bj
from .models import IntelItem, NetState, RawItem
from .store import Store
from .network import NetworkMonitor
from .engine import Engine
from .scheduler import Scheduler

__all__ = ["Config", "load_config", "now_bj", "IntelItem", "NetState", "RawItem",
           "Store", "NetworkMonitor", "Engine", "Scheduler"]
