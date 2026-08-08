"""NotebookLM client construction safety tests."""

import asyncio
import sys
from types import SimpleNamespace

from src import notebooklm_client


def test_client_disables_hidden_provider_retries(monkeypatch, tmp_path):
    storage_path = tmp_path / "storage_state.json"
    storage_path.write_text("{}", encoding="utf-8")
    captured: dict[str, object] = {}

    class FakeClient:
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
