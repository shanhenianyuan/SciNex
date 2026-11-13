"""intel.deepread - deep individual intelligence read (精读).

User selects which item to deep-read (呈现按热度排序, 用户自主挑选). The deep
analysis reuses the LLM and mirrors the spirit of the reader's one-click
interpretation — but the prompt text must NOT contain the 5 words that make
the front-end send() auto-switch to the (slow, networked) agent model:
  调研 | 调查 | 全面了解 | 深度分析 | 国内外
So the section labels are renamed to tech-oriented wording.

Output is markdown prose saved under deep/<date>.json keyed by item id, plus
returned to the front-end for immediate rendering.
"""
from __future__ import annotations

import json
from typing import Callable, Optional

from .config import Config, now_bj
from .models import IntelItem

_DEEP_SYS = (
    "你是严谨的 AI 技术纵深分析师, 面向专业读者写一篇中文深度研判。要求: 直击底层机理、"
    "紧扣这条技术动态本身, 不空谈、不编造; 用'■'做小标题, 结构与信息密度兼顾。"
)

_DEEP_PROMPT = (  # 触发词核对: 已避开 '调研/调查/全面了解/深度分析/国内外'
    "请针对下面这条 AI 技术动态做深度研判, 逐一输出:\n"
    "■ 这条动态是什么: 一句话定位它发布了什么成果、出现在什么技术环节。\n"
    "■ 针对的问题: 它要解决的技术痛点是什么? 背后的机理缺陷(算力/数学/数据/架构/对齐等)在哪? 不解决会怎样?\n"
    "■ 解决方案原理: 核心方案是什么? 关键设计为何能对症下药? 主模块/组件如何衔接流动?\n"
    "■ 影响与价值: 对技术前沿、对已有工作(先行成果/并列方案)、对落地使用的影响; 谁最可能受益/受冲击。\n"
    "■ 局限与后续: 它承认的不足、未解决的问题、可能的方向; 给你的研判: 这条值不值得重点跟随、为什么。\n"
    "最后 2-3 句精华结论。中文, 有技术纵深, 不杜撰。\n\n"
    "动态信息如下:\n标题: {title}\n来源: {source} / {date}\n{body}"
)


class DeepReader:
    def __init__(self, cfg: Config, llm: Callable):
        self.cfg = cfg
        self.llm = llm

    def read(self, it: IntelItem) -> dict:
        body = "\n".join(filter(None, [
            "成果补充: " + it.released if it.released else "",
            "针对问题: " + it.problem if it.problem else "",
            "解决价值: " + it.solved if it.solved else "",
            "摘要: " + (it.summary or "")[:1200]]))
        user = _DEEP_PROMPT.format(title=it.title, source=it.source,
                                   date=it.date, body=body)[:6000]
        payload = {
            "model": self.cfg.analyze_model,
            "messages": [{"role": "system", "content": _DEEP_SYS},
                         {"role": "user", "content": user}],
            "temperature": 0.3,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        text = ""
        try:
            resp = self.llm(payload)
            text = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
        except Exception:
            text = ""
        return {"id": it.id, "title": it.title, "source": it.source, "key": it.key,
                "url": it.url, "date": it.date, "watcher": it.watcher,
                "item": it.to_dict(), "text": text.strip(),
                "ts": now_bj().isoformat()}
