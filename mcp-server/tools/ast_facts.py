# -*- coding: utf-8 -*-
"""AST-derived facts about the tool/route layer - the "derived facts" step
in registry.py's stated architecture:

    AST / decorators / source  ->  derived facts  ->  registry validation  ->  generated docs

Pure source analysis, no imports of mcp/pyrevit/anything that requires a
runtime environment - so this is safe to import from a CPython test, from
scripts/gen_tool_docs.py, or from anywhere else that needs ground truth
about what tools/routes actually exist, without ever importing the tool or
route modules themselves (several of which require a real pyrevit/DB
environment at import time).

Every function here returns facts about ONE parsed source file; callers
glob tools/*_tools.py or revit_mcp/*.py and combine the results. Nothing
here talks to the filesystem beyond reading the files it's given.
"""
import ast
import glob
import os

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
MCP_SERVER_DIR = os.path.dirname(TOOLS_DIR)
REVIT_MCP_DIR = os.path.join(MCP_SERVER_DIR, "revit_mcp")


def list_tool_files():
    """Every tools/*_tools.py file - the actual tool-definition modules,
    excluding tools/utils.py, tools/registry.py, tools/ast_facts.py and
    tools/__init__.py itself, none of which define @mcp.tool() functions."""
    return sorted(glob.glob(os.path.join(TOOLS_DIR, "*_tools.py")))


def list_route_files():
    """Every revit_mcp/*.py file except __init__.py."""
    return sorted(
        f for f in glob.glob(os.path.join(REVIT_MCP_DIR, "*.py"))
        if not f.endswith("__init__.py")
    )


def _parse(path):
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()
    return ast.parse(source, filename=path)


def _static_string_content(node):
    """The static, guaranteed-present text of an expression node, or None.

    Handles the three shapes this project's route/tool code actually uses
    to build an endpoint path or transaction name:
      - a plain string literal
      - "...{}...".format(x) - the literal text always appears verbatim in
        the real runtime value, regardless of what x evaluates to
      - an f-string (JoinedStr) - concatenates only the literal Constant
        segments, replacing each interpolated {expr} with "<dynamic>" so
        the result is still recognizable as the same route (e.g.
        "/get_view/{view_name}" -> "/get_view/<dynamic>")

    Mirrors tests/test_transaction_names.py's proven extraction technique
    (same three shapes, same reasoning) rather than reinventing a weaker
    version of it.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value

    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "format":
            base = _static_string_content(func.value)
            if base is not None:
                return base

    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            else:
                parts.append("<dynamic>")
        return "".join(parts)

    return None


def extract_tools(path):
    """Every @mcp.tool() async function name defined in one tools/*.py file."""
    tree = _parse(path)
    names = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            is_tool_deco = (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and dec.func.attr == "tool"
            ) or (isinstance(dec, ast.Attribute) and dec.attr == "tool")
            if is_tool_deco:
                names.append(node.name)
                break
    return names


def extract_tool_summaries(path):
    """{tool_name: first_docstring_line} for every @mcp.tool() function in
    one tools/*.py file - the same "one-line description" shape
    mcp-server/README.md's tool tables already use, so generated docs read
    the same way. Empty string if the function has no docstring."""
    tree = _parse(path)
    result = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        is_tool = False
        for dec in node.decorator_list:
            if (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and dec.func.attr == "tool"
            ) or (isinstance(dec, ast.Attribute) and dec.attr == "tool"):
                is_tool = True
                break
        if not is_tool:
            continue
        doc = ast.get_docstring(node) or ""
        first_line = doc.strip().splitlines()[0].strip() if doc.strip() else ""
        result[node.name] = first_line
    return result


def all_tool_summaries():
    """{tool_name: first_docstring_line} across every tools/*_tools.py file."""
    out = {}
    for path in list_tool_files():
        out.update(extract_tool_summaries(path))
    return out


def extract_tool_endpoints(path):
    """{tool_name: [endpoint_path, ...]} for every @mcp.tool() function in
    one tools/*.py file - every string argument to a revit_get/revit_post/
    revit_image call found anywhere in the function body, resolved via
    _static_string_content so "/element_properties/{}".format(element_id)
    and f"/get_view/{view_name}" both resolve to a recognizable path
    instead of silently counting as "calls nothing"."""
    tree = _parse(path)
    result = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        is_tool = False
        for dec in node.decorator_list:
            if (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and dec.func.attr == "tool"
            ) or (isinstance(dec, ast.Attribute) and dec.attr == "tool"):
                is_tool = True
                break
        if not is_tool:
            continue

        endpoints = []
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Name)
                and sub.func.id in ("revit_get", "revit_post", "revit_image")
                and sub.args
            ):
                value = _static_string_content(sub.args[0])
                if value is not None:
                    endpoints.append(value)
        result[node.name] = endpoints
    return result


def extract_routes(path):
    """[(route_path, (methods,), func_name), ...] for every @api.route(...)
    in one revit_mcp/*.py file."""
    tree = _parse(path)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if not (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and dec.func.attr == "route"
            ):
                continue
            route_path = None
            if dec.args:
                route_path = _static_string_content(dec.args[0])
            methods = []
            for kw in dec.keywords:
                if kw.arg == "methods" and isinstance(kw.value, ast.List):
                    methods = [
                        e.value for e in kw.value.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)
                    ]
            out.append((route_path, tuple(methods), node.name))
    return out


def route_calls_commit_verified(path):
    """True if the string "commit_verified" is called anywhere in this
    file - a file-level signal (not per-route), used only as a coarse
    cross-check, not as a source of per-tool truth (registry.py's
    "mutating" field is hand-declared for that reason)."""
    tree = _parse(path)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "commit_verified"
        ):
            return True
    return False


def route_has_transaction(path):
    """True if this file constructs at least one DB.Transaction(...) -
    used by test_tool_registry.py as a one-directional cross-check: a tool
    the registry marks mutating=True must map to a route file that opens
    at least one real transaction (the converse is not asserted -
    preview_delete_impact opens a transaction but is deliberately
    mutating=False, since it is always rolled back)."""
    tree = _parse(path)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "Transaction"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "DB"
        ):
            return True
    return False


def all_tools():
    """{tool_name: source_file_basename} across every tools/*_tools.py file."""
    out = {}
    for path in list_tool_files():
        for name in extract_tools(path):
            out[name] = os.path.basename(path)
    return out


def all_tool_endpoints():
    """{tool_name: [endpoint, ...]} across every tools/*_tools.py file."""
    out = {}
    for path in list_tool_files():
        out.update(extract_tool_endpoints(path))
    return out


def all_routes():
    """[(route_file_basename, path, methods, func_name), ...] across every
    revit_mcp/*.py file."""
    out = []
    for path in list_route_files():
        base = os.path.basename(path)
        for route_path, methods, func_name in extract_routes(path):
            out.append((base, route_path, methods, func_name))
    return out
