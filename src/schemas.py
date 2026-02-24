"""Pydantic request/response models."""

from datetime import datetime

from pydantic import BaseModel, Field


# --- Notebooks ---

class NotebookCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)


class NotebookResponse(BaseModel):
    id: str
    title: str
    created_at: datetime
    last_synced_at: datetime | None = None
    source_count: int = 0
    is_active: bool = True

    model_config = {"from_attributes": True}


class NotebookDetail(NotebookResponse):
    sources: list["SourceResponse"] = []
    query_count: int = 0


# --- Sources ---

class SourceResponse(BaseModel):
    id: str
    notebook_id: str
    title: str
    source_type: str = "pdf"
    zotero_key: str | None = None
    file_name: str | None = None
    status: str = "ready"
    uploaded_at: datetime

    model_config = {"from_attributes": True}


class SourceFromZotero(BaseModel):
    collection_key: str | None = None
    item_keys: list[str] | None = None


# --- Queries ---

class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=5000)


class CitationResponse(BaseModel):
    id: int
    citation_number: int
    source_id: str | None = None
    source_title: str | None = None
    cited_text: str | None = None
    start_char: int | None = None
    end_char: int | None = None

    model_config = {"from_attributes": True}


class QueryResponse(BaseModel):
    id: int
    notebook_id: str
    question: str
    answer: str | None = None
    status: str = "pending"
    asked_at: datetime
    answered_at: datetime | None = None
    citations: list[CitationResponse] = []

    model_config = {"from_attributes": True}


class QueryListItem(BaseModel):
    id: int
    question: str
    status: str
    asked_at: datetime
    answered_at: datetime | None = None
    citation_count: int = 0

    model_config = {"from_attributes": True}


# --- Batch ---

class BatchQueryRequest(BaseModel):
    questions: list[str] = Field(..., min_length=1)
    delay_seconds: float = Field(default=2.0, ge=0, le=30)


class BatchQueryResponse(BaseModel):
    batch_id: str
    notebook_id: str
    total_questions: int
    queries: list[QueryListItem] = []


class BatchStatus(BaseModel):
    batch_id: str
    total: int
    completed: int
    failed: int
    pending: int
    queries: list[QueryListItem] = []


# --- Export ---

class ExportFootnote(BaseModel):
    number: int
    source_file: str = ""
    quoted_text: str = ""
    context_snippet: str = ""
    aria_label: str = ""


class ExportResponse(BaseModel):
    """Bot.py-compatible JSON export format for viewer.html."""
    timestamp: str
    notebook_url: str = ""
    question: str
    response_text: str = ""
    clean_html: str = ""
    footnotes: list[ExportFootnote] = []
    notebook_sources: list[str] = []
    model: str = "notebooklm"


# --- Health ---

class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "0.1.0"
    database: str = "unknown"
    notebooklm_auth: str = "unknown"


class StatusResponse(BaseModel):
    status: str = "ok"
    version: str = "0.1.0"
    database: str = "unknown"
    database_tables: list[str] = []
    notebooklm_auth: str = "unknown"
    notebooklm_notebooks: int | None = None
    zotero_configured: bool = False


# --- Zotero ---

class ZoteroCollection(BaseModel):
    key: str
    name: str
    parent_key: str | None = None
    num_items: int = 0


class ZoteroCollectionTree(BaseModel):
    """Collection with full hierarchical path info."""
    key: str
    name: str
    parent_key: str | None = None
    num_items: int = 0
    full_path: str = ""
    depth: int = 1
    children_keys: list[str] = []


class ZoteroItem(BaseModel):
    key: str
    title: str
    item_type: str = ""
    creators: list[str] = []
    date: str = ""
    has_pdf: bool = False
    pdf_filename: str | None = None


# --- Orchestrator (Natural Language Notebook Builder) ---

class BuildNotebookRequest(BaseModel):
    instruction: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="Natural language instruction for building a notebook",
        json_schema_extra={
            "examples": [
                "Make a notebook from the O'Neill papers in jan",
                "Create a notebook with Castoriadis primary sources",
                "Build a notebook from _2026_jan / deutschmann",
            ]
        },
    )


class BuildNotebookSourceInfo(BaseModel):
    title: str
    file_name: str | None = None
    status: str = "ready"
    zotero_key: str | None = None


class BuildNotebookIntent(BaseModel):
    collection_key: str | None = None
    collection_path: str | None = None
    notebook_title: str | None = None
    confidence: float = 0.0
    reasoning: str | None = None
    alternatives: list[dict] = []


class BuildNotebookStep(BaseModel):
    step: str
    duration_s: float = 0.0
    collections_found: int | None = None
    intent: BuildNotebookIntent | None = None
    total_items: int | None = None
    items_with_pdfs: int | None = None
    notebook_id: str | None = None
    notebook_title: str | None = None
    attempted: int | None = None
    uploaded: int | None = None


class BuildNotebookResponse(BaseModel):
    status: str
    notebook_id: str | None = None
    notebook_title: str | None = None
    collection_path: str | None = None
    collection_key: str | None = None
    sources_uploaded: int = 0
    sources: list[BuildNotebookSourceInfo] = []
    intent: BuildNotebookIntent | None = None
    duration_s: float = 0.0
    steps: list[dict] = []
