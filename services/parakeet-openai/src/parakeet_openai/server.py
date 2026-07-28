"""Entrypoint. LAN-bound by default, because that is the whole point.

The fleet needs this reachable from nyla.mey.house, hana.mey.house and the
command chair -- a loopback bind would make the service look healthy on mizuki
and be invisible to every caller that needs it. That exact fault cost us Nyla's
5pm brief on 2026-07-28.
"""

from __future__ import annotations

import logging
import os

import uvicorn

from .app import create_app


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("PARAKEET_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    uvicorn.run(
        create_app(),
        host=os.environ.get("PARAKEET_OPENAI_HOST", "0.0.0.0"),  # noqa: S104 - LAN by design
        port=int(os.environ.get("PARAKEET_OPENAI_PORT", "5057")),
        log_level=os.environ.get("PARAKEET_LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
