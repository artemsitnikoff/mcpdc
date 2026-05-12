"""Page tools for Confluence MCP server."""

import json
import logging
from typing import Any, Dict, List

from mcp.server import Server
from mcp.types import Tool, TextContent

from ..client import ConfluenceClient
from ..converters import storage_to_markdown, markdown_to_storage_hint
from ..errors import ConfluenceError, ConfluenceNotFoundError

logger = logging.getLogger(__name__)


def register_page_tools(server: Server, confluence: ConfluenceClient) -> None:
    """Register page-related MCP tools."""

    @server.call_tool()
    async def confluence_get_page(arguments: Dict[str, Any]) -> List[TextContent]:
        """
        Get a Confluence page by ID or by space key and title.

        Args:
            id: Page ID (if provided, space_key and title are ignored)
            space_key: Space key (required if id not provided)
            title: Page title (required if id not provided)
            format: Output format - 'storage' (XHTML) or 'markdown' (default: storage)
            include_version: Include version information (default: true)

        Returns:
            Page content with metadata
        """
        try:
            page_id = arguments.get("id")
            space_key = arguments.get("space_key")
            title = arguments.get("title")
            output_format = arguments.get("format", "storage").lower()
            include_version = arguments.get("include_version", True)

            # Validate arguments
            if not page_id and not (space_key and title):
                return [TextContent(
                    type="text",
                    text="Error: Either 'id' or both 'space_key' and 'title' are required"
                )]

            if output_format not in ["storage", "markdown"]:
                return [TextContent(
                    type="text",
                    text="Error: format must be 'storage' or 'markdown'"
                )]

            # Prepare expand parameters
            expand_parts = ["body.storage", "space"]
            if include_version:
                expand_parts.append("version")

            expand = ",".join(expand_parts)

            # Get the page
            if page_id:
                page = await confluence.get_content(page_id, expand=expand)
            else:
                page = await confluence.get_content_by_title(space_key, title, expand=expand)

            # Extract content
            storage_content = page.get("body", {}).get("storage", {}).get("value", "")

            # Format the response
            response = {
                "id": page["id"],
                "title": page["title"],
                "type": page["type"],
                "space": {
                    "key": page["space"]["key"],
                    "name": page["space"]["name"],
                },
                "url": page["_links"]["webui"],
                "content_format": output_format,
            }

            if include_version:
                response["version"] = {
                    "number": page["version"]["number"],
                    "when": page["version"]["when"],
                    "by": page["version"]["by"]["displayName"],
                }

            # Add content based on format
            if output_format == "markdown":
                response["content"] = storage_to_markdown(storage_content)
                response["note"] = "Content converted from Storage Format to Markdown (lossy conversion)"
            else:
                response["content"] = storage_content

            return [TextContent(
                type="text",
                text=f"Page Retrieved:\n\n{_format_page_response(response)}"
            )]

        except ConfluenceNotFoundError as e:
            logger.error(f"Page not found: {e}")
            return [TextContent(
                type="text",
                text=f"Page not found: {e.message}"
            )]
        except ConfluenceError as e:
            logger.error(f"Confluence page error: {e}")
            return [TextContent(
                type="text",
                text=f"Failed to get page: {e.message}"
            )]
        except Exception as e:
            logger.error(f"Unexpected page error: {e}")
            return [TextContent(
                type="text",
                text=f"Failed to get page: {str(e)}"
            )]

    @server.call_tool()
    async def confluence_create_page(arguments: Dict[str, Any]) -> List[TextContent]:
        """
        Create a new Confluence page.

        Args:
            space_key: Space key where the page will be created
            title: Page title
            content: Page content in Confluence Storage Format (XHTML)
            parent_id: Parent page ID (optional)

        Returns:
            Created page information
        """
        try:
            space_key = arguments.get("space_key")
            title = arguments.get("title")
            content = arguments.get("content", "")
            parent_id = arguments.get("parent_id")

            if not space_key or not title:
                return [TextContent(
                    type="text",
                    text="Error: 'space_key' and 'title' are required"
                )]

            # Prepare page data
            page_data = {
                "type": "page",
                "title": title,
                "space": {"key": space_key},
                "body": {
                    "storage": {
                        "value": content,
                        "representation": "storage",
                    }
                },
            }

            # Add parent if specified
            if parent_id:
                page_data["ancestors"] = [{"id": parent_id}]

            # Create the page
            result = await confluence.create_content(page_data)

            response = {
                "id": result["id"],
                "title": result["title"],
                "space": result["space"]["key"],
                "url": result["_links"]["webui"],
                "version": result["version"]["number"],
                "created": result["version"]["when"],
                "created_by": result["version"]["by"]["displayName"],
            }

            return [TextContent(
                type="text",
                text=f"Page Created Successfully:\n\n{_format_create_response(response)}"
            )]

        except ConfluenceError as e:
            logger.error(f"Confluence create error: {e}")
            return [TextContent(
                type="text",
                text=f"Failed to create page: {e.message}"
            )]
        except Exception as e:
            logger.error(f"Unexpected create error: {e}")
            return [TextContent(
                type="text",
                text=f"Failed to create page: {str(e)}"
            )]

    @server.call_tool()
    async def confluence_update_page(arguments: Dict[str, Any]) -> List[TextContent]:
        """
        Update an existing Confluence page.

        Args:
            id: Page ID to update
            title: New page title (optional, keeps current if not provided)
            content: New page content in Storage Format (optional, keeps current if not provided)

        Returns:
            Updated page information
        """
        try:
            page_id = arguments.get("id")
            new_title = arguments.get("title")
            new_content = arguments.get("content")

            if not page_id:
                return [TextContent(
                    type="text",
                    text="Error: 'id' is required"
                )]

            if not new_title and not new_content:
                return [TextContent(
                    type="text",
                    text="Error: At least one of 'title' or 'content' must be provided"
                )]

            # Get current page to preserve existing values
            current_page = await confluence.get_content(
                page_id,
                expand="body.storage,space,version"
            )

            # Prepare update data
            update_data = {
                "id": page_id,
                "type": "page",
                "title": new_title or current_page["title"],
                "space": {"key": current_page["space"]["key"]},
                "body": {
                    "storage": {
                        "value": new_content or current_page["body"]["storage"]["value"],
                        "representation": "storage",
                    }
                },
            }

            # Update the page (client handles version automatically)
            result = await confluence.update_content(page_id, update_data)

            response = {
                "id": result["id"],
                "title": result["title"],
                "space": result["space"]["key"],
                "url": result["_links"]["webui"],
                "version": result["version"]["number"],
                "updated": result["version"]["when"],
                "updated_by": result["version"]["by"]["displayName"],
            }

            return [TextContent(
                type="text",
                text=f"Page Updated Successfully:\n\n{_format_update_response(response)}"
            )]

        except ConfluenceError as e:
            logger.error(f"Confluence update error: {e}")
            return [TextContent(
                type="text",
                text=f"Failed to update page: {e.message}"
            )]
        except Exception as e:
            logger.error(f"Unexpected update error: {e}")
            return [TextContent(
                type="text",
                text=f"Failed to update page: {str(e)}"
            )]

    @server.call_tool()
    async def confluence_delete_page(arguments: Dict[str, Any]) -> List[TextContent]:
        """
        Delete (move to trash) a Confluence page.

        Args:
            id: Page ID to delete

        Returns:
            Confirmation of deletion
        """
        try:
            page_id = arguments.get("id")

            if not page_id:
                return [TextContent(
                    type="text",
                    text="Error: 'id' is required"
                )]

            # Get page info before deletion
            page = await confluence.get_content(page_id, expand="space")

            # Delete the page
            await confluence.delete_content(page_id)

            response = {
                "id": page["id"],
                "title": page["title"],
                "space": page["space"]["key"],
                "status": "deleted",
                "note": "Page moved to trash",
            }

            return [TextContent(
                type="text",
                text=f"Page Deleted Successfully:\n\n{_format_delete_response(response)}"
            )]

        except ConfluenceError as e:
            logger.error(f"Confluence delete error: {e}")
            return [TextContent(
                type="text",
                text=f"Failed to delete page: {e.message}"
            )]
        except Exception as e:
            logger.error(f"Unexpected delete error: {e}")
            return [TextContent(
                type="text",
                text=f"Failed to delete page: {str(e)}"
            )]


