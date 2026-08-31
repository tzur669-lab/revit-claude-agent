# -*- coding: utf-8 -*-
"""
Registry/drift enforcement (Milestone 2 of the M1-M5 architecture upgrade).

The root cause the M1-M5 upgrade found for this repo's documentation drift
(README.md claiming 48 tools three times and 51 once in the same file;
mcp-server/LLM.txt claiming 31; docs/architecture.md's own category table
not summing to its own row count) was structural: no test anywhere
asserted a documented count against the actual code. These tests are that
missing check, built on the same technique tests/test_transaction_names.py
already proved for the tracker/handler coupling: derive the real facts by
walking the AST of the actual source files, then assert claims against
that derivation - never against another hand-maintained number.

tools/registry.py carries ONLY the four fields no source-scan can safely
infer (category, risk, mutating, contract) - see its own module docstring.
Everything else asserted here (which tools exist, which routes exist, how
they map to each other) comes from tools/ast_facts.py, which parses source
directly and imports nothing that requires a runtime environment.
"""
import os
import re
import sys

# Same technique as test_format_response.py: tools/ is a proper package but
# tests/ has no __init__.py, so pytest's default import mode only prepends
# tests/ itself onto sys.path, not its parent - mcp-server/ needs adding
# explicitly to import "tools.ast_facts"/"tools.registry" by their real
# package path.
_MCP_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _MCP_SERVER_DIR not in sys.path:
    sys.path.insert(0, _MCP_SERVER_DIR)

from tools import ast_facts
from tools.registry import TOOLS, CATEGORIES, RISK_LEVELS

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MCP_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# The registry itself: every real tool has an entry, every entry names a
# real tool - fails in both directions, per this milestone's own rule.
# ---------------------------------------------------------------------------

def test_registry_has_no_missing_tools():
    real_tools = set(ast_facts.all_tools().keys())
    registry_tools = set(TOOLS.keys())
    missing = real_tools - registry_tools
    assert not missing, (
        "tool(s) exist in tools/*_tools.py with NO registry.py entry - add "
        "one: %s" % sorted(missing)
    )


def test_registry_has_no_stale_tools():
    real_tools = set(ast_facts.all_tools().keys())
    registry_tools = set(TOOLS.keys())
    stale = registry_tools - real_tools
    assert not stale, (
        "registry.py names tool(s) that no longer exist in tools/*_tools.py "
        "- remove: %s" % sorted(stale)
    )


def test_every_registry_entry_has_valid_fields():
    for name, entry in TOOLS.items():
        assert entry["category"] in CATEGORIES, "%s: bad category %r" % (name, entry["category"])
        assert entry["risk"] in RISK_LEVELS, "%s: bad risk %r" % (name, entry["risk"])
        assert isinstance(entry["mutating"], bool), "%s: mutating must be bool" % name
        contract = entry["contract"]
        assert contract == "documented" or contract == "read_only" or contract.startswith(
            ("out_of_scope:", "known_gap:")
        ), "%s: bad contract %r" % (name, contract)


# ---------------------------------------------------------------------------
# Tool <-> route relationship - derived, not assumed. If the real
# architecture were not what these assert, the fix is to change the
# assertion to match reality, not to restructure the code to fit a
# pre-conceived shape (see this milestone's own governing rule).
# ---------------------------------------------------------------------------

def _normalize_path(p):
    """Collapse both route-declaration path params (/foo/<element_id>) and
    a tool's own dynamic-endpoint construction (/foo/{} from .format(), or
    /foo/<dynamic> from ast_facts's f-string handling) to the same
    placeholder, so a route and the tool that calls it compare equal
    regardless of which syntax built the dynamic segment."""
    if p is None:
        return None
    p = re.sub(r"/<[^>]+>", "/<param>", p)
    p = re.sub(r"/\{\}", "/<param>", p)
    return p


def test_every_tool_calls_at_least_one_recognizable_endpoint():
    endpoints = ast_facts.all_tool_endpoints()
    unresolved = [name for name, eps in endpoints.items() if not eps]
    assert not unresolved, (
        "tool(s) whose endpoint call(s) could not be statically resolved "
        "(ast_facts.extract_tool_endpoints doesn't yet handle however this "
        "endpoint string is built) - extend _static_string_content rather "
        "than silently treating this as 'calls nothing': %s" % unresolved
    )


