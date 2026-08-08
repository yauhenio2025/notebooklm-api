"""Durable asynchronous batch-query boundary tests."""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from src.models import Query
from src.routes import batch as batch_routes
from src.schemas import BatchQueryRequest, QueryResponse


class ScalarResult:
    def __init__(self, items: list[Query]) -> None:
        self.items = items

    def scalars(self):
        return self

    def all(self) -> list[Query]:
        return self.items

    def scalar_one(self) -> Query:
        if len(self.items) != 1:
            raise AssertionError(f"expected one query, got {len(self.items)}")
        return self.items[0]


class CreateSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.commits = 0
        self.next_id = 100

    def add(self, value: object) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, value: object) -> None:
        if isinstance(value, Query) and value.id is None:
            value.id = self.next_id
            self.next_id += 1


class ProcessSession:
    def __init__(self, queries: list[Query]) -> None:
        self.queries = queries
        self.added: list[object] = []
        self.commit_states: list[tuple[str, ...]] = []
        self.rollbacks = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute(self, _statement: object) -> ScalarResult:
        return ScalarResult(self.queries)

    def add(self, value: object) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.commit_states.append(tuple(query.status for query in self.queries))

    async def rollback(self) -> None:
        self.rollbacks += 1


def _query(
    *, query_id: int, status: str = "pending", question: str = "Question"
) -> Query:
    query = Query(
        notebook_id="nb-1",
        question=question,
        batch_id="batch-1",
        turn_number=query_id,
        status=status,
        asked_at=datetime.now(timezone.utc),
    )
    query.id = query_id
    return query


def test_batch_post_is_an_accepted_async_operation():
    post_route = next(
        route
        for route in batch_routes.router.routes
        if route.path == "/notebooks/{notebook_id}/batch-query"
    )
    assert post_route.status_code == 202


def test_batch_post_returns_before_background_completion_and_retains_task(
    monkeypatch,
    caplog,
):
    question = "Private question that must not enter logs"
    provider_error = "private background failure detail"
    session = CreateSession()

    async def get_notebook(_db: object, _notebook_id: str):
        return object()

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocked_process(*_args: object):
            started.set()
            await release.wait()
            raise RuntimeError(provider_error)

        monkeypatch.setattr(batch_routes, "get_notebook", get_notebook)
        monkeypatch.setattr(batch_routes, "_process_batch", blocked_process)
        caplog.set_level(logging.INFO)

        response = await batch_routes.api_batch_query(
            "nb-1",
            BatchQueryRequest(questions=[question], delay_seconds=0),
            session,
        )
        await asyncio.wait_for(started.wait(), timeout=1)

        assert not release.is_set()
        assert response.total_questions == 1
        assert response.queries[0].id == 100
        assert response.queries[0].status == "pending"
        assert str(uuid.UUID(response.batch_id)) == response.batch_id
        assert len(response.batch_id) == 36
        assert len(batch_routes._background_tasks) == 1

        tasks = tuple(batch_routes._background_tasks)
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)
        assert not batch_routes._background_tasks

    asyncio.run(scenario())

    assert question not in caplog.text
    assert provider_error not in caplog.text
    assert "error_type=RuntimeError" in caplog.text


def test_batch_query_commits_running_before_one_provider_call_then_completes(
    monkeypatch,
    caplog,
):
    question = "Private completed question"
    query = _query(query_id=1, question=question)
    session = ProcessSession([query])

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        class BlockingChat:
            def __init__(self) -> None:
                self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

            async def ask(self, *args: object, **kwargs: object):
                self.calls.append((args, kwargs))
                started.set()
                await release.wait()
                return SimpleNamespace(
                    answer="Grounded answer",
                    conversation_id="conversation-1",
                    turn_number=1,
                    references=[
                        SimpleNamespace(
                            citation_number=1,
                            source_id="source-1",
                            cited_text="Exact support",
                            start_char=10,
                            end_char=23,
                        )
                    ],
                )

        chat = BlockingChat()

        async def get_client():
            return SimpleNamespace(chat=chat)

        monkeypatch.setattr(batch_routes, "async_session", lambda: session)
        monkeypatch.setattr(batch_routes, "get_notebooklm_client", get_client)
        caplog.set_level(logging.INFO)

        task = asyncio.create_task(batch_routes._process_batch("batch-1", "nb-1", 0))
        await asyncio.wait_for(started.wait(), timeout=1)

        assert query.status == "running"
        assert session.commit_states == [("running",)]
        assert len(chat.calls) == 1

        release.set()
        await task

        assert len(chat.calls) == 1
        assert query.status == "completed"
        assert query.answer == "Grounded answer"
        assert session.commit_states == [("running",), ("completed",)]
        assert len(session.added) == 1
        assert query.metadata_ == {
            "citation_count": 1,
            "answer_length": 15,
            "batch_position": 1,
        }

    asyncio.run(scenario())
    assert question not in caplog.text


