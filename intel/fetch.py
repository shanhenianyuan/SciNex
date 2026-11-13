"""intel.fetch - three-source fetchers: arXiv papers, HuggingFace models, GitHub repos.

All outbound calls go through the injected `http_get` callback provided by
serve.py, which already honors OUTBOUND_PROXY (91's tunnel) vs direct (local).
This keeps the module deployment-agnostic and matches the "don't hard-code
network" rule.

Each fetcher returns a list[RawItem] (normalized). Failures must raise a
NetworkError so the Engine can mark the network state down.
"""
from __future__ import annotations

import html
import json
import re
import time
import urllib.parse
from typing import Callable, List, Optional

from .config import Config, now_bj, TZ_BJ
from .models import RawItem


class NetworkError(Exception):
    """网络层失败(超时/5xx/不可达), 触发断网状态。"""


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(s or "")).strip()


def _norm_topics(topics: List[str]) -> List[str]:
    """规范化主题: HTML 实体解码 + 去空白 + 过滤空项。"""
    out = []
    for t in topics or []:
        t = re.sub(r"\s+", " ", html.unescape(t or "")).strip()
        if t:
            out.append(t)
    return out


class Fetcher:
    def __init__(self, cfg: Config, http_get: Callable):
        self.cfg = cfg
        self.g = http_get

    def _get(self, url: str, timeout: int = 20) -> Optional[str]:
        code, body = self.g(url, timeout=timeout, retries=1,
                            accept="application/json, */*")
        if code is None or code < 0:
            raise NetworkError("unreachable: %s" % url.split("?")[0])
        if code == 200:
            return body
        if code >= 500:
            raise NetworkError("http %s: %s" % (code, url.split("?")[0]))
        return None          # 404/429 等视为"无此结果", 非网络失败

    # ---------- arXiv ----------
    def arxiv(self) -> List[RawItem]:
        out, seen = [], set()
        cutoff = now_bj().date().isoformat()
        for topic in self.cfg.arxiv_topics:
            if len(out) >= self.cfg.arxiv_pool_cap:
                break
            q = self._arxiv_query(topic)
            url = ("https://export.arxiv.org/api/query?search_query=%s"
                   "&sortBy=submittedDate&sortOrder=descending&max_results=%d"
                   % (q, self.cfg.arxiv_max_per_topic))
            body = self._get(url, timeout=25)
            if not body:
                time.sleep(1)
                continue
            for e in re.findall(r"<entry>(.*?)</entry>", body, re.S):
                m_id = re.search(r"<id>(.*?)</id>", e, re.S)
                aid = m_id.group(1).split("/abs/")[-1].strip() if m_id else ""
                m_t = re.search(r"<title>(.*?)</title>", e, re.S)
                m_s = re.search(r"<summary>(.*?)</summary>", e, re.S)
                m_p = re.search(r"<published>(\d{4}-\d{2}-\d{2})", e)
                m_a = re.findall(r"<name>(.*?)</name>", e)
                title = _clean(m_t.group(1)) if m_t else ""
                if not title or not aid or aid in seen:
                    continue
                seen.add(aid)
                out.append(RawItem(
                    source="arxiv", key=aid, title=title,
                    url="https://arxiv.org/abs/%s" % aid,
                    date=(m_p.group(1) if m_p else cutoff),
                    summary=_clean(m_s.group(1))[:self.cfg.analyze_max_chars] if m_s else "",
                    meta={"authors": ", ".join(a.strip() for a in m_a[:8])}))
            time.sleep(1.2)      # arXiv throttle ~1 req/3s, 保守间隔
        return out

    @staticmethod
    def _arxiv_query(topic: str) -> str:
        terms = topic.replace("&", "and")
        return re.sub(r"\s+", "+", terms)

    # ---------- HuggingFace ----------
    def huggingface(self) -> List[RawItem]:
        out, seen = [], set()
        # 近期更新模型
        url = ("https://huggingface.co/api/models?sort=lastModified&direction=-1"
               "&limit=%d&full=false" % self.cfg.hf_max)
        body = self._get(url)
        if body:
            try:
                items = json.loads(body)
            except json.JSONDecodeError:
                items = []
            for it in items:
                self._hf_item(it, out, seen, "updated")
        # 热点模型(补覆盖签到型热点)
        url2 = ("https://huggingface.co/api/trending?type=model&limit=%d&full=true" % self.cfg.hf_trending_max)
        body2 = self._get(url2)
        if body2:
            try:
                j = json.loads(body2)
            except json.JSONDecodeError:
                j = None
            trending = []
            if isinstance(j, dict):
                # 新结构: {"recentlyTrending":[{"repoData":{...}}, ...]}
                for t in j.get("recentlyTrending") or []:
                    rd = t.get("repoData") if isinstance(t, dict) else None
                    if isinstance(rd, dict):
                        trending.append(rd)
                # 兼容旧结构 {"models":[...]}
                if not trending and isinstance(j.get("models"), list):
                    trending = j["models"]
            elif isinstance(j, list):
                trending = j
            for it in trending:
                self._hf_item(it, out, seen, "trending")
        return out

    def _hf_item(self, it: dict, out: List[RawItem], seen: set, kind: str):
        mid = it.get("id") or it.get("modelId") or ""
        title = mid or it.get("name") or ""
        if not mid or mid in seen:
            return
        seen.add(mid)
        created = (it.get("createdAt") or it.get("lastModified") or "")[:10]
        downloads = it.get("downloads") or 0
        likes = it.get("likes") or 0
        desc = it.get("description") or ""
        out.append(RawItem(
            source="hf", key=mid, title=title,
            url="https://huggingface.co/%s" % mid,
            date=created or now_bj().date().isoformat(),
            summary=_clean(desc)[:self.cfg.analyze_max_chars],
            meta={"kind": kind, "downloads": downloads, "likes": likes,
                  "library": it.get("library_name") or ""}))

    # ---------- GitHub ----------
    def github(self) -> List[RawItem]:
        out, seen = [], set()
        since = (now_bj() - _days(self.cfg.gh_search_window_days)).date().isoformat()
        # 用带引号关键词短语搜索(topic: qualifier 只匹配单 token, 多词组会 422)
        parts = ['"%s"' % t.strip()
                 for t in _norm_topics(self.cfg.arxiv_topics)[:6]]
        q = urllib.parse.quote(" OR ".join(parts) + " created:>=%s" % since)
        url = ("https://api.github.com/search/repositories"
               "?q=%s&sort=updated&order=desc&per_page=%d"
               % (q, self.cfg.gh_max))
        # GitHub 也关注大模型/agent 关键词仓库
        url2 = ("https://api.github.com/search/repositories"
                "?q=%s&sort=updated&order=desc&per_page=%d"
                % ("llm+agent+in:name,description,readme", self.cfg.gh_max))
        for u in (url, url2):
            body = self._get(u)
            if not body:
                time.sleep(1)
                continue
            try:
                items = json.loads(body).get("items", [])
            except json.JSONDecodeError:
                items = []
            for r in items:
                full = r.get("full_name") or ""
                pushed = (r.get("pushed_at") or r.get("updated_at") or "")[:10]
                stars = r.get("stargazers_count") or 0
                if not full or full in seen or pushed < since:
                    continue
                if stars < self.cfg.gh_min_stars:          # 过滤低星垃圾仓
                    continue
                seen.add(full)
                desc = r.get("description") or r.get("name") or ""
                out.append(RawItem(
                    source="github", key=full, title=full,
                    url=r.get("html_url") or ("https://github.com/%s" % full),
                    date=pushed,
                    summary=_clean(desc)[:self.cfg.analyze_max_chars],
                    meta={"stars": r.get("stargazers_count") or 0,
                          "forks": r.get("forks_count") or 0,
                          "language": r.get("language") or "",
                          "owner": (r.get("owner") or {}).get("login") or ""}))
        return out


