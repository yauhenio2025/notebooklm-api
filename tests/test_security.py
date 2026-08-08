"""Consumer-auth, OpenAPI, and CORS regression tests."""

import asyncio
import logging

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src import main as app_main
from src import notebooklm_client
from src.config import Settings, get_settings
from src.main import SENSITIVE_TRANSPORT_LOGGERS, app, suppress_sensitive_transport_logs
from src.security import api_keys_match, require_consumer_api_key


def _settings(*, key: str = "", origins: str = "") -> Settings:
    return Settings(
        notebooklm_api_key=key,
        cors_allowed_origins=origins,
        _env_file=None,
    )


def test_api_key_comparison_accepts_only_the_exact_key():
    assert api_keys_match("ganrl-consumer-secret", "ganrl-consumer-secret")
    assert not api_keys_match("ganrl-consumer-secreu", "ganrl-consumer-secret")
    assert not api_keys_match("", "ganrl-consumer-secret")
    assert not api_keys_match("longer-ganrl-consumer-secret", "ganrl-consumer-secret")


def test_consumer_auth_fails_closed_when_key_is_not_configured():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(require_consumer_api_key("anything", _settings()))
    assert exc.value.status_code == 503


def test_consumer_auth_rejects_missing_and_wrong_keys():
    configured = _settings(key="ganrl-consumer-secret")
    for supplied in (None, "", "wrong"):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(require_consumer_api_key(supplied, configured))
        assert exc.value.status_code == 401


def test_consumer_auth_accepts_the_configured_key():
    result = asyncio.run(
        require_consumer_api_key(
            "ganrl-consumer-secret",
            _settings(key="ganrl-consumer-secret"),
        )
    )
    assert result is None


def test_tracked_configuration_defaults_contain_no_live_credentials():
    database_default = Settings.model_fields["database_url"].default
    zotero_default = Settings.model_fields["zotero_api_key"].default

    assert database_default == "postgresql://localhost:5432/notebooklm"
    assert "@" not in database_default
    assert zotero_default == ""


def test_batch_query_timeout_is_environment_configurable_and_bounded(monkeypatch):
    timeout_field = Settings.model_fields["notebooklm_query_timeout_seconds"]
    assert timeout_field.default == 1200

    monkeypatch.setenv("NOTEBOOKLM_QUERY_TIMEOUT_SECONDS", "900")
    assert Settings(_env_file=None).notebooklm_query_timeout_seconds == 900

    for valid_timeout in (60, 3600):
        assert (
            Settings(
                notebooklm_query_timeout_seconds=valid_timeout,
                _env_file=None,
            ).notebooklm_query_timeout_seconds
            == valid_timeout
        )

    for invalid_timeout in (59, 3601):
        with pytest.raises(ValidationError):
            Settings(
                notebooklm_query_timeout_seconds=invalid_timeout,
                _env_file=None,
            )


def test_startup_runs_batch_recovery_after_database_initialization(monkeypatch):
    events: list[str] = []

    async def init_db():
        events.append("database_initialized")

    async def recover():
        events.append("batch_recovered")
        return 0

    async def schedule_pending():
        events.append("pending_scheduled")
        return 0

    async def supervise(_stop: asyncio.Event):
        events.append("recovery_supervisor_started")
        try:
            await asyncio.Event().wait()
        finally:
            events.append("recovery_supervisor_stopped")

    async def shutdown_batches():
        events.append("batch_tasks_stopped")
        return 0

    async def close_client():
        events.append("client_closed")

    async def close_db():
        events.append("database_closed")

    monkeypatch.setattr(app_main, "init_db", init_db)
    monkeypatch.setattr(app_main, "recover_orphaned_batch_queries", recover)
    monkeypatch.setattr(app_main, "schedule_pending_batch_queries", schedule_pending)
    monkeypatch.setattr(app_main, "run_batch_recovery_supervisor", supervise)
    monkeypatch.setattr(app_main, "shutdown_batch_tasks", shutdown_batches)
    monkeypatch.setattr(notebooklm_client, "close_client", close_client)
    monkeypatch.setattr(app_main, "close_db", close_db)

    async def scenario():
        async with app_main.lifespan(app_main.app):
            await asyncio.sleep(0)
            assert events == [
                "database_initialized",
                "batch_recovered",
                "pending_scheduled",
                "recovery_supervisor_started",
            ]

    asyncio.run(scenario())
    assert events == [
        "database_initialized",
        "batch_recovered",
        "pending_scheduled",
        "recovery_supervisor_started",
        "recovery_supervisor_stopped",
        "batch_tasks_stopped",
        "client_closed",
        "database_closed",
    ]


def test_sensitive_transport_request_logging_is_suppressed():
    for logger_name in SENSITIVE_TRANSPORT_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.INFO)

    suppress_sensitive_transport_logs()

    assert all(
        logging.getLogger(logger_name).level == logging.WARNING
        for logger_name in SENSITIVE_TRANSPORT_LOGGERS
    )


def test_http_gate_reads_x_api_key_and_never_has_an_unconfigured_bypass():
    test_app = FastAPI()
    configured = _settings(key="ganrl-consumer-secret")
    test_app.dependency_overrides[get_settings] = lambda: configured

    @test_app.get("/api/private", dependencies=[Depends(require_consumer_api_key)])
    async def private_route():
        return {"ok": True}

    client = TestClient(test_app)
    assert client.get("/api/private").status_code == 401
    assert client.get("/api/private", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get(
        "/api/private", headers={"X-API-Key": "ganrl-consumer-secret"}
    ).json() == {"ok": True}

    test_app.dependency_overrides[get_settings] = lambda: _settings()
    assert (
        client.get("/api/private", headers={"X-API-Key": "anything"}).status_code == 503
    )


def test_openapi_marks_status_and_every_api_operation_as_secured():
    schema = app.openapi()
    scheme = schema["components"]["securitySchemes"]["NotebookLMConsumerKey"]
    assert scheme["type"] == "apiKey"
    assert scheme["in"] == "header"
    assert scheme["name"] == "X-API-Key"

    assert "security" not in schema["paths"]["/health"]["get"]
    assert schema["paths"]["/status"]["get"]["security"] == [
        {"NotebookLMConsumerKey": []}
    ]

    for path, path_item in schema["paths"].items():
        if not path.startswith("/api/"):
            continue
        for operation in path_item.values():
            assert operation["security"] == [{"NotebookLMConsumerKey": []}], path


def _cors_client(origins: str) -> TestClient:
    test_app = FastAPI()
    test_app.add_middleware(
        CORSMiddleware,
        allow_origins=_settings(origins=origins).cors_allowed_origins_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-API-Key"],
    )

    @test_app.get("/health")
    async def health():
        return {"status": "ok"}

    return TestClient(test_app)


def test_cors_allows_only_an_explicit_origin():
    client = _cors_client("https://ganrl.example")
    headers = {
        "Origin": "https://ganrl.example",
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "X-API-Key",
    }
    allowed = client.options("/health", headers=headers)
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "https://ganrl.example"

    denied = client.options(
        "/health",
        headers={**headers, "Origin": "https://attacker.example"},
    )
    assert denied.status_code == 400
    assert "access-control-allow-origin" not in denied.headers


def test_cors_is_closed_by_default_and_rejects_wildcards():
    client = _cors_client("")
    response = client.options(
        "/health",
        headers={
            "Origin": "https://ganrl.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers

    with pytest.raises(ValueError, match="may not contain"):
        _ = _settings(origins="*").cors_allowed_origins_list