def test_every_called_endpoint_matches_a_registered_route():
    endpoints = ast_facts.all_tool_endpoints()
    routes = ast_facts.all_routes()
    route_paths = {_normalize_path(path) for _file, path, _methods, _fn in routes}

    orphan_calls = {}
    for tool_name, eps in endpoints.items():
        for ep in eps:
            if _normalize_path(ep) not in route_paths:
                orphan_calls.setdefault(tool_name, []).append(ep)
    assert not orphan_calls, (
        "tool(s) call an endpoint with no matching @api.route in "
        "revit_mcp/*.py: %r" % orphan_calls
    )


def test_every_registered_route_is_called_by_at_least_one_tool():
    endpoints = ast_facts.all_tool_endpoints()
    routes = ast_facts.all_routes()
    called_paths = {_normalize_path(ep) for eps in endpoints.values() for ep in eps}

    orphan_routes = [
        (file, path, fn) for file, path, _methods, fn in routes
        if _normalize_path(path) not in called_paths
    ]
    assert not orphan_routes, (
        "route(s) registered in revit_mcp/*.py with no tool wrapper calling "
        "them: %r" % orphan_routes
    )


def test_tool_and_route_totals_are_equal():
    """Not an assumption of 1:1 - a derived fact this repo happens to
    satisfy today (51 tools, 51 routes, checked individually above). If a
    future change makes this genuinely N:M, this specific equality check
    is the one to relax, with a comment explaining the new shape - the
    per-tool/per-route checks above are what actually matter."""
    tools_total = len(ast_facts.all_tools())
    routes_total = len(ast_facts.all_routes())
    assert tools_total == routes_total, (
        "tool count (%d) and route count (%d) diverged - see the "
        "per-tool/per-route tests above for exactly which side has the "
        "extra or missing entries" % (tools_total, routes_total)
    )


# ---------------------------------------------------------------------------
# mutating=True cross-check (one-directional, by design - see registry.py's
# docstring on preview_delete_impact for why the converse is not asserted).
# ---------------------------------------------------------------------------

def test_every_mutating_tool_maps_to_a_route_file_with_a_transaction():
    tool_to_file = ast_facts.all_tools()
    route_files_with_tx = {
        f for f in ast_facts.list_route_files() if ast_facts.route_has_transaction(f)
    }
    route_file_basenames_with_tx = {os.path.basename(f) for f in route_files_with_tx}

    # Map tool name -> the revit_mcp/*.py basename it actually calls into,
    # via its endpoint's route file (not its tools/*_tools.py file name -
    # those don't correspond 1:1, e.g. family_tools.py's tools live in
    # revit_mcp/placement.py).
    endpoints = ast_facts.all_tool_endpoints()
    routes = ast_facts.all_routes()
    path_to_route_file = {_normalize_path(path): file for file, path, _methods, _fn in routes}

    violations = []
    for tool_name, entry in TOOLS.items():
        if not entry["mutating"]:
            continue
        eps = endpoints.get(tool_name, [])
        route_files = {path_to_route_file.get(_normalize_path(ep)) for ep in eps}
        if not route_files & route_file_basenames_with_tx:
            violations.append(tool_name)

    assert not violations, (
        "tool(s) marked mutating=True in registry.py whose route file "
        "contains no DB.Transaction call at all - either the registry "
        "entry is wrong, or the route handler genuinely never persists "
        "anything: %s" % violations
    )


# ---------------------------------------------------------------------------
# Contract coverage: every mutating tool is either documented or has an
# explicit reason it is not.
# ---------------------------------------------------------------------------

