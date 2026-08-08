"""At-most-once query submission and sanitized boundary tests."""

import asyncio
import logging
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.models import Query
from src.routes import notebooks as notebook_routes
from src.routes import queries as query_routes
from src.routes import sources as source_routes
from src.schemas import NotebookCreate, QueryRequest, SourceFromText
from src.services import query_service


class RecordingSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.commits = 0

    def add(self, value: object) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, value: object) -> None:
        if isinstance(value, Query) and value.id is None:
            value.id = 17


class FailingChat:
    def __init__(self, secret_marker: str) -> None:
        self.secret_marker = secret_marker
        self.calls = 0

    async def ask(self, *_args: object, **_kwargs: object) -> object:
        self.calls += 1
        raise RuntimeError(self.secret_marker)


def test_ambiguous_chat_failure_is_persisted_and_never_retried(monkeypatch, caplog):
    secret_marker = "private-provider-error-must-not-escape"
    question = "A private question about a provisional argument"
    chat = FailingChat(secret_marker)
    client = SimpleNamespace(chat=chat)
    session = RecordingSession()

    async def get_client():
        return client

    monkeypatch.setattr(query_service, "get_notebooklm_client", get_client)
    caplog.set_level(logging.INFO)

    with pytest.raises(query_service.QueryOutcomeAmbiguousError):
        asyncio.run(query_service.ask_question(session, "nb-1", question))

    stored = next(item for item in session.added if isinstance(item, Query))
    assert chat.calls == 1
    assert stored.status == "failed"
    assert stored.metadata_["outcome_ambiguous"] is True
    assert stored.metadata_["retry_safe"] is False
    assert secret_marker not in caplog.text
    assert question not in caplog.text
    assert stored.metadata_["question_sha256"] in caplog.text


def test_query_route_returns_only_a_sanitized_ambiguous_failure(monkeypatch, caplog):
    secret_marker = "provider-response-secret"

    async def get_notebook(_db: object, _notebook_id: str):
        return object()

    async def fail_query(*_args: object, **_kwargs: object):
        raise query_service.QueryOutcomeAmbiguousError(secret_marker)

    monkeypatch.setattr(query_routes, "get_notebook", get_notebook)
    monkeypatch.setattr(query_routes, "ask_question", fail_query)
    caplog.set_level(logging.INFO)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            query_routes.api_query_notebook(
                "nb-1",
                QueryRequest(question="Where is the mechanism?"),
                object(),
            )
        )

    assert raised.value.status_code == 503
    assert raised.value.detail == (
        "NotebookLM could not complete the query; its outcome may be ambiguous"
    )
    assert secret_marker not in str(raised.value.detail)
    assert secret_marker not in caplog.text


def test_notebook_creation_failure_is_sanitized(monkeypatch, caplog):
    secret_marker = "private-notebook-provider-error"

    async def fail_create(*_args: object, **_kwargs: object):
        raise RuntimeError(secret_marker)

    monkeypatch.setattr(notebook_routes, "create_notebook", fail_create)
    caplog.set_level(logging.INFO)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            notebook_routes.api_create_notebook(
                NotebookCreate(title="Private project title"), object()
            )
        )

    assert raised.value.status_code == 503
    assert raised.value.detail == "NotebookLM service is unavailable"
    assert secret_marker not in caplog.text


def test_text_source_upload_failure_is_sanitized(monkeypatch, caplog):
    secret_marker = "private-source-provider-error"

    async def get_notebook(_db: object, _notebook_id: str):
        return object()

    async def get_client():
        return object()

    async def fail_upload(*_args: object, **_kwargs: object):
        raise RuntimeError(secret_marker)

    monkeypatch.setattr(source_routes, "get_notebook", get_notebook)
    monkeypatch.setattr(source_routes, "get_notebooklm_client", get_client)
    monkeypatch.setattr(source_routes, "upload_text_source", fail_upload)
    caplog.set_level(logging.INFO)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            source_routes.api_upload_from_text(
                "nb-1",
                SourceFromText(title="Private source", content="Private full text"),
                object(),
            )
        )

    assert raised.value.status_code == 503
    assert raised.value.detail == "NotebookLM service is unavailable"
    assert secret_marker not in caplog.text
