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
import os, sys, socket, json, logging, time, re, html, difflib, urllib.request, urllib.parse, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
        else:
            self._serve_html()

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
