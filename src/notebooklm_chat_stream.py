"""Bound NotebookLM chat memory without confusing wire traffic with the answer.

Google's ``GenerateFreeFormStreamed`` endpoint sends progressive snapshots. A
later snapshot repeats the answer-so-far, its citations, and additional support
state. The pinned SDK normally buffers every snapshot and only then keeps the
longest answer. That makes cumulative transport traffic look like one enormous
answer and retains all superseded copies in memory.

This module installs a narrow transport shim for that one endpoint. It reads
one newline-delimited frame at a time, retains only the same winning frame the
SDK parser would choose, and discards superseded snapshots immediately. The
SDK still performs the authoritative final parse, including citation text.
Other NotebookLM RPCs continue through the SDK's original transport unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from notebooklm._chat.wire import _extract_chunk_with_parseable
from notebooklm.exceptions import RPCResponseTooLargeError

logger = logging.getLogger(__name__)

_CHAT_ENDPOINT_SUFFIX = "/GenerateFreeFormStreamed"
_ANTI_XSSI = b")]}'"
_DECODED_CHUNK_BYTES = 64 * 1024
_STRIP_REBUFFERED_HEADERS = frozenset({"content-encoding", "content-length"})

_OriginalStreamPost = Callable[..., Awaitable[httpx.Response]]
_original_stream_post: _OriginalStreamPost | None = None
_wire_max_bytes: int | None = None
_answer_max_bytes: int | None = None
_citation_max_bytes: int | None = None


@dataclass(frozen=True)
class _RetainedFrame:
    raw_line: bytes
    answer_chars: int
    answer_bytes: int
    citation_text_bytes: int
    citation_count: int


class _ChatFrameReducer:
    """Retain only the frame the pinned SDK's longest-answer rule would use."""

    def __init__(self, frame_max_bytes: int) -> None:
        self.frame_max_bytes = frame_max_bytes
        self.frame_count = 0
        self.parseable_frame_count = 0
        self.max_frame_bytes = 0
        self.best_marked: _RetainedFrame | None = None
        self.best_unmarked: _RetainedFrame | None = None
        self.first_parseable_empty: bytes | None = None
        self.last_unparseable: bytes | None = None

    def accept_declared_length(self, declared_bytes: int) -> None:
        if declared_bytes > self.frame_max_bytes:
            raise RPCResponseTooLargeError(
                "NotebookLM chat frame exceeded the retained-frame safety limit "
                f"of {self.frame_max_bytes} bytes (declared {declared_bytes} bytes)",
                limit_bytes=self.frame_max_bytes,
                bytes_read=declared_bytes,
            )

    def accept_frame(self, raw_line: bytes) -> None:
        frame_bytes = len(raw_line)
        if frame_bytes > self.frame_max_bytes:
            raise RPCResponseTooLargeError(
                "NotebookLM chat frame exceeded the retained-frame safety limit "
                f"of {self.frame_max_bytes} bytes (read {frame_bytes} bytes)",
                limit_bytes=self.frame_max_bytes,
                bytes_read=frame_bytes,
            )

        self.frame_count += 1
        self.max_frame_bytes = max(self.max_frame_bytes, frame_bytes)
        decoded = raw_line.decode("utf-8", errors="replace")
        text, is_answer, refs, _conversation_id, parseable = (
            _extract_chunk_with_parseable(decoded)
        )
        if not parseable:
            # Keep only one bounded diagnostic sample. The SDK's final parser
            # will turn it into its normal wire-drift error if no valid frame
            # ever arrives.
            if self.parseable_frame_count == 0:
                self.last_unparseable = raw_line
            return

        self.parseable_frame_count += 1
        self.last_unparseable = None
        if not text:
            if self.winner is None and self.first_parseable_empty is None:
                self.first_parseable_empty = raw_line
            return

        self.first_parseable_empty = None
        retained = _RetainedFrame(
            raw_line=raw_line,
            answer_chars=len(text),
            answer_bytes=len(text.encode("utf-8")),
            citation_text_bytes=sum(
                len(ref.cited_text.encode("utf-8"))
                for ref in refs
                if ref.cited_text is not None
            ),
            citation_count=len(refs),
        )
        current = self.best_marked if is_answer else self.best_unmarked
        # Match notebooklm-py's parser exactly: equal-length finalization
        # snapshots do not replace the first winning frame.
        if current is None or retained.answer_chars > current.answer_chars:
            if is_answer:
                self.best_marked = retained
                self.best_unmarked = None
            else:
                if self.best_marked is None:
                    self.best_unmarked = retained

    @property
    def winner(self) -> _RetainedFrame | None:
        return self.best_marked or self.best_unmarked

    def synthetic_body(self) -> bytes:
        winner = self.winner
        if winner is not None:
            raw_line = winner.raw_line
        elif self.first_parseable_empty is not None:
            raw_line = self.first_parseable_empty
        elif self.last_unparseable is not None:
            raw_line = self.last_unparseable
        else:
            return _ANTI_XSSI + b"\n"
        return b"".join(
            (
                _ANTI_XSSI,
                b"\n",
                str(len(raw_line)).encode("ascii"),
                b"\n",
                raw_line,
                b"\n",
            )
        )


