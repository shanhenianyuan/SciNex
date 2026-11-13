"""intel.scheduler - daily scheduled run + network-recovery auto-retry.

A single daemon thread polls every INTEL_SCHEDULER_POLL_SEC seconds. On each
tick it:
  1. If the daily run time (INTEL_TIME, Beijing) has been reached today and
     today's run hasn't happened yet  -> trigger a run.
  2. If the last run failed and NetworkMonitor.should_retry()
     (network recovered, or backoff point passed) -> trigger a re-run.

The Engine owns the actual run + state marking; the scheduler only decides
*when* and calls engine.run(manual=False). Concurrency is guarded by the
engine's lock so a manual POST and a scheduled tick never overlap.
"""
from __future__ import annotations

import threading
import time

from .config import Config, now_bj
from .network import NetworkMonitor
from .store import Store


def _parse_hhmm(s: str) -> tuple:
    try:
        h, m = [int(x) for x in s.strip().split(":", 1)]
        return h % 24, m % 60
    except (ValueError, AttributeError):
        return 0, 30          # 默认 00:30


class Scheduler:
    def __init__(self, cfg: Config, store: Store, net: NetworkMonitor, engine,
                 manual_run_cb):
        self.cfg = cfg
        self.store = store
        self.net = net
        self.engine = engine              # duck-typed: run(manual: bool) -> None
        self._stop = threading.Event()

    def start(self) -> "Scheduler":
        t = threading.Thread(target=self._loop, daemon=True, name="intel-sched")
        t.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        hh, mm = _parse_hhmm(self.cfg.run_time_bj)
        last_scheduled_date = None
        while not self._stop.wait(self.cfg.scheduler_poll_sec):
            try:
                ni = now_bj()
                today = ni.strftime("%Y-%m-%d")
                now_min = ni.hour * 60 + ni.minute
                target_min = hh * 60 + mm
                try:
                    st = self.net.state()
                except Exception:
                    continue
                # 1) 定时触发: 已到点 & 今天还没跑成功/尝试过调度
                if (now_min >= target_min and last_scheduled_date != today
                        and st.last_status not in ("running",)):
                    last_scheduled_date = today
                    dirty = st.last_success_bj.split(" ")[0]  # 最近成功日期
                    if dirty != today:      # 今天还没成功跑过
                        try:
                            self.engine.run(manual=False)
                        except Exception:
                            pass
                    continue
                # 2) 断网恢复自动补跑
                if self.net.should_retry():
                    try:
                        self.engine.run(manual=False)
                    except Exception:
                        pass
            except Exception:
                continue
