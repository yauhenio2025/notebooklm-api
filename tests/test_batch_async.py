"""Durable asynchronous batch-query boundary tests."""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.dml import Update

from src.database import Base
from src.models import Citation, Notebook, Query
from src.routes import batch as batch_routes
from src.schemas import BatchQueryRequest, QueryResponse
from src.services import batch_recovery_service


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    """Permit real SQLite UPDATE...RETURNING tests for the PostgreSQL model."""
    return "JSON"


class ScalarResult:
    def __init__(self, items: list[object]) -> None:
        self.items = items

    def scalars(self):
        return self

    def all(self) -> list[object]:
        return self.items

    def one_or_none(self):
        if not self.items:
            return None
        if len(self.items) != 1:
            raise AssertionError(f"expected at most one row, got {len(self.items)}")
        return self.items[0]

    def scalar_one_or_none(self):
        return self.one_or_none()

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
    def __init__(
        self,
        queries: list[Query],
        *,
        claim_lock: asyncio.Lock | None = None,
    ) -> None:
        self.queries = queries
        self.added: list[object] = []
        self.commit_states: list[tuple[str, ...]] = []
        self.rollbacks = 0
        self.claim_lock = claim_lock or asyncio.Lock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute(self, statement: object) -> ScalarResult:
        if not isinstance(statement, Update):
            return ScalarResult(list(self.queries))

        values = {
            column.key: getattr(value, "value", None)
            for column, value in statement._values.items()
        }
        target_status = values.get("status")
        if target_status == "running":
            async with self.claim_lock:
                query = next(
                    (item for item in self.queries if item.status == "pending"),
                    None,
                )
                if query is None:
                    return ScalarResult([])
                query.status = "running"
                query.metadata_ = dict(values["metadata"])
                return ScalarResult(
                    [
                        SimpleNamespace(
                            id=query.id,
                            question=query.question,
                            turn_number=query.turn_number,
                        )
                    ]
                )

        metadata = values.get("metadata")
        owner = metadata.get("claim_owner") if isinstance(metadata, dict) else None
        query = next(
            (
                item
                for item in self.queries
                if item.status == "running"
                and isinstance(item.metadata_, dict)
                and item.metadata_.get("claim_owner") == owner
            ),
            None,
        )
        if query is None:
            return ScalarResult([])
        for key, value in values.items():
            setattr(query, "metadata_" if key == "metadata" else key, value)
        return ScalarResult([query.id])

    def add(self, value: object) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.commit_states.append(tuple(query.status for query in self.queries))

    async def rollback(self) -> None:
        self.rollbacks += 1


class AsyncSyncSession:
    """Small async adapter around a real SQLAlchemy session for race tests."""

    def __init__(self, session: Session) -> None:
        self.session = session

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args: object) -> None:
        await asyncio.to_thread(self.session.close)

    async def execute(self, statement: object):
        return await asyncio.to_thread(self.session.execute, statement)

    async def commit(self) -> None:
        await asyncio.to_thread(self.session.commit)

    async def rollback(self) -> None:
        await asyncio.to_thread(self.session.rollback)

    def add(self, value: object) -> None:
        self.session.add(value)


def _query(
    *, query_id: int, status: str = "pending", question: str = "Question"
) -> Query:
    query = Query(
        notebook_id="nb-1",
        question=question,
        batch_id="batch-1",
        turn_number=1,
        status=status,
        asked_at=datetime.now(timezone.utc),
    )
    query.id = query_id
    return query


