"""NotebookLM client construction safety tests."""

import asyncio
import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src import notebooklm_client
from src.config import MEBIBYTE, Settings


def test_client_passes_response_cap_and_disables_hidden_retries(monkeypatch, tmp_path):
    storage_path = tmp_path / "storage_state.json"
    storage_path.write_text("{}", encoding="utf-8")
    captured: dict[str, object] = {}
    configured_response_cap = 8 * MEBIBYTE

    class FakeTransport:
        async def perform_authed_post(self, **_kwargs: object):
            return object()

    class FakeClient:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(_transport=FakeTransport())

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object):
            return None

    class FakeNotebookLMClient:
        @classmethod
        async def from_storage(cls, path: str, **kwargs: object):
            captured["path"] = path
            captured.update(kwargs)
            return FakeClient()

    monkeypatch.setitem(
        sys.modules,
        "notebooklm",
        SimpleNamespace(NotebookLMClient=FakeNotebookLMClient),
    )
    monkeypatch.setattr(
        notebooklm_client,
        "_profile_paths",
        lambda: (storage_path, tmp_path / "master_token.json"),
    )
    monkeypatch.setattr(notebooklm_client, "seed_profile_from_secret", lambda: None)
    monkeypatch.setattr(
        notebooklm_client,
        "get_settings",
        lambda: SimpleNamespace(
            notebooklm_chat_response_max_bytes=configured_response_cap
        ),
    )
    monkeypatch.setattr(notebooklm_client, "_client", None)
    monkeypatch.setattr(notebooklm_client, "_client_initialized", False)

    async def scenario():
        assert await notebooklm_client.get_notebooklm_client() is not None
        await notebooklm_client.close_client()

    asyncio.run(scenario())

    assert captured == {
        "path": str(storage_path),
        "rate_limit_max_retries": 0,
        "server_error_max_retries": 0,
        "chat_response_max_bytes": configured_response_cap,
    }


def test_chat_response_cap_is_environment_configurable_and_bounded(monkeypatch):
    field = Settings.model_fields["notebooklm_chat_response_max_bytes"]
    assert field.default == 32 * MEBIBYTE

    for valid_value in (MEBIBYTE, 8 * MEBIBYTE, 64 * MEBIBYTE):
        monkeypatch.setenv(
            "NOTEBOOKLM_CHAT_RESPONSE_MAX_BYTES",
            str(valid_value),
        )
        assert (
            Settings(_env_file=None).notebooklm_chat_response_max_bytes
            == valid_value
        )

    for invalid_value in (MEBIBYTE - 1, 64 * MEBIBYTE + 1):
        monkeypatch.setenv(
            "NOTEBOOKLM_CHAT_RESPONSE_MAX_BYTES",
            str(invalid_value),
        )
        with pytest.raises(ValidationError):
            Settings(_env_file=None)


def test_chat_transport_forces_auth_refresh_replay_off():
    calls: list[dict[str, object]] = []

    class FakeTransport:
        async def perform_authed_post(self, **kwargs: object):
            calls.append(dict(kwargs))
            return "response"

    client = SimpleNamespace(chat=SimpleNamespace(_transport=FakeTransport()))
    notebooklm_client._disable_chat_internal_retries(client)

    async def scenario():
        return await client.chat._transport.perform_authed_post(
            build_request="builder",
            log_label="chat.ask",
            disable_internal_retries=False,
            disable_read_timeout_retries=True,
        )

    assert asyncio.run(scenario()) == "response"
    assert calls == [
        {
            "build_request": "builder",
            "log_label": "chat.ask",
            "disable_internal_retries": True,
            "disable_read_timeout_retries": True,
        }
    ]


def test_chat_transport_contract_drift_fails_closed():
    client = SimpleNamespace(chat=SimpleNamespace())

    try:
        notebooklm_client._disable_chat_internal_retries(client)
    except RuntimeError as exc:
        assert str(exc) == "NotebookLM chat transport contract changed"
    else:  # pragma: no cover - explicit failure message for contract drift
        raise AssertionError("missing chat transport must fail initialization")
