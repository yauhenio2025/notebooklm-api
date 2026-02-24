# NotebookLM API

> FastAPI service wrapping Google NotebookLM via notebooklm-py for notebook management, querying with citations, and Zotero source integration.

## Overview

This service provides an HTTP API on top of Google's NotebookLM, enabling programmatic notebook creation, source upload (including from Zotero), question-asking with structured citation extraction, and batch querying. It replaces the Playwright-based bot.py automation with the lighter notebooklm-py library that uses Google's undocumented batchexecute RPC protocol.

## Tech Stack
- Python 3.11+
- FastAPI + Uvicorn
- SQLAlchemy 2.0 (async) + asyncpg + PostgreSQL (Render)
- notebooklm-py (Google batchexecute protocol)
- anthropic (Claude API for natural language orchestration)
- httpx (Zotero API client)
- Pydantic v2

## Quick Reference
- Run locally: `./start`
- Run production: `uvicorn src.main:app --host 0.0.0.0 --port $PORT`
- Test locally: `curl http://localhost:8000/health`
- Production URL: `https://notebooklm-api-40ns.onrender.com`
- Render service ID: `srv-d6emdnogjchc7384omh0`
- API docs: `https://notebooklm-api-40ns.onrender.com/docs`

## Architecture Notes
- notebooklm-py handles all Google NotebookLM communication (no browser needed at runtime)
- Authentication via exported cookie JSON from `notebooklm login` on the DigitalOcean droplet
- Singleton NotebookLM client initialized lazily from NOTEBOOKLM_AUTH_JSON env var
- PostgreSQL stores notebooks, sources, queries, and citations for history/export
- Zotero integration fetches PDFs and uploads them to NotebookLM notebooks
- Natural language orchestrator: Claude Haiku parses instructions → resolves Zotero collections → creates notebooks → uploads PDFs
- Zotero library: 362 collections, max depth 5, paginated fetching across 4 pages

## Documentation
- Feature inventory: `docs/FEATURES.md`
- Change history: `docs/CHANGELOG.md`

## Environment Variables
- `DATABASE_URL` - PostgreSQL connection string (Render provides this)
- `NOTEBOOKLM_AUTH_JSON` - JSON string of Google auth cookies from notebooklm-py login
- `ZOTERO_API_KEY` - Zotero API key for group library access
- `ZOTERO_GROUP_ID` - Zotero group library ID (default: 5579237)
- `ANTHROPIC_API_KEY` - Anthropic API key for Claude-powered intent parsing in /api/build-notebook
- `DROPLET_HOST` - DigitalOcean droplet IP for auth refresh (default: 207.154.192.181)
- `DROPLET_SSH_KEY` - SSH private key (PEM format) for root@droplet, used by POST /api/auth/refresh

## Render Deployment
- Service: `notebooklm-api` (Starter plan, Singapore)
- Database: `notebook-lm-db` (Render PostgreSQL, Starter, Singapore)
- DB internal URL: `postgresql://notebook_lm_db_user:...@dpg-d6ekteruibrs73df6au0-a/notebook_lm_db`
- Auto-deploy: enabled on `master` branch push

## notebooklm-py API Notes
- All methods are async coroutines (must await)
- Client: `await NotebookLMClient.from_storage(path)` — async context manager
- Queries: `await client.chat.ask(notebook_id, question)` — returns `AskResult`
- AskResult: `.answer` (str), `.references` (list[ChatReference]), `.conversation_id`, `.turn_number`
- ChatReference: `.source_id`, `.citation_number`, `.cited_text`, `.start_char`, `.end_char`
- Sources: `await client.sources.add_file(notebook_id, path, wait=True)`
- Source: `.id`, `.title`, `.kind` (SourceType enum), `.is_ready`

## Code Conventions
- Async everywhere (async def endpoints, async SQLAlchemy sessions)
- Structured JSON logging
- Pydantic models for all request/response schemas
- Services layer between routes and database/external clients
