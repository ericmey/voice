"""Tests for the Slice-5 voicebook-stream TTS adapter.

Load-bearing here: the adapter POSTs exactly {voice_id, text} to /speak/stream,
maps the raw s16le PCM to 24 kHz mono frames through the LiveKit AudioEmitter,
fails loud on an empty voice_id, and turns an HTTP error into an APIStatusError
(never a silent empty turn). These run WITHOUT the live service — a fake aiohttp
session stands in — so CI protects the contract the live seam test proved.
"""

import asyncio

import aiohttp
import pytest
from livekit.agents import APIConnectOptions, APIStatusError
from voicebook_tts import VoicebookTTS
from yarl import URL

# 0.5 s of known s16le PCM @ 24 kHz (non-empty so AudioEmitter accepts the turn).
_PCM = (b"\x01\x02" * 6000)


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
                URL("http://vb:5060/speak/stream"), "POST", (), URL("http://vb:5060/speak/stream")
            )
            raise aiohttp.ClientResponseError(ri, (), status=self.status, message=f"HTTP {self.status}")

    async def text(self):
        return "error body"


class _FakeSession:
    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        return self._resp


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


# --- async behaviour ---------------------------------------------------


def test_run_posts_correct_request_and_maps_frames():
    async def go():
        sess = _FakeSession(_FakeResp(chunks=(_PCM,)))
        t = VoicebookTTS(voice_id="sumi-v1", base_url="http://vb:5060/", http_session=sess)
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


def test_http_error_maps_to_apistatuserror_not_silent():
    async def go():
        sess = _FakeSession(_FakeResp(status=429))
        t = VoicebookTTS(voice_id="sumi-v1", base_url="http://vb:5060", http_session=sess)
        # max_retry=0 so the 429 surfaces deterministically instead of retrying.
        async for _ in t.synthesize("hi", conn_options=APIConnectOptions(max_retry=0)):
            pass

    with pytest.raises(APIStatusError) as exc:
        asyncio.run(go())
    assert exc.value.status_code == 429


def test_wrong_audio_format_header_rejected():
    async def go():
        sess = _FakeSession(_FakeResp(headers={"X-Audio-Format": "mp3"}))
        t = VoicebookTTS(voice_id="sumi-v1", base_url="http://vb:5060", http_session=sess)
        async for _ in t.synthesize("hi", conn_options=APIConnectOptions(max_retry=0)):
            pass

    # a 200 that isn't the s16le contract must fail, not be played as noise
    with pytest.raises(Exception, match="s16le"):
        asyncio.run(go())
