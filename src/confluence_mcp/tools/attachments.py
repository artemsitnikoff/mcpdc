"""Attachment tools for Confluence MCP server."""

import base64
import logging
from pathlib import Path
from typing import Any, Dict, List

from mcp.server import Server
from mcp.types import Tool, TextContent

from ..client import ConfluenceClient
from ..errors import ConfluenceError, ConfluencePathError

logger = logging.getLogger(__name__)


def _resolve_download_path(user_path: str, base_dir: str) -> Path:
    """Resolve `user_path` inside `base_dir`, rejecting traversal.

    The caller's `file_path` is *always* treated as relative — absolute paths
    are reinterpreted under the sandbox. After resolution we verify the result
    sits inside the sandbox; anything else (symlink games, `..`) raises.
    """
    base = Path(base_dir).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)

    candidate = Path(user_path)
    if candidate.is_absolute():
        # Strip the leading slash so the path stays inside the sandbox.
        candidate = Path(*candidate.parts[1:]) if len(candidate.parts) > 1 else Path(candidate.name)

    target = (base / candidate).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise ConfluencePathError(str(user_path), str(base)) from exc
    return target


def register_attachment_tools(server: Server, confluence: ConfluenceClient) -> None:
    """Register attachment-related MCP tools."""

    @server.call_tool()
    async def confluence_list_attachments(arguments: Dict[str, Any]) -> List[TextContent]:
        """
        List attachments on a Confluence page.

        Args:
            page_id: Page ID to get attachments for
            limit: Maximum number of attachments to return (default: 25, max: 100)
            start: Starting index for pagination (default: 0)

        Returns:
            List of attachments with metadata
        """
        try:
            page_id = arguments.get("page_id")
            if not page_id:
                return [TextContent(
                    type="text",
                    text="Error: 'page_id' parameter is required"
                )]

            limit = min(int(arguments.get("limit", 25)), 100)
            start = int(arguments.get("start", 0))

            # Get attachments
            result = await confluence.get_attachments(
                page_id=page_id,
                limit=limit,
                start=start,
            )

            # Format attachments
            attachments = []
            for attachment in result.get("results", []):
                metadata = attachment.get("metadata", {})

                attachment_data = {
                    "id": attachment["id"],
                    "title": attachment["title"],
                    "media_type": metadata.get("mediaType", "unknown"),
                    "file_size": metadata.get("fileSize", 0),
                    "comment": metadata.get("comment", ""),
                    "version": attachment["version"]["number"],
                    "created": attachment["version"]["when"],
                    "created_by": attachment["version"]["by"]["displayName"],
                    "download_url": attachment["_links"]["download"],
                }

                attachments.append(attachment_data)

            # Format response
            response = {
                "page_id": page_id,
                "total": result.get("totalSize", len(attachments)),
                "start": start,
                "limit": limit,
                "attachments": attachments,
            }

            # Add pagination info
            if "_links" in result and "next" in result["_links"]:
                response["has_more"] = True
                response["next_start"] = start + limit
            else:
                response["has_more"] = False

            return [TextContent(
                type="text",
                text=f"Attachments:\n\n{_format_attachments_response(response)}"
            )]

        except ConfluenceError as e:
            logger.error(f"Confluence attachments error: {e}")
            return [TextContent(
                type="text",
                text=f"Failed to get attachments: {e.message}"
            )]
        except Exception as e:
            logger.error(f"Unexpected attachments error: {e}")
            return [TextContent(
                type="text",
                text=f"Failed to get attachments: {str(e)}"
            )]

    @server.call_tool()
    async def confluence_download_attachment(arguments: Dict[str, Any]) -> List[TextContent]:
        """
        Download an attachment from a Confluence page.

        Args:
            page_id: Page ID containing the attachment
            filename: Name of the attachment to download
            output: Output type - 'base64' (return as base64 string) or 'file' (save to path)
            file_path: Local file path to save to (required if output='file')

        Returns:
            Download result with content (base64) or file path
        """
        try:
            page_id = arguments.get("page_id")
            filename = arguments.get("filename")
            output = arguments.get("output", "base64").lower()
            file_path = arguments.get("file_path")

            if not page_id or not filename:
                return [TextContent(
                    type="text",
                    text="Error: 'page_id' and 'filename' parameters are required"
                )]

            if output not in ["base64", "file"]:
                return [TextContent(
                    type="text",
                    text="Error: output must be 'base64' or 'file'"
                )]

            if output == "file" and not file_path:
                return [TextContent(
                    type="text",
                    text="Error: 'file_path' is required when output='file'"
                )]

            # Download the attachment
            content = await confluence.download_attachment(page_id, filename)

            response = {
                "page_id": page_id,
                "filename": filename,
                "size": len(content),
                "output": output,
            }

            if output == "base64":
                # Return as base64 string
                response["content"] = base64.b64encode(content).decode('utf-8')
            else:
                # Save to file — sandbox the caller-supplied path under the
                # configured download directory.
                try:
                    file_path_obj = _resolve_download_path(
                        file_path, confluence.settings.confluence_download_dir
                    )
                except ConfluencePathError as e:
                    return [TextContent(type="text", text=f"Refused: {e.message}")]

                file_path_obj.parent.mkdir(parents=True, exist_ok=True)
                with open(file_path_obj, 'wb') as f:
                    f.write(content)

                response["file_path"] = str(file_path_obj)

            return [TextContent(
                type="text",
                text=f"Attachment Downloaded:\n\n{_format_download_response(response)}"
            )]

        except ConfluenceError as e:
            logger.error(f"Confluence download error: {e}")
            return [TextContent(
                type="text",
                text=f"Failed to download attachment: {e.message}"
            )]
        except Exception as e:
            logger.error(f"Unexpected download error: {e}")
            return [TextContent(
                type="text",
                text=f"Failed to download attachment: {str(e)}"
            )]

    @server.call_tool()
    async def confluence_upload_attachment(arguments: Dict[str, Any]) -> List[TextContent]:
        """
        Upload an attachment to a Confluence page.

        Args:
            page_id: Page ID to upload attachment to
            file_path: Path to the file to upload
            comment: Optional comment for the upload

        Returns:
            Upload result with attachment information
        """
        try:
            page_id = arguments.get("page_id")
            file_path = arguments.get("file_path")
            comment = arguments.get("comment")

            if not page_id or not file_path:
                return [TextContent(
                    type="text",
                    text="Error: 'page_id' and 'file_path' parameters are required"
                )]

            # Read the file — but check size on disk first so we don't pull a
            # multi-gigabyte file into memory only to reject it.
            file_path_obj = Path(file_path)
            if not file_path_obj.exists():
                return [TextContent(
                    type="text",
                    text=f"Error: File not found: {file_path}"
                )]

            size = file_path_obj.stat().st_size
            limit = confluence.settings.confluence_max_upload_bytes
            if size > limit:
                return [TextContent(
                    type="text",
                    text=f"Refused: file {file_path} is {size} bytes; CONFLUENCE_MAX_UPLOAD_BYTES is {limit}",
                )]

            with open(file_path_obj, 'rb') as f:
                content = f.read()

            filename = file_path_obj.name

            # Upload the attachment
            result = await confluence.upload_attachment(
                page_id=page_id,
                filename=filename,
                content=content,
                comment=comment,
            )

            # Extract result information
            if "results" in result and result["results"]:
                attachment = result["results"][0]

                response = {
                    "id": attachment["id"],
                    "title": attachment["title"],
                    "page_id": page_id,
                    "file_size": len(content),
                    "version": attachment["version"]["number"],
                    "created": attachment["version"]["when"],
                    "created_by": attachment["version"]["by"]["displayName"],
                    "download_url": attachment["_links"]["download"],
                }

                if comment:
                    response["comment"] = comment
            else:
                # Fallback for different response format
                response = {
                    "filename": filename,
                    "page_id": page_id,
                    "file_size": len(content),
                    "status": "uploaded",
                }

            return [TextContent(
                type="text",
                text=f"Attachment Uploaded Successfully:\n\n{_format_upload_response(response)}"
            )]

        except ConfluenceError as e:
            logger.error(f"Confluence upload error: {e}")
            return [TextContent(
                type="text",
                text=f"Failed to upload attachment: {e.message}"
            )]
        except Exception as e:
            logger.error(f"Unexpected upload error: {e}")
            return [TextContent(
                type="text",
                text=f"Failed to upload attachment: {str(e)}"
            )]


