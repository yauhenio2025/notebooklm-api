# Feature Inventory

> Auto-maintained by Claude Code. Last updated: 2026-02-24

## Health & Monitoring

### Health Check
- **Status**: Active
- **Description**: Basic health endpoint for Render monitoring - checks DB connectivity and auth config
- **Entry Points**:
  - `src/routes/health.py:18` - GET /health endpoint
- **Added**: 2026-02-24

### Status Check
- **Status**: Active
- **Description**: Detailed status including DB tables, NotebookLM client state, Zotero config
- **Entry Points**:
  - `src/routes/health.py:40` - GET /status endpoint
- **Added**: 2026-02-24

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
- **Description**: Send questions to NotebookLM notebooks, extract answer + citations, persist to DB
- **Entry Points**:
  - `src/routes/queries.py:19-39` - POST /api/notebooks/{id}/query
  - `src/services/query_service.py:18-112` - Query execution and citation extraction
- **Dependencies**: notebooklm-py, PostgreSQL
- **Added**: 2026-02-24

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
- **Description**: List and delete sources in notebooks
- **Entry Points**:
  - `src/routes/sources.py:17-63` - Source endpoints
  - `src/services/source_service.py:17-107` - Source operations
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
- **Description**: Export query responses in JSON format compatible with viewer.html
- **Entry Points**:
  - `src/routes/export.py:17-71` - Export endpoint
  - `src/routes/export.py:74-112` - HTML builder from plain text + citations
- **Added**: 2026-02-24

## Orchestrator (Natural Language Notebook Builder)

### Build Notebook from Instruction
- **Status**: Active
- **Description**: Accept natural language like "Make a notebook from the O'Neill papers in jan" — uses Claude Haiku to resolve Zotero collection, creates NotebookLM notebook, uploads all PDFs
- **Entry Points**:
  - `src/routes/orchestrator.py:18-50` - POST /api/build-notebook endpoint
  - `src/services/orchestrator_service.py:31-100` - LLM intent parsing with Claude
  - `src/services/orchestrator_service.py:103-203` - Full pipeline (tree → parse → items → create → upload)
- **Dependencies**: anthropic, httpx, notebooklm-py, PostgreSQL
- **Added**: 2026-02-24

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
- **Description**: Async SQLAlchemy with asyncpg driver, 4 tables (notebooks, sources, queries, citations)
- **Entry Points**:
  - `src/database.py:1-44` - Engine, session, lifecycle
  - `src/models.py:1-104` - ORM models
- **Added**: 2026-02-24

### NotebookLM Client
- **Status**: Active
- **Description**: Singleton wrapper around notebooklm-py, lazy-initialized from auth JSON env var
- **Entry Points**:
  - `src/notebooklm_client.py:1-56` - Client initialization and management
- **Dependencies**: notebooklm-py
- **Added**: 2026-02-24
