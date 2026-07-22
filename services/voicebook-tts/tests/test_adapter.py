"""Tests for the Hermes command-TTS adapter (ops/hermes-adapter-voicebook-tts.py).

Run as a subprocess against a local fake service — the adapter is a standalone
script, and testing it as one is the point: that is exactly how Hermes invokes it.

The failure these cover is specific. An earlier adapter wrote straight to the
output path, so an interrupted write left a truncated file *while* exiting
non-zero, and Hermes would see both an error and an output. Manual red-proofing
missed it: unreachable-service and unknown-voice both fail BEFORE any write, so
they could never exercise the write path at all.
"""

from __future__ import annotations

import http.server
import io
import subprocess
import sys
import threading
import wave
from pathlib import Path

import pytest

ADAPTER = Path(__file__).parent.parent / "ops" / "hermes-adapter-voicebook-tts.py"


def _wav_bytes(seconds: float = 0.2, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x01\x00" * int(rate * seconds))
    return buf.getvalue()


class _Handler(http.server.BaseHTTPRequestHandler):
    payload = b""

    def do_POST(self):  # noqa: N802 - stdlib interface
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, *a):  # silence
        pass


@pytest.fixture
def fake_service():
    _Handler.payload = _wav_bytes()
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}/speak"
    srv.shutdown()


def _run(url: str, inp: Path, out: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(ADAPTER),
            "--input",
            str(inp),
            "--output",
            str(out),
            "--voice-id",
            "sumi-v1",
            "--url",
            url,
            "--timeout",
            "10",
        ],
        capture_output=True,
        text=True,
    )


def _orphans(d: Path) -> list[Path]:
    return [p for p in d.iterdir() if p.name.endswith(".part")]


def test_success_writes_wav_and_leaves_no_temp(tmp_path: Path, fake_service: str):
    inp = tmp_path / "in.txt"
    inp.write_text("hello")
    out = tmp_path / "out.wav"
    r = _run(fake_service, inp, out)
    assert r.returncode == 0, r.stderr
    assert out.read_bytes()[:4] == b"RIFF"
    assert _orphans(tmp_path) == []


def test_success_atomically_replaces_an_existing_destination(tmp_path: Path, fake_service: str):
    inp = tmp_path / "in.txt"
    inp.write_text("hello")
    out = tmp_path / "out.wav"
    out.write_bytes(b"STALE-PREVIOUS-CONTENT")
    r = _run(fake_service, inp, out)
    assert r.returncode == 0, r.stderr
    assert out.read_bytes()[:4] == b"RIFF"
    assert b"STALE" not in out.read_bytes()
    assert _orphans(tmp_path) == []


def test_post_create_failure_cleans_up_and_leaves_destination_untouched(
    tmp_path: Path, fake_service: str
):
    """The case manual testing could not reach.

    The destination is an existing DIRECTORY. Temp creation in the parent
    succeeds, the bytes are written and fsynced, and only then does os.replace
    fail. That exercises cleanup AFTER a temp file exists — the exact path the
    atomic rewrite was added to protect.
    """
    inp = tmp_path / "in.txt"
    inp.write_text("hello")
    out = tmp_path / "out.wav"
    out.mkdir()  # replace() onto a directory fails
    sentinel = out / "keep.txt"
    sentinel.write_text("must survive")

    r = _run(fake_service, inp, out)

    assert r.returncode != 0, "post-create failure must exit non-zero"
    assert sentinel.read_text() == "must survive", "destination was disturbed"
    assert _orphans(tmp_path) == [], f"orphaned temp left behind: {_orphans(tmp_path)}"


def test_unreachable_service_writes_nothing(tmp_path: Path):
    inp = tmp_path / "in.txt"
    inp.write_text("hello")
    out = tmp_path / "out.wav"
    r = _run("http://127.0.0.1:1/speak", inp, out)
    assert r.returncode != 0
    assert not out.exists()
    assert _orphans(tmp_path) == []


def test_empty_input_writes_nothing(tmp_path: Path, fake_service: str):
    inp = tmp_path / "in.txt"
    inp.write_text("   ")
    out = tmp_path / "out.wav"
    r = _run(fake_service, inp, out)
    assert r.returncode != 0
    assert not out.exists()


# --- discriminating tests: import the seam directly -------------------------
#
# The CLI-level tests above assert the CONTRACT and both a naive and an atomic
# implementation satisfy them — verified by reverting to a direct write and
# watching all five still pass. These import atomic_write and force a failure
# AFTER the temp file exists, which is the only point where the two differ.

import importlib.util  # noqa: E402


def _adapter_module():
    spec = importlib.util.spec_from_file_location("vb_adapter", ADAPTER)
    assert spec is not None and spec.loader is not None, f"cannot load {ADAPTER}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_atomic_write_replaces_existing(tmp_path: Path):
    mod = _adapter_module()
    out = tmp_path / "o.wav"
    out.write_bytes(b"OLD")
    mod.atomic_write(out, b"NEWPAYLOAD")
    assert out.read_bytes() == b"NEWPAYLOAD"
    assert _orphans(tmp_path) == []


def test_replace_failure_after_temp_exists_preserves_destination(tmp_path: Path, monkeypatch):
    """The discriminating case.

    os.replace fails AFTER the temp file has been written and fsynced. A naive
    `open(path,"wb")` would already have truncated the destination by this
    point; the atomic path must leave the previous good render byte-identical
    and clean up its temp.
    """
    mod = _adapter_module()
    out = tmp_path / "o.wav"
    original = b"PREVIOUS-GOOD-RENDER"
    out.write_bytes(original)

    def boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(mod.os, "replace", boom)

    with pytest.raises(OSError, match="simulated replace failure"):
        mod.atomic_write(out, b"NEWPAYLOAD")

    assert out.read_bytes() == original, "destination was damaged by a failed write"
    assert _orphans(tmp_path) == [], f"orphaned temp: {_orphans(tmp_path)}"


def test_write_failure_after_temp_created_leaves_no_orphan(tmp_path: Path, monkeypatch):
    mod = _adapter_module()
    out = tmp_path / "o.wav"
    real_fsync = mod.os.fsync

    def boom(fd):
        real_fsync(fd)
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(mod.os, "fsync", boom)

    with pytest.raises(OSError, match="simulated fsync failure"):
        mod.atomic_write(out, b"NEWPAYLOAD")

    assert not out.exists(), "destination created despite failure"
    assert _orphans(tmp_path) == [], f"orphaned temp: {_orphans(tmp_path)}"
