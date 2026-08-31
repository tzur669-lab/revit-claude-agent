# -*- coding: utf-8 -*-
"""The one machine-readable source of truth for what an MCP tool IS -
not how many there are or what its route is (both are derived by AST from
tools/*_tools.py and revit_mcp/*.py at test/doc-generation time - see
tests/test_tool_registry.py and scripts/gen_tool_docs.py) but the four
things no amount of source-scanning can safely infer: which bucket a tool
belongs in, how risky it is, whether it changes the model, and whether its
operation contract is written down.

Architecture, deliberately:

    AST / decorators / source  ->  derived facts  ->  registry validation  ->  generated docs

NOT:

    registry  ->  tool count

This module must never be treated as an inventory. If a tool exists in
tools/*_tools.py with no entry here, or an entry here names a tool that no
longer exists, tests/test_tool_registry.py fails in that exact direction -
on purpose, so this file cannot go stale silently the way README.md's tool
counts did.

Field meanings
--------------
category   : one of CATEGORIES below - the same seven buckets
             docs/architecture.md already used (its counts had drifted;
             the buckets themselves were sound, so kept rather than
             reinvented).
risk       : one of RISK_LEVELS below.
                 read        - can never change the model, by construction
                 additive    - only ever creates new elements/files; never
                               touches something that already existed
                 modifying   - changes or removes an EXISTING element's
                               state (parameters, position, view assignment)
                 destructive : removes an element, or - execute_revit_code -
                               can do literally anything, including things
                               no other tool can (filesystem, network,
                               arbitrary API calls). The controlled escape
                               hatch (see docs/architecture.md); classifying
                               it destructive is a deliberate ceiling, not a
                               claim that every submission is destructive.
mutating   : True only when a real, successful call can leave the Revit
             MODEL changed. False for every read tool, AND for
             preview_delete_impact specifically - it opens a real
             DB.Transaction and calls doc.Delete() for real, but that
             transaction is unconditionally rolled back in a `finally`
             block (revit_mcp/impact.py) - by the module's own design, nothing
             a caller does through this tool ever persists. A test cross-
             checks the derivable half of this claim: no tool marked
             mutating=True may map to a route with zero DB.Transaction
             calls in its handler file (test_tool_registry.py).
contract   : where docs/operation-contracts.md stands on this tool today.
                 "documented"        - has a real entry/section there
                 "read_only"         - legitimately read-only; the doc's own
                                       scope is level-1/2 mutation contracts
                                       plus the read-only/dry-run tools it
                                       explicitly does cover (impact.py,
                                       validation.py) - a simple query tool
                                       needs no entry at all
                 "out_of_scope:<reason>" - a mutating tool the doc
                                       DELIBERATELY excludes, with its own
                                       stated reason (currently only
                                       load_family - see
                                       operation-contracts.md's own
                                       "deliberately out of scope" section)
                 "known_gap:<reason>"  - a mutating tool that SHOULD be
                                       documented/verified and currently
                                       is not - a tracked defect, not a
                                       design decision. Extends the three
                                       values named when this registry was
                                       planned; the plan's own vocabulary
                                       had no way to distinguish "we chose
                                       to exclude this" from "this is a
                                       bug we haven't closed yet", and
                                       collapsing the two would have made
                                       tag_walls/set_active_view (real,
                                       already-known gaps - see the M1-M5
                                       architecture upgrade's D6) look like
                                       deliberate exclusions. Noted as a
                                       deviation in that upgrade's
                                       implementation report.

Every tool from every category appears below, grouped by its
tools/*_tools.py file in the same order tools/__init__.py registers them,
so this file can be read top-to-bottom against that one.
"""

CATEGORIES = frozenset(
    ["creation", "query", "editing", "analysis", "documentation", "interop", "advanced"]
)
RISK_LEVELS = frozenset(["read", "additive", "modifying", "destructive"])


def _entry(category, risk, mutating, contract):
    assert category in CATEGORIES, category
    assert risk in RISK_LEVELS, risk
    assert isinstance(mutating, bool)
    assert contract == "documented" or contract == "read_only" or contract.startswith(
        ("out_of_scope:", "known_gap:")
    ), contract
    return {"category": category, "risk": risk, "mutating": mutating, "contract": contract}


