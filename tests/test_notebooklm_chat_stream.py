"""NotebookLM progressive chat stream reduction tests."""

import asyncio
import json

import httpx
import pytest
from notebooklm._chat.wire import parse_streaming_chat_response
from notebooklm.exceptions import ChatError, RPCResponseTooLargeError

from src import notebooklm_chat_stream
from src.notebooklm_chat_stream import _ChatFrameReducer, _reduce_chat_stream

SOURCE_ID = "11111111-1111-1111-1111-111111111111"


def _answer_frame(answer: str, cited_text: str, *, marked: bool = True) -> bytes:
    text_payload = [[[None, None, cited_text]]]
    passages = [[[10, 10 + len(cited_text), text_payload]]]
    detail = [
        None,
        None,
        0.9,
        [[None, 0, len(answer)]],
        passages,
        [[SOURCE_ID]],
    ]
    citation = [["chunk-1"], detail]
    type_block = [None, None, None, [citation], 1 if marked else 0]
    first = [answer, None, ["stream-id"], None, type_block]
    inner = json.dumps([first], separators=(",", ":"), ensure_ascii=False)
    outer = [["wrb.fr", None, inner]]
    return json.dumps(outer, separators=(",", ":"), ensure_ascii=False).encode()


def _error_frame() -> bytes:
    return json.dumps([["er", "rpc-id", 13]], separators=(",", ":")).encode()


def _empty_frame() -> bytes:
    return json.dumps(
        [["wrb.fr", None, json.dumps([])]], separators=(",", ":")
    ).encode()


def _wire_body(*frames: bytes) -> bytes:
    parts = [b")]}'\n"]
    for frame in frames:
        parts.extend((str(len(frame)).encode(), b"\n", frame, b"\n"))
    return b"".join(parts)


class _FakeResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self.status_code = 200
        self.headers = {
            "content-type": "application/json; charset=utf-8",
            "content-encoding": "gzip",
            "content-length": "123",
            "x-test": "kept",
        }
        self.request = httpx.Request("POST", "https://example.invalid/chat")
        self._chunks = chunks
        self.requested_chunk_size = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self, chunk_size=None):
        self.requested_chunk_size = chunk_size
        for chunk in self._chunks:
            yield chunk


class _FakeClient(httpx.AsyncClient):
    def __init__(self, chunks: list[bytes]) -> None:
        self.response = _FakeResponse(chunks)

    def stream(self, *_args, **_kwargs):
        return self.response


def test_progressive_stream_exceeds_frame_cap_cumulatively_and_keeps_citations():
    early = _answer_frame("Arríghi", "superseded passage")
    winner = _answer_frame("Arríghi's final answer", "kept source passage")
    # Repeated cumulative snapshots push total traffic above the per-frame cap,
    # and seven-byte chunks split both framing and UTF-8 code points.
    body = _wire_body(*([early] * 30), winner, winner, winner)
    frame_cap = max(len(early), len(winner)) + 32
    assert len(body) > frame_cap
    chunks = [body[index : index + 7] for index in range(0, len(body), 7)]
    client = _FakeClient(chunks)

    async def scenario():
        return await _reduce_chat_stream(
            client,
            "https://notebooklm.google.test/GenerateFreeFormStreamed",
            body="request",
            headers={"x-test": "request"},
            timeout=None,
            frame_max_bytes=frame_cap,
            wire_max_bytes=len(body) + 1,
            answer_max_bytes=4096,
            citation_max_bytes=4096,
        )

    response = asyncio.run(scenario())
    parsed = parse_streaming_chat_response(response.text)

    assert parsed.answer == "Arríghi's final answer"
    assert len(parsed.references) == 1
    assert parsed.references[0].cited_text == "kept source passage"
    assert "superseded passage" not in response.text
    assert len(response.content) < frame_cap + 64
    assert response.headers["x-test"] == "kept"
    assert "content-encoding" not in response.headers
    assert response.headers["content-length"] == str(len(response.content))
    assert client.response.requested_chunk_size == 64 * 1024


def test_late_error_frame_is_not_discarded_as_a_losing_snapshot():
    body = _wire_body(_answer_frame("usable answer", "passage"), _error_frame())

    async def scenario():
        return await _reduce_chat_stream(
            _FakeClient([body]),
            "https://notebooklm.google.test/GenerateFreeFormStreamed",
            body="request",
            headers=None,
            timeout=None,
            frame_max_bytes=4096,
            wire_max_bytes=8192,
            answer_max_bytes=4096,
            citation_max_bytes=4096,
        )

    with pytest.raises(ChatError):
        asyncio.run(scenario())


