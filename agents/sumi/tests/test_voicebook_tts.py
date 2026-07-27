"""Tests for the Slice-5 voicebook-stream TTS adapter.

Load-bearing here: the adapter POSTs exactly {voice_id, text} to /speak/stream,
maps the raw s16le PCM to 24 kHz mono frames through the LiveKit AudioEmitter,
fails loud on an empty voice_id, and turns an HTTP error into an APIStatusError
(never a silent empty turn). These run WITHOUT the live service — a fake aiohttp
session stands in — so CI protects the contract the live seam test proved.
"""

import asyncio
import struct
from typing import cast

import aiohttp
import pytest
from livekit.agents import APIConnectOptions, APIStatusError
from multidict import CIMultiDict, CIMultiDictProxy
from voicebook_tts import VoicebookTTS, build_streaming_voicebook_tts
from yarl import URL

# 0.5 s of known s16le PCM @ 24 kHz (non-empty so AudioEmitter accepts the turn).
_PCM = b"\x01\x02" * 6000


def _streaming_wav(pcm: bytes) -> bytes:
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        0xFFFFFFFF,
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        24000,
        48000,
        2,
        16,
        b"data",
        0xFFFFFFFF,
    ) + pcm


class _FakeContent:
    def __init__(self, chunks):
        self._chunks = chunks

    async def iter_chunks(self):
        for c in self._chunks:
            yield c, True