def _days(n: int):
    import datetime as _dt
    return _dt.timedelta(days=n)


def fetch_all(cfg: Config, http_get: Callable) -> dict:
    """抓取三源, 返回 {"arxiv":[...],"hf":[...],"github":[...],"errors":[...]}。

    生产级容错: 三源独立抓取, 单个源失败只记入 errors(不影响其余源);
    仅当三源全部失败抛 NetworkError(Engine 据此标记断网/重试)。
    """
    f = Fetcher(cfg, http_get)
    result = {"arxiv": [], "hf": [], "github": [], "errors": []}
    for name, fn in (("arxiv", f.arxiv), ("hf", f.huggingface), ("github", f.github)):
        try:
            result[name] = fn()
        except NetworkError as e:
            result["errors"].append({"source": name, "kind": "network",
                                     "error": str(e)})
        except Exception as e:                     # 解析等非网络错也容错
            result["errors"].append({"source": name, "kind": "parse",
                                     "error": "parse-error: %s" % e})
    if not (result["arxiv"] or result["hf"] or result["github"]):
        # 全失败: 仅当存在真实网络错误才判断网(交状态机退避); 若全是解析/空
        # (如 API 结构临时变化) 不算断网, 原样返回供上层按 no-data 处理。
        net_errs = [e for e in result["errors"] if e.get("kind") == "network"]
        if net_errs:
            raise NetworkError("all-sources-failed: %s" % net_errs[0]["error"])
    return result
