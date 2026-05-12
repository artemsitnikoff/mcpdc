"""Comment tools for Confluence MCP server."""

import logging
from typing import Any, Dict, List

from mcp.server import Server
from mcp.types import Tool, TextContent

from ..client import ConfluenceClient
from ..converters import storage_to_markdown
from ..errors import ConfluenceError

logger = logging.getLogger(__name__)


def register_comment_tools(server: Server, confluence: ConfluenceClient) -> None:
    """Register comment-related MCP tools."""

    @server.call_tool()
    async def confluence_list_comments(arguments: Dict[str, Any]) -> List[TextContent]:
        """
        List comments on a Confluence page.

        Args:
            page_id: Page ID to get comments for
            limit: Maximum number of comments to return (default: 25, max: 100)
            start: Starting index for pagination (default: 0)
            format: Output format - 'storage' (XHTML) or 'markdown' (default: storage)

        Returns:
            List of comments with content and metadata
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
            output_format = arguments.get("format", "storage").lower()

            if output_format not in ["storage", "markdown"]:
                return [TextContent(
                    type="text",
                    text="Error: format must be 'storage' or 'markdown'"
                )]

            # Get comments
            result = await confluence.get_comments(
                page_id=page_id,
                expand="body.storage,version",
                limit=limit,
                start=start,
            )

            # Format comments
            comments = []
            for comment in result.get("results", []):
                storage_content = comment.get("body", {}).get("storage", {}).get("value", "")

                comment_data = {
                    "id": comment["id"],
                    "title": comment.get("title", ""),
                    "author": comment["version"]["by"]["displayName"],
                    "created": comment["version"]["when"],
                    "version": comment["version"]["number"],
                    "url": comment["_links"]["webui"],
                    "content_format": output_format,
                }

                # Add content based on format
                if output_format == "markdown":
                    comment_data["content"] = storage_to_markdown(storage_content)
                else:
                    comment_data["content"] = storage_content

                comments.append(comment_data)

            # Format response
            response = {
                "page_id": page_id,
                "total": result.get("totalSize", len(comments)),
                "start": start,
                "limit": limit,
                "comments": comments,
            }

            # Add pagination info
            if "_links" in result and "next" in result["_links"]:
                response["has_more"] = True
                response["next_start"] = start + limit
            else:
                response["has_more"] = False

            return [TextContent(
                type="text",
                text=f"Comments:\n\n{_format_comments_response(response)}"
            )]

        except ConfluenceError as e:
            logger.error(f"Confluence comments error: {e}")
            return [TextContent(
                type="text",
                text=f"Failed to get comments: {e.message}"
            )]
        except Exception as e:
            logger.error(f"Unexpected comments error: {e}")
            return [TextContent(
                type="text",
                text=f"Failed to get comments: {str(e)}"
            )]

    @server.call_tool()
    async def confluence_add_comment(arguments: Dict[str, Any]) -> List[TextContent]:
        """
        Add a comment to a Confluence page.

        Args:
            page_id: Page ID to add comment to
            content: Comment content in Confluence Storage Format (XHTML)

        Returns:
            Created comment information
        """
        try:
            page_id = arguments.get("page_id")
            content = arguments.get("content")

            if not page_id:
                return [TextContent(
                    type="text",
                    text="Error: 'page_id' parameter is required"
                )]

            if not content:
                return [TextContent(
                    type="text",
                    text="Error: 'content' parameter is required"
                )]

            # Prepare comment data — Confluence 7.4.6 requires container.type=page,
            # otherwise the server responds with 500.
            comment_data = {
                "type": "comment",
                "container": {"id": page_id, "type": "page"},
                "body": {
                    "storage": {
                        "value": content,
                        "representation": "storage",
                    }
                },
            }

            # Create the comment
            result = await confluence.create_content(comment_data)

            response = {
                "id": result["id"],
                "page_id": page_id,
                "url": result["_links"]["webui"],
                "version": result["version"]["number"],
                "created": result["version"]["when"],
                "created_by": result["version"]["by"]["displayName"],
            }

            return [TextContent(
                type="text",
                text=f"Comment Added Successfully:\n\n{_format_comment_create_response(response)}"
            )]

        except ConfluenceError as e:
            logger.error(f"Confluence comment create error: {e}")
            return [TextContent(
                type="text",
                text=f"Failed to add comment: {e.message}"
            )]
        except Exception as e:
            logger.error(f"Unexpected comment create error: {e}")
            return [TextContent(
                type="text",
                text=f"Failed to add comment: {str(e)}"
            )]


def _format_comments_response(response: Dict[str, Any]) -> str:
    """Format comments response for display."""
    lines = [
        f"Page ID: {response['page_id']}",
        f"Total Comments: {response['total']}",
        f"Showing: {response['start'] + 1}-{response['start'] + len(response['comments'])}",
    ]

    if response.get("has_more"):
        lines.append(f"Next page: start={response['next_start']}")

    lines.append("")

    for i, comment in enumerate(response["comments"], 1):
        lines.extend([
            f"{i}. Comment {comment['id']}",
            f"   Author: {comment['author']}",
            f"   Created: {comment['created']}",
            f"   Version: {comment['version']}",
            f"   URL: {comment['url']}",
        ])

        if comment.get("title"):
            lines.append(f"   Title: {comment['title']}")

        lines.extend([
            f"   Content Format: {comment['content_format']}",
            "   Content:",
            "   " + "-" * 30,
        ])

        # Indent content lines
        content_lines = comment['content'].split('\n')
        for line in content_lines:
            lines.append(f"   {line}")

        lines.extend(["   " + "-" * 30, ""])

    return "\n".join(lines)


def _format_comment_create_response(response: Dict[str, Any]) -> str:
    """Format comment create response for display."""
    return "\n".join([
        f"Comment ID: {response['id']}",
        f"Page ID: {response['page_id']}",
        f"URL: {response['url']}",
        f"Version: {response['version']}",
        f"Created: {response['created']}",
        f"Created By: {response['created_by']}",
    ])


# Tool definitions for the MCP server
COMMENT_TOOLS = [
    Tool(
        name="confluence_list_comments",
        description="List comments on a Confluence page",
        inputSchema={
            "type": "object",
            "properties": {
                "page_id": {
                    "type": "string",
                    "description": "Page ID to get comments for",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of comments to return (default: 25, max: 100)",
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
                "format": {
                    "type": "string",
                    "enum": ["storage", "markdown"],
                    "description": "Output format - 'storage' (XHTML) or 'markdown' (default: storage)",
                    "default": "storage",
                },
            },
            "required": ["page_id"],
        },
    ),
    Tool(
        name="confluence_add_comment",
        description="Add a comment to a Confluence page",
        inputSchema={
            "type": "object",
            "properties": {
                "page_id": {
                    "type": "string",
                    "description": "Page ID to add comment to",
                },
                "content": {
                    "type": "string",
                    "description": "Comment content in Confluence Storage Format (XHTML)",
                },
            },
            "required": ["page_id", "content"],
        },
    ),
]