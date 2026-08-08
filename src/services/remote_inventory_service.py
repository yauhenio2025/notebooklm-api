"""Read the actual Google NotebookLM inventory without consulting local rows.

These functions are deliberately read-only. They exist to reconcile the small
crash window in which Google accepted a notebook/source mutation but the
wrapper process died before committing the corresponding PostgreSQL record.
"""

from collections.abc import Mapping
from typing import Any

from src.schemas import RemoteNotebookResponse, RemoteSourceResponse


class RemoteInventoryShapeError(RuntimeError):
    """The provider returned an object that cannot satisfy the narrow contract."""


def _field(item: object, name: str) -> object | None:
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, None)


def _required_text(item: object, name: str) -> str:
    value = _field(item, name)
    if not isinstance(value, str) or not value.strip():
        raise RemoteInventoryShapeError(f"remote item has no usable {name}")
    return value.strip()


def _label(value: object | None, *, default: str) -> str:
    """Turn provider enums/scalars into a bounded, display-safe label."""
    if value is None:
        return default
    enum_value = getattr(value, "value", value)
    if not isinstance(enum_value, (str, int, bool)):
        return default
    label = " ".join(str(enum_value).split()).strip()
    return label[:128] or default


def _readiness(item: object, *, default: str) -> str:
    is_ready = _field(item, "is_ready")
    if is_ready is True:
        return "ready"
    if is_ready is False:
        return "processing"
    return _label(_field(item, "status"), default=default)


async def list_actual_remote_notebooks(client: Any) -> list[RemoteNotebookResponse]:
    """List notebooks from Google through notebooklm-py, bypassing wrapper DB."""
    remote_items = await client.notebooks.list()
    return [
        RemoteNotebookResponse(
            id=_required_text(item, "id"),
            title=_required_text(item, "title"),
            status=_readiness(item, default="available"),
            type="notebook",
        )
        for item in remote_items
    ]


async def list_actual_remote_sources(
    client: Any,
    notebook_id: str,
) -> list[RemoteSourceResponse]:
    """List one notebook's sources from Google, bypassing wrapper DB."""
    remote_items = await client.sources.list(notebook_id)
    responses: list[RemoteSourceResponse] = []
    for item in remote_items:
        source_type = _field(item, "kind")
        if source_type is None:
            source_type = _field(item, "type")
        responses.append(
            RemoteSourceResponse(
                id=_required_text(item, "id"),
                title=_required_text(item, "title"),
                status=_readiness(item, default="unknown"),
                type=_label(source_type, default="unknown"),
            )
        )
    return responses