def test_total_wire_safety_ceiling_remains_distinct_from_frame_limit():
    frame = _answer_frame("short answer", "short passage")
    body = _wire_body(frame, frame, frame)

    async def scenario():
        return await _reduce_chat_stream(
            _FakeClient([body]),
            "https://notebooklm.google.test/GenerateFreeFormStreamed",
            body="request",
            headers=None,
            timeout=None,
            frame_max_bytes=len(frame) + 10,
            wire_max_bytes=len(body) - 1,
            answer_max_bytes=4096,
            citation_max_bytes=4096,
        )

    with pytest.raises(RPCResponseTooLargeError, match="transport traffic"):
        asyncio.run(scenario())


def test_declared_single_frame_limit_is_still_enforced():
    reducer = _ChatFrameReducer(frame_max_bytes=100)

    with pytest.raises(RPCResponseTooLargeError, match="retained-frame"):
        reducer.accept_declared_length(101)


def test_winner_selection_matches_sdk_character_count_not_utf8_byte_count():
    reducer = _ChatFrameReducer(frame_max_bytes=4096)
    reducer.accept_frame(_answer_frame("é", "first passage"))
    reducer.accept_frame(_answer_frame("a", "equal-length later passage"))

    parsed = parse_streaming_chat_response(reducer.synthetic_body().decode())
    assert parsed.answer == "é"
    assert parsed.references[0].cited_text == "first passage"


def test_real_query_string_url_activates_reducer(monkeypatch):
    frame = _answer_frame("small final answer", "separate citation")
    body = _wire_body(*([frame] * 20))
    client = _FakeClient([body])
    monkeypatch.setattr(notebooklm_chat_stream, "_wire_max_bytes", len(body) + 1)
    monkeypatch.setattr(notebooklm_chat_stream, "_answer_max_bytes", 4096)
    monkeypatch.setattr(notebooklm_chat_stream, "_citation_max_bytes", 4096)

    async def scenario():
        return await notebooklm_chat_stream.chat_aware_stream_post_with_size_cap(
            client,
            "https://notebooklm.google.com/service/GenerateFreeFormStreamed"
            "?bl=boq_labs-tailwind-ui_20260807.16_p0&_reqid=12345&rt=c",
            body="request",
            headers=None,
            max_bytes=len(frame) + 32,
        )

    response = asyncio.run(scenario())
    parsed = parse_streaming_chat_response(response.text)
    assert parsed.answer == "small final answer"
    assert parsed.references[0].cited_text == "separate citation"
    assert len(response.content) < len(body)


def test_non_chat_url_delegates_to_original_transport(monkeypatch):
    calls = []
    expected = httpx.Response(200, content=b"delegated")

    async def original(*args, **kwargs):
        calls.append((args, kwargs))
        return expected

    monkeypatch.setattr(notebooklm_chat_stream, "_original_stream_post", original)

    async def scenario():
        return await notebooklm_chat_stream.chat_aware_stream_post_with_size_cap(
            object(),
            "https://notebooklm.google.com/service/OtherRpc?rt=c",
            body="request",
            headers={"x-test": "yes"},
            max_bytes=123,
        )

    assert asyncio.run(scenario()) is expected
    assert len(calls) == 1
    assert calls[0][1]["max_bytes"] == 123


@pytest.mark.parametrize(
    ("answer_limit", "citation_limit", "message"),
    ((4, 4096, "prose answer"), (4096, 4, "citation passages")),
)
def test_answer_and_citation_limits_are_enforced_separately(
    answer_limit, citation_limit, message
):
    frame = _answer_frame("long answer", "long citation")
    body = _wire_body(frame)

    async def scenario():
        return await _reduce_chat_stream(
            _FakeClient([body]),
            "https://notebooklm.google.test/GenerateFreeFormStreamed",
            body="request",
            headers=None,
            timeout=None,
            frame_max_bytes=4096,
            wire_max_bytes=8192,
            answer_max_bytes=answer_limit,
            citation_max_bytes=citation_limit,
        )

    with pytest.raises(RPCResponseTooLargeError, match=message):
        asyncio.run(scenario())


def test_reducer_releases_obsolete_fallback_frames():
    reducer = _ChatFrameReducer(frame_max_bytes=4096)
    reducer.accept_frame(b"not-json")
    assert reducer.last_unparseable is not None

    reducer.accept_frame(_empty_frame())
    assert reducer.last_unparseable is None
    assert reducer.first_parseable_empty is not None

    reducer.accept_frame(_answer_frame("unmarked", "context", marked=False))
    assert reducer.first_parseable_empty is None
    assert reducer.best_unmarked is not None

    reducer.accept_frame(_answer_frame("marked", "context"))
    assert reducer.best_marked is not None
    assert reducer.best_unmarked is None
    assert reducer.first_parseable_empty is None
    assert reducer.last_unparseable is None

    reducer.accept_frame(b"\xff")
    assert reducer.last_unparseable is None
