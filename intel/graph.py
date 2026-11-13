"""intel.graph - cross-day knowledge network accumulation.

Each day's analyzed items are merged into a persistent graph.json:
  nodes   = unique scientific/model/software works (id = source:key), each
            carrying released/problem/solved/topic/hot/watcher/date/sources.
  edges   = thematic/technical links between items sharing a topic or a
            strong keyword overlap.
  topics  = {topic: {count, last_seen, items}} rolling aggregates that reveal
            emerging hot spots over time.

The graph is additive (idempotent on re-run) so it grows into a real knowledge
net as days accumulate — the user's "逐渐构成知识网" requirement.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import List

from .config import Config
from .models import IntelItem
from .store import Store

_STOP = {"the", "and", "for", "with", "using", "based", "toward", "towards",
         "large", "model", "models", "language", "via", "on", "from", "that",
         "this", "new", "novel", "an", "a", "of", "to", "in", "its", "their",
         "approach", "method", "framework", "paper", "system", "improving"}


def _tokens(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower())
            if len(w) > 2 and w not in _STOP}


def _overlap(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / float(len(ta | tb))


class GraphBuilder:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = Store(cfg)

    def merge(self, items: List[IntelItem]) -> dict:
        g = self.store.load_graph()
        nodes, edges = g.get("nodes", []), g.get("edges", [])
        topics = g.get("topics", {})
        by_id = {n["id"]: n for n in nodes}
        today = _today()

        # --- upsert nodes ---
        for it in items:
            nid = it.id
            node = {
                "id": nid, "source": it.source, "key": it.key, "title": it.title,
                "url": it.url, "date": it.date,
                "released": it.released, "problem": it.problem, "solved": it.solved,
                "topic": it.topic, "hot": it.hot, "watcher": it.watcher,
                "seen": [today], "sources": [it.source],
            }
            if nid in by_id:
                prev = by_id[nid]
                if today not in prev["seen"]:
                    prev["seen"].append(today)
                prev["date"] = max(prev["date"], it.date)
                prev["hot"] = max(prev["hot"], it.hot)
                prev["watcher"] = prev.get("watcher") or it.watcher
                if it.released:
                    prev["released"] = it.released or prev.get("released")
                    prev["problem"] = it.problem or prev.get("problem")
                    prev["solved"] = it.solved or prev.get("solved")
            else:
                by_id[nid] = node
                nodes.append(node)

        # --- edges: 主题相同 / 关键词高度重叠 ---
        existing = set()
        for e in edges:
            existing.add((e["from"], e["to"]))
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a, b = items[i], items[j]
                if a.source == "arxiv" and b.source == "arxiv":
                    pass                    # 都是论文也走主题/词重叠
                sim = _overlap(a.title, b.title)
                same_topic = a.topic and a.topic == b.topic
                if (sim >= self.cfg.graph_edge_min_sim or same_topic) and sim > 0:
                    u, v = a.id, b.id
                    rel = "主题相关" if same_topic else "技术相近"
                    for x, y in ((u, v), (v, u)):
                        if (x, y) in existing:
                            continue
                        edges.append({"from": x, "to": y, "label": rel,
                                      "sim": round(sim, 2), "since": today})
                        existing.add((x, y))

        # --- 主题聚合 ---
        for it in items:
            if not it.topic:
                continue
            t = topics.setdefault(it.topic, {"count": 0, "last_seen": "", "items": []})
            t["count"] += 1
            t["last_seen"] = max(t["last_seen"], today)
            # 该主题下保留最近各 item 去重的 id, 用 count 限制内存
            seen_items = set(t["items"])
            keep_n = 30
            if it.id not in seen_items:
                t["items"].append(it.id)
                t["items"] = t["items"][-keep_n:]

        # 裁剪: 边数上限(防无限), 主题 items 上限
        if len(edges) > self.cfg.graph_keep_edges:
            # 保最近接入的边(时间戳 new 在后)
            edges.sort(key=lambda e: e.get("since", ""))
            edges = edges[-self.cfg.graph_keep_edges:]
        g = {"nodes": nodes, "edges": edges, "topics": topics}
        self.store.save_graph(g)
        return g


def _today() -> str:
    import datetime
    return datetime.datetime.now().date().isoformat()