def _format_page_response(response: Dict[str, Any]) -> str:
    """Format page response for display."""
    lines = [
        f"Title: {response['title']}",
        f"ID: {response['id']}",
        f"Type: {response['type']}",
        f"Space: {response['space']['name']} ({response['space']['key']})",
        f"URL: {response['url']}",
        f"Content Format: {response['content_format']}",
    ]

    if "version" in response:
        lines.extend([
            f"Version: {response['version']['number']}",
            f"Last Modified: {response['version']['when']}",
            f"Modified By: {response['version']['by']}",
        ])

    if "note" in response:
        lines.append(f"Note: {response['note']}")

    lines.extend(["", "Content:", "=" * 50, response['content']])

    if response['content_format'] == 'storage':
        lines.extend(["", markdown_to_storage_hint()])

    return "\n".join(lines)


def _format_create_response(response: Dict[str, Any]) -> str:
    """Format create response for display."""
    return "\n".join([
        f"Title: {response['title']}",
        f"ID: {response['id']}",
        f"Space: {response['space']}",
        f"URL: {response['url']}",
        f"Version: {response['version']}",
        f"Created: {response['created']}",
        f"Created By: {response['created_by']}",
    ])


def _format_update_response(response: Dict[str, Any]) -> str:
    """Format update response for display."""
    return "\n".join([
        f"Title: {response['title']}",
        f"ID: {response['id']}",
        f"Space: {response['space']}",
        f"URL: {response['url']}",
        f"Version: {response['version']}",
        f"Updated: {response['updated']}",
        f"Updated By: {response['updated_by']}",
    ])


