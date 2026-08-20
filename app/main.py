"""Entrypoint for the Transcoder service.

Contract:
  - Responsibility: Initialize runtime settings from environment, configure logging,
    construct the FastAPI application instance via `create_app(settings)`, and launch Uvicorn.
"""

from __future__ import annotations

import uvicorn

from app.api import create_app
from app.config import Settings, configure_logging


def main() -> None:
    """Bootstrap application settings, logging, and Uvicorn server."""
    settings = Settings.from_env()
    configure_logging(api_key=settings.api_key, level=settings.log_level)
    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None)


if __name__ == "__main__":
    main()

