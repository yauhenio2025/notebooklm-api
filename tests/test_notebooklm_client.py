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
    installed: list[dict[str, int]] = []
    configured_frame_cap = 128 * MEBIBYTE
    configured_answer_cap = 2 * MEBIBYTE
    configured_citation_cap = 32 * MEBIBYTE
    configured_wire_cap = 512 * MEBIBYTE

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
        "install_chat_stream_reducer",
        lambda **limits: installed.append(limits),
    )
    monkeypatch.setattr(
        notebooklm_client,
        "get_settings",
        lambda: SimpleNamespace(
            notebooklm_chat_frame_max_bytes=configured_frame_cap,
            notebooklm_chat_answer_max_bytes=configured_answer_cap,
            notebooklm_chat_citation_max_bytes=configured_citation_cap,
            notebooklm_chat_wire_max_bytes=configured_wire_cap,
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
        "chat_response_max_bytes": configured_frame_cap,
    }
    assert installed == [
        {
            "wire_max_bytes": configured_wire_cap,
            "answer_max_bytes": configured_answer_cap,
            "citation_max_bytes": configured_citation_cap,
        }
    ]


def test_chat_frame_cap_is_environment_configurable_and_bounded(monkeypatch):
    field = Settings.model_fields["notebooklm_chat_frame_max_bytes"]
    assert field.default == 192 * MEBIBYTE

    for valid_value in (16 * MEBIBYTE, 128 * MEBIBYTE, 256 * MEBIBYTE):
        monkeypatch.setenv(
            "NOTEBOOKLM_CHAT_FRAME_MAX_BYTES",
            str(valid_value),
        )
        monkeypatch.setenv(
            "NOTEBOOKLM_CHAT_CITATION_MAX_BYTES",
            str(min(valid_value, 64 * MEBIBYTE)),
        )
        assert (
            Settings(_env_file=None).notebooklm_chat_frame_max_bytes
            == valid_value
        )

    for invalid_value in (16 * MEBIBYTE - 1, 256 * MEBIBYTE + 1):
        monkeypatch.setenv(
            "NOTEBOOKLM_CHAT_FRAME_MAX_BYTES",
            str(invalid_value),
        )
        with pytest.raises(ValidationError):
            Settings(_env_file=None)


def test_chat_wire_cap_is_configured_separately_from_one_frame(monkeypatch):
    field = Settings.model_fields["notebooklm_chat_wire_max_bytes"]
    assert field.default == 1024 * MEBIBYTE

    monkeypatch.setenv("NOTEBOOKLM_CHAT_FRAME_MAX_BYTES", str(128 * MEBIBYTE))
    monkeypatch.setenv("NOTEBOOKLM_CHAT_WIRE_MAX_BYTES", str(512 * MEBIBYTE))
    configured = Settings(_env_file=None)
    assert configured.notebooklm_chat_frame_max_bytes == 128 * MEBIBYTE
    assert configured.notebooklm_chat_wire_max_bytes == 512 * MEBIBYTE

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
