"""Remote NotebookLM reconciliation inventory tests."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from notebooklm import Source, SourceStatus

from src.routes import remote as remote_routes
from src.services.remote_inventory_service import (
    RemoteInventoryShapeError,
    list_actual_remote_notebooks,
    list_actual_remote_sources,
)


class FakeCollection:
    def __init__(self, items=None, *, error: Exception | None = None):
        self.items = list(items or [])
        self.error = error
        self.calls: list[tuple[object, ...]] = []

    async def list(self, *args):
        self.calls.append(args)
        if self.error is not None:
            raise self.error
        return self.items


class FakeClient:
    def __init__(self, *, notebooks=None, sources=None):
        self.notebooks = notebooks or FakeCollection()
        self.sources = sources or FakeCollection()


def _client_for_routes(fake_client: FakeClient) -> TestClient:
    test_app = FastAPI()
    test_app.include_router(remote_routes.router, prefix="/api")

    async def fake_dependency():
        return fake_client

    test_app.dependency_overrides[remote_routes.require_remote_client] = fake_dependency
    return TestClient(test_app)


def test_remote_notebooks_are_read_from_provider_objects_not_database_rows():
    provider = FakeCollection(
        [
            SimpleNamespace(
                id="nb-1", title="ganrl · project · corpus", status="active"
            ),
            SimpleNamespace(id="nb-2", title="Recovery candidate", is_ready=False),
        ]
    )
    result = asyncio.run(list_actual_remote_notebooks(FakeClient(notebooks=provider)))

    assert [item.model_dump() for item in result] == [
        {
            "id": "nb-1",
            "title": "ganrl · project · corpus",
            "status": "active",
            "type": "notebook",
        },
        {
            "id": "nb-2",
            "title": "Recovery candidate",
            "status": "processing",
            "type": "notebook",
        },
    ]
    assert provider.calls == [()]


def test_remote_sources_preserve_stable_identity_readiness_and_type():
    source_kind = SimpleNamespace(value="pdf")
    provider = FakeCollection(
        [
            SimpleNamespace(
                id="source-1",
                title="Adam Smith in Beijing.pdf",
                is_ready=True,
                kind=source_kind,
            ),
            {
                "id": "source-2",
                "title": "Page-separated text",
                "status": "indexing",
                "type": "text",
            },
        ]
    )
    result = asyncio.run(
        list_actual_remote_sources(FakeClient(sources=provider), "nb-managed")
    )

    assert [item.model_dump() for item in result] == [
        {
            "id": "source-1",
            "title": "Adam Smith in Beijing.pdf",
            "status": "ready",
            "type": "pdf",
        },
        {
            "id": "source-2",
            "title": "Page-separated text",
            "status": "indexing",
            "type": "text",
        },
    ]
    assert provider.calls == [("nb-managed",)]


def test_remote_sources_map_pinned_provider_status_enums():
    provider = FakeCollection(
        [
            Source(id="source-ready", title="Ready source", status=SourceStatus.READY),
            Source(
                id="source-processing",
                title="Processing source",
                status=SourceStatus.PROCESSING,
            ),
            Source(
                id="source-preparing",
                title="Preparing source",
                status=SourceStatus.PREPARING,
            ),
            Source(id="source-error", title="Failed source", status=SourceStatus.ERROR),
        ]
    )

    result = asyncio.run(
        list_actual_remote_sources(FakeClient(sources=provider), "nb-managed")
    )

    assert [item.status for item in result] == [
        "ready",
        "processing",
        "processing",
        "error",
    ]


def test_remote_source_preserves_terminal_provider_error_instead_of_processing():
    provider = FakeCollection(
        [
            SimpleNamespace(
                id="source-error-status",
                title="Failed source",
                is_ready=False,
                status=SimpleNamespace(value="error"),
                kind="text",
            ),
            SimpleNamespace(
                id="source-error-flag",
                title="Another failed source",
                is_ready=False,
                is_error=True,
                kind="text",
            ),
        ]
    )

    result = asyncio.run(
        list_actual_remote_sources(FakeClient(sources=provider), "nb-managed")
    )

    assert [item.status for item in result] == ["error", "error"]


def test_remote_notebook_inventory_skips_malformed_rows_without_logging_values(caplog):
    secret_id = "private-malformed-notebook-id"
    secret_title = "private-malformed-notebook-title"
    provider = FakeCollection(
        [
            SimpleNamespace(id="nb-1", title="Usable notebook"),
            SimpleNamespace(id=secret_id),
            SimpleNamespace(id="", title=secret_title),
        ]
    )

    result = asyncio.run(list_actual_remote_notebooks(FakeClient(notebooks=provider)))

    assert [item.id for item in result] == ["nb-1"]
    assert "Skipped malformed remote notebook records count=2" in caplog.text
    assert secret_id not in caplog.text
    assert secret_title not in caplog.text


def test_remote_source_inventory_remains_strict_for_malformed_rows():
    client = FakeClient(
        sources=FakeCollection(
            [
                SimpleNamespace(id="source-valid", title="Usable source", kind="pdf"),
                SimpleNamespace(id="source-malformed"),
            ]
        )
    )
    with pytest.raises(RemoteInventoryShapeError, match="no usable title"):
        asyncio.run(list_actual_remote_sources(client, "nb-managed"))


def test_remote_routes_return_narrow_payloads_from_fake_client():
    fake = FakeClient(
        notebooks=FakeCollection([SimpleNamespace(id="nb-1", title="Managed title")]),
        sources=FakeCollection(
            [SimpleNamespace(id="src-1", title="Held PDF", is_ready=True, kind="pdf")]
        ),
    )
    client = _client_for_routes(fake)

    notebooks = client.get("/api/remote/notebooks")
    assert notebooks.status_code == 200
    assert notebooks.json() == [
        {
            "id": "nb-1",
            "title": "Managed title",
            "status": "available",
            "type": "notebook",
        }
    ]

    sources = client.get("/api/remote/notebooks/nb-1/sources")
    assert sources.status_code == 200
    assert sources.json() == [
        {
            "id": "src-1",
            "title": "Held PDF",
            "status": "ready",
            "type": "pdf",
        }
    ]
    assert fake.notebooks.calls == [()]
    assert fake.sources.calls == [("nb-1",)]


def test_provider_failure_is_sanitized_in_response_and_logs(caplog):
    secret_marker = "private-provider-payload-must-not-escape"
    fake = FakeClient(
        notebooks=FakeCollection(error=RuntimeError(secret_marker)),
    )
    response = _client_for_routes(fake).get("/api/remote/notebooks")

    assert response.status_code == 502
    assert response.json() == {
        "detail": {
            "code": "remote_inventory_failed",
            "message": "NotebookLM could not return the requested remote inventory",
        }
    }
    assert secret_marker not in response.text
    assert secret_marker not in caplog.text
    assert "RuntimeError" in caplog.text


def test_missing_remote_client_has_a_sanitized_503(monkeypatch):
    async def no_client():
        return None

    monkeypatch.setattr(remote_routes, "get_notebooklm_client", no_client)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(remote_routes.require_remote_client())
    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "remote_notebooklm_unavailable"
