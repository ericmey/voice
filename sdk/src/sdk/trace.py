"""Out-of-band debug trace — bypasses multiprocessing log bridge.

livekit-agents pre-forks job workers via multiprocessing.spawn and bridges
their stdlib logs back to the parent over IPC. The bridge drops records from
workers that were spawned after the first job cycle. This writes a line to
a file on disk regardless of whether the log bridge cooperates.

Writes to ``$LIVEKIT_VOICE_LOGS/agent.trace``. If that env var is unset,
trace calls are silent no-ops. Filesystem errors are logged at ERROR level
so operators know when trace data is being lost.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

logger = logging.getLogger("voice.trace")


def trace(msg: str) -> None:
    """Append a timestamped trace line. Never raises."""
    logs_dir = os.environ.get("LIVEKIT_VOICE_LOGS")
    if not logs_dir:
        return
    try:
        path = Path(logs_dir) / "agent.trace"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} pid={os.getpid()} {msg}\n")
    except Exception as exc:
        logger.error("trace write failed: %s", exc)
