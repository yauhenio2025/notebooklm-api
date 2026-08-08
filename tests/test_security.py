"""Consumer-auth, OpenAPI, and CORS regression tests."""

import asyncio

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from src.config import Settings, get_settings
from src.main import app
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


def test_http_gate_reads_x_api_key_and_never_has_an_unconfigured_bypass():
    test_app = FastAPI()
    configured = _settings(key="ganrl-consumer-secret")
    test_app.dependency_overrides[get_settings] = lambda: configured

    @test_app.get("/api/private", dependencies=[Depends(require_consumer_api_key)])
    async def private_route():
        return {"ok": True}

    client = TestClient(test_app)
    assert client.get("/api/private").status_code == 401
    assert client.get(
        "/api/private", headers={"X-API-Key": "wrong"}
    ).status_code == 401
    assert client.get(
        "/api/private", headers={"X-API-Key": "ganrl-consumer-secret"}
    ).json() == {"ok": True}

    test_app.dependency_overrides[get_settings] = lambda: _settings()
    assert client.get(
        "/api/private", headers={"X-API-Key": "anything"}
    ).status_code == 503


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
