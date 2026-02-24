# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added
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
