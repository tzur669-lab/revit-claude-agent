# -*- coding: utf-8 -*-
"""
Verifies tools/__init__.py's _attach_registry_annotations actually took
effect - not merely that it ran without raising.

The runtime pass is wrapped in try/except (see its own docstring) so a
future FastMCP internal change degrades registration to "no annotations"
rather than crashing the whole server - that degradation is acceptable in
production. It is NOT acceptable here: this test constructs a real
FastMCP server, runs the real registration path, and inspects the actual
registered Tool objects' .annotations field directly. If a dependency bump
ever breaks the private _tool_manager access this pass relies on, this
test must fail loudly rather than silently pass because "nothing raised".
"""
import os
import sys

_MCP_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _MCP_SERVER_DIR not in sys.path:
    sys.path.insert(0, _MCP_SERVER_DIR)

import pytest

fastmcp = pytest.importorskip("mcp.server.fastmcp")
FastMCP = fastmcp.FastMCP

from tools import register_tools
from tools.registry import TOOLS


async def _noop_get(*args, **kwargs):
    return {}


async def _noop_post(*args, **kwargs):
    return {}


async def _noop_image(*args, **kwargs):
    return {}


@pytest.fixture(scope="module")
def registered_mcp():
    mcp = FastMCP("test-server")
    register_tools(mcp, _noop_get, _noop_post, _noop_image)
    return mcp


def _get_tool(mcp, name):
    return mcp._tool_manager.get_tool(name)


def test_every_registry_tool_is_actually_registered_on_the_server(registered_mcp):
    missing = [name for name in TOOLS if _get_tool(registered_mcp, name) is None]
    assert not missing, (
        "tool(s) in registry.py were not found on the real, registered "
        "FastMCP server - registration itself is broken: %s" % missing
    )


def test_every_read_tool_has_read_only_hint_true(registered_mcp):
    wrong = []
    for name, entry in TOOLS.items():
        if entry["risk"] != "read":
            continue
        tool = _get_tool(registered_mcp, name)
        annotations = tool.annotations
        if annotations is None or annotations.readOnlyHint is not True:
            wrong.append((name, annotations))
    assert not wrong, (
        "read-risk tool(s) missing readOnlyHint=True on the actual "
        "registered Tool object: %r" % wrong
    )


def test_every_destructive_tool_has_destructive_hint_true(registered_mcp):
    wrong = []
    for name, entry in TOOLS.items():
        if entry["risk"] != "destructive":
            continue
        tool = _get_tool(registered_mcp, name)
        annotations = tool.annotations
        if annotations is None or annotations.destructiveHint is not True:
            wrong.append((name, annotations))
    assert not wrong, (
        "destructive-risk tool(s) missing destructiveHint=True on the "
        "actual registered Tool object: %r" % wrong
    )


def test_no_read_tool_has_destructive_hint_true(registered_mcp):
    """The converse - a read tool must never be marked destructive."""
    wrong = []
    for name, entry in TOOLS.items():
        if entry["risk"] != "read":
            continue
        tool = _get_tool(registered_mcp, name)
        annotations = tool.annotations
        if annotations is not None and annotations.destructiveHint is True:
            wrong.append(name)
    assert not wrong, "read-risk tool(s) incorrectly marked destructiveHint=True: %r" % wrong


def test_no_destructive_tool_has_read_only_hint_true(registered_mcp):
    """The converse - a destructive tool must never be marked read-only."""
    wrong = []
    for name, entry in TOOLS.items():
        if entry["risk"] != "destructive":
            continue
        tool = _get_tool(registered_mcp, name)
        annotations = tool.annotations
        if annotations is not None and annotations.readOnlyHint is True:
            wrong.append(name)
    assert not wrong, "destructive-risk tool(s) incorrectly marked readOnlyHint=True: %r" % wrong


def test_every_applicable_tool_has_some_annotations_object(registered_mcp):
    """Every tool this repo classifies with a risk level must have SOME
    ToolAnnotations attached - not None. A None here means the attachment
    pass silently failed for that tool (or all tools), which the try/except
    in production is specifically allowed to do - but not unnoticed here."""
    missing = [
        name for name in TOOLS
        if _get_tool(registered_mcp, name).annotations is None
    ]
    assert not missing, (
        "tool(s) have no ToolAnnotations at all - the attachment pass in "
        "tools/__init__.py._attach_registry_annotations silently failed: %s"
        % missing
    )
