# SciNex · Research Reading Engine

A browser-native research paper reader: a single-file HTML frontend with a built-in
AI research agent, featuring knowledge-graph extraction, inline translation,
citation analysis, and deep web research. A lightweight same-origin proxy (no CORS,
SSE streaming) powers it locally.

## Features

- **Single-file frontend** `paper-reader.html`: zero build step, everything in one
  HTML (pdf.js loaded from CDN).
- **AI Q&A / research agent**: a built-in agent that queries domestic & international
  web search, fetches source pages, and produces a grounded comparative analysis.
- **Knowledge graph**: automatically extracts *predecessor → this paper → successor*
  relations from the paper text and renders a citation / reference network (`/kg/v1`).
- **Citation analysis**: select a citation number to view, locate, and analyze the
  corresponding reference.
- **Inline translation (with pronunciation)**: select any text to translate instantly;
  English entries include IPA and text-to-speech.
- **Deep web research**: cross-language (EN + ZH) search with structured output.
- **PDF reading**: built-in pdf.js rendering with scroll positioning and English
  reading aids.

## Brand

**SciNex** (Sci + Nexus — a hub of research knowledge). Primary color: teal `#2c6e74`.

## Installation

```bash
# 1. Runtime dependencies (Python standard library only — no third-party packages)
python3 -m venv .venv && source .venv/bin/activate

# 2. Configure endpoints & key (server still boots without .env; endpoints must
#    be injected via environment variables)
cp .env.example .env
#    edit .env: LLM_BASE / VIS_BASE / LLM_API_KEY

# 3. Start the proxy (default port 7860)
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
| `/kg/v1/build` | Knowledge-graph extraction (POST) |

## License

MIT — see `LICENSE`.

---

**Note to self-hosters**: this repository has been sanitized from a private deployment;
all endpoints and keys were migrated to `.env` injection and no private information is
included. Use lowercase variable names when configuring your own deployment, and never
commit `.env`.
