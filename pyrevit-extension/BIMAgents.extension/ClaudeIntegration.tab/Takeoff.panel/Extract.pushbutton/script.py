#! python3
# -*- coding: utf-8 -*-
"""Extract selected beams/slabs and copy a Claude-ready QTO payload to the clipboard."""

__title__ = "Extract\nQTO Data"
__author__ = "BIMAgents"

import json

from pyrevit import DB, forms, revit, script

logger = script.get_logger()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SUPPORTED_CATEGORIES = {
    int(DB.BuiltInCategory.OST_StructuralFraming): "Structural Framing (Beam)",
    int(DB.BuiltInCategory.OST_Floors): "Floor (Slab)",
}

FT_TO_M = 0.3048
FT2_TO_M2 = 0.09290304
FT3_TO_M3 = 0.028316846592


# ---------------------------------------------------------------------------
# Unit conversion helpers (robust across Revit API versions)
# ---------------------------------------------------------------------------

def to_meters(feet_value):
    if feet_value is None:
        return None
    try:
        return DB.UnitUtils.ConvertFromInternalUnits(feet_value, DB.UnitTypeId.Meters)
    except Exception:
        return feet_value * FT_TO_M


def to_square_meters(sqft_value):
    if sqft_value is None:
        return None
    try:
        return DB.UnitUtils.ConvertFromInternalUnits(sqft_value, DB.UnitTypeId.SquareMeters)
    except Exception:
        return sqft_value * FT2_TO_M2


def to_cubic_meters(cft_value):
    if cft_value is None:
        return None
    try:
        return DB.UnitUtils.ConvertFromInternalUnits(cft_value, DB.UnitTypeId.CubicMeters)
    except Exception:
        return cft_value * FT3_TO_M3


def to_millimeters(feet_value):
    meters = to_meters(feet_value)
    if meters is None:
        return None
    return round(meters * 1000.0, 1)


# ---------------------------------------------------------------------------
# Revit element helpers
# ---------------------------------------------------------------------------

def get_element_id_value(revit_id):
    """Return the raw integer/int64 value of an ElementId across API versions."""
    try:
        return revit_id.Value
    except AttributeError:
        return revit_id.IntegerValue


def get_param_value(element, param_name):
    """Look up an instance parameter by name and return a JSON-friendly value."""
    try:
        param = element.LookupParameter(param_name)
        if param is None or not param.HasValue:
            return None
        if param.StorageType == DB.StorageType.Double:
            return param.AsDouble()
        if param.StorageType == DB.StorageType.Integer:
            return param.AsInteger()
        if param.StorageType == DB.StorageType.String:
            return param.AsString()
        return param.AsValueString()
    except Exception:
        return None


def get_type_param_value(document, element, param_name):
    try:
        element_type = document.GetElement(element.GetTypeId())
        if element_type is None:
            return None
        return get_param_value(element_type, param_name)
    except Exception:
        return None


def get_builtin_double(element, builtin_param):
    try:
        param = element.get_Parameter(builtin_param)
        if param is not None and param.HasValue:
            return param.AsDouble()
    except Exception:
        pass
    return None


def get_material_name(document, element):
    try:
        material_param = element.get_Parameter(DB.BuiltInParameter.STRUCTURAL_MATERIAL_PARAM)
        if material_param is not None and material_param.HasValue:
            material = document.GetElement(material_param.AsElementId())
            if material is not None:
                return material.Name
    except Exception:
        pass
    return None


def get_length_feet(element):
    try:
        location = element.Location
        if isinstance(location, DB.LocationCurve):
            return location.Curve.Length
    except Exception:
        pass
    return None


def get_family_name(document, element):
    try:
        element_type = document.GetElement(element.GetTypeId())
        if element_type is None:
            return None
        if hasattr(element_type, "FamilyName") and element_type.FamilyName:
            return element_type.FamilyName
        if getattr(element_type, "Family", None) is not None:
            return element_type.Family.Name
    except Exception:
        pass
    return None


def get_type_name(document, element):
    try:
        element_type = document.GetElement(element.GetTypeId())
        if element_type is not None:
            return element_type.Name
    except Exception:
        pass
    return None


def get_level_name(document, element):
    try:
        level_id = getattr(element, "LevelId", None)
        if level_id is None:
            return None
        level = document.GetElement(level_id)
        if level is not None:
            return level.Name
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Category-specific geometric/structural extraction
# ---------------------------------------------------------------------------

def extract_beam_data(document, element):
    length_ft = get_length_feet(element)
    volume_ft3 = get_builtin_double(element, DB.BuiltInParameter.HOST_VOLUME_COMPUTED)
    width_ft = get_type_param_value(document, element, "b") or get_type_param_value(document, element, "Width")
    depth_ft = get_type_param_value(document, element, "h") or get_type_param_value(document, element, "Depth")

    return {
        "geometry": {
            "length_m": to_meters(length_ft),
            "volume_m3": to_cubic_meters(volume_ft3),
            "section_width_mm": to_millimeters(width_ft),
            "section_depth_mm": to_millimeters(depth_ft),
        },
        "structural": {
            "structural_usage": get_param_value(element, "Structural Usage"),
        },
    }


