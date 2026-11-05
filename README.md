# SciNex · Research Reading Engine

A browser-native research paper reader: a single-file HTML frontend with a built-in
AI research agent, featuring knowledge-graph extraction, inline translation,
citation analysis, and deep web research. A lightweight same-origin proxy (no CORS,
SSE streaming) powers it locally.

## Features

- **Single-file frontend** `paper-reader.html`: zero build step, everything in one
  HTML. **PDF.js is fully self-hosted** (`pdf.min.js` + `pdf.worker.min.js` shipped
  in-repo) — zero CDN dependency, no external network needed for rendering.
- **PDF & Word reading**: renders PDF via bundled pdf.js; **Word documents
  (`.doc`/`.docx`) are converted to PDF server-side with LibreOffice** (`/conv/v1`)
  and reused through the exact same render pipeline — annotations, text selection,
  and citation analysis work identically on Word files, no external office software
  needed on the client.
- **arXiv online search & download**: type a paper title in the search box; if it's
  not already in the local library, SciNex automatically searches arXiv (`/search/v1`,
  fuzzy `all:` search with exact-title fallback) and lets you **download & import the
  PDF in one click** (`/dlpdf/v1`) — a real "search → download → read" loop for papers
  you haven't stored locally.
- **AI Q&A / research agent**: a built-in agent that queries domestic & international
  web search, fetches source pages, and produces a grounded comparative analysis.
- **Knowledge graph**: automatically extracts *predecessor → this paper → successor*
  relations from the paper text and renders a citation / reference network (`/kg/v1`).
- **Citation analysis (loop engine)**: select any set of citation numbers (e.g.
  `[8, 35, 50, 52]`); a **verification loop engine** resolves each one, verifies its
  output against three required facets (research content / problem solved /
  remaining gaps), and **retries until complete** before streaming the final result
  (`/citeref/v1`, SSE).
- **Inline translation (with pronunciation)**: select any text to translate instantly;
  English entries include IPA and text-to-speech.
- **Deep web research**: cross-language (EN + ZH) search with structured output.
- **PDF reading**: scroll positioning, English reading aids, and 20-second timeout
  guard so rendering never hangs silently.

## Brand

**SciNex** (Sci + Nexus — a hub of research knowledge). Primary color: teal `#2c6e74`.

## Installation

```bash
# 1. Runtime dependencies (Python standard library only — no third-party packages)
python3 -m venv .venv && source .venv/bin/activate

# 2. (Optional) Word import (.doc/.docx) requires a LibreOffice binary on the server:
#    sudo apt install libreoffice   (soffice / libreoffice / lowriter on PATH)

# 3. Configure endpoints & key (server still boots without .env; endpoints must
#    be injected via environment variables)
cp .env.example .env
#    edit .env: LLM_BASE / VIS_BASE / LLM_API_KEY

# 4. Start the proxy (default port 7860)
python3 serve.py
#    open http://127.0.0.1:7860/
```

The port can be overridden with the `PORT` environment variable
(e.g. `PORT=8899 python3 serve.py`).

## Configuration

All secrets are injected through environment variables or a same-directory `.env`
file. The repository contains no real credentials or private addresses.

| Variable | Required | Description |
|----------|----------|-------------|
| `LLM_BASE` | yes | LLM gateway base URL; must include `/v1` (OpenAI-compatible, e.g. deepseek / vLLM / self-hosted gateway) |
| `VIS_BASE` | no | Vision vLLM base URL; falls back to `LLM_BASE` when unset |
| `LLM_API_KEY` | yes | LLM gateway API key (injected server-side; the browser never sees it) |
| `PORT` | no | HTTP listen port (default 7860) |

### Config source & precedence

1. Environment variables (highest)
2. Same-directory `.env` file
3. `~/.config/scinex.env`

`.env` accepts `KEY=VALUE` or `export KEY=VALUE`; blank lines and `#` comments
are ignored.

## Routes

| Route | Description |
|-------|-------------|
| `/` | Statically serves `paper-reader.html` |
| `/llm/v1/*` | LLM gateway reverse proxy (OpenAI-compatible, SSE streaming passthrough), key injected server-side |
| `/vis/v1/*` | Vision vLLM reverse proxy |
| `/agent/v1/*` | Built-in AI research agent (agent loop with web_search / arxiv / crossref tools), SSE streaming |
| `/kg/v1/build` | Knowledge-graph extraction (POST) |
| `/conv/v1` | Word → PDF conversion via LibreOffice (POST, returns PDF bytes) |
| `/search/v1` | arXiv online search (POST `{q, max}`, fuzzy + exact-title fallback, returns JSON `items`) |
| `/dlpdf/v1` | arXiv PDF download (POST `{arxiv}`, returns `application/pdf` bytes — picked up by the search box for one-click import) |
| `/citeref/v1` | Citation-analysis loop engine (POST, SSE — resolves, verifies & retries each reference until complete) |

## License

MIT — see `LICENSE`.

---

**Note to self-hosters**: this repository has been sanitized from a private deployment;
all endpoints and keys were migrated to `.env` injection and no private information is
included. Use lowercase variable names when configuring your own deployment, and never
commit `.env`.
