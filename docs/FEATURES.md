# Feature Inventory

> Auto-maintained by Claude Code. Last updated: 2026-07-11 (master-token auth)

## Health & Monitoring

### Health Check
- **Status**: Active
- **Description**: Basic health endpoint for Render monitoring - checks DB connectivity and master-token profile state
- **Entry Points**:
  - `src/routes/health.py:37` - GET /health endpoint
  - `src/routes/health.py:17-34` - _auth_state() profile-state probe
- **Added**: 2026-02-24 | **Modified**: 2026-07-11

### Status Check
- **Status**: Active
- **Description**: Detailed status including DB tables, live NotebookLM client probe, Zotero config
- **Entry Points**:
  - `src/routes/health.py:60` - GET /status endpoint
- **Added**: 2026-02-24 | **Modified**: 2026-07-11

### Auth Refresh (master token)
- **Status**: Active
- **Description**: POST /api/auth/refresh — re-mints fresh Google cookies from the durable master token (headless, no browser/droplet) and resets the NotebookLM client singleton. The library also self-heals expired sessions in-process (layer-4 recovery); this is the manual path.
- **Entry Points**:
  - `src/routes/health.py:110-131` - POST /api/auth/refresh endpoint
  - `src/services/auth_service.py:18-71` - full_auth_refresh() re-mint orchestration
  - `src/notebooklm_client.py:40-58` - seed_profile_from_secret() (Render secret file → profile dir)
  - `src/notebooklm_client.py:61-70` - mint_storage_state() cookie re-mint
- **Dependencies**: notebooklm-py[headless] (gpsoauth), MASTER_TOKEN_FILE secret
- **Added**: 2026-02-24 | **Modified**: 2026-07-11

## Notebooks

### Notebook CRUD
- **Status**: Active
- **Description**: Create, list, get, delete NotebookLM notebooks with local DB persistence
- **Entry Points**:
  - `src/routes/notebooks.py:20-86` - API endpoints
  - `src/services/notebook_service.py:16-108` - Service layer
- **Dependencies**: notebooklm-py, PostgreSQL
- **Added**: 2026-02-24

### Notebook Sync
- **Status**: Active
- **Description**: Sync notebook state from NotebookLM to local DB (sources, metadata)
- **Entry Points**:
  - `src/routes/notebooks.py:74-86` - POST /api/notebooks/{id}/sync
  - `src/services/notebook_service.py:73-108` - Sync logic
- **Added**: 2026-02-24

## Queries

### Ask Question
- **Status**: Active
- **Description**: Send questions to NotebookLM notebooks, extract answer + citations with fulltext enrichment, persist to DB with bibliographic data
- **Entry Points**:
  - `src/routes/queries.py:19-39` - POST /api/notebooks/{id}/query
  - `src/services/query_service.py:24-119` - Query execution and citation extraction
  - `src/services/query_service.py:150-190` - Citation fulltext enrichment (_enrich_citations)
- **Dependencies**: notebooklm-py, PostgreSQL
- **Added**: 2026-02-24 | **Modified**: 2026-02-24

### Query History
- **Status**: Active
- **Description**: List and retrieve past queries with citations
- **Entry Points**:
  - `src/routes/queries.py:42-73` - GET endpoints for query listing and detail
  - `src/services/query_service.py:115-139` - Query retrieval
- **Added**: 2026-02-24

## Sources

### Source Management
- **Status**: Active
- **Description**: List, delete, and sync sources in notebooks. Uploads use canonical ID resolution and real filenames.
- **Entry Points**:
  - `src/routes/sources.py:17-81` - Source endpoints (list, upload, sync-ids, delete)
  - `src/services/source_service.py:19-229` - Source operations with canonical ID sync
- **Added**: 2026-02-24 | **Modified**: 2026-02-24

### Source ID Sync
- **Status**: Active
- **Description**: POST /api/notebooks/{id}/sources/sync-ids — retroactively fixes DB source IDs to match canonical NotebookLM IDs
- **Entry Points**:
  - `src/routes/sources.py:72-86` - POST sync-ids endpoint
  - `src/services/source_service.py:152-204` - sync_source_ids() logic
- **Added**: 2026-02-24

### Zotero Integration
- **Status**: Active
- **Description**: Browse Zotero collections/items and upload PDFs from Zotero to NotebookLM
- **Entry Points**:
  - `src/routes/zotero.py:13-52` - Zotero browsing endpoints
  - `src/routes/sources.py:33-63` - Upload from Zotero endpoint
  - `src/services/zotero_service.py:1-157` - Zotero API client
  - `src/services/source_service.py:60-107` - Upload orchestration
- **Dependencies**: httpx, Zotero API, notebooklm-py
- **Added**: 2026-02-24

## Batch Operations

### Batch Query
- **Status**: Active
- **Description**: Submit multiple questions at once, processed sequentially in background
- **Entry Points**:
  - `src/routes/batch.py:24-81` - Batch submit and status endpoints
  - `src/routes/batch.py:99-127` - Background processing task
- **Added**: 2026-02-24

## Export

### Bot.py-Compatible Export
- **Status**: Active
- **Description**: Export query responses in JSON format compatible with viewer.html, with formatted bibliographic citations
- **Entry Points**:
  - `src/routes/export.py:17-75` - Export endpoint with bib data
  - `src/routes/export.py:78-105` - _format_citation() for "Author (Year), Title" format
  - `src/routes/export.py:108-153` - HTML builder from plain text + citations
- **Added**: 2026-02-24 | **Modified**: 2026-02-24

## Orchestrator (Natural Language Notebook Builder)

### Build Notebook from Instruction
- **Status**: Active
- **Description**: Accept natural language like "Make a notebook from the O'Neill papers in jan" — uses Claude Haiku to resolve Zotero collection, creates NotebookLM notebook, uploads all PDFs. Auto-retries with auth refresh if cookies are expired.
- **Entry Points**:
  - `src/routes/orchestrator.py:18-50` - POST /api/build-notebook endpoint
  - `src/services/orchestrator_service.py:22-108` - LLM intent parsing with Claude
  - `src/services/orchestrator_service.py:111-312` - Full pipeline with auto-retry on auth failure
- **Dependencies**: anthropic, httpx, notebooklm-py, asyncssh, PostgreSQL
- **Added**: 2026-02-24 | **Modified**: 2026-02-24

### Collection Tree Browser
- **Status**: Active
- **Description**: Returns full Zotero collection hierarchy (362 collections) with paths, depth, children
- **Entry Points**:
  - `src/routes/orchestrator.py:53-67` - GET /api/zotero/tree endpoint
  - `src/services/zotero_service.py:53-87` - Tree building with paginated fetch
- **Dependencies**: httpx, Zotero API
- **Added**: 2026-02-24

## Infrastructure

### Database (PostgreSQL)
- **Status**: Active
- **Description**: Async SQLAlchemy with asyncpg driver, 4 tables with idempotent column migrations
- **Entry Points**:
  - `src/database.py:1-67` - Engine, session, lifecycle, migrations
  - `src/models.py:1-110` - ORM models (Source has authors/publication_date/item_type, Citation has source_authors/source_date)
- **Added**: 2026-02-24 | **Modified**: 2026-02-24

### NotebookLM Client
- **Status**: Active
- **Description**: Singleton wrapper around notebooklm-py, lazy-initialized from auth JSON env var
- **Entry Points**:
  - `src/notebooklm_client.py:1-56` - Client initialization and management
- **Dependencies**: notebooklm-py
- **Added**: 2026-02-24
