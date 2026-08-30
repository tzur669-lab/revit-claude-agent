# -*- coding: UTF-8 -*-
"""
Document Module for Revit MCP
Save / persistence operations for the active document.

Note: Save / SaveAs must NOT run inside a Transaction — these handlers
deliberately do not open one.
"""

from pyrevit import routes, revit, DB
from utils import repair_hebrew_in
import json
import os
import time
import logging

logger = logging.getLogger(__name__)

# How recent a file's mtime must be, after Save()/SaveAs() returns, to count
# as evidence the write actually happened. Save()/SaveAs() are void calls -
# unlike Transaction.Commit() there is no status to read - so this is the
# only available post-condition. Provisional; generous enough to tolerate
# slow disks/antivirus scanning without papering over a genuine no-op.
SAVE_RECENCY_SECONDS = 30.0


def _verify_file_written(path):
    """Post-condition for Save()/SaveAs(): the file must exist and its mtime
    must be recent enough to plausibly be from the write that just happened.
    Save()/SaveAs() throw on real failure, but that alone does not prove a
    write reached disk (e.g. Revit's own retained-backup workflow, or a
    virtualised/networked path a test double could stub); this is a cheap,
    honest check rather than trusting the absence of an exception."""
    try:
        if not path or not os.path.exists(path):
            return {"ok": False, "method": "file_mtime", "reason": "File does not exist after save"}
        age_s = time.time() - os.path.getmtime(path)
        ok = age_s <= SAVE_RECENCY_SECONDS
        result = {"ok": ok, "method": "file_mtime", "actual": {"age_seconds": round(age_s, 1)}}
        if not ok:
            result["reason"] = "File exists but its mtime is older than {:.0f}s - save may not have written it".format(SAVE_RECENCY_SECONDS)
        return result
    except Exception as e:
        return {"ok": None, "status": "not_checked", "reason": "Could not stat file: {}".format(str(e))}


def register_document_routes(api):
    """Register document persistence routes with the API."""

    @api.route("/save_document/", methods=["POST"])
    def save_document(doc, request):
        """
        Save the active document. If a file_path is given (or the document has
        never been saved, e.g. it was started from a template), performs SaveAs;
        otherwise saves in place.

        Payload (all optional):
        {
            "file_path": "C:\\\\path\\\\to\\\\Model.rvt",
            "overwrite": true
        }
        """
        try:
            if not doc:
                return routes.make_response(
                    data={"error": "No active Revit document"}, status=503
                )

            data = {}
            if request and request.data:
                data = json.loads(request.data) if isinstance(request.data, str) else request.data
            data = repair_hebrew_in(data)

            file_path = data.get("file_path")
            overwrite = bool(data.get("overwrite", True))

            # A document started from a template (or never saved) has no real
            # path on disk yet, so it must be SaveAs'd to an explicit location.
            is_new = True
            try:
                is_new = doc.IsModelInPlace if hasattr(doc, "IsModelInPlace") else False
            except Exception:
                pass
            path_on_disk = ""
            try:
                path_on_disk = doc.PathName or ""
            except Exception:
                path_on_disk = ""

            if file_path:
                # Ensure parent directory exists
                try:
                    parent = os.path.dirname(file_path)
                    if parent and not os.path.exists(parent):
                        os.makedirs(parent)
                except Exception as mk_err:
                    logger.warning("Could not create directory: {}".format(str(mk_err)))

                save_opts = DB.SaveAsOptions()
                save_opts.OverwriteExistingFile = overwrite
                model_path = DB.ModelPathUtils.ConvertUserVisiblePathToModelPath(file_path)
                doc.SaveAs(model_path, save_opts)
                verified = _verify_file_written(file_path)
                return routes.make_response(data={
                    "status": "success",
                    "operation": "save_as",
                    "file_path": file_path,
                    "verified": verified,
                    "message": "Document saved to {}".format(file_path),
                })

            if not path_on_disk:
                return routes.make_response(
                    data={
                        "error": "Document has never been saved — provide a file_path to save it (e.g. C:\\\\Models\\\\ESB.rvt)."
                    },
                    status=400,
                )

            # Save in place
            doc.Save()
            verified = _verify_file_written(path_on_disk)
            return routes.make_response(data={
                "status": "success",
                "operation": "save",
                "file_path": path_on_disk,
                "verified": verified,
                "message": "Document saved",
            })

        except Exception as e:
            logger.error("save_document failed: {}".format(str(e)))
            return routes.make_response(
                data={"error": str(e)}, status=500
            )

    logger.info("Document routes registered successfully")