class _FakeResp:
    def __init__(self, *, status=200, chunks=(_PCM,), headers=None):
        self.status = status
        self.headers = headers or {"X-Audio-Format": "s16le", "X-Sample-Rate": "24000"}
        self.content = _FakeContent(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            ri = aiohttp.RequestInfo(
                URL("http://vb:5060/speak/stream"),
                "POST",
                CIMultiDictProxy(CIMultiDict()),
                URL("http://vb:5060/speak/stream"),
            )
            raise aiohttp.ClientResponseError(
                ri, (), status=self.status, message=f"HTTP {self.status}"
            )

    async def text(self):
        return "error body"


class _FakeSession:
    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        return self._resp


def _as_client_session(session: _FakeSession) -> aiohttp.ClientSession:
    """Type-only bridge for the deliberately small aiohttp test double."""
    return cast(aiohttp.ClientSession, session)


# --- sync contract -----------------------------------------------------


def test_audio_contract_matches_service():
    t = VoicebookTTS(voice_id="sumi-v1")
    assert t.sample_rate == 24000
    assert t.num_channels == 1
    # full-text input -> the pipeline wraps with its StreamAdapter
    assert t.capabilities.streaming is False


def test_base_url_trailing_slash_normalized():
    t = VoicebookTTS(voice_id="sumi-v1", base_url="http://vb:5060/")
    assert t._base_url == "http://vb:5060"


def test_empty_voice_id_fails_loud():
    with pytest.raises(ValueError, match="voice_id"):
        VoicebookTTS(voice_id="")


def test_unknown_wire_format_fails_loud():
    with pytest.raises(ValueError, match="wire_format"):
        VoicebookTTS(voice_id="sumi-v1", wire_format="mp3")


def test_worker_adapter_is_explicitly_streaming():
    t = build_streaming_voicebook_tts(voice_id="sumi-v1")
    assert t.capabilities.streaming is True


def test_unknown_text_mode_fails_loud():
    with pytest.raises(ValueError, match="text_mode"):
        build_streaming_voicebook_tts(voice_id="sumi-v1", text_mode="maybe")


# --- async behaviour ---------------------------------------------------


def test_run_posts_correct_request_and_maps_frames():
    async def go():
        sess = _FakeSession(_FakeResp(chunks=(_PCM,)))
        t = VoicebookTTS(
            voice_id="sumi-v1",
            base_url="http://vb:5060/",
            http_session=_as_client_session(sess),
        )
        frames = []
        async for ev in t.synthesize("hello Eric"):
            frames.append(ev.frame)
        return sess, frames

    sess, frames = asyncio.run(go())

    url, kw = sess.calls[0]
    assert url == "http://vb:5060/speak/stream"
    assert kw["json"] == {"voice_id": "sumi-v1", "text": "hello Eric"}

    assert frames, "adapter yielded no audio frames"
    assert all(f.sample_rate == 24000 and f.num_channels == 1 for f in frames)
    assert sum(f.samples_per_channel for f in frames) > 0


def test_wav_wire_format_uses_decoder_backed_emitter_path():
    async def go():
        wav = _streaming_wav(_PCM)
        sess = _FakeSession(
            _FakeResp(
                chunks=(wav[:137], wav[137:]),
                headers={"X-Audio-Format": "wav", "X-Sample-Rate": "24000"},
            )
        )
        t = VoicebookTTS(
            voice_id="sumi-v1",
            base_url="http://vb:5060/",
            wire_format="wav",
            http_session=_as_client_session(sess),
        )
        frames = []
        async for ev in t.synthesize("hello Eric"):
            frames.append(ev.frame)
        return sess, frames

    sess, frames = asyncio.run(go())
    _, kw = sess.calls[0]
    assert kw["json"] == {
        "voice_id": "sumi-v1",
        "text": "hello Eric",
        "response_format": "wav",
    }
    assert frames
    assert sum(f.samples_per_channel for f in frames) == len(_PCM) // 2


def test_http_error_maps_to_apistatuserror_not_silent():
    async def go():
        sess = _FakeSession(_FakeResp(status=429))
        t = VoicebookTTS(
            voice_id="sumi-v1",
            base_url="http://vb:5060",
            http_session=_as_client_session(sess),
        )
        # max_retry=0 so the 429 surfaces deterministically instead of retrying.
        async for _ in t.synthesize("hi", conn_options=APIConnectOptions(max_retry=0)):
            pass

    with pytest.raises(APIStatusError) as exc:
        asyncio.run(go())
    assert exc.value.status_code == 429


def test_wrong_audio_format_header_rejected():
    async def go():
        sess = _FakeSession(_FakeResp(headers={"X-Audio-Format": "mp3"}))
        t = VoicebookTTS(
            voice_id="sumi-v1",
            base_url="http://vb:5060",
            http_session=_as_client_session(sess),
        )
        async for _ in t.synthesize("hi", conn_options=APIConnectOptions(max_retry=0)):
            pass

    # a 200 that isn't the s16le contract must fail, not be played as noise
    with pytest.raises(Exception, match="s16le"):
        asyncio.run(go())


@pytest.mark.parametrize(
    ("text", "expected_requests"),
    [
        ("Good evening, Eric. I'm here. How are you?", 1),
        (
            "I was organizing the bookshelf by spine color. It is a small comfort, in its way.",
            1,
        ),
        (
            "There is something peaceful about making order from small things. "
            "Shall we just sit in the quiet for a while?",
            1,
        ),
    ],
)
def test_worker_adapter_coalesces_first_call_sized_phrases(text, expected_requests):
    async def go():
        sess = _FakeSession(_FakeResp(chunks=(_PCM,)))
        adapter = build_streaming_voicebook_tts(
            voice_id="sumi-v1",
            base_url="http://vb:5060",
            http_session=_as_client_session(sess),
        )
        frames = []
        async with adapter.stream() as stream:
            # Model the real LLM path: text arrives incrementally rather than
            # as one already-complete string.
            for start in range(0, len(text), 7):
                stream.push_text(text[start : start + 7])
            stream.end_input()
            async for ev in stream:
                frames.append(ev.frame)
        return sess, frames

    sess, frames = asyncio.run(go())
    assert len(sess.calls) == expected_requests
    assert frames
    posted_text = " ".join(call[1]["json"]["text"] for call in sess.calls)
    assert " ".join(posted_text.split()) == " ".join(text.split())


def test_worker_adapter_reproduces_e2e_sentence_boundary_split():
    text = (
        "You know me already, Eric. I'm the one who notices what's out of place and sets it "
        "right without being asked. I don't speak much, but I am here. What would you like "
        "to know?"
    )
    expected = [
        "You know me already, Eric. I'm the one who notices what's out of place and sets it "
        "right without being asked.",
        "I don't speak much, but I am here. What would you like to know?",
    ]

    async def go():
        sess = _FakeSession(_FakeResp(chunks=(_PCM,)))
        adapter = build_streaming_voicebook_tts(
            voice_id="sumi-v1",
            base_url="http://vb:5060",
            http_session=_as_client_session(sess),
        )
        async with adapter.stream() as stream:
            # Match the incremental shape used by the existing adapter test and
            # observed in the qualified E2E turn.
            for start in range(0, len(text), 7):
                stream.push_text(text[start : start + 7])
            stream.end_input()
            async for _ in stream:
                pass
        return [call[1]["json"]["text"] for call in sess.calls]

    actual = asyncio.run(go())
    assert [" ".join(part.split()) for part in actual] == expected


def test_whole_reply_mode_sends_long_turn_as_one_request():
    text = (
        "There was once a grandfather clock in a house that had grown very still. "
        "The pendulum had stopped swinging, and the gears had forgotten how to turn. "
        "For years it sat in the corner, collecting dust and memories. "
    ) * 5

    async def go():
        sess = _FakeSession(_FakeResp(chunks=(_PCM,)))
        adapter = build_streaming_voicebook_tts(
            voice_id="sumi-v1",
            base_url="http://vb:5060",
            text_mode="whole_reply",
            http_session=_as_client_session(sess),
        )
        async with adapter.stream() as stream:
            for start in range(0, len(text), 7):
                stream.push_text(text[start : start + 7])
            stream.end_input()
            async for _ in stream:
                pass
        return sess.calls

    calls = asyncio.run(go())
    assert len(calls) == 1
    assert " ".join(calls[0][1]["json"]["text"].split()) == " ".join(text.split())
