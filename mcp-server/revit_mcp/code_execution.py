# -*- coding: UTF-8 -*-
"""
Code Execution Module for Revit MCP
Handles direct execution of IronPython code in Revit context.
"""
from pyrevit import routes, revit, DB
from utils import suppress_warnings, repair_hebrew_in, commit_verified
from code_safety import classify
import io
import json
import logging
import os
import sys
import time
import traceback
from StringIO import StringIO

# Standard logger setup
logger = logging.getLogger(__name__)

# Best-effort, append-only audit trail of every execute_revit_code
# submission - see _write_audit_record's own docstring. Deliberately its
# own file/directory, sharing no lock with the tracker (.lock), the scribe
# (.scribe.lock), or the lessons file (.lessons.lock) - none of those
# protect this file and this file protects none of theirs.
_AUDIT_DIR = os.path.join(os.path.expanduser("~"), ".claude", "revit-tracker", "audit")


def _write_audit_record(record):
    """Append one JSON line to this month's audit file. NEVER allowed to
    affect execution: any failure here (directory creation, a full disk,
    a permission error, a value that doesn't serialize) is caught and
    silently swallowed. A logging failure must never roll back the
    transaction, never raise to the MCP caller, and never turn a
    successful execution into an error - that guarantee is the entire
    point of keeping this as a small, separate, best-effort function
    rather than folding it into the main try/except above."""
    try:
        if not os.path.isdir(_AUDIT_DIR):
            os.makedirs(_AUDIT_DIR)
        path = os.path.join(_AUDIT_DIR, "code-{}.ndjson".format(time.strftime("%Y-%m")))
        line = json.dumps(record, ensure_ascii=False)
        with io.open(path, "a", encoding="utf-8") as f:
            f.write(line)
            f.write(u"\n")
    except Exception:
        pass


def _audit_safe(record):
    """Call _write_audit_record with a second, outer safety net around the
    call site itself - not just inside that function's own body. Belt and
    suspenders deliberately: _write_audit_record's internal try/except
    covers its own normal failure modes (disk full, permission denied, a
    value that won't serialize) cheaply, but the guarantee this milestone
    requires is unconditional - "for any reason" - and a call site with no
    guard of its own would still propagate if _write_audit_record itself
    were ever broken, replaced, or extended in a way that steps outside
    its own try/except. This is what every one of the three call sites
    below actually calls."""
    try:
        _write_audit_record(record)
    except Exception:
        pass


