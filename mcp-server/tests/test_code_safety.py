# -*- coding: utf-8 -*-
"""
Unit tests for revit_mcp/code_safety.py - Milestone 4 of the M1-M5
architecture upgrade.

classify() is advisory observability, not a sandbox or security boundary -
see the module's own docstring. These tests prove three things:
  1. every named danger signal is actually detected (imports, __import__,
     file writes, doc.Delete, nested/aliased member calls);
  2. classify() never raises, even on malformed/unparseable input;
  3. classify() never returns anything that could be mistaken for an
     authorization decision - it has no "allowed" concept at all, only a
     risk label and signals.

Whether execution actually proceeds regardless of risk is proven
separately in test_code_execution_never_blocks.py, against the real
execute_code route handler.
"""
from conftest_revit_mcp import import_revit_mcp_module

code_safety = import_revit_mcp_module("code_safety")


# ---------------------------------------------------------------------------
# Destructive signals - imports
# ---------------------------------------------------------------------------

def test_import_os_is_destructive():
    risk, signals = code_safety.classify("import os\nprint(os.getcwd())")
    assert risk == code_safety.RISK_DESTRUCTIVE
    assert any("os" in s for s in signals)


def test_import_subprocess_is_destructive():
    risk, signals = code_safety.classify("import subprocess")
    assert risk == code_safety.RISK_DESTRUCTIVE


def test_from_os_import_is_destructive():
    risk, signals = code_safety.classify("from os import path")
    assert risk == code_safety.RISK_DESTRUCTIVE


def test_import_shutil_is_destructive():
    risk, signals = code_safety.classify("import shutil")
    assert risk == code_safety.RISK_DESTRUCTIVE


def test_import_socket_is_destructive():
    risk, signals = code_safety.classify("import socket")
    assert risk == code_safety.RISK_DESTRUCTIVE


def test_dotted_import_of_destructive_module_is_still_caught():
    """import os.path - the submodule form - must resolve to the same
    top-level "os" the flat form does."""
    risk, signals = code_safety.classify("import os.path")
    assert risk == code_safety.RISK_DESTRUCTIVE


# ---------------------------------------------------------------------------
# Destructive signals - __import__, open(), doc.Delete
# ---------------------------------------------------------------------------

def test_dunder_import_call_is_destructive():
    risk, signals = code_safety.classify('m = __import__("os")')
    assert risk == code_safety.RISK_DESTRUCTIVE
    assert any("__import__" in s for s in signals)


def test_open_read_mode_is_not_destructive():
    risk, signals = code_safety.classify('f = open("x.txt", "r")')
    assert risk != code_safety.RISK_DESTRUCTIVE


def test_open_no_mode_defaults_to_read():
    risk, signals = code_safety.classify('f = open("x.txt")')
    assert risk != code_safety.RISK_DESTRUCTIVE


def test_open_write_mode_is_destructive():
    risk, signals = code_safety.classify('f = open("x.txt", "w")')
    assert risk == code_safety.RISK_DESTRUCTIVE


def test_open_append_mode_is_destructive():
    risk, signals = code_safety.classify('f = open("x.txt", "a")')
    assert risk == code_safety.RISK_DESTRUCTIVE


def test_open_mode_keyword_argument_is_checked():
    risk, signals = code_safety.classify('f = open("x.txt", mode="wb")')
    assert risk == code_safety.RISK_DESTRUCTIVE


def test_open_non_literal_mode_is_not_silently_treated_as_read():
    """The mode is a variable, not a literal - cannot be determined
    statically. Must not be silently classified as safe."""
    risk, signals = code_safety.classify('m = "w"\nf = open("x.txt", m)')
    assert risk != code_safety.RISK_READ
    assert any("mode not statically known" in s for s in signals)


def test_doc_delete_is_destructive():
    risk, signals = code_safety.classify("doc.Delete(element_id)")
    assert risk == code_safety.RISK_DESTRUCTIVE
    assert any("Delete" in s for s in signals)


# ---------------------------------------------------------------------------
# Nested / member calls and aliases
# ---------------------------------------------------------------------------

