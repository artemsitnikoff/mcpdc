"""Entry point for running Confluence MCP server via python -m confluence_mcp."""

import logging
import sys

import uvicorn

from .app import create_app
from .config import get_settings


def setup_logging():
    """Setup logging configuration."""
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
        ],
    )

    # Reduce noise from some libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)


def main():
    """Main entry point."""
    settings = get_settings()
    setup_logging()

    logger = logging.getLogger(__name__)
    logger.info(f"Starting Confluence MCP Server on {settings.host}:{settings.port}")
    logger.info(f"Confluence URL: {settings.confluence_base_url}")
    logger.info(f"Log level: {settings.log_level}")

    app = create_app()

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_config=None,  # We handle logging ourselves
        access_log=True,
    )


if __name__ == "__main__":
    main()