def register_code_execution_routes(api):
    """Register code execution routes with the API."""

    @api.route("/execute_code/", methods=["POST"])
    def execute_code(doc, request):
        """
        Execute IronPython code in Revit context.

        Expected payload:
        {
            "code": "python code as string",
            "description": "optional description of what the code does"
        }
        """
        try:
            # Parse the request data
            data = (
                json.loads(request.data)
                if isinstance(request.data, str)
                else request.data
            )
            # Repair Hebrew (and other non-ASCII) text mangled by a
            # cross-engine-scope quirk in pyrevit core's request parsing -
            # see repair_hebrew_in's docstring in utils.py. Safe no-op on
            # text that was never corrupted.
            data = repair_hebrew_in(data)
            code_to_execute = data.get("code", "")
            description = data.get("description", "Code execution")

            if not code_to_execute:
                return routes.make_response(
                    data={"error": "No code provided"}, status=400
                )

            logger.info("Executing code: {}".format(description))

            # Advisory classification only - see code_safety.py's own
            # docstring. classify() NEVER blocks execution; risk/signals
            # are metadata attached to the response and the audit record,
            # nothing more.
            risk, risk_signals = classify(code_to_execute)
            submitted_at = time.strftime("%Y-%m-%dT%H:%M:%S")

            # Code whose first line is "#!notx" runs with no wrapping
            # transaction and manages its own. Revit's edit scopes
            # (StairsEditScope, SketchEditScope, TopographyEditScope) refuse
            # to start while the document is already modifiable.
            no_tx = code_to_execute.lstrip().startswith("#!notx")

            t = None
            if not no_tx:
                t = DB.Transaction(doc, "MCP Code Execution: {}".format(description))
                t.Start()
                suppress_warnings(t)

            try:
                # Capture stdout to return any print statements
                old_stdout = sys.stdout
                captured_output = StringIO()
                sys.stdout = captured_output

                # Create a namespace with common Revit objects available.
                # System and clr are pre-imported so callers can use the
                # Revit 2027-safe ElementId pattern: DB.ElementId(System.Int64(id)).
                # In 2027 a bare DB.ElementId(<int>) raises "Multiple targets
                # could match" because of new BuiltInParameter/BuiltInCategory/Int64
                # overloads, so exposing System here avoids a common foot-gun.
                import clr as _clr
                import System as _System
                namespace = {
                    "doc": doc,
                    "DB": DB,
                    "revit": revit,
                    "clr": _clr,
                    "System": _System,
                    "__builtins__": __builtins__,
                    "print": lambda *args: captured_output.write(
                        " ".join(str(arg) for arg in args) + "\n"
                    ),
                }

                # Execute the code
                exec(code_to_execute, namespace)

                # Restore stdout
                sys.stdout = old_stdout

                # Get any printed output
                output = captured_output.getvalue()
                captured_output.close()

                # Commit the transaction (absent when the caller opted out).
                # Level 1 only - the "operation" here is arbitrary submitted
                # code with no fixed contract, so a generic level-2
                # post-condition cannot be defined; not_checked is the
                # honest, structural answer for this endpoint, not a gap.
                #
                # tx_ok is tri-state: True/False for a real transaction,
                # None for #!notx (t is None). Check "is False" specifically -
                # "not tx_ok" would also catch None and wrongly fail every
                # #!notx call, which by design never has a transaction to
                # verify at all.
                tx_ok, tx_status = commit_verified(t)
                if tx_ok is False:
                    _audit_safe({
                        "at": submitted_at, "description": description,
                        "code": code_to_execute, "risk": risk, "risk_signals": risk_signals,
                        "tx_status": tx_status, "tx_ok": tx_ok,
                        "outcome": "tx_not_committed",
                    })
                    return routes.make_response(
                        data={
                            "status": "error",
                            "tx_status": tx_status,
                            "tx_ok": tx_ok,
                            "output": output,
                            "risk": risk,
                            "risk_signals": risk_signals,
                            "error": "Transaction did not commit (tx_status={}) - the model is unchanged, even though the code ran without raising.".format(tx_status),
                        },
                        status=500,
                    )

                _audit_safe({
                    "at": submitted_at, "description": description,
                    "code": code_to_execute, "risk": risk, "risk_signals": risk_signals,
                    "tx_status": tx_status, "tx_ok": tx_ok,
                    "outcome": "success",
                })
                return routes.make_response(
                    data={
                        "status": "success",
                        "description": description,
                        "output": (
                            output
                            if output
                            else "Code executed successfully (no output)"
                        ),
                        "code_executed": code_to_execute,
                        "tx_status": tx_status,
                        "tx_ok": tx_ok,
                        "risk": risk,
                        "risk_signals": risk_signals,
                        "verified": {
                            "ok": None,
                            "status": "not_checked",
                            "reason": "execute_revit_code runs arbitrary submitted code with no fixed contract - only transaction-level verification (tx_status) applies here.",
                        },
                    }
                )

            except Exception as exec_error:
                # Restore stdout if something went wrong
                sys.stdout = old_stdout

                # Capture any partial output before the error
                partial_output = captured_output.getvalue()
                captured_output.close()

                # Rollback transaction if it's still active
                if t is not None and t.HasStarted() and not t.HasEnded():
                    t.RollBack()

                # Get the full traceback
                error_traceback = traceback.format_exc()

                # Build enhanced error message with hints
                error_type = type(exec_error).__name__
                error_msg = str(exec_error)
                enhanced_message = "{}: {}".format(error_type, error_msg)

                # Add helpful hints for common errors
                hints = []
                if error_type == "AttributeError":
                    if error_msg == "Name" or "Name" in error_msg:
                        hints.append(
                            "The 'Name' property may not be directly accessible in IronPython. "
                            "Try using getattr(element, 'Name', 'N/A') or "
                            "element.get_Parameter(DB.BuiltInParameter.ALL_MODEL_TYPE_NAME).AsString()"
                        )
                    else:
                        hints.append(
                            "Some Revit API properties are not directly accessible in IronPython. "
                            "Try using getattr(obj, 'property_name', default_value) for safe access."
                        )
                elif error_type == "NullReferenceException" or "NoneType" in error_msg:
                    hints.append(
                        "An object is None/null. Ensure you check if elements exist before "
                        "accessing their properties: 'if element:' or 'if element is not None:'"
                    )
                elif error_type == "InvalidOperationException":
                    hints.append(
                        "This operation may require being inside a transaction, or the element "
                        "may be in a state that doesn't allow this operation."
                    )
                elif "Transaction" in error_msg or "transaction" in error_msg:
                    hints.append(
                        "Transaction error. Note that this endpoint already wraps your code "
                        "in a transaction. Avoid starting nested transactions."
                    )

                logger.error("Code execution failed: {}".format(enhanced_message))
                logger.error("Traceback: {}".format(error_traceback))

                response_data = {
                    "status": "error",
                    "error": enhanced_message,
                    "error_type": error_type,
                    "traceback": error_traceback,
                    "code_attempted": code_to_execute,
                    "risk": risk,
                    "risk_signals": risk_signals,
                }

                if partial_output:
                    response_data["partial_output"] = partial_output

                if hints:
                    response_data["hints"] = hints

                _audit_safe({
                    "at": submitted_at, "description": description,
                    "code": code_to_execute, "risk": risk, "risk_signals": risk_signals,
                    "outcome": "exception", "error": enhanced_message,
                })
                return routes.make_response(
                    data=response_data,
                    status=500,
                )

        except Exception as e:
            logger.error("Execute code request failed: {}".format(str(e)))
            return routes.make_response(data={"error": str(e)}, status=500)

    logger.info("Code execution routes registered successfully.")
