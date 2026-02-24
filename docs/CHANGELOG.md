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
