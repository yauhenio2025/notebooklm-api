"""NotebookLM client construction safety tests."""

import asyncio
import sys
from types import SimpleNamespace

from src import notebooklm_client


def test_client_disables_hidden_provider_retries(monkeypatch, tmp_path):
    storage_path = tmp_path / "storage_state.json"
    storage_path.write_text("{}", encoding="utf-8")
    captured: dict[str, object] = {}

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
    }


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
