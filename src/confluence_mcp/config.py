"""Configuration settings for Confluence MCP server.

Server-level config only. Per-user credentials (username/password) are passed
in by the MCP client via `Authorization: Basic` on every request and never
live in Settings or `.env`.
"""

import os
from typing import Optional

from pydantic import Field, field_validator, ConfigDict
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    confluence_base_url: Optional[str] = Field(None, description="Confluence Server base URL")
    confluence_verify_ssl: bool = Field(True, description="Verify SSL certificates")

    # FastAPI server
    host: str = Field("127.0.0.1", description="Host to bind the server to")
    port: int = Field(8765, description="Port to bind the server to")
    log_level: str = Field("INFO", description="Log level")

    # Safety limits — confluence attachments default to 100MB max, but
    # holding that in memory inside a small container is a recipe for OOMs.
    confluence_max_upload_bytes: int = Field(
        25 * 1024 * 1024, description="Reject uploads larger than this (bytes)"
    )
    confluence_max_download_bytes: int = Field(
        25 * 1024 * 1024, description="Reject downloads larger than this (bytes)"
    )
    # Sandbox for the download-to-file tool. Relative path here is resolved
    # against the process CWD at startup; the tool refuses paths that escape it.
    confluence_download_dir: str = Field(
        "./downloads", description="Sandbox directory for attachment downloads"
    )

    @field_validator("confluence_base_url")
    @classmethod
    def normalize_base_url(cls, v: Optional[str]) -> Optional[str]:
        """Remove trailing slash from base URL."""
        return v.rstrip("/") if v else None

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Validate log level."""
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        v_upper = v.upper()
        if v_upper not in valid_levels:
            raise ValueError(f"Log level must be one of: {valid_levels}")
        return v_upper

    def validate_required_fields(self) -> None:
        """Validate that required fields are present for production use.

        Credentials are NOT required here — they're supplied per-request via
        the MCP client's `Authorization: Basic` header.
        """
        if not self.confluence_base_url:
            raise ValueError("CONFLUENCE_BASE_URL is required")

    model_config = ConfigDict(
        env_file=".env",
        case_sensitive=False,
        # `extra="ignore"` so that legacy `.env` files left over from the
        # shared-credentials era (CONFLUENCE_USERNAME / CONFLUENCE_PASSWORD)
        # don't break startup. Those variables are no longer read; new clients
        # supply credentials via Basic Auth.
        extra="ignore",
    )


def get_settings(validate_required: bool = True) -> Settings:
    """Get settings instance."""
    settings = Settings()
    if validate_required and not os.getenv("PYTEST_CURRENT_TEST"):
        settings.validate_required_fields()
    return settings