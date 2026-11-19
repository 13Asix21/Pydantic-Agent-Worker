"""Entrypoint: ``python -m agent_bridge.main``."""

from __future__ import annotations

import uvicorn

from agent_bridge.config.settings import Settings
from agent_bridge.server.app import create_app


def main() -> None:
    """Load settings and run the ASGI server."""
    settings = Settings()
    uvicorn.run(create_app(settings), host=settings.server.host, port=settings.server.port)


if __name__ == "__main__":
    main()