def test_batch_query_failure_is_ambiguous_not_retryable_and_sanitized(
    monkeypatch,
    caplog,
):
    question = "Private failed question"
    provider_error = "private provider error payload"
    query = _query(query_id=2, question=question)
    session = ProcessSession([query])

    class FailingChat:
        def __init__(self) -> None:
            self.calls = 0

        async def ask(self, *_args: object, **_kwargs: object):
            self.calls += 1
            raise RuntimeError(provider_error)

    chat = FailingChat()

    async def get_client():
        return SimpleNamespace(chat=chat)

    monkeypatch.setattr(batch_routes, "async_session", lambda: session)
    monkeypatch.setattr(batch_routes, "get_notebooklm_client", get_client)
    caplog.set_level(logging.INFO)

    asyncio.run(batch_routes._process_batch("batch-1", "nb-1", 0))

    assert chat.calls == 1
    assert query.status == "failed"
    assert session.commit_states == [("running",), ("failed",)]
    assert query.metadata_ == {
        "error_type": "RuntimeError",
        "outcome_ambiguous": True,
        "retry_safe": False,
        "batch_position": 1,
    }
    assert query.outcome_ambiguous is True
    assert query.retry_safe is False
    assert query.error_type == "RuntimeError"
    assert session.rollbacks == 0
    detail = QueryResponse.model_validate(query)
    assert detail.outcome_ambiguous is True
    assert detail.retry_safe is False
    assert detail.error_type == "RuntimeError"
    assert question not in caplog.text
    assert provider_error not in caplog.text
    assert provider_error not in str(query.metadata_)


def test_batch_status_counts_running_as_outstanding_and_exposes_safe_flags():
    pending = _query(query_id=1, status="pending")
    running = _query(query_id=2, status="running")
    completed = _query(query_id=3, status="completed")
    failed = _query(query_id=4, status="failed")
    failed.metadata_ = {
        "error_type": "RuntimeError",
        "outcome_ambiguous": True,
        "retry_safe": False,
    }
    session = ProcessSession([pending, running, completed, failed])

    result = asyncio.run(batch_routes.api_batch_status("batch-1", session))

    assert result.total == 4
    assert result.completed == 1
    assert result.failed == 1
    assert result.pending == 2
    failed_item = next(item for item in result.queries if item.id == failed.id)
    assert failed_item.outcome_ambiguous is True
    assert failed_item.retry_safe is False
    assert failed_item.error_type == "RuntimeError"


def test_pre_submission_client_failure_is_retryable_and_sanitized(monkeypatch, caplog):
    question = "Private unsubmitted question"
    client_error = "private client initialization payload"
    query = _query(query_id=5, question=question)
    session = ProcessSession([query])

    async def get_client():
        raise RuntimeError(client_error)

    monkeypatch.setattr(batch_routes, "async_session", lambda: session)
    monkeypatch.setattr(batch_routes, "get_notebooklm_client", get_client)
    caplog.set_level(logging.INFO)

    asyncio.run(batch_routes._process_batch("batch-1", "nb-1", 0))

    assert query.status == "failed"
    assert session.commit_states == [("failed",)]
    assert query.metadata_ == {
        "error_type": "RuntimeError",
        "outcome_ambiguous": False,
        "retry_safe": True,
        "batch_position": 1,
    }
    assert question not in caplog.text
    assert client_error not in caplog.text
