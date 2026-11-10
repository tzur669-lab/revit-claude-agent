# -*- coding: utf-8 -*-
"""
Unit tests for commit_verified() (revit_mcp/utils.py) - the level-1
verification helper Milestone 1 added at the exact point every mutating
route handler used to call t.Commit() and trust it. Uses a fake
Transaction with a controllable Commit() return value (see
conftest_revit_mcp.py for the minimal pyrevit/DB shim this requires),
covering all 7 real TransactionStatus members enumerated live from Revit
on 2026-08-31, plus the #!notx (t is None) case.
"""
from conftest_revit_mcp import import_revit_mcp_utils

utils = import_revit_mcp_utils()


class _FakeTransaction(object):
    def __init__(self, status):
        self._status = status

    def Commit(self):
        return self._status


def _status(name):
    return getattr(utils.DB.TransactionStatus, name)


def test_committed_is_the_only_ok_status():
    tx_ok, tx_status = utils.commit_verified(_FakeTransaction(_status("Committed")))
    assert tx_ok is True
    assert tx_status == "Committed"


def test_rolled_back_is_not_ok():
    """The exact mechanism behind 'MoveElement returned success and moved
    nothing': the failure preprocessor returns ProceedWithRollBack on a
    real error, Commit() returns RolledBack without raising, and a caller
    that only checks 'did an exception happen' would report success."""
    tx_ok, tx_status = utils.commit_verified(_FakeTransaction(_status("RolledBack")))
    assert tx_ok is False
    assert tx_status == "RolledBack"


def test_every_non_committed_status_fails_closed():
    for name in ("Uninitialized", "Started", "Pending", "Error", "Proceed"):
        tx_ok, tx_status = utils.commit_verified(_FakeTransaction(_status(name)))
        assert tx_ok is False, "%s must not be treated as success" % name
        assert tx_status == name


def test_notx_returns_tristate_none_not_false():
    """#!notx (t is None): the transaction outcome was not observable by
    this helper, which is a DIFFERENT thing from a failure. A caller that
    tests `if not tx_ok` (rather than `if tx_ok is False`) would wrongly
    treat every #!notx call as a failure - this is a real bug caught live
    while wiring code_execution.py, guarded here so it cannot regress."""
    tx_ok, tx_status = utils.commit_verified(None)
    assert tx_ok is None
    assert tx_ok is not False
    assert tx_status == "self_managed"


def test_tx_ok_is_never_a_bare_truthy_stand_in():
    """Every real return must be exactly True, False, or None - never a
    string, never 1/0 - since callers branch on identity (`is False`,
    `is None`), not truthiness."""
    for name in ("Committed", "RolledBack", "Uninitialized", "Started", "Pending", "Error", "Proceed"):
        tx_ok, _ = utils.commit_verified(_FakeTransaction(_status(name)))
        assert tx_ok in (True, False)
    tx_ok, _ = utils.commit_verified(None)
    assert tx_ok is None
