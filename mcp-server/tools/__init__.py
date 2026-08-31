# -*- coding: utf-8 -*-
"""Tool registration system for Revit MCP Server"""


def register_tools(mcp_server, revit_get_func, revit_post_func, revit_image_func):
    """Register all tools with the MCP server"""
    # Import all tool modules
    from .status_tools import register_status_tools
    from .view_tools import register_view_tools
    from .family_tools import register_family_tools
    from .model_tools import register_model_tools
    from .colors_tools import register_colors_tools
    from .code_execution_tools import register_code_execution_tools
    from .building_tools import register_building_tools
    from .editing_tools import register_editing_tools
    from .structure_tools import register_structure_tools
    from .annotation_tools import register_annotation_tools
    from .analysis_tools import register_analysis_tools
    from .documentation_tools import register_documentation_tools
    from .room_tools import register_room_tools
    from .view_management_tools import register_view_management_tools
    from .tag_tools import register_tag_tools
    from .transform_tools import register_transform_tools
    from .mep_tools import register_mep_tools
    from .parameter_tools import register_parameter_tools
    from .interop_tools import register_interop_tools
    from .detail_tools import register_detail_tools
    from .clash_tools import register_clash_tools
    from .document_tools import register_document_tools
    from .impact_tools import register_impact_tools
    from .validation_tools import register_validation_tools

    # Register tools from each module
    register_status_tools(mcp_server, revit_get_func)
    register_view_tools(mcp_server, revit_get_func, revit_post_func, revit_image_func)
    register_family_tools(mcp_server, revit_get_func, revit_post_func)
    register_model_tools(mcp_server, revit_get_func)
    register_colors_tools(mcp_server, revit_get_func, revit_post_func)
    register_code_execution_tools(
        mcp_server, revit_get_func, revit_post_func, revit_image_func
    )
    register_building_tools(mcp_server, revit_get_func, revit_post_func, revit_image_func)
    register_editing_tools(mcp_server, revit_get_func, revit_post_func, revit_image_func)
    register_structure_tools(mcp_server, revit_get_func, revit_post_func, revit_image_func)
    register_annotation_tools(mcp_server, revit_get_func, revit_post_func, revit_image_func)
    register_analysis_tools(mcp_server, revit_get_func, revit_post_func, revit_image_func)
    register_documentation_tools(mcp_server, revit_get_func, revit_post_func, revit_image_func)
    register_room_tools(mcp_server, revit_get_func, revit_post_func, revit_image_func)
    register_view_management_tools(mcp_server, revit_get_func, revit_post_func, revit_image_func)
    register_tag_tools(mcp_server, revit_get_func, revit_post_func, revit_image_func)
    register_transform_tools(mcp_server, revit_get_func, revit_post_func, revit_image_func)
    register_mep_tools(mcp_server, revit_get_func, revit_post_func, revit_image_func)
    register_parameter_tools(mcp_server, revit_get_func, revit_post_func, revit_image_func)
    register_interop_tools(mcp_server, revit_get_func, revit_post_func, revit_image_func)
    register_detail_tools(mcp_server, revit_get_func, revit_post_func, revit_image_func)
    register_clash_tools(mcp_server, revit_get_func, revit_post_func, revit_image_func)
    register_document_tools(mcp_server, revit_get_func, revit_post_func, revit_image_func)
    register_impact_tools(mcp_server, revit_get_func, revit_post_func, revit_image_func)
    register_validation_tools(mcp_server, revit_get_func, revit_post_func, revit_image_func)

    _attach_registry_annotations(mcp_server)


def _attach_registry_annotations(mcp_server):
    """Attach MCP-standard ToolAnnotations (readOnlyHint / destructiveHint)
    to every already-registered tool, sourced from registry.py's "risk"
    field - one edit site instead of adding annotations= to all 51
    individual @mcp.tool() decorator call sites across 24 files.

    Reaches into FastMCP's private ToolManager (mcp._tool_manager) because
    there is no public API for mutating an already-registered tool's
    annotations - the Tool.annotations field is a plain, mutable pydantic
    field, just not exposed for external mutation. This is exactly the kind
    of internal surface that can move under a dependency bump, so failure
    here is caught and logged, never allowed to break tool registration
    itself: every tool is fully usable with or without this pass succeeding,
    since annotations are advisory metadata, not part of the MCP protocol's
    functional contract. See tests/test_tool_registry.py for the strict
    check that this pass actually took effect - a caught exception here
    must make THAT test fail, not silently look like success.
    """
    try:
        from mcp.types import ToolAnnotations
        from .registry import TOOLS
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            "Could not import ToolAnnotations/registry - tool annotations "
            "not attached: %s", e
        )
        return

    try:
        tool_manager = mcp_server._tool_manager
    except AttributeError as e:
        import logging
        logging.getLogger(__name__).warning(
            "FastMCP's internal tool manager attribute has changed or is "
            "missing - tool annotations not attached: %s", e
        )
        return

    for name, entry in TOOLS.items():
        try:
            tool = tool_manager.get_tool(name)
        except Exception:
            tool = None
        if tool is None:
            continue
        try:
            tool.annotations = ToolAnnotations(
                readOnlyHint=(entry["risk"] == "read"),
                destructiveHint=(entry["risk"] == "destructive"),
            )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "Could not set annotations for tool %r: %s", name, e
            )
