# -*- coding: utf-8 -*-
"""Design validation tools — check rooms against external design standards"""

from mcp.server.fastmcp import Context
from .utils import format_response


def register_validation_tools(mcp, revit_get, revit_post, revit_image=None):
    """Register design-validation tools with the MCP server."""
    _ = revit_get, revit_image  # Acknowledge unused parameters

    @mcp.tool()
    async def validate_design(
        room_ids: list[int] = None,
        rules_path: str = None,
        extra_rules: dict = None,
        ctx: Context = None,
    ) -> str:
        """Check rooms against external design standards (read-only).

        This tool has no jurisdiction of its own — it knows how to check a
        room against a rule, not which country's numbers apply. Reports
        FACT, ASSUMPTION, WARNING and VIOLATION as distinct things for each
        room — never a single pass/fail. Room-type is inferred from the
        room's name (an ASSUMPTION, named explicitly), then checked against
        that type's minimum area; a room type whose rule opts into
        "extended checks" (a building code's own special-purpose room, e.g.
        a protected space or a wet room) also gets net area, bounding-wall
        thickness, ceiling height and volume checked.

        The rule VALUES (the actual area/thickness numbers, and which
        jurisdiction they come from) live in a private, per-user JSON file,
        not in this repo — point rules_path at whichever building code
        applies to this project. If none exists yet at the default path,
        the tool reports that clearly instead of guessing at numbers. Room
        WIDTH, window-area-vs-floor-area rules, and the kitchen
        work-triangle are explicitly not checked yet (reported as
        not_checked, not silently skipped) — see docs/operation-contracts.md.

        Args:
            room_ids: Specific room element ids to check (defaults to every room in the model)
            rules_path: Override path to the rules JSON file (defaults to a per-user path)
            extra_rules: Optional, request-scoped room_types to merge over the rules file by
                "id" (same shape: {"room_types": [...]}) — e.g. a project-specific constraint.
                Never written to disk or to the rules file; applies to this call only.
            ctx: MCP context for logging
        """
        data = {
            "room_ids": room_ids,
            "rules_path": rules_path,
            "extra_rules": extra_rules,
        }
        response = await revit_post("/validate_design/", data, ctx)
        return format_response(response)