def _format_attachments_response(response: Dict[str, Any]) -> str:
    """Format attachments response for display."""
    lines = [
        f"Page ID: {response['page_id']}",
        f"Total Attachments: {response['total']}",
        f"Showing: {response['start'] + 1}-{response['start'] + len(response['attachments'])}",
    ]

    if response.get("has_more"):
        lines.append(f"Next page: start={response['next_start']}")

    lines.append("")

    for i, attachment in enumerate(response["attachments"], 1):
        file_size_mb = attachment['file_size'] / 1024 / 1024 if attachment['file_size'] > 0 else 0

        lines.extend([
            f"{i}. {attachment['title']}",
            f"   ID: {attachment['id']}",
            f"   Type: {attachment['media_type']}",
            f"   Size: {attachment['file_size']} bytes ({file_size_mb:.2f} MB)",
            f"   Version: {attachment['version']}",
            f"   Created: {attachment['created']}",
            f"   Created By: {attachment['created_by']}",
            f"   Download URL: {attachment['download_url']}",
        ])

        if attachment.get("comment"):
            lines.append(f"   Comment: {attachment['comment']}")

        lines.append("")

    return "\n".join(lines)


def _format_download_response(response: Dict[str, Any]) -> str:
    """Format download response for display."""
    lines = [
        f"Filename: {response['filename']}",
        f"Page ID: {response['page_id']}",
        f"Size: {response['size']} bytes",
        f"Output: {response['output']}",
    ]

    if response['output'] == 'file':
        lines.append(f"Saved to: {response['file_path']}")
    else:
        content_preview = response['content'][:100]
        if len(response['content']) > 100:
            content_preview += "..."
        lines.extend([
            "Base64 Content (first 100 chars):",
            content_preview,
        ])

    return "\n".join(lines)