def _sqlite_sessions(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'batch-race.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(
        engine,
        tables=[Notebook.__table__, Query.__table__, Citation.__table__],
    )
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions() as session:
        session.add(Notebook(id="nb-1", title="Notebook"))
        session.add(
            Query(
                notebook_id="nb-1",
                question="Private atomic question",
                batch_id="batch-1",
                turn_number=1,
                status="pending",
                asked_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
    return engine, sessions


def test_batch_post_is_an_accepted_async_operation():
    post_route = next(
        route
        for route in batch_routes.router.routes
        if route.path == "/notebooks/{notebook_id}/batch-query"
    )
    assert post_route.status_code == 202


def test_batch_schema_accepts_exactly_one_question():
    assert BatchQueryRequest(questions=["one"]).questions == ["one"]
    for invalid in ([], [""], ["x" * 5001], ["one", "two"]):
        with pytest.raises(ValidationError):
            BatchQueryRequest(questions=invalid)


def test_two_concurrent_processors_make_one_provider_call(monkeypatch, tmp_path):
    engine, sessions = _sqlite_sessions(tmp_path)
    claim_results: list[batch_routes._ClaimedBatchQuery | None] = []
    real_claim = batch_routes._claim_pending_query

    async def observed_claim(*args: object, **kwargs: object):
        claim = await real_claim(*args, **kwargs)
        claim_results.append(claim)
        return claim

    class Chat:
        def __init__(self) -> None:
            self.calls = 0

        async def ask(self, *_args: object, **_kwargs: object):
            self.calls += 1
            await asyncio.sleep(0.02)
            return SimpleNamespace(
                answer="Grounded",
                conversation_id="conversation-1",
                turn_number=1,
                references=[],
            )

    chat = Chat()

    async def get_client():
        return SimpleNamespace(chat=chat)

    monkeypatch.setattr(
        batch_routes,
        "async_session",
        lambda: AsyncSyncSession(sessions()),
    )
    monkeypatch.setattr(batch_routes, "get_notebooklm_client", get_client)
    monkeypatch.setattr(batch_routes, "_claim_pending_query", observed_claim)

    async def scenario():
        await asyncio.gather(
            batch_routes._process_batch("batch-1", "nb-1", 0),
            batch_routes._process_batch("batch-1", "nb-1", 0),
        )

    asyncio.run(scenario())

    with sessions() as session:
        query = session.scalar(select(Query))
        assert query is not None and query.status == "completed"
        assert query.metadata_["claim_owner"]
        assert query.metadata_["deadline_at"]
    assert chat.calls == 1
    assert sum(claim is not None for claim in claim_results) == 1
    assert any(claim is None for claim in claim_results)
    engine.dispose()


def test_overdue_recovery_cannot_be_overwritten_by_old_owner(monkeypatch, tmp_path):
    engine, sessions = _sqlite_sessions(tmp_path)

    async def scenario():
        owner_db = AsyncSyncSession(sessions())
        claim = await batch_routes._claim_pending_query(
            owner_db,
            batch_id="batch-1",
            notebook_id="nb-1",
        )
        assert claim is not None

        # A configuration change after the claim cannot change its persisted
        # deadline.  The grace window is calculated only from that value.
        monkeypatch.setattr(
            batch_routes,
            "get_settings",
            lambda: SimpleNamespace(notebooklm_query_timeout_seconds=60),
        )
        assert await batch_recovery_service.recover_overdue_running_batch_queries(
            owner_db,
            now=claim.deadline_at + timedelta(seconds=30),
        ) == 0
        assert await batch_recovery_service.recover_overdue_running_batch_queries(
            owner_db,
            now=claim.deadline_at + timedelta(seconds=61),
        ) == 1

        result = SimpleNamespace(
            answer="Late answer",
            conversation_id="late-conversation",
            turn_number=1,
            references=[],
        )
        completed, _, _ = await batch_routes._persist_owned_success(
            owner_db,
            claim,
            result,
        )
        failed = await batch_routes._persist_owned_failure(
            owner_db,
            claim,
            RuntimeError("private late failure"),
        )
        assert not completed
        assert not failed
        await owner_db.__aexit__()

    asyncio.run(scenario())

    with sessions() as session:
        query = session.scalar(select(Query))
        assert query is not None and query.status == "failed"
        assert query.answer is None
        assert query.metadata_["error_type"] == "ProcessInterrupted"
        assert session.scalar(select(func.count(Citation.id))) == 0
    engine.dispose()


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


def test_startup_schedules_pending_batches_once_per_batch(monkeypatch):
    class PendingSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object):
            return None

        async def execute(self, _statement: object):
            return ScalarResult([("batch-1", "nb-1"), ("batch-2", "nb-2")])

    scheduled: list[tuple[str, str]] = []
    monkeypatch.setattr(batch_routes, "async_session", lambda: PendingSession())
    monkeypatch.setattr(
        batch_routes,
        "_start_batch_task",
        lambda batch_id, notebook_id, _delay: scheduled.append((batch_id, notebook_id)),
    )

    assert asyncio.run(batch_routes.schedule_pending_batch_queries()) == 2
    assert scheduled == [("batch-1", "nb-1"), ("batch-2", "nb-2")]


def test_active_task_registry_suppresses_in_process_scheduling_storm(monkeypatch):
    async def scenario():
        release = asyncio.Event()
        calls = 0

        async def blocked_process(*_args: object):
            nonlocal calls
            calls += 1
            await release.wait()

        monkeypatch.setattr(batch_routes, "_process_batch", blocked_process)
        first = batch_routes._start_batch_task("batch-keyed", "nb-1", 0)
        second = batch_routes._start_batch_task("batch-keyed", "nb-1", 0)
        assert first is second
        await asyncio.sleep(0)
        assert calls == 1
        release.set()
        await first
        await asyncio.sleep(0)
        assert "batch-keyed" not in batch_routes._active_batch_tasks

    asyncio.run(scenario())


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
        assert query.metadata_["batch_position"] == 1
        assert datetime.fromisoformat(query.metadata_["started_at"]).tzinfo is not None

        release.set()
        await task

        assert len(chat.calls) == 1
        assert query.status == "completed"
        assert query.answer == "Grounded answer"
        assert session.commit_states == [
            ("running",),
            ("completed",),
            ("completed",),
        ]
        assert len(session.added) == 1
        assert query.metadata_["citation_count"] == 1
        assert query.metadata_["answer_length"] == 15
        assert query.metadata_["batch_position"] == 1
        assert str(uuid.UUID(query.metadata_["claim_owner"])) == query.metadata_[
            "claim_owner"
        ]
        assert datetime.fromisoformat(
            query.metadata_["deadline_at"]
        ) > datetime.fromisoformat(query.metadata_["started_at"])

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
    assert session.commit_states == [("running",), ("failed",), ("failed",)]
    assert query.metadata_["error_type"] == "RuntimeError"
    assert query.metadata_["outcome_ambiguous"] is True
    assert query.metadata_["retry_safe"] is False
    assert query.metadata_["batch_position"] == 1
    assert "claim_owner" in query.metadata_
    assert "deadline_at" in query.metadata_
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


def test_batch_status_counts_running_as_outstanding_and_exposes_safe_flags(monkeypatch):
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
    scheduled: list[tuple[str, str]] = []
    monkeypatch.setattr(
        batch_routes,
        "_start_batch_task",
        lambda batch_id, notebook_id, _delay: scheduled.append((batch_id, notebook_id)),
    )

    result = asyncio.run(batch_routes.api_batch_status("batch-1", session))

    assert result.total == 4
    assert result.completed == 1
    assert result.failed == 1
    assert result.pending == 2
    failed_item = next(item for item in result.queries if item.id == failed.id)
    assert failed_item.outcome_ambiguous is True
    assert failed_item.retry_safe is False
    assert failed_item.error_type == "RuntimeError"
    assert scheduled == [("batch-1", "nb-1")]


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

    assert query.status == "pending"
    assert session.commit_states == []
    assert question not in caplog.text
    assert client_error not in caplog.text


def test_batch_query_timeout_is_ambiguous_and_never_retried(monkeypatch, caplog):
    question = "Private timed-out question"
    query = _query(query_id=6, question=question)
    session = ProcessSession([query])

    class HangingChat:
        def __init__(self) -> None:
            self.calls = 0

        async def ask(self, *_args: object, **_kwargs: object):
            self.calls += 1
            await asyncio.Event().wait()

    chat = HangingChat()

    async def get_client():
        return SimpleNamespace(chat=chat)

    monkeypatch.setattr(batch_routes, "async_session", lambda: session)
    monkeypatch.setattr(batch_routes, "get_notebooklm_client", get_client)
    monkeypatch.setattr(
        batch_routes,
        "get_settings",
        lambda: SimpleNamespace(notebooklm_query_timeout_seconds=0.01),
    )
    caplog.set_level(logging.INFO)

    asyncio.run(batch_routes._process_batch("batch-1", "nb-1", 0))

    assert chat.calls == 1
    assert query.status == "failed"
    assert session.commit_states == [("running",), ("failed",), ("failed",)]
    assert query.metadata_["error_type"] == "TimeoutError"
    assert query.metadata_["outcome_ambiguous"] is True
    assert query.metadata_["retry_safe"] is False
    assert query.metadata_["batch_position"] == 1
    assert question not in caplog.text


def test_recovery_uses_only_persisted_deadlines_and_preserves_legacy_rows(
    caplog,
):
    observed_at = datetime.now(timezone.utc)
    recent_running = _query(
        query_id=7,
        status="running",
        question="Private recent running question",
    )
    recent_running.metadata_ = {
        "claim_owner": "recent-owner",
        "started_at": (observed_at - timedelta(minutes=30)).isoformat(),
        "deadline_at": (observed_at - timedelta(seconds=30)).isoformat(),
    }
    overdue_running = _query(
        query_id=8,
        status="running",
        question="Private overdue running question",
    )
    overdue_running.metadata_ = {
        "claim_owner": "overdue-owner",
        "started_at": (observed_at - timedelta(seconds=121)).isoformat(),
        "deadline_at": (observed_at - timedelta(seconds=61)).isoformat(),
    }
    legacy_overdue = _query(
        query_id=9,
        status="running",
        question="Private legacy running question",
    )
    legacy_overdue.metadata_ = {"batch_position": 1}
    legacy_overdue.asked_at = observed_at - timedelta(seconds=121)
    malformed_deadline = _query(
        query_id=15,
        status="running",
        question="Private malformed deadline question",
    )
    malformed_deadline.metadata_ = {
        "claim_owner": "malformed-owner",
        "started_at": (observed_at - timedelta(days=1)).isoformat(),
        "deadline_at": "not-a-date",
    }
    pending = _query(
        query_id=10,
        status="pending",
        question="Private interrupted pending question",
    )
    pending.asked_at = observed_at - timedelta(days=1)
    completed = _query(query_id=11, status="completed")
    completed.metadata_ = {"preserve": "completed"}
    failed = _query(query_id=12, status="failed")
    failed.metadata_ = {"preserve": "failed"}
    non_batch_pending = _query(query_id=13, status="pending")
    non_batch_pending.batch_id = None
    session = ProcessSession(
        [
            recent_running,
            overdue_running,
            legacy_overdue,
            malformed_deadline,
            pending,
            completed,
            failed,
            non_batch_pending,
        ]
    )

    caplog.set_level(logging.INFO)

    recovered_count = asyncio.run(
        batch_recovery_service.recover_overdue_running_batch_queries(
            session,
            now=observed_at,
        )
    )

    assert recovered_count == 1
    assert recent_running.status == "running"
    assert overdue_running.status == "failed"
    assert overdue_running.metadata_["error_type"] == "ProcessInterrupted"
    assert overdue_running.metadata_["outcome_ambiguous"] is True
    assert overdue_running.metadata_["retry_safe"] is False
    assert overdue_running.metadata_["deadline_at"] == (
        observed_at - timedelta(seconds=61)
    ).isoformat()
    assert legacy_overdue.status == "running"
    assert legacy_overdue.metadata_ == {"batch_position": 1}
    assert malformed_deadline.status == "running"
    assert pending.status == "pending"
    assert completed.status == "completed"
    assert completed.metadata_ == {"preserve": "completed"}
    assert failed.status == "failed"
    assert failed.metadata_ == {"preserve": "failed"}
    assert non_batch_pending.status == "pending"
    assert "recovered_count=1" in caplog.text
    for query in (
        recent_running,
        overdue_running,
        legacy_overdue,
        malformed_deadline,
        pending,
        completed,
        failed,
        non_batch_pending,
    ):
        assert query.question not in caplog.text


def test_batch_status_poll_leaves_legacy_running_row_untouched(
    monkeypatch,
    caplog,
):
    question = "Private status-polled orphan"
    query = _query(query_id=14, status="running", question=question)
    query.metadata_ = {"batch_position": 1}
    query.asked_at = datetime.now(timezone.utc) - timedelta(seconds=130)
    session = ProcessSession([query])

    caplog.set_level(logging.INFO)

    result = asyncio.run(batch_routes.api_batch_status("batch-1", session))

    assert result.pending == 1
    assert result.failed == 0
    assert query.status == "running"
    assert query.metadata_ == {"batch_position": 1}
    assert question not in caplog.text
