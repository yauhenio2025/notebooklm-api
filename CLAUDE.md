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
- Authentication via master token (notebooklm-py 0.8.0a3, pinned git main): a durable Google credential minted once via `notebooklm login --master-token` (one interactive browser sign-in), stored as `master_token.json` in the auth profile dir. Expired sessions re-mint fresh cookies in-process, headlessly — no droplet, no manual refresh.
- On Render: the token is a Secret File (`/etc/secrets/master_token.json`); the app seeds it into the writable profile dir (`NOTEBOOKLM_HOME=/opt/render/project/.notebooklm`) and mints `storage_state.json` at startup (ephemeral disk is fine — every boot re-mints)
- Singleton NotebookLM client initialized lazily from the profile's storage_state.json (`src/notebooklm_client.py`)
- SECURITY: the master token is a full-account durable Google credential — never print/log/commit it; rotation = re-mint locally + replace the Render secret file
- Single-consumer per Google account: concurrent re-mints from several processes invalidate each other's sessions — keep ONE service instance
- PostgreSQL stores notebooks, sources, queries, and citations for history/export
- Zotero integration fetches PDFs and uploads them to NotebookLM notebooks
- Natural language orchestrator: Claude Haiku parses instructions → resolves Zotero collections → creates notebooks → uploads PDFs
- Zotero library: 362 collections, max depth 5, paginated fetching across 4 pages

## Documentation
- Feature inventory: `docs/FEATURES.md`
- Change history: `docs/CHANGELOG.md`

## Environment Variables
- `DATABASE_URL` - PostgreSQL connection string (Render provides this)
- `MASTER_TOKEN_FILE` - Path to master_token.json secret file (Render: /etc/secrets/master_token.json); seeded into the profile dir at startup
- `NOTEBOOKLM_HOME` - notebooklm-py home dir override (Render: /opt/render/project/.notebooklm — must be writable); unset locally (defaults to ~/.notebooklm)
- `NOTEBOOKLM_PROFILE` - auth profile name (optional, library default: default)
- `ZOTERO_API_KEY` - Zotero API key for group library access
- `ZOTERO_GROUP_ID` - Zotero group library ID (default: 5579237)
- `ANTHROPIC_API_KEY` - Anthropic API key for Claude-powered intent parsing in /api/build-notebook

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
