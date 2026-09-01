# -*- coding: UTF-8 -*-
"""Advisory risk classification for code submitted to execute_revit_code.

This is NOT a sandbox, NOT a security boundary, and NOT a guarantee of
safety. It is a best-effort, static AST scan that produces metadata for
observability and the audit trail - it must NEVER prevent execution (see
classify()'s own docstring) and it does NOT claim to detect every
dangerous operation a piece of Python can perform. A submission that does
nothing more exotic than `element.LookupParameter("Comments").Set(x)`
mutates the model just as much as `doc.Delete(id)` does, and no purely
syntactic scan can tell every such case apart from a read with full
confidence - this module aims for the named, well-known danger signals
(filesystem/process/network imports, __import__, file writes, doc.Delete)
plus a broad, deliberately-approximate "this looks like it changes
something" signal, not a complete taxonomy.

Pure logic, no pyrevit/DB import - importable and testable under plain
CPython, and written IronPython 2.7-compatible (no f-strings, no
Python-3-only syntax) since it also runs inside code_execution.py's real
route handler.
"""
import ast

RISK_READ = "read"
RISK_MODIFY = "modify"
RISK_DESTRUCTIVE = "destructive"
RISK_UNKNOWN = "unknown"

# Modules whose mere presence - regardless of what they're used for -
# reaches well outside the Revit model: filesystem, process, network.
_DESTRUCTIVE_IMPORT_MODULES = frozenset([
    "os", "subprocess", "shutil", "socket", "sys", "ctypes",
    "urllib", "urllib2", "httplib", "winreg",
])

# Open modes that write, append, or create - not merely "r"/"rb"/omitted.
_WRITE_MODE_MARKERS = ("w", "a", "x", "+")

# Broad, approximate set of method-name verbs that suggest a mutation is
# happening somewhere in the call - deliberately generic since Revit API
# mutation methods vary widely (Set, Create, Insert, regenerate, ...) and
# a name-based heuristic cannot enumerate them exhaustively. Matched by
# suffix/prefix, not exact string, to catch NewWall/NewFamilyInstance/etc.
_MODIFY_LIKE_PREFIXES = ("Set", "New", "Create", "Insert", "Add", "Remove", "Move", "Delete", "Write")
_MODIFY_LIKE_EXACT = frozenset(["Regenerate", "Commit", "RollBack"])


def has_notx_flag(code):
    """True if the submission opts out of the wrapping transaction - the
    #!notx convention in code_execution.py. Detecting this does not change
    its meaning or behavior; it is purely reported as a signal."""
    return code.lstrip().startswith("#!notx")


try:
    _string_types = (str, unicode)
except NameError:
    _string_types = (str,)


def _string_literal_value(node):
    """Read a string literal AST node's value across both AST dialects this
    module has to run under: CPython 3.8+ represents it as
    ast.Constant(value=...), while IronPython 2.7's ast module has no
    Constant type at all and instead uses the pre-3.8 ast.Str(s=...) node.
    ast.Constant literally does not exist as an attribute under IronPython
    - referencing it unconditionally (as this function used to do) raises
    AttributeError, uncaught, from inside classify()'s AST walk - which
    contradicts classify()'s own "NEVER raises" guarantee and, worse,
    happens before the real code has even started executing, silently
    blocking execution outright. Found live, 2026-09-01, by submitting a
    script containing a plain `open(path, "w")` call through the real
    IronPython route.

    Matched by class name rather than `isinstance(node, ast.Constant)` so
    this never touches the (possibly absent) ast.Constant attribute at
    all. Returns None for anything that isn't a string literal in either
    dialect, same as the "could not determine statically" case already
    handled by this function's caller."""
    cls_name = node.__class__.__name__
    if cls_name == "Constant":
        value = getattr(node, "value", None)
        return value if isinstance(value, _string_types) else None
    if cls_name == "Str":
        value = getattr(node, "s", None)
        return value if isinstance(value, _string_types) else None
    return None


def _open_call_is_write(node):
    """Best-effort read of open()'s mode argument. Returns True (write),
    False (read), or None (could not determine statically - e.g. the mode
    is a variable, not a literal) - callers treat None conservatively."""
    mode_arg = None
    if len(node.args) >= 2:
        mode_arg = node.args[1]
    else:
        for kw in node.keywords:
            if kw.arg == "mode":
                mode_arg = kw.value
                break
    if mode_arg is None:
        return False  # open(path) alone defaults to "r"
    mode = _string_literal_value(mode_arg)
    if mode is not None:
        return any(marker in mode for marker in _WRITE_MODE_MARKERS)
    return None


def _import_module_names(node):
    if isinstance(node, ast.Import):
        return [alias.name.split(".")[0] for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        if node.module:
            return [node.module.split(".")[0]]
        return []
    return []


def classify(code):
    """Classify submitted code by risk. Returns (risk, signals):
    risk is one of RISK_READ/RISK_MODIFY/RISK_DESTRUCTIVE/RISK_UNKNOWN;
    signals is a list of short strings naming what was detected (possibly
    empty). NEVER raises - a code string that fails to parse degrades to
    (RISK_UNKNOWN, ["parse_error: ..."]), never an exception, since
    classify() runs before the real exec() and must not itself become a
    new way for execute_revit_code to fail.

    This function makes NO decision about whether to run the code - see
    this module's own docstring. It only observes."""
    signals = []
    if has_notx_flag(code):
        signals.append("notx")

    try:
        tree = ast.parse(code)
    except Exception as e:
        signals.append("parse_error: {}".format(str(e)))
        return RISK_UNKNOWN, signals

    risk = RISK_READ

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for mod in _import_module_names(node):
                if mod in _DESTRUCTIVE_IMPORT_MODULES:
                    signals.append("import: {}".format(mod))
                    risk = RISK_DESTRUCTIVE

        elif isinstance(node, ast.Call):
            func = node.func

            if isinstance(func, ast.Name) and func.id == "__import__":
                signals.append("call: __import__")
                risk = RISK_DESTRUCTIVE

            elif isinstance(func, ast.Name) and func.id == "open":
                is_write = _open_call_is_write(node)
                if is_write is True:
                    signals.append("open: write-mode")
                    risk = RISK_DESTRUCTIVE
                elif is_write is None:
                    signals.append("open: mode not statically known")
                    if risk == RISK_READ:
                        risk = RISK_MODIFY

            elif isinstance(func, ast.Attribute):
                # Member calls: obj.Delete(...), obj.SubMember.Delete(...),
                # doc.Delete(...) - matched on the final attribute name
                # regardless of how deep the attribute chain is (aliases
                # like "d = doc; d.Delete(x)" still resolve here since the
                # match is on .attr, not on what obj itself is).
                if func.attr == "Delete":
                    signals.append("call: .Delete(...)")
                    risk = RISK_DESTRUCTIVE
                elif func.attr in _MODIFY_LIKE_EXACT or any(
                    func.attr.startswith(p) for p in _MODIFY_LIKE_PREFIXES
                ):
                    signals.append("call: .{}(...)".format(func.attr))
                    if risk == RISK_READ:
                        risk = RISK_MODIFY

    return risk, signals
