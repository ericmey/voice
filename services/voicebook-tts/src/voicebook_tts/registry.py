"""Server-owned voice registry.

The client sends a ``voice_id`` and nothing else. This module owns the mapping
from that id to a deployed master WAV, its exact reference transcript, and the
SHA-256 the file must have.

Why the server owns it: a client-supplied path or hash would let a caller point
the service at any file on disk, or assert a hash the file does not have. The
whole point of a voicebook master is that it is *the* woman — accepting either
from outside would make that guarantee decorative.

Masters are read-only inputs. Canon is never mutated by this service.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


class RegistryError(RuntimeError):
    """Registry could not be trusted. Always fatal — never degrade to a guess."""


class UnknownVoice(KeyError):
    """Caller asked for a voice_id that is not in the allowlist."""


class MasterIntegrityError(RegistryError):
    """A master's on-disk bytes do not match its expected hash."""


@dataclass(frozen=True)
class VoiceEntry:
    """One girl's deployed voice. Immutable by construction."""

    voice_id: str
    master_path: Path
    reference_transcript: str
    expected_sha256: str

    def actual_sha256(self) -> str:
        h = hashlib.sha256()
        with self.master_path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def verify(self) -> None:
        """Raise unless the file exists, is readable, and hashes as expected."""
        if not self.master_path.is_file():
            raise MasterIntegrityError(f"{self.voice_id}: master missing at {self.master_path}")
        actual = self.actual_sha256()
        if actual != self.expected_sha256:
            raise MasterIntegrityError(
                f"{self.voice_id}: master hash mismatch at {self.master_path}\n"
                f"  expected {self.expected_sha256}\n"
                f"  actual   {actual}\n"
                "Refusing to speak — this is not the accepted voice."
            )


class VoiceRegistry:
    """Allowlist of speakable voices, verified at startup and before each use."""

    def __init__(self, entries: dict[str, VoiceEntry]) -> None:
        self._entries = dict(entries)

    @property
    def voice_ids(self) -> list[str]:
        return sorted(self._entries)

    def verify_all(self) -> None:
        """Startup gate. One bad entry fails the whole service, loudly."""
        if not self._entries:
            raise RegistryError("registry is empty — refusing to start with no voices")
        for entry in self._entries.values():
            entry.verify()

    def get(self, voice_id: str) -> VoiceEntry:
        """Fetch and RE-VERIFY. Startup verification is not a permanent promise —
        a master can be swapped, truncated, or re-rendered while the service is
        resident, and a long-lived process would happily keep serving it."""
        try:
            entry = self._entries[voice_id]
        except KeyError:
            raise UnknownVoice(voice_id) from None
        entry.verify()
        return entry
