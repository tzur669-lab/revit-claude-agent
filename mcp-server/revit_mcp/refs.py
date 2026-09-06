# -*- coding: UTF-8 -*-
"""
Ref resolution -- the one indirection every plan action's `targets` goes
through, so Phase B can add room/unit/level/zone ref kinds without
replacing the task architecture (revision-4 ADR). Phase A implements
exactly one kind: "element".
"""
from utils import make_element_id, get_element_id_value


class RefResolutionError(Exception):
    pass


def resolve_ref(doc, ref):
    """[DB.ElementId, ...] for the ref's targets. Raises RefResolutionError
    for an unsupported kind -- never silently substitutes a different
    object than the one the ref names."""
    kind = ref.get("kind") if isinstance(ref, dict) else None
    if kind == "element":
        ids = ref.get("ids") or []
        return [make_element_id(i) for i in ids]
    raise RefResolutionError("unsupported ref kind: {}".format(kind))


def ref_uid_map(doc, ref):
    """{int_id: uid_or_None} for every id in an element ref. uid is None
    when the id no longer resolves at all. Used to detect "the target
    changed under us" between propose time and execution: an id whose
    UniqueId now differs from what was captured at propose time is a
    precondition failure, not something to silently proceed past."""
    elem_ids = resolve_ref(doc, ref)
    out = {}
    for eid in elem_ids:
        elem = doc.GetElement(eid)
        out[get_element_id_value(eid)] = (elem.UniqueId if elem is not None else None)
    return out