def _format_delete_response(response: Dict[str, Any]) -> str:
    """Format delete response for display."""
    return "\n".join([
        f"Title: {response['title']}",
        f"ID: {response['id']}",
        f"Space: {response['space']}",
        f"Status: {response['status']}",
        f"Note: {response['note']}",
    ])


# Tool definitions for the MCP server
PAGE_TOOLS = [
    Tool(
        name="confluence_get_page",
        description="Get a Confluence page by ID or by space key and title",
        inputSchema={
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Page ID (if provided, space_key and title are ignored)",
                },
                "space_key": {
                    "type": "string",
                    "description": "Space key (required if id not provided)",
                },
                "title": {
                    "type": "string",
                    "description": "Page title (required if id not provided)",
                },
                "format": {
                    "type": "string",
                    "enum": ["storage", "markdown"],
                    "description": "Output format - 'storage' (XHTML) or 'markdown' (default: storage)",
                    "default": "storage",
                },
                "include_version": {
                    "type": "boolean",
                    "description": "Include version information (default: true)",
                    "default": True,
                },
            },
            "anyOf": [
                {"required": ["id"]},
                {"required": ["space_key", "title"]},
            ],
        },
    ),
    Tool(
        name="confluence_create_page",
        description="Create a new Confluence page",
        inputSchema={
            "type": "object",
            "properties": {
                "space_key": {
                    "type": "string",
                    "description": "Space key where the page will be created",
                },
                "title": {
                    "type": "string",
                    "description": "Page title",
                },
                "content": {
                    "type": "string",
                    "description": "Page content in Confluence Storage Format (XHTML)",
                    "default": "",
                },
                "parent_id": {
                    "type": "string",
                    "description": "Parent page ID (optional)",
                },
            },
            "required": ["space_key", "title"],
        },
    ),
    Tool(
        name="confluence_update_page",
        description="Update an existing Confluence page",
        inputSchema={
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Page ID to update",
                },
                "title": {
                    "type": "string",
                    "description": "New page title (optional, keeps current if not provided)",
                },
                "content": {
                    "type": "string",
                    "description": "New page content in Storage Format (optional, keeps current if not provided)",
                },
            },
            "required": ["id"],
        },
    ),
    Tool(
        name="confluence_delete_page",
        description="Delete (move to trash) a Confluence page",
        inputSchema={
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Page ID to delete",
                },
            },
            "required": ["id"],
        },
    ),
]