def install_chat_stream_reducer(
    *,
    wire_max_bytes: int,
    answer_max_bytes: int,
    citation_max_bytes: int,
) -> None:
    """Install the reducer at the pinned SDK kernel seam, idempotently."""
    global _answer_max_bytes, _citation_max_bytes
    global _original_stream_post, _wire_max_bytes

    if min(wire_max_bytes, answer_max_bytes, citation_max_bytes) <= 0:
        raise ValueError("NotebookLM chat stream limits must be positive")

    from notebooklm import _kernel

    current = _kernel.stream_post_with_size_cap
    if current is chat_aware_stream_post_with_size_cap:
        _wire_max_bytes = wire_max_bytes
        _answer_max_bytes = answer_max_bytes
        _citation_max_bytes = citation_max_bytes
        return
    if not callable(current) or current.__module__ != "notebooklm._streaming_post":
        raise RuntimeError("NotebookLM streaming transport contract changed")

    _original_stream_post = current
    _wire_max_bytes = wire_max_bytes
    _answer_max_bytes = answer_max_bytes
    _citation_max_bytes = citation_max_bytes
    _kernel.stream_post_with_size_cap = chat_aware_stream_post_with_size_cap


async def chat_aware_stream_post_with_size_cap(
    client: httpx.AsyncClient,
    url: str,
    *,
    body: Any,
    headers: dict[str, str] | None,
    timeout: httpx.Timeout | float | None = None,
    max_bytes: int | None = None,
) -> httpx.Response:
    """Reduce progressive chat frames; delegate every other RPC unchanged."""
    if not httpx.URL(url).path.rstrip("/").endswith(_CHAT_ENDPOINT_SUFFIX):
        if _original_stream_post is None:
            raise RuntimeError("NotebookLM chat stream reducer is not installed")
        return await _original_stream_post(
            client,
            url,
            body=body,
            headers=headers,
            timeout=timeout,
            max_bytes=max_bytes,
        )

    if max_bytes is None:
        raise RuntimeError("NotebookLM chat frame limit is not configured")
    if (
        _wire_max_bytes is None
        or _answer_max_bytes is None
        or _citation_max_bytes is None
    ):
        raise RuntimeError("NotebookLM chat stream limits are not configured")
    if _wire_max_bytes < max_bytes:
        raise RuntimeError("NotebookLM chat wire limit is smaller than its frame limit")

    return await _reduce_chat_stream(
        client,
        url,
        body=body,
        headers=headers,
        timeout=timeout,
        frame_max_bytes=max_bytes,
        wire_max_bytes=_wire_max_bytes,
        answer_max_bytes=_answer_max_bytes,
        citation_max_bytes=_citation_max_bytes,
    )


