"""intel.analyze - LLM structured analysis of fetched items.

Each raw item is sent to the injected LLM (via serve.py's llm_completion,
which targets the internal deepseek gateway and stays internal-direct — it is
NOT routed through OUTBOUND_PROXY). The model returns a strict JSON of:
  released (发布了什么成果) / problem (针对什么问题) / solved (解决了什么)
  / topic (技术主题短词) / hot (0-10 热度).

LLM failures are non-fatal: we keep the item with a low-confidence default so
the daily report is never empty. Watch-item matching happens here too.
"""
from __future__ import annotations

import json
import re
from typing import Callable, List, Optional

from .config import Config, now_bj
from .models import IntelItem, RawItem

# 精读/分析 prompt 内严禁出现 send() 自动切 agent 的 5 个触发词:
#   调研 | 调查 | 全面了解 | 深度分析 | 国内外
# 故用「情报研判」「技术脉络」等替代。若误加, 前端 send() 会静默改走联网 agent。
_ANALYZE_SYS = (
    "你是资深 AI 技术情报分析师。针对用户给的一条技术动态, 输出严格的 JSON(不要 markdown, 不要多余文字), 字段:"
    '{"released":"一句话说明发布了什么成果","problem":"针对什么问题(技术痛点/现状短板)","solved":"解决了什么问题(怎么解决、价值)","topic":"所属技术主题(2-6字, 如 推理模型/智能体/多模态)","hot":5}'
    "。hot 是 0-10 的热度评分, 依据: 关注度、颠覆性、对领域的影响。用中文, 简洁具体, 不要编造事实。"
)


def _extract_json(text: str) -> Optional[dict]:
    """从 LLM 输出中稳健提取 JSON(容忍代码块/前后缀)。"""
    if not text:
        return None
    t = re.sub(r"```(?:json)?", "", text).strip()
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _hot_of(r: RawItem) -> float:
    """无 LLM 时的启发式热度兜底: 依据源与乐观信号。"""
    base = 5.0
    if r.source == "arxiv":
        return base
    if r.source == "hf":
        d = r.meta.get("downloads") or 0
        return min(9.0, base + 0.5 + round(min(d, 100000) / 20000, 1))
    if r.source == "github":
        stars = r.meta.get("stars") or 0
        return min(9.0, base + 0.5 + round(min(stars, 20000) / 5000, 1))
    return base


class Analyzer:
    def __init__(self, cfg: Config, llm: Callable):
        self.cfg = cfg
        self.llm = llm          # injected; signature llm(payload)->dict(chunk)

    def _one(self, r: RawItem) -> IntelItem:
        it = IntelItem.from_raw(r)
        it.hot = _hot_of(r)
        snippet = (r.summary or r.title)[:self.cfg.analyze_max_chars]
        messages = [
            {"role": "system", "content": _ANALYZE_SYS},
            {"role": "user", "content": "标题: %s\n来源: %s\n%s" % (r.title, r.source, snippet)},
        ]
        payload = {
            "model": self.cfg.analyze_model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": 300,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        try:
            resp = self.llm(payload)
            content = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
            parsed = _extract_json(content)
            if parsed:
                it.released = str(parsed.get("released") or "")[:300]
                it.problem = str(parsed.get("problem") or "")[:300]
                it.solved = str(parsed.get("solved") or "")[:300]
                it.topic = str(parsed.get("topic") or "")[:30] or r.title[:12]
                try:
                    it.hot = float(parsed.get("hot") or it.hot)
                except (TypeError, ValueError):
                    pass
                it.hot = min(10.0, max(0.0, it.hot))
        except Exception:
            pass                 # LLM 挂了不致命, 保底返回
        return it

    def analyze(self, raw: List[RawItem], watch: List[dict]) -> List[IntelItem]:
        items = [self._one(r) for r in raw]
        # 重点跟踪项匹配: 标题/摘要命中 keyword 即打上 watcher 标记
        for it in items:
            hay = (it.title + " " + it.summary).lower()
            for w in watch:
                ks = w.get("keywords") or [w.get("name") or ""]
                if any(k.lower() in hay for k in ks if k):
                    it.watcher = w.get("name", "")
                    it.hot = min(10.0, it.hot + 1.0)   # 命中重点 → 加权
                    break
        # 热度排序, 稳定
        items.sort(key=lambda x: x.hot, reverse=True)
        return items


# ---------- 重点跟踪项卫生 ----------
def sanitize_watch(items) -> list:
    """清洗/校验重点跟踪项: 返回去除非法字段后的列表。"""
    out = []
    for w in (items or []):
        if not isinstance(w, dict):
            continue
        name = str(w.get("name") or "").strip()[:40]
        if not name:
            continue
        ks = [str(k).strip()[:60] for k in (w.get("keywords") or [])]
        ks = [k for k in ks if k]
        if not ks:
            ks = [name]
        out.append({"name": name, "keywords": ks,
                    "sources": w.get("sources") or ["arxiv", "hf", "github"]})
    return out[:40]