def test_nested_attribute_delete_is_caught():
    """doc.SubObject.Delete(...) - matched on the final .Delete regardless
    of chain depth."""
    risk, signals = code_safety.classify("doc.Regeneration.Delete(x)")
    assert risk == code_safety.RISK_DESTRUCTIVE


def test_aliased_doc_delete_is_still_caught():
    """d = doc; d.Delete(x) - the alias doesn't hide the .Delete call,
    since the match is purely on the final attribute name, not on
    resolving what the receiver object is."""
    risk, signals = code_safety.classify("d = doc\nd.Delete(element_id)")
    assert risk == code_safety.RISK_DESTRUCTIVE


def test_generic_mutation_verb_is_at_least_modify():
    risk, signals = code_safety.classify('elem.LookupParameter("Comments").Set("x")')
    assert risk in (code_safety.RISK_MODIFY, code_safety.RISK_DESTRUCTIVE)
    assert risk != code_safety.RISK_READ


def test_new_element_call_is_at_least_modify():
    risk, signals = code_safety.classify("wall = doc.Create.NewWall(curve, level_id, False)")
    assert risk != code_safety.RISK_READ


# ---------------------------------------------------------------------------
# Read-only code stays read
# ---------------------------------------------------------------------------

def test_pure_query_is_read():
    code = (
        "walls = DB.FilteredElementCollector(doc)."
        "OfCategory(DB.BuiltInCategory.OST_Walls).ToElements()\n"
        "print(len(walls))"
    )
    risk, signals = code_safety.classify(code)
    assert risk == code_safety.RISK_READ


def test_empty_string_is_read_with_no_signals():
    risk, signals = code_safety.classify("")
    assert risk == code_safety.RISK_READ
    assert signals == []


# ---------------------------------------------------------------------------
# Malformed code never raises
# ---------------------------------------------------------------------------

def test_malformed_code_returns_unknown_not_an_exception():
    risk, signals = code_safety.classify("def f(:\n    this is not python")
    assert risk == code_safety.RISK_UNKNOWN
    assert any("parse_error" in s for s in signals)


def test_completely_garbage_input_does_not_raise():
    risk, signals = code_safety.classify("!!! @@@ ### not python at all $$$")
    assert risk == code_safety.RISK_UNKNOWN


def test_none_like_empty_bytes_input_does_not_raise():
    # code_execution.py itself rejects an empty string before ever calling
    # classify(), but classify() itself must not raise if it somehow gets one.
    risk, signals = code_safety.classify("\n\n\n")
    assert risk == code_safety.RISK_READ


# ---------------------------------------------------------------------------
# #!notx flag detection - reported, never interpreted
# ---------------------------------------------------------------------------

def test_notx_flag_detected():
    risk, signals = code_safety.classify("#!notx\nprint('hi')")
    assert "notx" in signals


def test_notx_flag_with_leading_whitespace_detected():
    assert code_safety.has_notx_flag("   #!notx\ncode")


def test_no_notx_flag_when_absent():
    risk, signals = code_safety.classify("print('hi')")
    assert "notx" not in signals


def test_notx_does_not_by_itself_change_risk_classification():
    """#!notx is orthogonal to risk - a #!notx read-only script is still
    "read", and a #!notx script that deletes something is still
    "destructive". The flag is reported as a signal, never folded into
    the risk level itself."""
    risk_read, _ = code_safety.classify("#!notx\nprint('hi')")
    assert risk_read == code_safety.RISK_READ

    risk_destructive, _ = code_safety.classify("#!notx\ndoc.Delete(x)")
    assert risk_destructive == code_safety.RISK_DESTRUCTIVE


# ---------------------------------------------------------------------------
# classify() has no "allowed" concept - it cannot be mistaken for a gate
# ---------------------------------------------------------------------------

def test_classify_return_shape_has_no_allow_or_block_field():
    for code in ("import os", "doc.Delete(1)", "print(1)", "not python !!!"):
        result = code_safety.classify(code)
        assert isinstance(result, tuple) and len(result) == 2
        risk, signals = result
        assert risk in (
            code_safety.RISK_READ, code_safety.RISK_MODIFY,
            code_safety.RISK_DESTRUCTIVE, code_safety.RISK_UNKNOWN,
        )
        assert isinstance(signals, list)