async def _reduce_chat_stream(
    client: httpx.AsyncClient,
    url: str,
    *,
    body: Any,
    headers: dict[str, str] | None,
    timeout: httpx.Timeout | float | None,
    frame_max_bytes: int,
    wire_max_bytes: int,
    answer_max_bytes: int,
    citation_max_bytes: int,
) -> httpx.Response:
    if not isinstance(client, httpx.AsyncClient):
        # The pinned runtime uses HTTPX. Refuse an optional transport swap
        # before submitting the non-idempotent query because its iterator may
        # not support bounded decoded chunks.
        raise RuntimeError("NotebookLM chat reducer requires the HTTPX transport")
    stream_kwargs: dict[str, Any] = {"content": body}
    if headers:
        stream_kwargs["headers"] = headers
    if timeout is not None:
        stream_kwargs["timeout"] = timeout

    reducer = _ChatFrameReducer(frame_max_bytes)
    total_wire_bytes = 0
    line_buffer = bytearray()

    async with client.stream("POST", url, **stream_kwargs) as response:
        response.raise_for_status()

        def process_line(raw_line: bytes) -> None:
            line = raw_line.rstrip(b"\r").strip()
            if not line or line == _ANTI_XSSI:
                return
            if line.isdigit():
                reducer.accept_declared_length(int(line))
                return
            reducer.accept_frame(line)

        async for chunk in response.aiter_bytes(chunk_size=_DECODED_CHUNK_BYTES):
            total_wire_bytes += len(chunk)
            if total_wire_bytes > wire_max_bytes:
                raise RPCResponseTooLargeError(
                    "NotebookLM chat wire stream exceeded its runaway safety limit "
                    f"of {wire_max_bytes} bytes (read {total_wire_bytes} bytes); "
                    "this is transport traffic, not answer size",
                    limit_bytes=wire_max_bytes,
                    bytes_read=total_wire_bytes,
                )

            start = 0
            while True:
                newline = chunk.find(b"\n", start)
                if newline < 0:
                    line_buffer.extend(chunk[start:])
                    if len(line_buffer) > frame_max_bytes:
                        raise RPCResponseTooLargeError(
                            "NotebookLM chat frame exceeded the "
                            "retained-frame safety limit "
                            f"of {frame_max_bytes} bytes while streaming",
                            limit_bytes=frame_max_bytes,
                            bytes_read=len(line_buffer),
                        )
                    break
                line_buffer.extend(chunk[start:newline])
                raw_line = bytes(line_buffer)
                line_buffer.clear()
                process_line(raw_line)
                start = newline + 1

        if line_buffer:
            raw_line = bytes(line_buffer)
            line_buffer.clear()
            process_line(raw_line)

        winner = reducer.winner
        if winner is not None and winner.answer_bytes > answer_max_bytes:
            raise RPCResponseTooLargeError(
                "NotebookLM prose answer exceeded its separate safety limit "
                f"of {answer_max_bytes} bytes (read {winner.answer_bytes} bytes)",
                limit_bytes=answer_max_bytes,
                bytes_read=winner.answer_bytes,
            )
        if winner is not None and winner.citation_text_bytes > citation_max_bytes:
            raise RPCResponseTooLargeError(
                "NotebookLM citation passages exceeded their separate safety limit "
                f"of {citation_max_bytes} bytes "
                f"(read {winner.citation_text_bytes} bytes)",
                limit_bytes=citation_max_bytes,
                bytes_read=winner.citation_text_bytes,
            )
        retained_bytes = len(winner.raw_line) if winner is not None else 0
        discarded_bytes = max(0, total_wire_bytes - retained_bytes)
        logger.info(
            "Reduced NotebookLM chat stream wire_bytes=%d frame_count=%d "
            "max_frame_bytes=%d retained_frame_bytes=%d discarded_bytes=%d "
            "answer_bytes=%d citation_count=%d citation_text_bytes=%d",
            total_wire_bytes,
            reducer.frame_count,
            reducer.max_frame_bytes,
            retained_bytes,
            discarded_bytes,
            winner.answer_bytes if winner is not None else 0,
            winner.citation_count if winner is not None else 0,
            winner.citation_text_bytes if winner is not None else 0,
        )

        rebuilt_headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() not in _STRIP_REBUFFERED_HEADERS
        }
        return httpx.Response(
            status_code=response.status_code,
            headers=rebuilt_headers,
            content=reducer.synthetic_body(),
            request=response.request,
        )