def _format_upload_response(response: Dict[str, Any]) -> str:
    """Format upload response for display."""
    lines = [
        f"Page ID: {response['page_id']}",
        f"File Size: {response['file_size']} bytes",
    ]

    if "id" in response:
        lines.extend([
            f"Attachment ID: {response['id']}",
            f"Title: {response['title']}",
            f"Version: {response['version']}",
            f"Created: {response['created']}",
            f"Created By: {response['created_by']}",
            f"Download URL: {response['download_url']}",
        ])
    else:
        lines.extend([
            f"Filename: {response['filename']}",
            f"Status: {response['status']}",
        ])

    if response.get("comment"):
        lines.append(f"Comment: {response['comment']}")

    return "\n".join(lines)


# Tool definitions for the MCP server
ATTACHMENT_TOOLS = [
    Tool(
        name="confluence_list_attachments",
        description="List attachments on a Confluence page",
        inputSchema={
            "type": "object",
            "properties": {
                "page_id": {
                    "type": "string",
                    "description": "Page ID to get attachments for",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of attachments to return (default: 25, max: 100)",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 25,
                },
                "start": {
                    "type": "integer",
                    "description": "Starting index for pagination (default: 0)",
                    "minimum": 0,
                    "default": 0,
                },
            },
            "required": ["page_id"],
        },
    ),
    Tool(
        name="confluence_download_attachment",
        description="Download an attachment from a Confluence page",
        inputSchema={
            "type": "object",
            "properties": {
                "page_id": {
                    "type": "string",
                    "description": "Page ID containing the attachment",
                },
                "filename": {
                    "type": "string",
                    "description": "Name of the attachment to download",
                },
                "output": {
                    "type": "string",
                    "enum": ["base64", "file"],
                    "description": "Output type - 'base64' (return as base64 string) or 'file' (save to path)",
                    "default": "base64",
                },
                "file_path": {
                    "type": "string",
                    "description": "Local file path to save to (required if output='file')",
                },
            },
            "required": ["page_id", "filename"],
        },
    ),
    Tool(
        name="confluence_upload_attachment",
        description="Upload an attachment to a Confluence page",
        inputSchema={
            "type": "object",
            "properties": {
                "page_id": {
                    "type": "string",
                    "description": "Page ID to upload attachment to",
                },
                "file_path": {
                    "type": "string",
                    "description": "Path to the file to upload",
                },
                "comment": {
                    "type": "string",
                    "description": "Optional comment for the upload",
                },
            },
            "required": ["page_id", "file_path"],
        },
    ),
]