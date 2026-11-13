"""intel.config - env-driven configuration for the SciNex Frontier Intel module.

All tunables come from environment variables (or the same-directory .env via
serve.py's loader). Nothing is hard-coded; the module is deployment-agnostic
(local vs 91 container). See serve.py _pick_vars for the env/.env resolution.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from datetime import datetime
from zoneinfo import ZoneInfo

TZ_BJ = ZoneInfo("Asia/Shanghai")          # 用户约定: 北京时间
_FN = os.path.dirname(os.path.abspath(__file__))
# intel 包默认落在 serve.py 同目录下; 数据目录默认 serve.py 同目录的 sci_research/
# 容器内 91 经 INTEL_DIR env 指向 /opt/remote_ai/sci_research
_DEFAULT_INTEL_DIR = os.path.join(os.path.dirname(_FN), "sci_research")


def _env(name: str, default: str) -> str:
    """从环境变量取值, 优先真正的 os.environ, 其次 intel 同级的 .env(经 serve 注入)。"""
    v = os.environ.get(name)
    if v:
        return v.strip()
    # serve.py 会把 .env 当作 env 注入(shell 环境或 --env-file), 故此处只读 os.environ。
    return default


def now_bj() -> datetime:
    """当前北京时间 (naive, 无 tzinfo 便于直接比较/展示)。"""
    return datetime.now(TZ_BJ).replace(tzinfo=None)


@dataclass
class Config:
    # --- 存储 ---
    intel_dir: str = field(default_factory=lambda: _env("INTEL_DIR", _DEFAULT_INTEL_DIR))

    # --- 抓取 ---
    arxiv_topics: tuple = ("large language model", "multi-agent", "AI agent",
                           "multimodal model", "reinforcement learning",
                           "model alignment", "reasoning model", "vision language model")
    arxiv_window_days: int = 7              # 取最近 N 天的 arXiv 新文
    arxiv_max_per_topic: int = 5            # 每主题取前 N 篇
    arxiv_pool_cap: int = 40                # arXiv 池上限(去重后)
    hf_max: int = 15                        # HuggingFace近期模型数
    hf_trending_max: int = 15               # HF 热点模型数
    gh_max: int = 15                        # GitHub 近期热度仓库数
    gh_min_stars: int = 5                   # GitHub 最低 stars(过滤低星垃圾仓)
    gh_search_window_days: int = 14         # GitHub pushed_at 窗口
    item_cap_daily: int = 60                # 全池日上限(LLM 分析前先粗筛)

    # --- 分析(LLM) ---
    analyze_model: str = "deepseek-v4-1-flash"
    analyze_max_chars: int = 1600           # 每条送入 LLM 的摘要/描述截断长度
    hot_baseline: float = 5.0               # 无 LLM 评分时的兜底热度

    # --- 知识网 ---
    graph_edge_min_sim: float = 0.32        # 主题/关键词余弦相似阈值
    graph_keep_edges: int = 400             # 累积边数硬上限(防无限膨胀)

    # --- 调度 ---
    run_time_bj: str = field(default_factory=lambda: _env("INTEL_TIME", "00:30"))
    scheduler_poll_sec: int = 900           # 调度线程轮询间隔(s)=15min
    net_retry_initial_sec: int = 600        # 断网首次退避=10min
    net_retry_max_sec: int = 6 * 3600       # 退避上限=6h
    net_retry_factor: float = 2.0           # 退避倍数
    lock_timeout_min: int = 30              # 运行锁超时,防僵尸占用

    # --- 监听 ---
    watch_max: int = 40                     # 重点跟踪项上限
    watch_report_cap: int = 8               # 每重点项专栏最多展示条数

    def ensure_dir(self) -> "Config":
        os.makedirs(self.intel_dir, exist_ok=True)
        for sub in ("raw", "analysis", "deep"):
            os.makedirs(os.path.join(self.intel_dir, sub), exist_ok=True)
        return self


def load_config() -> Config:
    return Config().ensure_dir()
