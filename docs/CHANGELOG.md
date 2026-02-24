# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added
- Background auth keepalive: proactively refreshes NotebookLM cookies every 20 min to prevent session staleness ([src/main.py](src/main.py))
- Auto-retry on queries: detects stale auth/RPC/timeout errors, refreshes cookies, retries once — no manual intervention needed ([src/services/query_service.py](src/services/query_service.py))
- Export: handles citation ranges `[4-6]`, lists `[1,2]`, markdown `**bold**`/`*italic*`, and filters footnotes to only those referenced in answer text ([src/routes/export.py](src/routes/export.py))
- Citation enrichment: expand ~70 char cited_text to ~300 char passages using `SourceFulltext.find_citation_context()` ([src/services/query_service.py](src/services/query_service.py))
- Canonical source ID sync: after upload, resolves NotebookLM's real source ID via `sources.list()` to fix citation→source matching ([src/services/source_service.py](src/services/source_service.py))
- `POST /api/notebooks/{id}/sources/sync-ids` endpoint: retro-fix source IDs for existing notebooks ([src/routes/sources.py](src/routes/sources.py))
- Bibliographic data on sources: `authors`, `publication_date`, `item_type` columns from Zotero metadata ([src/models.py](src/models.py), [src/services/source_service.py](src/services/source_service.py))
- Bibliographic data on citations: `source_authors`, `source_date` columns copied from source during query ([src/models.py](src/models.py), [src/services/query_service.py](src/services/query_service.py))
- Formatted citations in export: `"Deutschmann (2001), Capitalism as Religion"` style ([src/routes/export.py](src/routes/export.py))
- Export footnotes now include `authors`, `date`, `formatted_citation` fields ([src/schemas.py](src/schemas.py))
- Idempotent column migrations in `init_db()` for new columns ([src/database.py](src/database.py))

### Fixed
- Source IDs now match canonical NotebookLM IDs so citation→source title resolution works (was always null)
- Temp file naming: PDFs uploaded with real filename (e.g. `deutschmann2001.pdf`) instead of `tmp*.pdf` so NotebookLM source titles are human-readable
- Citation fulltext fallback: `source_title` resolved from `SourceFulltext.title` when DB lookup misses

### Previously Added
- `POST /api/auth/refresh` endpoint — SSHes to DigitalOcean droplet, extracts fresh Google cookies via Playwright CDP, resets in-memory NotebookLM client ([src/routes/health.py](src/routes/health.py), [src/services/auth_service.py](src/services/auth_service.py))
- Auth service with 3 functions: `refresh_auth_from_droplet()`, `update_notebooklm_auth()`, `full_auth_refresh()` ([src/services/auth_service.py](src/services/auth_service.py))
- Auto-retry in orchestrator: when notebook creation fails with "not available" (expired auth), automatically refreshes cookies from droplet and retries once ([src/services/orchestrator_service.py](src/services/orchestrator_service.py))
- `AuthRefreshResponse` and `AuthRefreshExtraction` Pydantic schemas ([src/schemas.py](src/schemas.py))
- `asyncssh` dependency for SSH to droplet ([requirements.txt](requirements.txt))
- Config: `DROPLET_HOST` and `DROPLET_SSH_KEY` environment variables ([src/config.py](src/config.py))

### Previously Added
- Project scaffolding: FastAPI app with async SQLAlchemy + PostgreSQL ([src/main.py](src/main.py), [src/database.py](src/database.py))
- Configuration from environment variables with pydantic-settings ([src/config.py](src/config.py))
- Database models: notebooks, sources, queries, citations ([src/models.py](src/models.py))
- Pydantic request/response schemas ([src/schemas.py](src/schemas.py))
- Health check and status endpoints ([src/routes/health.py](src/routes/health.py))
- Notebook CRUD + sync endpoints ([src/routes/notebooks.py](src/routes/notebooks.py))
- Query endpoint with citation extraction ([src/routes/queries.py](src/routes/queries.py))
- Source management + Zotero upload pipeline ([src/routes/sources.py](src/routes/sources.py))
- Zotero browsing endpoints ([src/routes/zotero.py](src/routes/zotero.py))
- Batch query endpoint with background processing ([src/routes/batch.py](src/routes/batch.py))
- Bot.py-compatible JSON export for viewer.html ([src/routes/export.py](src/routes/export.py))
- NotebookLM client singleton wrapper ([src/notebooklm_client.py](src/notebooklm_client.py))
- Zotero API client for collections and PDF download ([src/services/zotero_service.py](src/services/zotero_service.py))
- Render deployment configuration ([render.yaml](render.yaml))

### Fixed
- All service methods now properly async (await all notebooklm-py calls)
- Corrected API path: `client.chat.ask()` not `client.notebooks.ask()`
- Batch processing updates existing Query records instead of creating duplicates
- NotebookLM client lifecycle uses async context manager properly

### Deployed
- Live at https://notebooklm-api-40ns.onrender.com (Starter plan, Singapore)
- Database: Render PostgreSQL with 4 tables (notebooks, sources, queries, citations)
- Verified: /health, /status, /api/zotero/collections all working

## [2026-02-24] Natural Language Notebook Builder

### Added
- `POST /api/build-notebook` — LLM-powered natural language notebook creation ([src/routes/orchestrator.py](src/routes/orchestrator.py))
- `GET /api/zotero/tree` — full 362-collection hierarchy with paths and depth ([src/routes/orchestrator.py](src/routes/orchestrator.py))
- Orchestrator service using Claude Haiku for intent parsing ([src/services/orchestrator_service.py](src/services/orchestrator_service.py))
- Paginated Zotero collection fetching — handles 362+ collections across 4 pages ([src/services/zotero_service.py](src/services/zotero_service.py))
- `list_collection_items_with_pdfs()` for accurate PDF detection ([src/services/zotero_service.py](src/services/zotero_service.py))
- `anthropic` dependency for Claude API calls ([requirements.txt](requirements.txt))
- `ANTHROPIC_API_KEY` env var support ([src/config.py](src/config.py))

### Fixed
- `sources/from-zotero` collection_key path now passes all items to upload pipeline instead of filtering on always-false `has_pdf` flag ([src/routes/sources.py](src/routes/sources.py))
