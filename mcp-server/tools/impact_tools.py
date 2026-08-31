# -*- coding: utf-8 -*-
"""Impact analysis tools — relationship inspection and delete-impact dry runs"""

from mcp.server.fastmcp import Context
from .utils import format_response


def register_impact_tools(mcp, revit_get, revit_post, revit_image=None):
    """Register impact-analysis tools with the MCP server."""
    _ = revit_get, revit_image  # Acknowledge unused parameters

    @mcp.tool()
    async def analyze_relationships(
        element_ids: list[int],
        max_dependents: int = 50,
        ctx: Context = None,
    ) -> str:
        """Inspect what one or more elements are connected to, read-only.

        For each element: Revit's own dependent-element list (grouped by
        category), geometry joins, its host (if it is hosted, e.g. a door in
        a wall), what it hosts (if anything is hosted on it), which rooms it
        bounds (for walls and similar), and which room contains it (for
        point-located content like furniture).

        This is Revit's own *informational* dependency graph — fast, but not
        a guarantee of what an actual delete would remove. For that, use
        preview_delete_impact instead.

        Args:
            element_ids: Element ids to inspect
            max_dependents: Cap on dependents listed per element (defaults to 50)
            ctx: MCP context for logging
        """
        data = {
            "element_ids": element_ids,
            "max_dependents": max_dependents,
        }
        response = await revit_post("/analyze_relationships/", data, ctx)
        return format_response(response)

    @mcp.tool()
    async def preview_delete_impact(
        element_ids: list[int],
        ctx: Context = None,
    ) -> str:
        """Preview what deleting elements would actually remove — nothing is deleted.

        Runs the real delete inside a transaction so Revit's own cascade
        logic executes for real (hosted elements, dangling dimensions, etc.),
        reads back the true resulting set of affected elements, then always
        rolls back. The model is left completely unchanged; this is the
        authoritative "what would break" answer, not a guess — Revit's own
        deletion logic decided the cascade, not a re-derived estimate.

        Args:
            element_ids: Element ids to test-delete
            ctx: MCP context for logging
        """
        data = {"element_ids": element_ids}
        response = await revit_post("/preview_delete_impact/", data, ctx)
        return format_response(response)