TOOLS = {
    # -- status_tools.py --------------------------------------------------
    "get_revit_status": _entry("query", "read", False, "read_only"),
    "get_revit_model_info": _entry("query", "read", False, "read_only"),

    # -- view_tools.py ------------------------------------------------------
    "get_revit_view": _entry("query", "read", False, "read_only"),
    "list_revit_views": _entry("query", "read", False, "read_only"),
    "get_current_view_info": _entry("query", "read", False, "read_only"),
    "get_current_view_elements": _entry("query", "read", False, "read_only"),

    # -- family_tools.py ------------------------------------------------
    "place_family": _entry("creation", "additive", True, "documented"),
    "list_families": _entry("query", "read", False, "read_only"),
    "list_family_categories": _entry("query", "read", False, "read_only"),
    "load_family": _entry("interop", "additive", True, "out_of_scope:no fixed post-condition - loading a family has no single verifiable outcome beyond doc.LoadFamily()'s own bool return; see operation-contracts.md"),

    # -- model_tools.py ---------------------------------------------------
    "list_levels": _entry("query", "read", False, "read_only"),

    # -- colors_tools.py ----------------------------------------------------
    "color_splash": _entry("editing", "modifying", True, "documented"),
    "clear_colors": _entry("editing", "modifying", True, "documented"),
    "list_category_parameters": _entry("query", "read", False, "read_only"),

    # -- code_execution_tools.py -------------------------------------------
    "execute_revit_code": _entry("advanced", "destructive", True, "documented"),

    # -- building_tools.py --------------------------------------------------
    "create_line_based_element": _entry("creation", "additive", True, "documented"),
    "create_surface_based_element": _entry("creation", "additive", True, "documented"),
    "create_level": _entry("creation", "additive", True, "documented"),

    # -- editing_tools.py -----------------------------------------------
    "delete_elements": _entry("editing", "destructive", True, "documented"),
    "modify_element": _entry("editing", "modifying", True, "documented"),
    "get_selected_elements": _entry("query", "read", False, "read_only"),

    # -- structure_tools.py -------------------------------------------------
    "create_grid": _entry("creation", "additive", True, "documented"),
    "create_structural_framing": _entry("creation", "additive", True, "documented"),

    # -- annotation_tools.py ------------------------------------------------
    "create_dimensions": _entry("documentation", "additive", True, "documented"),
    "tag_walls": _entry("editing", "additive", True, "documented"),

    # -- analysis_tools.py --------------------------------------------------
    "ai_element_filter": _entry("analysis", "read", False, "read_only"),
    "export_room_data": _entry("analysis", "read", False, "read_only"),
    "get_material_quantities": _entry("analysis", "read", False, "read_only"),
    "analyze_model_statistics": _entry("analysis", "read", False, "read_only"),

    # -- documentation_tools.py ----------------------------------------
    "create_sheet": _entry("creation", "additive", True, "documented"),
    "create_schedule": _entry("creation", "additive", True, "documented"),
    "export_document": _entry("documentation", "additive", True, "documented"),

    # -- room_tools.py --------------------------------------------------
    "create_room": _entry("creation", "additive", True, "documented"),
    "create_room_separation": _entry("creation", "additive", True, "documented"),

    # -- view_management_tools.py --------------------------------------
    "create_view": _entry("creation", "additive", True, "documented"),
    "set_active_view": _entry("editing", "modifying", True, "documented"),

    # -- tag_tools.py -----------------------------------------------------
    "tag_elements": _entry("editing", "additive", True, "documented"),

    # -- transform_tools.py -------------------------------------------------
    "transform_elements": _entry("editing", "modifying", True, "documented"),

    # -- mep_tools.py -----------------------------------------------------
    "create_duct": _entry("creation", "additive", True, "documented"),
    "create_pipe": _entry("creation", "additive", True, "documented"),
    "create_mep_system": _entry("creation", "additive", True, "documented"),

    # -- parameter_tools.py -------------------------------------------------
    "get_element_properties": _entry("query", "read", False, "read_only"),
    "set_parameter": _entry("editing", "modifying", True, "documented"),

    # -- interop_tools.py ---------------------------------------------------
    "export_ifc": _entry("interop", "additive", True, "documented"),
    "link_file": _entry("interop", "additive", True, "documented"),

    # -- detail_tools.py ----------------------------------------------------
    "create_detail_line": _entry("creation", "additive", True, "documented"),

    # -- clash_tools.py -----------------------------------------------------
    "check_clashes": _entry("analysis", "read", False, "read_only"),

    # -- document_tools.py --------------------------------------------------
    "save_document": _entry("interop", "additive", False, "documented"),

    # -- impact_tools.py ------------------------------------------------
    "analyze_relationships": _entry("analysis", "read", False, "documented"),
    "preview_delete_impact": _entry("analysis", "read", False, "documented"),

    # -- validation_tools.py ------------------------------------------------
    "validate_design": _entry("analysis", "read", False, "documented"),
}
