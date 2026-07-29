"""Named voice registry for NVIDIA Magpie TTS Zero Shot."""

from .app import create_app
from .registry import VoiceRegistry, VoiceSpec, load_registry

__all__ = ["VoiceRegistry", "VoiceSpec", "create_app", "load_registry"]