def test_every_mutating_tool_is_documented_or_has_a_reason():
    contract_path = os.path.join(REPO_ROOT, "docs", "operation-contracts.md")
    with open(contract_path, "r", encoding="utf-8") as f:
        contract_text = f.read()

    undocumented = []
    for name, entry in TOOLS.items():
        if not entry["mutating"]:
            continue
        contract = entry["contract"]
        if contract == "documented":
            if name not in contract_text:
                undocumented.append(
                    "%s: registry says 'documented' but the tool name does not "
                    "appear anywhere in docs/operation-contracts.md" % name
                )
        elif contract.startswith(("out_of_scope:", "known_gap:")):
            continue
        else:
            undocumented.append(
                "%s: mutating=True but contract=%r has no reason and no "
                "documented entry" % (name, contract)
            )
    assert not undocumented, "\n".join(undocumented)


# ---------------------------------------------------------------------------
# Tool-count claims in the docs must match the derived count - the
# specific defect this milestone exists to make impossible again.
# ---------------------------------------------------------------------------

def _read(*parts):
    with open(os.path.join(REPO_ROOT, *parts), "r", encoding="utf-8") as f:
        return f.read()


def test_readme_tool_counts_match_derived_count():
    real_count = len(ast_facts.all_tools())
    text = _read("README.md")
    # Every "<N> tool(s)"/"<N> MCP tool(s)"/"<N> tool definitions" claim,
    # anywhere in the file - not just one hand-picked line, since the
    # original defect was THREE separate stale lines plus one correct one
    # in the same file.
    claims = re.findall(r"(\d+)\s+(?:MCP\s+)?tool(?:s|\s+definitions)?\b", text)
    bad = [n for n in claims if int(n) != real_count]
    assert not bad, (
        "README.md contains tool-count claim(s) %r that do not match the "
        "derived count %d" % (bad, real_count)
    )


def test_mcp_server_readme_tool_counts_match_derived_count():
    real_count = len(ast_facts.all_tools())
    text = _read("mcp-server", "README.md")
    claims = re.findall(r"(\d+)\s+tools\b", text)
    heading_claims = re.findall(r"Supported Tools \((\d+)\)", text)
    bad = [n for n in claims + heading_claims if int(n) != real_count]
    assert not bad, (
        "mcp-server/README.md contains tool-count claim(s) %r that do not "
        "match the derived count %d" % (bad, real_count)
    )


def test_mcp_server_readme_category_headers_match_their_own_row_counts():
    """The specific drift this milestone found: a category heading like
    "### Modify (8)" whose table beneath it actually has 9 rows. Checks
    the heading's own claim against the table immediately under it -
    independent of registry.py, since this is about internal
    self-consistency of one file, not about matching the code."""
    text = _read("mcp-server", "README.md")
    sections = re.findall(
        r"^### .+?\((\d+)\)\s*\n\n\|.+?\n\|[-| ]+\n((?:\|.+\n?)+)",
        text,
        re.MULTILINE,
    )
    mismatches = []
    for claimed, table_body in sections:
        row_count = len([line for line in table_body.strip().splitlines() if line.strip()])
        if int(claimed) != row_count:
            mismatches.append((claimed, row_count))
    assert sections, "no category sections matched - the README's table format may have changed"
    assert not mismatches, (
        "category heading(s) whose claimed count does not match their own "
        "table's row count: %r" % mismatches
    )


def test_architecture_md_category_table_sums_to_the_derived_total():
    real_count = len(ast_facts.all_tools())
    text = _read("docs", "architecture.md")
    rows = re.findall(r"^\|\s*(\d+)\s*\|\s*[A-Za-z].*\|", text, re.MULTILINE)
    assert rows, "no category-count table rows matched in docs/architecture.md"
    total = sum(int(n) for n in rows)
    assert total == real_count, (
        "docs/architecture.md's per-category tool counts sum to %d, not "
        "the derived total %d" % (total, real_count)
    )


_ARCHITECTURE_MD_CATEGORY_NAMES = {
    "creation": "Creation", "query": "Query", "editing": "Editing",
    "analysis": "Analysis", "documentation": "Documentation",
    "interop": "Interop and save", "advanced": "Advanced",
}


