"""intel.network - network-failure state machine (explicit user requirement).

When any fetch source goes down, the failure is *recorded* (persisted to
meta.json) with a reason and an exponential-backoff retry point; the front-end
shows a banner. On network recovery the scheduler auto-triggers a re-run, and
a manual POST /intel/v1/run works anytime.

The callback here is lightweight; Engine owns the actual run logic. Network
detection is done by fetch health probes via the deps.http_get injected by
serve.py (which already honors OUTBOUND_PROXY vs direct).
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from .config import Config, now_bj
from .models import NetState
from .store import Store

_FMT = "%Y-%m-%d %H:%M:%S"


def _parse(s: str):
    try:
        return datetime.strptime(s, _FMT)
    except (ValueError, TypeError):
        return None


class NetworkMonitor:
    def __init__(self, cfg: Config, store: Store, http_get):
        self.cfg = cfg
        self.store = store
        self.http_get = http_get          # injected; honors OUTBOUND_PROXY

    # ---- 读取 ----
    def state(self) -> NetState:
        return self.store.load_net_state()

    # ---- 上报 ----
    def mark_running(self) -> None:
        st = self.store.load_net_state()
        st.last_run_bj = now_bj().strftime(_FMT)
        st.last_status = "running"
        self.store.save_net_state(st)

    def mark_success(self, n_items: int) -> None:
        st = self.store.load_net_state()
        st.last_status = "ok"
        st.last_success_bj = now_bj().strftime(_FMT)
        st.fail_count = 0
        st.next_attempt_bj = ""
        st.network_down = False
        st.last_error = ""
        st.last_items = n_items
        self.store.save_net_state(st)

    def mark_fail(self, reason: str, *, network: bool = True) -> None:
        """记录一次失败。network=True 表示网络层失败→置 network_down & 排重试。"""
        st = self.store.load_net_state()
        st.last_status = "fail"
        st.last_error = reason
        st.fail_count = st.fail_count + 1
        if network:
            st.network_down = True
            # 指数退避: 首次 INTEL_NET_RETRY_INITIAL_SEC, 之后 *factor, 封顶 max
            base = self.cfg.net_retry_initial_sec
            backoff = min(base * (self.cfg.net_retry_factor ** max(0, st.fail_count - 1)),
                          self.cfg.net_retry_max_sec)
            st.next_attempt_bj = (now_bj() + timedelta(seconds=backoff)).strftime(_FMT)
        self.store.save_net_state(st)

    def mark_ok_network(self) -> None:
        """网络探针恢复: 清 network_down 标记, 由调度器据此补跑。"""
        st = self.store.load_net_state()
        if st.network_down:
            st.network_down = False
            self.store.save_net_state(st)

    # ---- 判定 ----
    def should_retry(self) -> bool:
        """True 表示: 上次失败且(网络已恢复 或 已到重试点), 应启动补跑。"""
        st = self.store.load_net_state()
        if st.last_status != "fail":
            return False
        if not st.network_down:
            return True                       # 网络已恢复(手动探活或已 mark_ok)
        nx = _parse(st.next_attempt_bj)
        return nx is not None and now_bj() >= nx

    # ---- 网络探针(供调度器周期调用) ----
    def probe(self) -> bool:
        """探测外网可达性(轻量, 命中任一常用源)。返回 True=网络通。"""
        for url in ("https://arxiv.org/abs/1706.03762",
                    "https://huggingface.co/api/models/databricks/dbrx-instruct",
                    "https://api.github.com/repos/vllm-project/vllm"):
            try:
                code, _ = self.http_get(url, timeout=10, retries=0)
                if 200 <= code < 500:         # 任何非 5xx/超时 都视为网络通路
                    self.mark_ok_network()
                    return True
            except Exception:
                continue
            # 继续尝试下一个源, 全失败才判 down
        return False