def extract_slab_data(document, element):
    area_ft2 = get_builtin_double(element, DB.BuiltInParameter.HOST_AREA_COMPUTED)
    volume_ft3 = get_builtin_double(element, DB.BuiltInParameter.HOST_VOLUME_COMPUTED)
    thickness_ft = (
        get_type_param_value(document, element, "Thickness")
        or get_type_param_value(document, element, "Default Thickness")
    )

    return {
        "geometry": {
            "area_m2": to_square_meters(area_ft2),
            "volume_m3": to_cubic_meters(volume_ft3),
            "thickness_mm": to_millimeters(thickness_ft),
        },
        "structural": {
            "structural_usage": get_param_value(element, "Structural Usage"),
        },
    }


def build_element_record(document, element):
    category = element.Category
    category_id = get_element_id_value(category.Id) if category is not None else None
    category_label = SUPPORTED_CATEGORIES.get(category_id)
    if category_label is None:
        return None

    record = {
        "element_id": str(get_element_id_value(element.Id)),
        "category": category_label,
        "family": get_family_name(document, element),
        "type": get_type_name(document, element),
        "level": get_level_name(document, element),
        "material": get_material_name(document, element),
    }

    if category_id == int(DB.BuiltInCategory.OST_StructuralFraming):
        record.update(extract_beam_data(document, element))
    elif category_id == int(DB.BuiltInCategory.OST_Floors):
        record.update(extract_slab_data(document, element))

    return record


# ---------------------------------------------------------------------------
# Clipboard payload
# ---------------------------------------------------------------------------

def build_clipboard_text(payload):
    """Wrap the JSON payload with short context so it's ready to paste as-is."""
    payload_json = json.dumps(payload, indent=2)
    return (
        "Quantity Takeoff (QTO) data extracted from Autodesk Revit.\n"
        "Project: {project_name}\n"
        "Elements included: {element_count} (skipped: {skipped_count})\n\n"
        "Please analyze this structural QTO data (beams and slabs): group by "
        "category/type/material, compute totals (total beam length, total "
        "concrete/steel volume, total slab area, element counts per type), "
        "and flag any elements with missing or suspicious geometric data.\n\n"
        "```json\n{payload_json}\n```"
    ).format(
        project_name=payload["project_name"],
        element_count=payload["element_count"],
        skipped_count=payload["skipped_count"],
        payload_json=payload_json,
    )


def copy_to_clipboard(text):
    """Copy text to the Windows clipboard, with a direct WinForms fallback."""
    try:
        script.clipboard_copy(text)
        return
    except Exception as primary_error:
        logger.warning(
            "pyrevit clipboard_copy failed, falling back to WinForms Clipboard: {0}".format(
                primary_error
            )
        )

    try:
        import clr

        clr.AddReference("System.Windows.Forms")
        from System.Windows.Forms import Clipboard

        Clipboard.SetText(text)
    except Exception as fallback_error:
        raise RuntimeError("Failed to copy data to clipboard: {0}".format(fallback_error))


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def get_selected_elements(document, ui_document):
    selection_ids = ui_document.Selection.GetElementIds()
    return [document.GetElement(element_id) for element_id in selection_ids]


def main():
    document = revit.doc
    ui_document = revit.uidoc

    selected_elements = get_selected_elements(document, ui_document)
    if not selected_elements:
        forms.alert(
            "No elements selected. Select one or more beams (Structural "
            "Framing) or slabs (Floors) in the model, then run this tool "
            "again.",
            title="Claude QTO Extract",
        )
        return

    element_records = []
    skipped_count = 0
    for element in selected_elements:
        try:
            record = build_element_record(document, element)
        except Exception as extraction_error:
            logger.warning(
                "Failed to extract data for element {0}: {1}".format(
                    get_element_id_value(element.Id), extraction_error
                )
            )
            record = None
        if record is None:
            skipped_count += 1
            continue
        element_records.append(record)

    if not element_records:
        forms.alert(
            "None of the {0} selected element(s) are supported beams "
            "(Structural Framing) or slabs (Floors). Adjust your selection "
            "and try again.".format(len(selected_elements)),
            title="Claude QTO Extract",
        )
        return

    payload = {
        "project_name": document.Title,
        "element_count": len(element_records),
        "skipped_count": skipped_count,
        "elements": element_records,
    }
    clipboard_text = build_clipboard_text(payload)

    try:
        copy_to_clipboard(clipboard_text)
    except RuntimeError as clipboard_error:
        forms.alert(
            str(clipboard_error),
            title="Claude QTO Extract — Clipboard Error",
            warn_icon=True,
        )
        return

    forms.alert(
        "Data copied to clipboard! You can now paste it into Claude.",
        title="Claude QTO Extract",
        expanded=json.dumps(payload, indent=2),
    )


if __name__ == "__main__":
    main()
