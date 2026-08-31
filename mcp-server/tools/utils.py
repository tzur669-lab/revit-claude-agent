# -*- coding: utf-8 -*-
"""Utility functions for MCP tools"""

import json

# Above this size the "=== DATA ===" JSON block is truncated with an explicit
# marker rather than silently cut. A truncated list must never be able to
# look like a complete one - every key present and every list's true length
# is shown even when the JSON body itself had to be cut off.
MAX_DATA_CHARS = 12000

# Keys that are always rendered elsewhere (by the headline, or - only for
# "status": "active" responses - the dedicated status formatter) or are pure
# noise for a caller reading the response. Never duplicated into the DATA
# block. "code_executed" specifically echoes the submitted source back -
# useful for the audit trail, not for a caller reading their own response.
_ALWAYS_EXCLUDED_FROM_DATA = frozenset(
    ["status", "health", "success", "tx_status", "tx_ok", "code_executed", "_http_status"]
)

# Fixed precedence for selecting the single "headline" value from a success
# response. Iterating this tuple - never response.keys() - is what makes the
# choice deterministic and independent of dict insertion order. No response
# in this codebase currently carries more than one of these keys at once,
# so this order is not a behavior change for any existing handler; it exists
# so a future one is unambiguous.
_HEADLINE_KEYS = ("message", "output", "result", "data")


def _select_headline(response):
    """Return (key, text) for the first candidate present in the fixed
    precedence order above, or (None, None) if none of the four apply."""
    for key in _HEADLINE_KEYS:
        if key in response:
            value = response[key]
            text = value if key in ("message", "output") else str(value)
            return key, text
    return None, None


def _verification_suffix(response):
    """Level-1/level-2 verification evidence (tx_status / verified), if a
    handler provided it, formatted as a short suffix appended after the
    headline - regardless of which headline key won.

    Additive only: a handler that supplies neither field is completely
    unaffected. Applying this unconditionally (not only when "message" won
    the headline) is what lets tx_status/verified reach the caller for
    execute_revit_code, whose success payload only ever carries "output" -
    the one tool whose verified.ok is permanently None and whose tx_status
    is therefore the only evidence anything happened at all."""
    parts = []
    if "tx_status" in response:
        parts.append("tx_status: {}".format(response["tx_status"]))
    v = response.get("verified")
    if isinstance(v, dict):
        parts.append("verified.ok: {}".format(v.get("ok")))
        if v.get("reason"):
            parts.append("reason: {}".format(v["reason"]))
        if v.get("failures"):
            parts.append("failures: {}".format(len(v["failures"])))
    if not parts:
        return ""
    return "\n[" + " | ".join(parts) + "]"


def _verified_belongs_in_data(verified):
    """The passing/not-checked case is already summarised by
    _verification_suffix (ok + reason); only a failure - or the per-element
    failure list - earns a second, fuller place in the DATA block."""
    if not isinstance(verified, dict):
        return False
    return verified.get("ok") is False or bool(verified.get("failures"))


def _data_block(response, headline_key):
    """Every response key not already rendered by the headline or the
    verification suffix, as pretty-printed JSON.

    This is the fix for the single biggest gap in this file: a successful
    response used to surface exactly one key and silently drop the rest,
    which made validate_design / analyze_relationships /
    preview_delete_impact effectively unusable - their entire "results"
    payload never reached the caller. Returns "" when nothing remains."""
    excluded = set(_ALWAYS_EXCLUDED_FROM_DATA)
    if headline_key is not None:
        excluded.add(headline_key)
    fields = {}
    for key, value in response.items():
        if key in excluded:
            continue
        if key == "verified" and not _verified_belongs_in_data(value):
            continue
        fields[key] = value
    if not fields:
        return ""

    try:
        body = json.dumps(fields, indent=2, ensure_ascii=False, default=str, sort_keys=True)
    except Exception:
        body = str(fields)

    if len(body) > MAX_DATA_CHARS:
        list_lengths = dict(
            (key, len(value)) for key, value in fields.items() if isinstance(value, list)
        )
        note_lines = [
            "DATA_TRUNCATED: true",
            "keys_present: {}".format(sorted(fields.keys())),
        ]
        if list_lengths:
            note_lines.append("list_lengths: {}".format(list_lengths))
        note_lines.append(
            "note: the JSON below is INCOMPLETE - truncated at {} chars. "
            "Do not treat any list shown below as the full list.".format(MAX_DATA_CHARS)
        )
        body = "\n".join(note_lines) + "\n" + body[:MAX_DATA_CHARS]

    return "\n\n=== DATA ===\n" + body


