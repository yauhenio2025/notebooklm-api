# NotebookLM API

> FastAPI service wrapping Google NotebookLM via notebooklm-py for notebook management, querying with citations, and Zotero source integration.

## Overview

This service provides an HTTP API on top of Google's NotebookLM, enabling programmatic notebook creation, source upload (including from Zotero), question-asking with structured citation extraction, and batch querying. It replaces the Playwright-based bot.py automation with the lighter notebooklm-py library that uses Google's undocumented batchexecute RPC protocol.

## Tech Stack
- Python 3.11+
- FastAPI + Uvicorn
- SQLAlchemy 2.0 (async) + asyncpg + PostgreSQL (Render)
- notebooklm-py (Google batchexecute protocol)
- httpx (Zotero API client)
- Pydantic v2

## Quick Reference
- Run locally: `./start`
- Run production: `uvicorn src.main:app --host 0.0.0.0 --port $PORT`
- Test: `curl http://localhost:8000/health`

## Architecture Notes
- notebooklm-py handles all Google NotebookLM communication (no browser needed at runtime)
- Authentication via exported cookie JSON from `notebooklm login` on the DigitalOcean droplet
- Singleton NotebookLM client initialized lazily from NOTEBOOKLM_AUTH_JSON env var
- PostgreSQL stores notebooks, sources, queries, and citations for history/export
- Zotero integration fetches PDFs and uploads them to NotebookLM notebooks

## Documentation
- Feature inventory: `docs/FEATURES.md`
- Change history: `docs/CHANGELOG.md`

## Environment Variables
- `DATABASE_URL` - PostgreSQL connection string (Render provides this)
- `NOTEBOOKLM_AUTH_JSON` - JSON string of Google auth cookies from notebooklm-py login
- `ZOTERO_API_KEY` - Zotero API key for group library access
- `ZOTERO_GROUP_ID` - Zotero group library ID (default: 5579237)

## Code Conventions
- Async everywhere (async def endpoints, async SQLAlchemy sessions)
- Structured JSON logging
- Pydantic models for all request/response schemas
- Services layer between routes and database/external clients
