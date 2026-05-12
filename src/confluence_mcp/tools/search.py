"""Search tools for Confluence MCP server."""

import logging
from typing import Any, Dict, List

from mcp.server import Server
from mcp.types import Tool, TextContent

from ..client import ConfluenceClient
from ..errors import ConfluenceError

logger = logging.getLogger(__name__)


def register_search_tools(server: Server, confluence: ConfluenceClient) -> None:
    """Register search-related MCP tools."""

    @server.call_tool()
    async def confluence_search(arguments: Dict[str, Any]) -> List[TextContent]:
        """
        Search Confluence content using CQL (Confluence Query Language).

        Args:
            cql: CQL query string (e.g., "space = 'DEMO' and type = 'page'")
            limit: Maximum number of results to return (default: 25, max: 100)
            start: Starting index for pagination (default: 0)
            include_excerpt: Include content excerpt in results (default: true)

        Returns:
            List of matching content with id, title, space, type, and optionally excerpt.
        """
        try:
            cql = arguments.get("cql")
            if not cql:
                return [TextContent(
                    type="text",
                    text="Error: 'cql' parameter is required"
                )]

            limit = min(int(arguments.get("limit", 25)), 100)
            start = int(arguments.get("start", 0))
            include_excerpt = arguments.get("include_excerpt", True)

            # Set up expand parameters
            expand_parts = ["space", "version"]
            if include_excerpt:
                expand_parts.append("excerpt")

            expand = ",".join(expand_parts)

            # Perform search
            result = await confluence.search_content(
                cql=cql,
                limit=limit,
                start=start,
                expand=expand,
            )

            # Format results
            search_results = []
            for item in result.get("results", []):
                search_result = {
                    "id": item["id"],
                    "title": item["title"],
                    "type": item["type"],
                    "space": {
                        "key": item["space"]["key"],
                        "name": item["space"]["name"],
                    },
                    "url": item["_links"]["webui"],
                    "version": item["version"]["number"],
                }

                if include_excerpt and "excerpt" in item:
                    search_result["excerpt"] = item["excerpt"]

                search_results.append(search_result)

            # Format response
            response = {
                "query": cql,
                "total": result.get("totalSize", len(search_results)),
                "start": start,
                "limit": limit,
                "results": search_results,
            }

            # Add pagination info
            if "_links" in result and "next" in result["_links"]:
                response["has_more"] = True
                response["next_start"] = start + limit
            else:
                response["has_more"] = False

            return [TextContent(
                type="text",
                text=f"Search Results:\n\n{_format_search_results(response)}"
            )]

        except ConfluenceError as e:
            logger.error(f"Confluence search error: {e}")
            return [TextContent(
                type="text",
                text=f"Search failed: {e.message}"
            )]
        except Exception as e:
            logger.error(f"Unexpected search error: {e}")
            return [TextContent(
                type="text",
                text=f"Search failed: {str(e)}"
            )]


def _format_search_results(response: Dict[str, Any]) -> str:
    """Format search results for display."""
    lines = [
        f"Query: {response['query']}",
        f"Total: {response['total']} results",
        f"Showing: {response['start'] + 1}-{response['start'] + len(response['results'])}",
    ]

    if response.get("has_more"):
        lines.append(f"Next page: start={response['next_start']}")

    lines.append("")

    for i, result in enumerate(response["results"], 1):
        lines.extend([
            f"{i}. {result['title']}",
            f"   ID: {result['id']}",
            f"   Type: {result['type']}",
            f"   Space: {result['space']['name']} ({result['space']['key']})",
            f"   Version: {result['version']}",
            f"   URL: {result['url']}",
        ])

        if "excerpt" in result and result["excerpt"]:
            excerpt = result["excerpt"].strip()
            if excerpt:
                lines.append(f"   Excerpt: {excerpt[:200]}{'...' if len(excerpt) > 200 else ''}")

        lines.append("")

    return "\n".join(lines)


# Tool definitions for the MCP server
SEARCH_TOOLS = [
    Tool(
        name="confluence_search",
        description="Search Confluence content using CQL (Confluence Query Language)",
        inputSchema={
            "type": "object",
            "properties": {
                "cql": {
                    "type": "string",
                    "description": "CQL query string (e.g., 'space = \"DEMO\" and type = \"page\"')",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results (default: 25, max: 100)",
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
                "include_excerpt": {
                    "type": "boolean",
                    "description": "Include content excerpt in results (default: true)",
                    "default": True,
                },
            },
            "required": ["cql"],
        },
    ),
]