"""OpenAI-shaped transcription shim over NVIDIA Riva/Parakeet streaming gRPC."""

from .app import create_app

__all__ = ["create_app"]