def format_response(response):
    """Helper function to format API responses consistently for MCP tools.

    Args:
        response: The response from a revit_get or revit_post call, can be dict or string

    Returns:
        str: Formatted string response suitable for MCP tool return values

    Success shape: one "headline" value (message > output > result > data,
    the first present - see _HEADLINE_KEYS), then a one-line verification
    suffix when tx_status/verified are present, then an "=== DATA ===" JSON
    block carrying every other key the handler returned. Every key a handler
    sends back reaches the caller one way or another - none are silently
    dropped, which used to make several tools (validate_design,
    analyze_relationships, preview_delete_impact) effectively unusable.
    """
    if isinstance(response, dict):
        # Check for different success patterns
        status = response.get("status", "").lower()
        health = response.get("health", "").lower()

        # A dict carrying an explicit non-200 HTTP status (see main.py's
        # _revit_call, which now parses a structured error body instead of
        # only ever returning a plain string) is always an error - the HTTP
        # layer's verdict cannot be overridden by anything inside the body.
        http_status = response.get("_http_status")

        # Routes signal failure with an "error" key or an explicit failure
        # status. Any other dict (including data-bearing responses that omit
        # "status", e.g. get_revit_model_info) is a success — otherwise good
        # data is mislabeled as an error.
        has_error = (
            (http_status is not None and http_status != 200)
            or bool(response.get("error"))
            or status in ("error", "failed", "failure", "exception")
        )
        is_success = not has_error

        if is_success:
            headline_key, headline_text = _select_headline(response)

            if headline_key is not None:
                return (
                    headline_text
                    + _verification_suffix(response)
                    + _data_block(response, headline_key)
                )
            elif status == "active":  # Status check responses
                # Format status response nicely
                status_parts = ["=== REVIT STATUS ==="]
                status_parts.append("Status: {}".format(response.get("status", "Unknown")))
                status_parts.append("Health: {}".format(response.get("health", "Unknown")))

                if "api_name" in response:
                    status_parts.append("API: {}".format(response["api_name"]))
                if "document_title" in response:
                    status_parts.append("Document: {}".format(response["document_title"]))
                if "revit_available" in response:
                    status_parts.append("Revit Available: {}".format(response["revit_available"]))

                # Add any other fields that might be present
                known_fields = {"status", "health", "api_name", "document_title", "revit_available"}
                other_fields = set(response.keys()) - known_fields
                if other_fields:
                    status_parts.append("")
                    for field in sorted(other_fields):
                        status_parts.append("{}: {}".format(field.replace("_", " ").title(), response[field]))

                return "\n".join(status_parts)
            else:
                # Structured success payload without a standard wrapper key
                # (e.g. model info, level lists). Surface the data instead of
                # hiding it behind a generic message.
                data_fields = dict((k, v) for k, v in response.items()
                                   if k not in ("status", "health", "success"))
                if data_fields:
                    parts = []
                    for key in sorted(data_fields):
                        parts.append("{}: {}".format(key.replace("_", " ").title(),
                                                      data_fields[key]))
                    return "\n".join(parts)
                return "Operation completed successfully"
        else:
            # Error case - provide verbose debugging information
            error_msg = response.get("error", "Unknown error occurred")
            traceback_info = response.get("traceback", "")
            details = response.get("details", "")
            status = response.get("status", "unknown")

            # Build comprehensive error message
            error_parts = ["=== ERROR DETAILS ==="]
            error_parts.append("Status: {}".format(status))
            if http_status is not None:
                error_parts.append("HTTP Status: {}".format(http_status))
            error_parts.append("Error: {}".format(error_msg))

            if details:
                error_parts.append("Details: {}".format(details))

            if traceback_info:  # Code execution error with traceback
                error_parts.append("\n=== TRACEBACK ===")
                error_parts.append(traceback_info)

            # Add any additional fields that might be helpful for debugging
            debug_fields = ["code_attempted", "endpoint", "request_data", "response_code"]
            for field in debug_fields:
                if field in response:
                    error_parts.append("{}: {}".format(field.replace("_", " ").title(), response[field]))

            # Include full response for debugging if it has unexpected fields
            response_keys = set(response.keys()) - {
                "error", "traceback", "details", "status", "code_attempted",
                "endpoint", "request_data", "response_code", "_http_status",
            }
            if response_keys:
                error_parts.append("\n=== ADDITIONAL RESPONSE DATA ===")
                for key in sorted(response_keys):
                    error_parts.append("{}: {}".format(key, response[key]))

            return "\n".join(error_parts)
    else:
        # If response is already a string (error case from _revit_call)
        return str(response)
