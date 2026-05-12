"""Configuration settings for Confluence MCP server."""

import os
from typing import Optional

from pydantic import Field, field_validator, ConfigDict
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Confluence connection - make optional for tests
    confluence_base_url: Optional[str] = Field(None, description="Confluence Server base URL")
    confluence_username: Optional[str] = Field(None, description="Username for Basic Auth")
    confluence_password: Optional[str] = Field(None, description="Password for Basic Auth")
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
        """Validate that required fields are present for production use."""
        if not self.confluence_base_url:
            raise ValueError("CONFLUENCE_BASE_URL is required")
        if not self.confluence_username:
            raise ValueError("CONFLUENCE_USERNAME is required")
        if not self.confluence_password:
            raise ValueError("CONFLUENCE_PASSWORD is required")

    model_config = ConfigDict(
        env_file=".env",
        case_sensitive=False,
    )


def get_settings(validate_required: bool = True) -> Settings:
    """Get settings instance."""
    settings = Settings()
    if validate_required and not os.getenv("PYTEST_CURRENT_TEST"):
        settings.validate_required_fields()
    return settings


# Global settings instance for backwards compatibility
try:
    settings = get_settings()
except ValueError:
    # In test environment or missing config
    settings = Settings()