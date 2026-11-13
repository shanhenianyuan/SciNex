"""intel.models - plain dataclasses used across the Frontier Intel module.

These are pure data carriers: fetch/analyze/scheduler/engine agree on them so
each layer stays decoupled and independently testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional

SOURCE_LABEL = {"arxiv": "论文", "hf": "模型", "github": "代码", "watch": "重点"}


@dataclass
class RawItem:
    """抓取层产生的统一原始条目(三个源归一化)。"""
    source: str                 # arxiv | hf | github
    key: str                    # 源内唯一 id (arxiv 号 / hf 模型路径 / gh repo 全名)
    title: str
    url: str
    date: str                   # YYYY-MM-DD (发布/更新日期)
    summary: str = ""           # 摘要/描述(英文或原文)
    meta: dict = field(default_factory=dict)   # 源特有字段: authors/stars/downloads 等

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class IntelItem:
    """分析层产出: 已结构化的情报条目。"""
    # 透传 RawItem
    source: str
    key: str
    title: str
    url: str
    date: str
    summary: str = ""
    meta: dict = field(default_factory=dict)
    # 新增结构化字段
    released: str = ""          # 发布了什么成果
    problem: str = ""           # 针对什么问题
    solved: str = ""            # 解决了什么问题
    topic: str = ""             # 所属技术主题(短词)
    hot: float = 5.0            # 热度 0-10
    watcher: str = ""           # 命中哪个重点跟踪项(可空)
    deep: dict = field(default_factory=dict)   # 精读产物(见 deepread)

    @property
    def id(self) -> str:
        return "%s:%s" % (self.source, self.key)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_raw(cls, r: RawItem) -> "IntelItem":
        return cls(source=r.source, key=r.key, title=r.title, url=r.url,
                   date=r.date, summary=r.summary, meta=r.meta)


@dataclass
class NetState:
    """断网/运行状态机, 持久化到 meta.json。"""
    last_run_bj: str = ""        # 最近一次运行尝试(北京 YYYY-MM-DD HH:MM:SS)
    last_success_bj: str = ""    # 最近一次成功完成
    last_status: str = "idle"    # idle | running | ok | fail
    last_error: str = ""         # 失败原因(简要, 如 "network: arxiv timeout")
    fail_count: int = 0          # 连续失败次数(用于指数退避)
    next_attempt_bj: str = ""    # 网络失败后下次自动重试时间
    network_down: bool = False   # 是否处于断网标记
    last_items: int = 0          # 最近成功产出条数

    def to_dict(self) -> dict:
        return asdict(self)
