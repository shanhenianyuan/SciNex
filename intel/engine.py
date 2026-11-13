"""intel.engine - orchestration of the Frontier Intel pipeline.

run() executes the five-step closed loop: fetch -> analyze -> graph -> persist
-> (deep is on-demand, user-selected). It owns the lock (no concurrent runs),
marks the network state via NetworkMonitor, and guarantees every outcome
(success or failure) is persisted so the UI always has truthful status.

The engine receives ALL external capabilities via dependency injection
(http_get, llm) from serve.py — it never imports serve, never touches network
config directly, and is therefore unit-testable with stubs.
"""
from __future__ import annotations

import json
import threading
from typing import Callable, List, Optional

from .analyze import Analyzer, sanitize_watch
from .config import Config, now_bj
from .deepread import DeepReader
from .fetch import NetworkError, fetch_all
from .graph import GraphBuilder
from .models import IntelItem, RawItem
from .network import NetworkMonitor
from .store import Store


class Engine:
    def __init__(self, cfg: Config, store: Store, net: NetworkMonitor,
                 http_get: Callable, llm: Callable):
        self.cfg = cfg
        self.store = store
        self.net = net
        self.http_get = http_get
        self.llm = llm
        self._lock = threading.Lock()
        self.analyzer = Analyzer(cfg, llm)
        self.deep = DeepReader(cfg, llm)
        self.graph_builder = GraphBuilder(cfg)

    def running(self) -> bool:
        return self._lock.locked()

    def run(self, manual: bool = False) -> dict:
        """执行抓取→分析→建网→落盘 的完整循环。返回结果摘要 dict。

        抛 NetworkError 时由调用方对日志/状态做最终落盘; 本方法保证:
          成功 → save_daily(analysis) + save_graph + mark_success
          网络失败 → mark_fail(network=True) 并 re-raise
        非网络异常 → mark_fail(network=False) 并 re-raise
        """
        if not self._lock.acquire(blocking=False):
            return {"ok": False, "reason": "busy", "items": []}
        try:
            self.net.mark_running()
            watch = sanitize_watch(self.store.load_watch())
            try:
                raw_all = fetch_all(self.cfg, self.http_get)
            except NetworkError as e:
                self.net.mark_fail("network: %s" % e, network=True)
                raise
            except Exception as e:            # 非网络异常(如解析)也记为失败但不挂断网
                self.net.mark_fail("fetch-error: %s" % e, network=False)
                raise
            raw: List[RawItem] = (raw_all.get("arxiv") or []) \
                + (raw_all.get("hf") or []) + (raw_all.get("github") or [])
            errors = raw_all.get("errors") or []
            source_errs = [e for e in errors
                           if isinstance(e, dict) and e.get("source")]
            if not raw:
                self.net.mark_fail("no-data: 三个源均无新内容", network=False)
                return {"ok": True, "items": 0, "reason": "empty",
                        "errors": source_errs}
            # 日顶上限(粗筛优先保留热度高的: LLM 前用启发式)
            raw.sort(key=lambda r: _heuristic_hot(r), reverse=True)
            raw = raw[:self.cfg.item_cap_daily]
            items = self.analyzer.analyze(raw, watch)
            today = now_bj().strftime("%Y-%m-%d")
            day = {"date": today, "items": [it.to_dict() for it in items]}
            self.store.save_daily(day, today, "analysis")
            # 原始(含单源失败记录, 供 meta 审计; 仅为全源失败前的部分降级)
            self.store.save_daily({"date": today, "raw": [r.to_dict() for r in raw],
                                   "errors": source_errs}, today, "raw")
            g = self.graph_builder.merge(items)
            self.net.mark_success(len(items))
            return {"ok": True, "items": len(items), "date": today,
                    "graph_nodes": len(g.get("nodes", [])),
                    "errors": source_errs}
        finally:
            self._lock.release()

    def status(self) -> dict:
        st = self.net.state()
        d_out = self.store.list_days("analysis")[-30:]
        return {"state": st.to_dict(),
                "running": self.running(),
                "days": d_out,
                "watch_count": len(self.store.load_watch()),
                "intel_dir": self.cfg.intel_dir}

    def daily(self, date: Optional[str] = None) -> dict:
        if not date:
            days = self.store.list_days("analysis")
            date = days[-1] if days else now_bj().strftime("%Y-%m-%d")
        d = self.store.load_daily(date, "analysis")
        if not d:
            return {"date": date, "items": [], "ok": False}
        # 呈现排序: 按热度 desc(用户要求「按热度等维度排序, 我自选精读」)
        it = sorted(d.get("items", []), key=lambda x: x.get("hot", 0.0), reverse=True)
        return {"date": date, "items": it, "ok": True}

    def graph(self) -> dict:
        return self.store.load_graph()

    def deep_read(self, item_id: str, store: Optional[Store] = None) -> dict:
        """按需精读单条(id = source:key)。命中已缓存 deep 则直接返回。"""
        s = store or self.store
        it = self._find_item(item_id, s)
        if it is None:
            return {"ok": False, "reason": "item-not-found", "id": item_id}
        cached = s.load_daily(it.date, "deep").get("items", [])
        for c in cached:
            if c.get("id") == item_id and c.get("text"):
                return {"ok": True, "cached": True, **c}
        res = self.deep.read(it)
        date_path = it.date
        day = s.load_daily(date_path, "deep")
        items = day.setdefault("items", [])
        items = [c for c in items if c.get("id") != item_id]
        items.append(res)
        s.save_daily({"date": date_path, "items": items}, date_path, "deep")
        return {"ok": True, "cached": False, **res}

    def _find_item(self, item_id: str, s: Store) -> Optional[IntelItem]:
        for date in reversed(s.list_days("analysis")):
            d = s.load_daily(date, "analysis")
            for il in d.get("items", []):
                if str(il.get("source")) + ":" + str(il.get("key")) == item_id \
                        or il.get("id") == item_id:
                    return IntelItem.from_raw(RawItem(
                        source=il.get("source", ""), key=il.get("key", ""),
                        title=il.get("title", ""), url=il.get("url", ""),
                        date=il.get("date", ""), summary=il.get("summary", ""),
                        meta=il.get("meta", {})))
        return None

    # ---- 重点跟踪项配置 ----
    def watch(self, items: Optional[list] = None) -> dict:
        if items is None:
            return {"items": self.store.load_watch()}
        clean = sanitize_watch(items)
        self.store.save_watch(clean)
        return {"ok": True, "items": clean}


def _heuristic_hot(r: RawItem) -> float:
    """LLM 分析前用于日上限粗筛的轻量热度(与 analyze 内一致)。"""
    if r.source == "arxiv":
        return 5.0
    if r.source == "hf":
        return min(9.0, 5.5 + round(min(r.meta.get("downloads", 0), 100000) / 20000, 1))
    if r.source == "github":
        return min(9.0, 5.5 + round(min(r.meta.get("stars", 0), 20000) / 5000, 1))
    return 5.0
