# -*- coding: utf-8 -*-
"""
test_init_latency.py and test_model_info_format.py are standalone scripts,
not pytest test functions - each calls asyncio.run(main()) at module level
and requires a live Revit + pyRevit Routes session underneath the MCP
server they spawn. Importing them (which plain pytest collection does, by
matching the test_*.py filename pattern) would run that live-Revit code at
COLLECT time, before any marker could apply - a marker only filters test
ITEMS pytest already collected, not import-time side effects.

Run them directly instead, with Revit open:
    python tests/test_init_latency.py
    python tests/test_model_info_format.py

Excluded from default collection here rather than rewritten into pytest
functions - preserving their existing, working form per this project's
own "no opportunistic refactoring" rule.
"""

collect_ignore = [
    "test_init_latency.py",
    "test_model_info_format.py",
]
