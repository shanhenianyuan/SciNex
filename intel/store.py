"""intel.store - JSON persistence for the Frontier Intel module.

Atomic writes (write temp file + os.replace) so a crash mid-write never
corrupts a state/metadata file. All dated artifacts are keyed by YYYY-MM-DD
in Beijing time; the accumulated knowledge graph & watch config live at the
module root.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Any, Optional

from .config import Config, now_bj
from .models import NetState

_LOCK = threading.Lock()


def _atomic_write(path: str, obj: Any) -> None:
    """把 obj 以 JSON 原子写盘(tmp 同目录 + os.replace)。"""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


class Store:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.dir = cfg.intel_dir
        self.meta_path = os.path.join(self.dir, "meta.json")
        self.graph_path = os.path.join(self.dir, "graph.json")
        self.watch_path = os.path.join(self.dir, "watch.json")

    # ---------- meta / 状态 ----------
    def load_net_state(self) -> NetState:
        m = _read_json(self.meta_path, {})
        if not isinstance(m, dict):
            m = {}
        return NetState(**{k: v for k, v in m.items()
                           if k in NetState.__dataclass_fields__})

    def save_net_state(self, st: NetState) -> None:
        with _LOCK:
            _atomic_write(self.meta_path, st.to_dict())

    # ---------- 每日原始/分析 ----------
    def daily_path(self, date: Optional[str] = None, kind: str = "analysis") -> str:
        if not date:
            date = now_bj().strftime("%Y-%m-%d")
        return os.path.join(self.dir, kind, date + ".json")

    def save_daily(self, obj: dict, date: Optional[str] = None, kind: str = "analysis") -> None:
        with _LOCK:
            _atomic_write(self.daily_path(date, kind), obj)

    def load_daily(self, date: str, kind: str = "analysis") -> dict:
        return _read_json(self.daily_path(date, kind), {})

    def list_days(self, kind: str = "analysis") -> list:
        p = os.path.join(self.dir, kind)
        try:
            return sorted(f[:-5] for f in os.listdir(p) if f.endswith(".json"))
        except OSError:
            return []

    # ---------- 知识网 ----------
    def load_graph(self) -> dict:
        g = _read_json(self.graph_path, {})
        return g or {"nodes": [], "edges": [], "topics": {}}

    def save_graph(self, g: dict) -> None:
        with _LOCK:
            _atomic_write(self.graph_path, g)

    # ---------- 重点跟踪项 ----------
    def load_watch(self) -> list:
        w = _read_json(self.watch_path, [])
        return w if isinstance(w, list) else []

    def save_watch(self, items: list) -> None:
        with _LOCK:
            _atomic_write(self.watch_path, items)