def test_architecture_md_category_table_rows_match_individually():
    """A stronger check than the sum test above: two rows can be
    individually wrong (one over, one under) and still sum correctly - this
    project measured exactly that once (Editing claimed 8 instead of 9,
    Documentation claimed 3 instead of 2, and the total of 51 stayed
    accidentally correct throughout). Checks every row's own count against
    registry.py's actual per-category tally, by name, not just the total."""
    from collections import Counter
    real_counts = Counter(v["category"] for v in TOOLS.values())

    text = _read("docs", "architecture.md")
    rows = re.findall(r"^\|\s*(\d+)\s*\|\s*([A-Za-z ]+?)\s*\|", text, re.MULTILINE)
    assert rows, "no category-count table rows matched in docs/architecture.md"

    name_to_id = {v: k for k, v in _ARCHITECTURE_MD_CATEGORY_NAMES.items()}
    mismatches = []
    seen_ids = set()
    for claimed_count, name in rows:
        cat_id = name_to_id.get(name.strip())
        assert cat_id is not None, (
            "docs/architecture.md table has a category name %r not in "
            "_ARCHITECTURE_MD_CATEGORY_NAMES - add it there" % name
        )
        seen_ids.add(cat_id)
        if int(claimed_count) != real_counts[cat_id]:
            mismatches.append((name, int(claimed_count), real_counts[cat_id]))

    assert seen_ids == set(real_counts.keys()), (
        "docs/architecture.md's table categories %r do not match "
        "registry.py's actual categories %r" % (seen_ids, set(real_counts.keys()))
    )
    assert not mismatches, (
        "docs/architecture.md category row(s) whose claimed count does not "
        "match registry.py's actual per-category count "
        "(name, claimed, actual): %r" % mismatches
    )


_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def test_architecture_md_category_row_count_matches_registry_categories():
    text = _read("docs", "architecture.md")
    heading_match = re.search(r"###\s+\d+\s+tools,\s+(\w+)\s+categories", text)
    assert heading_match, "couldn't find the '<N> tools, <M> categories' heading"
    category_word = heading_match.group(1).lower()
    assert category_word in _WORD_NUMBERS, (
        "heading's category count %r is not a recognized number word - "
        "add it to _WORD_NUMBERS" % category_word
    )
    claimed_category_count = _WORD_NUMBERS[category_word]
    real_category_count = len({v["category"] for v in TOOLS.values()})
    assert claimed_category_count == real_category_count, (
        "docs/architecture.md claims %d categories; registry.py's tools "
        "actually use %d" % (claimed_category_count, real_category_count)
    )


def test_docs_tools_md_is_current():
    """docs/TOOLS.md must be exactly what scripts/gen_tool_docs.py would
    generate right now - reuses the generator's own generate() function
    rather than re-implementing the rendering here a second time."""
    sys.path.insert(0, os.path.join(MCP_SERVER_DIR, "scripts"))
    import gen_tool_docs

    expected = gen_tool_docs.generate()
    tools_md_path = os.path.join(REPO_ROOT, "docs", "TOOLS.md")
    assert os.path.exists(tools_md_path), (
        "docs/TOOLS.md does not exist - run 'python scripts/gen_tool_docs.py'"
    )
    with open(tools_md_path, "r", encoding="utf-8") as f:
        actual = f.read()
    assert actual == expected, (
        "docs/TOOLS.md is stale - run 'python scripts/gen_tool_docs.py' to "
        "regenerate it"
    )


def test_llm_txt_does_not_carry_a_stale_hardcoded_tool_count():
    """mcp-server/LLM.txt used to claim "31 tools" - the largest single
    drift found. Rather than maintaining a second hand-written inventory in
    parallel with a second regex test (guaranteed to drift again), this
    file's own fix is to point at generated docs/TOOLS.md instead of
    listing tools itself - so the only thing to guard here is that no
    bare "<N> tools" claim ever creeps back in."""
    text = _read("mcp-server", "LLM.txt")
    claims = re.findall(r"(\d+)\s+tools\b", text)
    real_count = len(ast_facts.all_tools())
    bad = [n for n in claims if int(n) != real_count]
    assert not bad, (
        "mcp-server/LLM.txt contains a stale tool-count claim %r (derived "
        "count is %d) - point at docs/TOOLS.md instead of hand-listing "
        "tools" % (bad, real_count)
    )
