#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SciNex - Research Reading Engine (same-origin proxy service)
-------------------------------------------------------------
- Statically serves paper-reader.html (same directory)
- /llm/v1/*  -> LLM gateway, API key injected server-side
- /vis/v1/*  -> vision vLLM
SSE (chunked) streaming passthrough so the browser can call the same origin,
completely bypassing CORS.

All endpoints and keys are injected via environment variables (or a same-directory
.env file); the repository contains no private values. See README "Configuration".
"""
import os, sys, socket, json, logging, time, re, html, difflib, subprocess, tempfile, shutil
import urllib.request, urllib.parse, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
SOFFICE = shutil.which("soffice") or shutil.which("libreoffice") or shutil.which("lowriter")

LOG = logging.getLogger("paper-proxy")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

HERE = os.path.dirname(os.path.abspath(__file__))

def _env_file_path():
    """环境变量 ENV_PATH 优先；否则回退同目录 .env 与 ~/.config/scinex.env。"""
    p = os.environ.get("ENV_PATH")
    if p:
        return p
    local = os.path.join(HERE, ".env")
    if os.path.isfile(local):
        return local
    return os.path.expanduser("~/.config/scinex.env")

ENV_PATH = _env_file_path()

def load_env():
    """读取 .env 为 dict，支持 KEY=VALUE 与 export KEY=VALUE，忽略空行/# 注释。"""
    d = {}
    try:
        with open(ENV_PATH, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                line = line[len("export "):] if line.startswith("export ") else line
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k:
                    d[k] = v
    except FileNotFoundError:
        pass  # 无 .env 也可运行，端点直接从环境变量取
    return d

def _pick_vars(*names):
    """依次从 环境变量 → .env 取首个存在的值。"""
    env = load_env()
    for n in names:
        v = os.environ.get(n) or env.get(n)
        if v:
            return v
    return None

# 端点与密钥均通过环境变量/同目录 .env 注入（仓库不含私密值）。
LLM_BASE = _pick_vars("LLM_BASE")            # 例: http://your-llm-gateway/v1
VIS_BASE = _pick_vars("VIS_BASE", "VLM_BASE") or LLM_BASE  # 例: 视觉端点，可省
KEY_ENV   = "LLM_API_KEY"
HTML_PATH = os.path.join(HERE, "paper-reader.html")
MAX_BODY = 64 * 1024 * 1024  # 图片 base64 可能较大

def load_key():
    """API Key：环境变量 LLM_API_KEY 优先，其次同目录 .env。"""
    return _pick_vars(KEY_ENV, "LLM_API_KEY")

KEY = load_key()
if not KEY:
    LOG.error("API key NOT loaded — /llm/v1 will return 401")

def _open_upstream(url, method, body, headers):
    """仅直连上游（LLM 网关与视觉端点均为内网可达，不走 7897 代理）。"""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(req, timeout=30)

def build_target(raw_path, query):
    """raw_path 形如 /llm/v1/chat/completions、/llm/v1/models、/vis/v1/*。
    前缀 /llm/v1 与 /vis/v1 整体剥掉，再拼到上游 base（base 已含末尾 /v1）。"""
    for pfx, base in (("/llm/v1", LLM_BASE), ("/vis/v1", VIS_BASE)):
        if raw_path == pfx:
            url = base.rstrip("/") + "/"
            break
        if raw_path.startswith(pfx + "/"):
            url = base.rstrip("/") + raw_path[len(pfx):]
            break
    else:
        return None
    if query:
        url += "?" + query
    return url

# ============================================================
# 知识网 & 智能体：外部文献数据源（仅直连、UA 伪装、带超时、无 key）
# ============================================================
WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
MAX_TOOL_OUT = 6000
AGENT_MAX_STEPS = 12
KG_MAX_REFS = 12
KG_MAX_SUCC = 5


def http_get(url, timeout=25, retries=2, headers=None, accept="application/json, */*",
             lang="en-US,en;q=0.9"):
    for i in range(max(1, retries)):
        try:
            hdrs = {"User-Agent": WEB_UA, "Accept": accept, "Accept-Language": lang}
            if headers:
                hdrs.update(headers)
            req = urllib.request.Request(url, headers=hdrs)
            op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with op.open(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (429, 403, 500, 502, 503, 504):
                time.sleep(1.5 * (i + 1))
                continue
            return e.code, (e.read() or b"").decode("utf-8", "replace")[:500]
        except Exception as ex:
            return -1, str(ex)
    return 429, "rate-limited"


def PDF_FETCH(url, timeout=60):
    """二进制下载（PDF 用，http_get 解 str 会破坏二进制）。返回 bytes 或 None。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": WEB_UA, "Accept": "application/pdf, */*"})
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open(req, timeout=timeout) as r:
            d = r.read()
            return d if r.status == 200 and d.startswith(b"%PDF") else None
    except Exception as ex:
        LOG.warning("pdf_fetch fail %s -> %s", url, ex)
        return None


def arxiv_search(title, max_results=5):
    """arXiv API 标题检索 -> 元数据+摘要。返回 list[dict]（title 已去空格）。"""
    q = urllib.parse.quote('ti:"%s"' % title)
    code, body = http_get(
        "http://export.arxiv.org/api/query?search_query=%s&max_results=%d" % (q, max_results),
        timeout=20)
    if code != 200:
        return []
    out = []
    for e in re.findall(r"<entry>(.*?)</entry>", body, re.S):
        m = re.search(r"<title>(.*?)</title>", e, re.S)
        t = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
        s = re.search(r"<summary>(.*?)</summary>", e, re.S)
        ab = re.sub(r"\s+", " ", s.group(1)).strip() if s else ""
        pu = re.search(r"<published>(.*?)</published>", e)
        yr = int(pu.group(1)[:4]) if pu else None
        aid = re.search(r"<id>(.*?)</id>", e)
        aid = aid.group(1).split("/abs/")[-1] if aid else ""
        au = re.findall(r"<name>(.*?)</name>", e)
        out.append({"arxiv": aid, "title": t, "year": yr, "abstract": ab,
                    "first_author": au[0] if au else "", "authors": au})
    return out


def arxiv_fuzzy_search(q, max_results=5):
    """UI 在线检索用：all: 宽泛 + 精确标题兜底，取并集去重；保证自由输入也能命中。"""
    seen, merged = set(), []
    queries = ['all:"%s"' % q, 'ti:"%s"' % q, 'all:%s' % q]
    for raw in queries:
        if len(merged) >= max_results:
            break
        u = urllib.parse.quote(raw)
        try:
            code, body = http_get(
                "http://export.arxiv.org/api/query?search_query=%s&max_results=%d"
                % (u, max_results), timeout=20)
        except Exception:
            continue
        if code != 200:
            continue
        for e in re.findall(r"<entry>(.*?)</entry>", body, re.S):
            m = re.search(r"<title>(.*?)</title>", e, re.S)
            t = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
            if not t:
                continue
            s = re.search(r"<summary>(.*?)</summary>", e, re.S)
            ab = re.sub(r"\s+", " ", s.group(1)).strip() if s else ""
            pu = re.search(r"<published>(.*?)</published>", e)
            yr = int(pu.group(1)[:4]) if pu else None
            aid = re.search(r"<id>(.*?)</id>", e)
            aid = aid.group(1).split("/abs/")[-1] if aid else ""
            au = re.findall(r"<name>(.*?)</name>", e)
            if aid in seen:
                continue
            seen.add(aid)
            merged.append({"arxiv": aid, "title": t, "year": yr, "abstract": ab,
                           "first_author": au[0] if au else "", "authors": au})
            if len(merged) >= max_results:
                break
    return merged


def best_arxiv(title):
    """arXiv 标题精确/近似匹配；返回最像的那条或 None。"""
    cs = arxiv_search(title, 5)
    if not cs:
        return None
    t0 = title.lower().strip()
    def score(c):
        t = c["title"].lower().strip()
        r = difflib.SequenceMatcher(None, t0, t).ratio()
        if t0 == t:
            r += 10
        return r
    best = max(cs, key=score)
    return best if score(best) >= 0.6 else None


def crossref_search(q, rows=4):
    url = ("https://api.crossref.org/works?query.bibliographic=%s&rows=%d"
           "&select=DOI,title,issued,container-title,is-referenced-by-count,type"
           % (urllib.parse.quote(q), rows))
    code, body = http_get(url, timeout=20)
    if code != 200:
        return []
    try:
        return json.loads(body).get("message", {}).get("items", [])
    except Exception:
        return []


def crossref_citing(doi, rows=6):
    """Crossref 被引：filter=is-referenced-by:<doi> 返回引用该工作记录的作品(尽力)。"""
    url = ("https://api.crossref.org/works?filter=is-referenced-by:%s&rows=%d"
           "&select=DOI,title,issued,container-title,is-referenced-by-count"
           % (urllib.parse.quote(doi, safe=""), rows))
    code, body = http_get(url, timeout=25)
    if code != 200:
        return []
    try:
        return json.loads(body).get("message", {}).get("items", [])
    except Exception:
        return []


def s2_paper(aid):
    code, body = http_get(
        "https://api.semanticscholar.org/graph/v1/paper/arXiv:%s"
        "?fields=title,year,venue,citationCount,externalIds" % aid, timeout=20, retries=2)
    if code != 200:
        return None
    try:
        return json.loads(body)
    except Exception:
        return None


def s2_citations(aid, limit=8):
    """S2 被引（尽力；共享 IP 常 429）。"""
    code, body = http_get(
        "https://api.semanticscholar.org/graph/v1/paper/arXiv:%s/citations"
        "?fields=title,year,venue&limit=%d" % (aid, limit), timeout=20, retries=2)
    if code != 200:
        return None
    try:
        d = json.loads(body)
        return [c["citingPaper"] for c in d.get("data", []) if c.get("citingPaper")]
    except Exception:
        return None


def fetch_page_text(url, maxchars=12000):
    if not url.lower().startswith(("http://", "https://")):
        return {"error": "invalid url"}
    code, body = http_get(url, timeout=25, retries=1)
    if code != 200:
        return {"error": "status %s" % code}
    body = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", body)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    body = html.unescape(body)
    body = re.sub(r"\s+", " ", body).strip()
    return {"text": body[:maxchars]}


def web_search_bing(q, n=5, lang="en"):
    """常规网页搜索（Bing）。lang='zh' 走中文源 cn.bing.com，返回国内技术来源
    （知乎/CSDN/公司博客等）；lang='en' 走国际源 www.bing.com，返回海外结果。"""
    if lang == "zh":
        url = "https://cn.bing.com/search?q=%s&setlang=zh-hans&count=%d" \
              % (urllib.parse.quote(q), n)
        lang_hdr, html_lang = "zh-CN,zh;q=0.9", "zh"
    else:
        url = "https://www.bing.com/search?q=%s&setlang=en&cc=US&PC=U316&count=%d" \
              % (urllib.parse.quote(q), n)
        lang_hdr, html_lang = "en-US,en;q=0.9", "en"
    code, body = http_get(url, timeout=18, retries=1, lang=lang_hdr)
    if code != 200:
        return []
    out = []
    for href, txt in re.findall(r'<h2><a[^>]*href="([^"]+)"[^>]*>(.*?)</a></h2>', body):
        txt = html.unescape(re.sub(r"<[^>]+>", "", txt)).strip()
        if txt:
            out.append({"title": txt, "url": href})
        if len(out) >= n:
            break
    return out


def llm_completion(payload, timeout=120):
    """服务端调 LLM 网关（同源注入 KEY），非流式 JSON。"""
    req = urllib.request.Request(
        LLM_BASE.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + (KEY or ""),
                 "User-Agent": "paper-proxy/1.0"})
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return {"error": "gateway %s: %s" % (e.code, (e.read() or b"").decode("utf-8", "replace")[:300])}
    except Exception as ex:
        return {"error": "gateway unreachable: %s" % ex}


# ---------- 智能体工具集 ----------
AGENT_TOOLS = [
    {"type": "function", "function": {
        "name": "arxiv_search",
        "description": "在 arXiv 按标题检索论文，获取年份、作者、摘要。查文献信息首选。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "论文标题或精确标题关键词"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "crossref_search",
        "description": "用 Crossref 按标题/作者查论文的 DOI、年份、会议或期刊、被引数。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "论文标题关键词"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "fetch_url",
        "description": "抓取并读取一个网页或论文详情页的文本（如 arxiv abs 页、引用落地页）。",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "http(s) 完整 URL"}},
            "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "常规网页搜索（Bing）。lang='zh' 搜中文资源（国内技术博客/知乎/CSDN/公司论坛等）；"
                      "lang='en' 或默认搜国际英文资源。需要调查某一技术/产品的国内外情况时，"
                      "用不同 lang 各搜一轮，并结合 fetch_url 读取结果页面。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "搜索词"},
            "lang": {"type": "string", "enum": ["zh", "en", "auto"],
                     "description": "搜索结果语言/区域：zh=中文/国内，en=英文/海外，auto=自动"}},
            "required": ["query"]}}},
]


def _abbrev(text, n=90):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text[:n] + ("…" if len(text) > n else "")


def run_agent(messages, model="deepseek-v4-1-flash", progress=lambda t: None):
    """deepseek-v4-1-flash 工具调用循环（≤AGENT_MAX_STEPS 轮），yield 事件 dict。
    progress(text) 回调用于发中间进度。最终答案作为 {'type':'answer','text':…} 产出。"""
    msgs = [{"role": "system", "content":
             "你是一个严谨的论文阅读与文献调研助手。需要查文献、引用、背景、论文发表信息时，"
             "先调用工具检索，再基于真实结果回答。所有回答用中文，给出确定性结论并简要说明依据。\n\n"
             "【深度调研模式】当用户要求『调研/调查/全面了解/研究某一技术或话题』时：\n"
             "1. 分别用 web_search(lang='en') 查海外资料、web_search(lang='zh') 查国内资料，两边都要覆盖；"
             "涉及学术论文再用 arxiv_search / crossref_search 补充。\n"
             "2. 用 fetch_url 读取搜索结果里最有价值的页面，获取实质性内容，不要只看标题。\n"
             "3. 综合国内外信息做对比分析，输出结构：研究现状→代表方案/产品（国内外分开）→技术特点与优劣→"
             "国内外差距与趋势→总结。每一点标注信息来源。\n"
             "4. 检索不到就明确说『未检索到』，绝不编造。"}]
    msgs += [m for m in messages if m.get("role") in ("user", "assistant")]
    for step in range(AGENT_MAX_STEPS):
        payload = {"model": model, "messages": msgs, "temperature": 0.3,
                   "stream": False, "tools": AGENT_TOOLS, "tool_choice": "auto",
                   "chat_template_kwargs": {"enable_thinking": False}}
        resp = llm_completion(payload)
        if resp.get("error"):
            yield {"type": "error", "text": resp["error"]}
            return
        ch = resp["choices"][0]["message"]
        if ch.get("tool_calls"):
            msgs.append({"role": "assistant", "content": ch.get("content") or "",
                         "tool_calls": ch["tool_calls"]})
            for tc in ch["tool_calls"]:
                fn = tc["function"]
                name, args = fn["name"], {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    pass
                progress("🧰 调用 %s(%s)" % (name, _abbrev(args.get("query") or args.get("url"), 40)))
                try:
                    result = AGENT_CALLS[name](args)
                except Exception as ex:
                    result = {"error": str(ex)}
                msgs.append({"role": "tool", "tool_call_id": tc.get("id", "call_%d" % step),
                             "content": json.dumps(result, ensure_ascii=False)[:MAX_TOOL_OUT]})
            continue
        text = ch.get("content") or ""
        if text.strip():
            yield {"type": "answer", "text": text}
            return
        yield {"type": "error", "text": "模型未产出回答"}
        return
    # 已达步骤上限：不死心，基于已积累的工具结果强制让模型直接作答一次
    try:
        msgs.append({"role": "user", "content":
                     "工具检索轮次已达上限，请停止一切新的检索，直接综合上面已经拿到的工具结果与对话历史，"
                     "马上用中文给出最终分析与结论（若关键信息缺失，请如实说明你基于哪些已获得的信息作答）。"})
        resp2 = llm_completion({"model": model, "messages": msgs, "temperature": 0.3,
                                "stream": False, "chat_template_kwargs": {"enable_thinking": False}})
        if resp2.get("error"):
            yield {"type": "error", "text": resp2["error"]}
            return
        txt2 = (resp2["choices"][0]["message"].get("content") or "").strip()
        if txt2:
            yield {"type": "answer", "text": txt2}
            return
    except Exception as ex:
        progress("⚠ 兜底作答失败：%s" % _abbrev(str(ex), 80))
    yield {"type": "error", "text": "已达工具调用次数上限，未能完稿。请换种问法重试。"}


def _run_items(items, ask_fn, verify_fn, model="deepseek-v4-1-flash",
               progress=lambda t: None, max_rounds=3):
    """查询类任务共用底座：逐目标 执行→验证→未达标带反馈续跑→全部完成后汇总。
    任何「对一批目标逐个查证并返回最终结果」的查询任务（引用分析 citeref /
    知识网 /kg/v1 / 深度调研 research）都用本引擎驱动，只需各自提供 ask_fn 与 verify_fn。

    items       : list[dict]，每项含 {'label': 展示用编号, 及其他自定义字段}
    ask_fn(item, round_no, last_ctx) -> messages
                  构造该目标第 round_no 轮的 LLM 提问；last_ctx 为上一轮未达标时的验证反馈（首轮为空串）。
    verify_fn(item, text) -> (ok: bool, feedback: str)
                  校验本轮产物是否达标；未达标返回 feedback，作为下一轮续跑的补强指令。
    yield 事件 dict：
      {'type':'item_done','item','rounds','ok','text'}   每个目标的最终产物
      {'type':'summary','text','total','ok_count'}       全部完成后的汇总（最终结果）
    内部复用 run_agent（自带 arxiv/crossref/web 工具），即新增查询任务无需重复工具闭环。"""
    finals = []
    for it in items:
        last, ok, feedback = "", False, ""
        for rnd in range(1, max_rounds + 1):
            progress("▶ 目标 [%s] · 第 %d 轮" % (it.get("label", it), rnd))
            ctx = ("这是第 %d 轮续跑。上一轮存在缺口，请先复核/重新检索，再针对以下反馈补齐内容后重新完整作答：\n%s"
                   % (rnd, feedback)) if feedback else ""
            answer = ""
            for ev in run_agent(ask_fn(it, rnd, ctx), model=model,
                                progress=lambda t: progress("   " + _abbrev(str(t), 64))):
                if ev["type"] == "answer":
                    answer = ev["text"]
                elif ev["type"] == "error":
                    progress("   ⚠ " + ev["text"])
            last = (answer or last or "").strip()
            if not last:
                feedback = "上一轮未能产出有效回答，请直接给出确定性结论，不要留空。"
                ok = False
                continue
            try:
                ok, feedback = verify_fn(it, last)
            except Exception as ex:
                ok, feedback = False, "验证过程出错：%s" % _abbrev(str(ex), 120)
            if ok:
                break
            if rnd < max_rounds:
                progress("   ✍ 未完整（第 %d/%d 轮）：%s" % (rnd, max_rounds, feedback))
        finals.append({"item": it.get("label", it), "rounds": rnd,
                       "ok": ok, "text": last})
        yield {"type": "item_done", "item": it.get("label", it), "rounds": rnd,
               "ok": ok, "text": last}
    ok_n = sum(1 for f in finals if f["ok"])
    parts = ["已对 %d 个目标逐项执行、验证并汇总：" % len(finals)]
    for f in finals:
        tag = "✔ 完整" if f["ok"] else "⚠ 第 %d 轮仍未完全验证" % f["rounds"]
        parts.append("■ 引用 [%s]（%s，执行 %d 轮）：\n%s"
                     % (f["item"], tag, f["rounds"], f["text"] or "（未产出有效结果）"))
    yield {"type": "summary", "text": "\n".join(parts), "total": len(finals),
           "ok_count": ok_n}


# ---------- 引用分析 citeref：基于 _run_items 共用底座的第一个实例 ----------
CITREF_NEED = ["研究内容", "解决了什么问题", "还有什么不足"]


def _citeref_ask(it, rnd, ctx):
    """构造某个引用编号第 rnd 轮的提问；ctx 为上一轮未达标时的验证反馈。"""
    n, title, tail = it["label"], it.get("title", ""), it.get("tail", "")
    extra = ("\n\n这是第 %d 轮续跑。上一轮答案存在缺口，请先复核/重新检索文献，"
             "再针对以下反馈补齐缺失部分后，完整重写三部分分析：\n%s" % (rnd, ctx)) if ctx else ""
    q = ("当前论文是《%s》。我在文中框选了引用序号 [%s]，请完整分析这条被引文献。\n\n"
         "下方是该论文截止参考文献部分的原文摘录（Refs 部分，可能含多条，编号不保证连续）：\n%s\n\n"
         "请完成以下任务：\n"
         "1) 在该参考文献列表中找到编号 [%s] 对应的那条文献，完整摘录它的作者、标题、年份/出处。"
         "若这条编号不在其中，请按可能的真实文献尽力判定。\n"
         "2) 根据该文献标题/作者，用 arxiv_search 或 crossref_search（或 web_search）真实检索这篇文献，"
         "阅读它的摘要/内容（优先 arXiv，检索不到如实说明，绝不编造）。\n"
         "3) 用中文给出对这篇被引用文献的分析，包含三部分：\n"
         "   ■ 研究内容：这篇文献做了什么、核心方法/思路是什么；\n"
         "   ■ 解决了什么问题：它解决了什么问题、贡献是什么；\n"
         "   ■ 还有什么不足：现存局限/后续待解决的问题；\n"
         "并说明引用它的论文当前文是在什么语境下引用 [%s]。若检索不到该文献，请如实说明"
         "你基于哪部分信息作答。%s") % (title, n, tail, n, n, extra)
    return [{"role": "user", "content": q}]


def _citeref_verify(it, text):
    """校验条引用是否完整达标；未达标返回 (False, 反馈)，供引擎续跑补强。"""
    missing = [s for s in CITREF_NEED if s not in text]
    if missing:
        return False, "答案缺少三部分中的「%s」，请补齐并完整重写。" % "、".join(missing)
    if len(text.strip()) < 120:
        return False, "答案过短、缺乏具体分析，请补充实质内容。"
    return True, ""


def _do_citeref(h):
    """引用分析 /citeref/v1 · SSE：一次框选 [8,35,50,52]，全部编号逐一查证并汇总。"""
    data = h._read_json()
    if not data or not str(data.get("title") or "").strip():
        h.send_error(400, "need {title}")
        return
    try:
        refs = [int(str(x).strip()) for x in (data.get("refs") or [])
                if str(x).strip().isdigit() and int(str(x).strip()) >= 1]
    except Exception:
        refs = []
    refs = sorted(set(refs))
    if not refs:
        h.send_error(400, "need non-empty {refs} array of citation numbers")
        return
    title = str(data["title"]).strip()
    tail = str(data.get("tail") or "").strip()
    model = data.get("model") or "deepseek-v4-1-flash"
    try:
        max_rounds = min(max(int(data.get("max_rounds") or 3), 1), 5)
    except Exception:
        max_rounds = 3
    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream; charset=utf-8")
    h.send_header("Cache-Control", "no-cache")
    h.send_header("Transfer-Encoding", "chunked")
    h.send_header("Connection", "close")
    h._cors()
    h.end_headers()
    items = [{"label": n, "title": title, "tail": tail} for n in refs]
    try:
        for ev in _run_items(items, _citeref_ask, _citeref_verify, model=model,
                             progress=lambda t: h._sse_event({"type": "progress", "text": t}),
                             max_rounds=max_rounds):
            h._sse_event(ev)
        h.wfile.write(b"0\r\n\r\n")
        h.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        LOG.info("citeref client aborted")


def _do_conv(h):
    """Word→PDF 转换 /conv/v1 · 普通响应：上传 .docx/.doc 原始字节，返回转换后的 PDF 字节。

    用 LibreOffice 无头转换，前端拿到 PDF 后走与 PDF 完全相同的 pdfjs 管线。
    """
    data = h._read_bytes()
    if data is None:
        LOG.info("conv: empty body from %s", h.client_address[0])
        h._json_error(400, "need body bytes (docx/doc)")
        return
    LOG.info("conv: %d bytes from %s", len(data), h.client_address[0])
    if not SOFFICE:
        h._json_error(500, "LibreOffice not installed on server")
        return
    fd, src = tempfile.mkstemp(suffix=".docx", prefix="scinex_conv_")
    t0 = time.time()
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        outdir = tempfile.mkdtemp(prefix="scinex_conv_out_")
        try:
            env = dict(os.environ)
            env["HOME"] = outdir
            proc = subprocess.run(
                [SOFFICE, "--headless", "--norestore", "--convert-to", "pdf",
                 "--outdir", outdir, src],
                capture_output=True, env=env, timeout=180)
            pdf = os.path.join(outdir, os.path.splitext(os.path.basename(src))[0] + ".pdf")
            if not os.path.isfile(pdf):
                LOG.warning("soffice failed rc=%s in %.1fs: %s", proc.returncode,
                            time.time() - t0, proc.stderr[:500])
                h._json_error(500, "word to pdf conversion failed")
                return
            with open(pdf, "rb") as f:
                out = f.read()
            LOG.info("conv: ok %d->%d bytes in %.1fs for %s", len(data), len(out),
                     time.time() - t0, h.client_address[0])
        finally:
            shutil.rmtree(outdir, ignore_errors=True)
    except subprocess.TimeoutExpired:
        LOG.warning("conv: TIMEOUT after %.1fs for %s", time.time() - t0,
                    h.client_address[0])
        try:
            os.unlink(src)
        except OSError:
            pass
        h._json_error(500, "conversion timed out")
        return
    finally:
        try:
            os.unlink(src)
        except OSError:
            pass
    h.send_response(200)
    h.send_header("Content-Type", "application/pdf")
    h.send_header("Content-Length", str(len(out)))
    h.send_header("Cache-Control", "no-store")
    h._cors()
    h.end_headers()
    h.wfile.write(out)


def _do_search(h):
    """在线检索 /search/v1 · POST {q, max} -> JSON {items:[{arxiv,title,year,first_author,abstract}]}。"""
    import json as _json_encode
    data = h._read_bytes()
    try:
        req = _json_encode.loads(data.decode("utf-8")) if data else {}
    except Exception:
        req = {}
    q = (req.get("q") or "").strip()
    if not q:
        h._json_error(400, "need {q}")
        return
    maxr = min(int(req.get("max") or 5), 10)
    items = arxiv_fuzzy_search(q, maxr)
    LOG.info("search: q=%r -> %d hits", q[:60], len(items))
    h._json_ok({"items": items})


def _do_dlpdf(h):
    """arXiv PDF 下载 /dlpdf/v1 · POST {arxiv} -> PDF 字节；失败返回 JSON 错误。"""
    data = h._read_bytes()
    try:
        req = json.loads(data.decode("utf-8")) if data else {}
    except Exception:
        req = {}
    aid = (req.get("arxiv") or "").strip()
    if not aid:
        h._json_error(400, "need {arxiv}")
        return
    base = aid.split("/abs/")[-1]
    if not re.match(r"^[\w.\-]+$", base):
        h._json_error(400, "bad arxiv id")
        return
    url = "http://export.arxiv.org/pdf/%s" % base
    t0 = time.time()
    blob = PDF_FETCH(url, timeout=120)
    if not blob:
        h._json_error(502, "failed to download pdf from arxiv")
        return
    LOG.info("dlpdf: %s -> %d bytes in %.1fs", base, len(blob), time.time() - t0)
    h.send_response(200)
    h.send_header("Content-Type", "application/pdf")
    h.send_header("Content-Length", str(len(blob)))
    h.send_header("Cache-Control", "no-store")
    h._cors()
    h.end_headers()
    h.wfile.write(blob)


def _do_arxiv(args):
    q = (args.get("query") or "").strip()
    out = []
    for c in arxiv_search(q, 3):
        ab = c["abstract"]
        out.append({"arxiv": c["arxiv"], "title": c["title"], "year": c["year"],
                    "first_author": c["first_author"],
                    "abstract": ab + ("…" if len(ab) > 400 else "")})
    return {"results": out} if out else {"error": "arXiv 未找到"}


def _do_crossref(args):
    q = (args.get("query") or "").strip()
    out = []
    for w in crossref_search(q, 4):
        out.append({"DOI": w.get("DOI"),
                    "title": (w.get("title") or [""])[0],
                    "year": ((w.get("issued", {}).get("date-parts") or [[None]])[0][0]),
                    "venue": (w.get("container-title") or [""])[0],
                    "citations": w.get("is-referenced-by-count", 0)})
    return {"results": out} if out else {"error": "Crossref 未找到"}


def _do_fetch(args):
    return fetch_page_text(args.get("url") or "")


def _do_web(args):
    r = web_search_bing(args.get("query") or "", lang=args.get("lang") or "en")
    return {"results": r} if r else {"error": "搜索源被限（Bing 空结果）"}


AGENT_CALLS = {"arxiv_search": _do_arxiv, "crossref_search": _do_crossref,
               "fetch_url": _do_fetch, "web_search": _do_web}


def _snip(ab, n=320):
    return (ab[:n] + ("…" if len(ab) > n else "")) if ab else ""


def build_knowledge_graph(title, refs=None, model="deepseek-v4-1-flash", n_succ=KG_MAX_SUCC):
    """入参: 论文标题 + 正文参考文献标题列表。返回 {paper, nodes, edges, note}。
    refs 同义: []。前驱=refs 经 arXiv 解析; 根=arXiv 命中; 后继=S2 尽力 or 模型建议标题再 arXiv。"""
    # 1) 定位本论文
    meta = best_arxiv(title.strip())
    if meta:
        paper = {"id": "root", "title": meta["title"], "year": meta["year"],
                 "arxiv": meta["arxiv"], "first_author": meta["first_author"],
                 "abstract": _snip(meta["abstract"]), "side": "root",
                 "sources": ["arXiv"]}
    else:
        paper = {"id": "root", "title": title.strip(), "year": None, "arxiv": None,
                 "first_author": "", "abstract": "", "side": "root",
                 "sources": ["标题(未在 arXiv 解析到)"]}

    # 2) 前驱：正文引用标题 -> arXiv 解析
    seen, nodes = set(), []
    for r in (refs or [])[:KG_MAX_REFS]:
        rt = re.sub(r"^[\s0-9\.\[\]\(\)\-—-]+", "", r).strip()
        if not rt or rt.lower() in seen:
            continue
        seen.add(rt.lower())
        m = best_arxiv(rt)
        if m:
            nodes.append({"id": "pre%d" % len(nodes), "title": m["title"], "year": m["year"],
                          "arxiv": m["arxiv"], "first_author": m["first_author"],
                          "abstract": _snip(m["abstract"]), "side": "pre",
                          "sources": ["正文引用", "arXiv"]})
        else:
            nodes.append({"id": "pre%d" % len(nodes), "title": rt, "year": None,
                          "arxiv": None, "first_author": "", "abstract": "",
                          "side": "pre", "sources": ["正文引用(未解析)"]})

    # 3) 后继
    succes = []
    if paper.get("arxiv"):
        cits = s2_citations(paper["arxiv"], 8) or []
        succes = [c for c in cits if c.get("title") and c.get("year")][:n_succ]
    succ_nodes = []
    for c in succes:
        m = best_arxiv(c["title"])
        if m:
            succ_nodes.append({"id": "succ%d" % len(succ_nodes), "title": m["title"],
                               "year": m["year"], "arxiv": m["arxiv"],
                               "first_author": m["first_author"],
                               "abstract": _snip(m["abstract"]), "side": "succ",
                               "sources": ["被引(S2)", "arXiv"]})
        else:
            succ_nodes.append({"id": "succ%d" % len(succ_nodes), "title": c["title"],
                               "year": c.get("year"), "venue": c.get("venue"),
                               "arxiv": None, "first_author": "", "abstract": "",
                               "side": "succ", "sources": ["被引(S2)"]})
    if not succ_nodes and paper.get("abstract"):
        # 模型建议著名后续作品 -> 再经 arXiv 解析(诚实标记)
        suggested = llm_completion({"model": model, "temperature": 0.3,
            "messages": [{"role": "system", "content":
                "你是文献脉络专家。只输出 3-5 个最著名的、直接承接该论文提出的方法或思想的后续论文标题，JSON 数组字符串，如 [\"Title A\",\"Title B\"]，不要其他文字。"},
                {"role": "user", "content": "%s (%s)\n摘要:%s" % (
                    paper["title"], paper.get("year"), paper.get("abstract")[:500])}],
            "chat_template_kwargs": {"enable_thinking": False}})
        titles = []
        try:
            content = (suggested.get("choices", [{}])[0]
                       .get("message", {}).get("content") or "")
            txt = re.sub(r"^.*?\[", "[", content, flags=re.S).strip()
            titles = json.loads(txt)
        except Exception:
            pass
        for t in titles[:n_succ]:
            m = best_arxiv(t)
            if m:
                succ_nodes.append({"id": "succ%d" % len(succ_nodes), "title": m["title"],
                                   "year": m["year"], "arxiv": m["arxiv"],
                                   "first_author": m["first_author"],
                                   "abstract": _snip(m["abstract"]), "side": "succ",
                                   "sources": ["模型补充", "arXiv"]})
    nodes += succ_nodes

    # 4) LLM 富文本合成：每节点 核心内容/存在问题 + 边关系
    brief = []
    for nd in [paper] + nodes:
        brief.append({"id": nd["id"], "title": nd["title"], "year": nd["year"],
                      "arxiv": nd.get("arxiv"), "abstract": (nd.get("abstract") or "")[:800],
                      "side": nd["side"], "raw": (nd.get("sources") or [])})
    prompt = ("论文: %s\n\n上下文节点(前驱pre=被本文引用的旧工作/本文root/后继succ=承接本文的新工作):\n%s\n\n"
              "深度分析每个节点(基于真实摘要+领域知识，不编造年份/作者)：对每个 id 输出 "
              "core(核心内容一句)、problems(存在问题/局限一句)、solved(解决了什么问题一句)、"
              "open(还遗留什么问题一句)、tech(核心技术/方法多名短语6-16字)。并对每条 pre→root 与 "
              "root→succ 的边给出 label(技术关系标签，如 奠基/改进/启发/挑战/扩展应用，中文2-4字)、"
              "type(取 build=奠基/extend=扩展/inspire=启发/challenge=挑战 之一)、"
              "relation(一句技术承接摘要30-60字，说明后续在哪些点上继承/改进了前驱)。"
              "只输出 JSON:{ids:{id:{core,problems,solved,open,tech}},"
              "edges:[{from,to,label,type,relation}]},无其他文字。"
              % (paper["title"], json.dumps(brief, ensure_ascii=False)))
    synth = llm_completion({"model": model, "temperature": 0.2,
        "messages": [{"role": "system", "content": "你是学术文献脉络分析专家，输出严格 JSON。"},
                     {"role": "user", "content": prompt}],
        "chat_template_kwargs": {"enable_thinking": False}})
    core_map, edge_list = {}, []
    try:
        content = (synth.get("choices", [{}])[0].get("message", {}).get("content") or "")
        raw = re.sub(r"```(?:json)?", "", content).strip()
        raw = re.sub(r"^[^{]*\{", "{", raw, flags=re.S)
        raw = re.sub(r"\}[^}]*$", "}", raw)
        d = json.loads(raw)
        core_map = d.get("ids") or {}
        for e in d.get("edges") or []:
            if e.get("from") and e.get("to"):
                edge_list.append({"from": e["from"], "to": e["to"],
                                  "label": e.get("label", "关联")[:8],
                                  "type": e.get("type", "build"),
                                  "relation": (e.get("relation") or "").strip()[:200]})
    except Exception:
        pass

    all_nodes = [paper] + nodes
    for nd in all_nodes:
        got = core_map.get(nd["id"]) or {}
        nd["core"] = got.get("core") or ""
        nd["problems"] = got.get("problems") or ""
        nd["solved"] = got.get("solved") or ""
        nd["open"] = got.get("open") or ""
        nd["tech"] = got.get("tech") or ""

    # 5) 缺边补全（pre/root/succ 各自挂到主线），按年份排序
    edges = [e for e in edge_list]
    have = {(e["from"], e["to"]) for e in edges}
    for nd in nodes:
        if nd["side"] == "pre" and (nd["id"], "root") not in have:
            edges.append({"from": nd["id"], "to": "root", "label": "引用/奠基",
                          "type": "build",
                          "relation": "被本文引用，其方法/结论是本文的起点"})
        elif nd["side"] == "succ" and ("root", nd["id"]) not in have:
            edges.append({"from": "root", "to": nd["id"], "label": "后续发展",
                          "type": "extend",
                          "relation": "在本文基础上继续扩展/改进"})
    for e in edges:
        e["label"] = e.get("label") or "关联"
        e.setdefault("type", "build")

    ordered = sorted(all_nodes, key=lambda n: (n.get("year") or 9999, n["id"]))
    note = ("前驱来自论文正文引用并经 arXiv 核实摘要；本文来自 arXiv；"
            + ("后继来自 Semantic Scholar 被引" if succes else "后继由模型根据影响力建议(经 arXiv 核实)")
            + "。核心内容/存在问题为模型基于真实元数据归纳，请以原文为准。")
    return {"paper": paper, "nodes": ordered, "edges": edges, "note": note}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PaperProxy/1.0"

    def log_message(self, fmt, *args):  # 静默默认请求日志，避免刷屏
        pass

    # ---------- CORS/OPTIONS ----------
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "authorization, content-type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Max-Age", "86400")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---------- 静态 HTML ----------
    def _serve_html(self):
        try:
            data = open(HTML_PATH, "rb").read()
        except OSError as e:
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(("missing %s: %s" % (HTML_PATH, e)).encode())
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    # ---------- 代理 ----------
    def _proxy(self, method):
        raw = self.path
        query = ""
        if "?" in raw:
            raw, query = raw.split("?", 1)
        raw = raw.rstrip("/")
        if raw in ("", "/", "/index.html"):
            return self._serve_html()
        target = build_target(raw, query)
        if not target:
            self.send_error(404, "path not routed")
            return
        # 读 body
        body = None
        if method in ("POST", "PUT", "PATCH"):
            try:
                clen = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                clen = 0
            if clen > MAX_BODY:
                self.send_error(413, "body too large")
                return
            body = self.rfile.read(clen) if clen else b""
        hdrs = {
            "Content-Type": self.headers.get("Content-Type", "application/json"),
            "User-Agent": "paper-proxy/1.0",
        }
        if target.startswith(LLM_BASE):
            if not KEY:
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "server-side key missing"}).encode())
                return
            hdrs["Authorization"] = "Bearer " + KEY
        # 转发
        try:
            resp = _open_upstream(target, method, body, hdrs)
        except Exception as e:
            LOG.warning("%s %s upstream error: %s", method, raw, e)
            self.send_response(502)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "upstream unreachable: %s" % e}).encode("utf-8"))
            return
        # 回写响应头
        self.send_response_only(resp.status)
        have_cl = False
        for k, v in resp.headers.items():
            kl = k.lower()
            if kl in ("transfer-encoding", "connection", "keep-alive", "content-encoding",
                      "content-length", "proxy-authenticate", "proxy-authorization",
                      "te", "trailer", "upgrade", "content-security-policy"):
                if kl == "content-length" and resp.headers.get("Content-Length", "").isdigit():
                    have_cl = True
                continue
            self.send_header(k, v)
        self._cors()
        chunked = not have_cl
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", resp.headers.get("Content-Length"))
        self.send_header("Connection", "close")
        self.end_headers()
        # 透传 body（SSE 每块即写即刷）
        try:
            while True:
                try:
                    chunk = resp.read(65536)
                except Exception:
                    break
                if not chunk:
                    break
                if chunked:
                    self.wfile.write(b"%x\r\n" % len(chunk))
                    self.wfile.write(chunk)
                    self.wfile.write(b"\r\n")
                else:
                    self.wfile.write(chunk)
                self.wfile.flush()
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            LOG.info("client aborted %s", raw)
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def do_GET(self):
        if self.path.startswith(("/llm/", "/vis/")):
            self._proxy("GET")
        elif self.path in ("/pdf.worker.min.js", "/pdf.min.js"):
            self._serve_static(self.path.lstrip("/"), "application/javascript")
        else:
            self._serve_html()

    # ---------- 本地静态资源 ----------
    def _serve_static(self, name, ctype):
        p = os.path.join(HERE, name)
        try:
            data = open(p, "rb").read()
        except OSError as e:
            self.send_error(500, "missing %s: %s" % (p, e))
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    # ---------- 智能体 /agent/v1 ----------
    def _read_json(self):
        try:
            clen = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            clen = 0
        if not clen or clen > MAX_BODY:
            return None
        body = self.rfile.read(clen)
        try:
            return json.loads(body.decode("utf-8"))
        except Exception:
            return None

    def _read_bytes(self):
        """读取并返回原始请求体字节（二进制安全）；超限或为空返回 None。"""
        try:
            clen = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            clen = 0
        if not clen or clen > MAX_BODY:
            return None
        return self.rfile.read(clen)

    def _json_error(self, code, msg):
        body = json.dumps({"error": msg}, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _json_ok(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _send_chunk(self, b):
        self.wfile.write(b"%x\r\n" % len(b))
        self.wfile.write(b)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _sse_event(self, obj):
        self._send_chunk(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8"))

    def _do_agent(self):
        data = self._read_json()
        if not data or not data.get("messages"):
            self.send_error(400, "need {model, messages}")
            return
        model = data.get("model") or "deepseek-v4-1-flash"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self._cors()
        self.end_headers()
        try:
            for ev in run_agent(data["messages"], model=model,
                                progress=lambda t: self._sse_event({"type": "progress", "text": t})):
                if ev["type"] == "answer":
                    text = ev["text"]
                    for i in range(0, len(text), 24):
                        self._sse_event({"id": "chatcmpl-agent", "object": "chat.completion.chunk",
                                         "choices": [{"index": 0, "delta": {"content": text[i:i + 24]},
                                                      "finish_reason": None}]})
                    self._sse_event({"id": "chatcmpl-agent", "object": "chat.completion.chunk",
                                     "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
                elif ev["type"] == "error":
                    self._sse_event({"id": "chatcmpl-agent", "object": "chat.completion.chunk",
                                     "choices": [{"index": 0, "delta": {"content": "[智能体错误] " + ev["text"]},
                                                  "finish_reason": None}]})
                    self._sse_event({"id": "chatcmpl-agent", "object": "chat.completion.chunk",
                                     "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            LOG.info("agent client aborted")

    # ---------- 知识网 /kg/v1 ----------
    def _do_kg(self):
        data = self._read_json()
        if not data or not (str(data.get("title") or "")).strip():
            self.send_error(400, "need {title}")
            return
        refs = data.get("refs")
        refs = [r for r in (refs or []) if isinstance(r, str)]
        model = data.get("model") or "deepseek-v4-1-flash"
        try:
            n_succ = min(max(int(data.get("n_succ") or KG_MAX_SUCC), 1), 8)
        except Exception:
            n_succ = KG_MAX_SUCC
        try:
            g = build_knowledge_graph(str(data["title"]).strip(), refs=refs, model=model, n_succ=n_succ)
            payload = json.dumps(g, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self._cors()
            self.end_headers()
            self.wfile.write(payload)
        except Exception as e:
            LOG.exception("kg build failed")
            self.send_response(502)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "kg build failed: %s" % e}, ensure_ascii=False).encode("utf-8"))

    def do_POST(self):
        p = self.path.split("?", 1)[0].rstrip("/")
        if p.startswith("/agent/v1"):
            self._do_agent()
        elif p.startswith("/citeref/v1"):
            _do_citeref(self)
        elif p.startswith("/conv/v1"):
            _do_conv(self)
        elif p.startswith("/search/v1"):
            _do_search(self)
        elif p.startswith("/dlpdf/v1"):
            _do_dlpdf(self)
        elif p.startswith("/kg/v1"):
            self._do_kg()
        else:
            self._proxy("POST")

def main():
    ports = [7860, 8899]
    httpd = ThreadingHTTPServer(("0.0.0.0", ports[0]), Handler)
    extra = None
    if len(ports) > 1:
        try:
            extra = ThreadingHTTPServer(("0.0.0.0", ports[1]), Handler)
        except OSError as e:
            LOG.warning("port %s not bindable: %s", ports[1], e)
    print("paper-proxy ready: key=%s body-limit=%dMB" % ("loaded" if KEY else "MISSING", MAX_BODY // (1024 * 1024)), flush=True)
    print("  http://0.0.0.0:%d/  (llm=%s vis=%s)" % (ports[0], LLM_BASE, VIS_BASE), flush=True)
    import threading
    def run(srv):
        srv.serve_forever()
    t = threading.Thread(target=run, args=(httpd,), daemon=True)
    t.start()
    if extra:
        threading.Thread(target=run, args=(extra,), daemon=True).start()
        print("  http://0.0.0.0:%d/ (backup)" % ports[1], flush=True)
    t.join()

if __name__ == "__main__":
    main()
