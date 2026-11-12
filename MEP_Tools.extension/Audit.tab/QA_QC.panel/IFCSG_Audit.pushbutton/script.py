# -*- coding: utf-8 -*-
"""
MEP_IFCSG_Auditor - IFC-SG / CORENET X QA/QC audit for MEP models.

A pyRevit push-button that prepares MEP models for Singapore CORENET X BIM
submissions (IFC-SG, also written IFC+SG). In one click it:

1. Collects Ducts, Pipes, Mechanical Equipment and Cable Trays from the
   active model.
2. Flags every element whose required IFC-SG parameters are missing,
   empty, whitespace-only or still a placeholder such as "TBC".
3. Runs a bounding-box clash pre-check between ducts/pipes and structural
   framing/columns, including structure in loaded Revit links.
4. Creates (or refreshes) the 3D view "QA_MEP_Compliance_Check" and colours
   every failed element red so the modeller can fix them visually.
5. Exports a timestamped CSV audit report to the Desktop and shows a
   summary dialog.

The checklist is configurable in ``audit_config.json`` next to this script
(Shift+Click the button to open it). Built-in defaults are used for any key
the file leaves out.

Engine compatibility: pyRevit IronPython 2.7 (default) and the pyRevit
CPython 3 engine. No f-strings or other Python-3-only syntax is used.
"""
from __future__ import print_function

import codecs
import csv
import datetime
import json
import os
import re
import sys
import tempfile
import time
import traceback
from collections import OrderedDict

import clr  # noqa: F401  -- initialises .NET interop under both engines

from System import Environment
from System.Collections.Generic import List

from Autodesk.Revit.DB import (
    BoundingBoxIntersectsFilter,
    BuiltInCategory,
    BuiltInParameter,
    CheckoutStatus,
    Color,
    DisplayStyle,
    ElementId,
    FillPatternElement,
    FillPatternTarget,
    FilteredElementCollector,
    Outline,
    OverrideGraphicSettings,
    RevitLinkInstance,
    StorageType,
    Transaction,
    TransactionStatus,
    View3D,
    ViewDetailLevel,
    ViewFamily,
    ViewFamilyType,
    WorksharingUtils,
    XYZ,
)

from pyrevit import script

try:
    # pyrevit.forms is WPF-based; some CPython builds cannot import it.
    # Every UI call below falls back to Revit's own TaskDialog.
    from pyrevit import forms
except Exception:  # pragma: no cover - depends on the pyRevit engine
    forms = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TOOL_NAME = "IFC-SG MEP Auditor"
PY2 = sys.version_info[0] == 2
MM_PER_FOOT = 304.8  # Revit's internal length unit is decimal feet

try:
    TEXT_TYPE = unicode  # noqa: F821  -- Python 2 / IronPython 2.7
except NameError:
    TEXT_TYPE = str

try:
    BUNDLE_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:  # pragma: no cover - __file__ is always set by pyRevit
    BUNDLE_DIR = os.getcwd()

CONFIG_FILE = os.path.join(BUNDLE_DIR, "audit_config.json")

CSV_HEADER = [
    "Element ID",
    "Category",
    "Family & Type",
    "Level",
    "Missing Parameters",
    "Potential Structural Clash (Yes/No)",
    "Clashing Structural Elements",
    "Audit Status",
]

OPTION_OPEN_VIEW = "Open QA 3D view"
OPTION_OPEN_REPORT = "Open CSV report"
OPTION_CLOSE = "Close"

# Parameter check results, ranked so the best result across duplicate
# parameters (or instance vs. type) wins.
STATUS_OK = "ok"
STATUS_PLACEHOLDER = "placeholder"
STATUS_EMPTY = "empty"
STATUS_NOT_FOUND = "not found"
STATUS_RANK = {
    STATUS_NOT_FOUND: 0,
    STATUS_EMPTY: 1,
    STATUS_PLACEHOLDER: 2,
    STATUS_OK: 3,
}

# Built-in default checklist. audit_config.json is merged on top of this.
DEFAULT_CONFIG = {
    # Label shown in the report -> Revit BuiltInCategory name.
    "audit_categories": OrderedDict([
        ("Ducts", "OST_DuctCurves"),
        ("Pipes", "OST_PipeCurves"),
        ("Mechanical Equipment", "OST_MechanicalEquipment"),
        ("Cable Trays", "OST_CableTray"),
    ]),
    # "*" applies to every audited category; other keys add per category.
    "required_parameters": OrderedDict([
        ("*", ["Mark", "Comments", "SG_Asset_Code", "Fire_Rating"]),
        ("Ducts", ["System Classification"]),
        ("Pipes", ["System Classification"]),
        ("Mechanical Equipment", ["System Classification"]),
    ]),
    "check_type_parameters": True,
    "flag_parameters_not_found": True,
    "placeholder_values": ["TBC", "TBA", "TBD"],
    "clash_check": {
        "enabled": True,
        "mep_categories": ["Ducts", "Pipes"],
        "structural_categories": ["OST_StructuralFraming", "OST_StructuralColumns"],
        "include_linked_models": True,
        "bbox_inset_mm": 5.0,
    },
    "qa_view": {
        "name": "QA_MEP_Compliance_Check",
        "fail_color_rgb": [255, 0, 0],
        "halftone_passing_elements": True,
        "passing_transparency": 0,
    },
    "report": {
        "output_folder": "",
        "failures_only": False,
        "show_output_window": True,
        "max_rows_in_output_window": 300,
    },
}

# Keys whose value replaces the default outright instead of being merged,
# so removing a category or parameter from the JSON really removes it.
REPLACE_KEYS = ("audit_categories", "required_parameters")


def _builtin_param(name):
    """Return BuiltInParameter.<name>, or None if this Revit version lacks it."""
    try:
        return getattr(BuiltInParameter, name)
    except AttributeError:
        return None


# Language-independent lookups for common built-in parameters. Anything not
# listed here (e.g. shared parameters) is looked up by its display name.
BUILTIN_PARAM_ALIASES = {
    "mark": _builtin_param("ALL_MODEL_MARK"),
    "comments": _builtin_param("ALL_MODEL_INSTANCE_COMMENTS"),
    "system classification": _builtin_param("RBS_SYSTEM_CLASSIFICATION_PARAM"),
}

# Fallback parameters that hold an element's level, tried in order.
LEVEL_PARAMS = [
    _builtin_param(name) for name in (
        "RBS_START_LEVEL_PARAM",
        "FAMILY_LEVEL_PARAM",
        "INSTANCE_REFERENCE_LEVEL_PARAM",
        "INSTANCE_SCHEDULE_ONLY_LEVEL_PARAM",
        "SCHEDULE_LEVEL_PARAM",
    )
]

logger = script.get_logger()


class AuditCancelled(Exception):
    """Raised when the user presses Cancel on a progress bar."""


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------
def to_text(value):
    """Convert any value (including .NET strings) to text without raising."""
    if value is None:
        return ""
    try:
        return TEXT_TYPE(value)
    except Exception:
        try:
            return str(value)
        except Exception:
            return repr(value)


def as_list(value):
    """Accept a single string or a list in the config and always return a list."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def eid_int(element_id):
    """Integer value of an ElementId on every Revit version.

    Revit 2024 added ``ElementId.Value`` (Int64) and later versions removed
    ``IntegerValue``; older versions only have ``IntegerValue``.
    """
    if element_id is None:
        return -1
    try:
        return int(element_id.Value)
    except AttributeError:
        return int(element_id.IntegerValue)


def is_valid_id(element_id):
    return element_id is not None and eid_int(element_id) != -1


def sanitize_filename(text, fallback="Model"):
    text = re.sub(r"\.rvt$", "", to_text(text).strip(), flags=re.IGNORECASE)
    text = re.sub(r'[\\/:*?"<>|\s]+', "_", text).strip("_.")
    return text or fallback


def deep_merge(base, override):
    """Merge ``override`` onto ``base`` recursively; keys starting with '_' are comments."""
    result = OrderedDict()
    for key, value in base.items():
        result[key] = deep_merge(value, {}) if isinstance(value, dict) else value
    for key, value in override.items():
        if to_text(key).startswith("_"):
            continue
        if key in REPLACE_KEYS:
            result[key] = value
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path, warnings):
    """Load audit_config.json and merge it over DEFAULT_CONFIG."""
    if not os.path.isfile(path):
        warnings.append("Config file not found - using built-in defaults.")
        return deep_merge(DEFAULT_CONFIG, {})
    try:
        with codecs.open(path, "r", encoding="utf-8-sig") as handle:
            user_cfg = json.load(handle, object_pairs_hook=OrderedDict)
        if not isinstance(user_cfg, dict):
            raise ValueError("top level must be a JSON object")
        return deep_merge(DEFAULT_CONFIG, user_cfg)
    except Exception as err:
        warnings.append(
            "Could not read audit_config.json ({}) - using built-in defaults.".format(err)
        )
        return deep_merge(DEFAULT_CONFIG, {})


def resolve_builtin_category(name):
    try:
        return getattr(BuiltInCategory, to_text(name).strip())
    except AttributeError:
        return None


# ---------------------------------------------------------------------------
# UI helpers (pyRevit forms with a TaskDialog fallback)
# ---------------------------------------------------------------------------
class _NullProgress(object):
    """Stand-in progress bar when pyrevit.forms is unavailable."""

    cancelled = False
    title = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def update_progress(self, *args, **kwargs):
        pass


def progress_bar(title):
    if forms is not None:
        try:
            return forms.ProgressBar(title=title, cancellable=True)
        except Exception:
            logger.debug("ProgressBar unavailable:\n%s", traceback.format_exc())
    return _NullProgress()


def tick(progress, index, total, step):
    """Update the progress bar every ``step`` items and honour Cancel."""
    if index % step == 0 or index == total:
        progress.update_progress(index, total)
    if getattr(progress, "cancelled", False):
        raise AuditCancelled()


def _task_dialog(title, message, sub_message=None, options=None):
    from Autodesk.Revit.UI import (
        TaskDialog,
        TaskDialogCommandLinkId,
        TaskDialogResult,
    )

    dialog = TaskDialog(title)
    dialog.MainInstruction = message
    if sub_message:
        dialog.MainContent = sub_message
    link_ids = [
        TaskDialogCommandLinkId.CommandLink1,
        TaskDialogCommandLinkId.CommandLink2,
        TaskDialogCommandLinkId.CommandLink3,
        TaskDialogCommandLinkId.CommandLink4,
    ]
    link_results = [
        TaskDialogResult.CommandLink1,
        TaskDialogResult.CommandLink2,
        TaskDialogResult.CommandLink3,
        TaskDialogResult.CommandLink4,
    ]
    options = list(options or [])[:4]
    for link_id, text in zip(link_ids, options):
        dialog.AddCommandLink(link_id, text)
    result = dialog.Show()
    for link_result, text in zip(link_results, options):
        if result == link_result:
            return text
    return None


def show_dialog(message, sub_message=None, options=None, warn=False):
    """Show a message (optionally with choice buttons); return the chosen option."""
    if forms is not None:
        try:
            return forms.alert(
                message,
                title=TOOL_NAME,
                sub_msg=sub_message,
                options=options,
                warn_icon=warn,
            )
        except Exception:
            logger.debug("forms.alert failed:\n%s", traceback.format_exc())
    return _task_dialog(TOOL_NAME, message, sub_message, options)


def open_with_shell(path):
    """Open a file with its default Windows application."""
    try:
        os.startfile(path)  # not available on every IronPython build
        return True
    except Exception:
        pass
    try:
        from System.Diagnostics import Process, ProcessStartInfo

        info = ProcessStartInfo(path)
        info.UseShellExecute = True  # required on .NET 8 (Revit 2025+)
        Process.Start(info)
        return True
    except Exception as err:
        logger.warning("Could not open %s: %s", path, err)
        return False


# ---------------------------------------------------------------------------
# Audit model
# ---------------------------------------------------------------------------
class AuditRecord(object):
    """Audit result for one MEP element."""

    def __init__(self, element, category, family_type, level):
        self.element_id = element.Id
        self.id_int = eid_int(element.Id)
        self.category = category
        self.family_type = family_type
        self.level = level
        self.missing = []  # list of (parameter name, status)
        self.clashes = []  # list of structural element descriptions

    @property
    def has_parameter_issue(self):
        return bool(self.missing)

    @property
    def has_clash(self):
        return bool(self.clashes)

    @property
    def failed(self):
        return self.has_parameter_issue or self.has_clash

    def missing_text(self):
        parts = []
        for name, status in self.missing:
            parts.append(name if status == STATUS_EMPTY else "{} [{}]".format(name, status))
        return "; ".join(parts)

    def clash_text(self, limit=10):
        if not self.clashes:
            return ""
        shown = self.clashes[:limit]
        text = "; ".join(shown)
        if len(self.clashes) > limit:
            text += " (+{} more)".format(len(self.clashes) - limit)
        return text


class AuditContext(object):
    """Holds the document, settings and lookup caches for one audit run."""

    def __init__(self, doc, cfg):
        self.doc = doc
        self.cfg = cfg
        self.check_type_params = bool(cfg.get("check_type_parameters", True))
        self.flag_not_found = bool(cfg.get("flag_parameters_not_found", True))
        self.placeholders = set(
            to_text(v).strip().upper() for v in as_list(cfg.get("placeholder_values"))
            if to_text(v).strip()
        )
        self._types = {}
        self._levels = {}

    # -- lookups -------------------------------------------------------------
    def required_parameters(self, category_label):
        """Global ("*") plus category-specific parameter names, de-duplicated."""
        table = self.cfg.get("required_parameters") or {}
        names = []
        for key in ("*", category_label):
            for name in as_list(table.get(key)):
                name = to_text(name).strip()
                if name and name not in names:
                    names.append(name)
        return names

    def element_type(self, element):
        type_id = element.GetTypeId()
        key = eid_int(type_id)
        if key == -1:
            return None
        if key not in self._types:
            self._types[key] = self.doc.GetElement(type_id)
        return self._types[key]

    def level_name(self, element):
        level_id = None
        try:
            level_id = element.LevelId
        except Exception:
            pass
        if not is_valid_id(level_id):
            try:
                level_id = element.ReferenceLevel.Id  # MEPCurve (ducts, pipes, trays)
            except Exception:
                level_id = None
        if not is_valid_id(level_id):
            for bip in LEVEL_PARAMS:
                if bip is None:
                    continue
                param = element.get_Parameter(bip)
                if param is not None and param.StorageType == StorageType.ElementId:
                    candidate = param.AsElementId()
                    if is_valid_id(candidate):
                        level_id = candidate
                        break
        if not is_valid_id(level_id):
            return "<No Level>"
        key = eid_int(level_id)
        if key not in self._levels:
            level = self.doc.GetElement(level_id)
            self._levels[key] = to_text(level.Name) if level is not None else "<No Level>"
        return self._levels[key]

    def family_and_type(self, element):
        param = element.get_Parameter(BuiltInParameter.ELEM_FAMILY_AND_TYPE_PARAM)
        if param is not None:
            value = param.AsValueString()
            if value:
                return to_text(value)
        etype = self.element_type(element)
        if etype is not None:
            family = _param_string(etype, "SYMBOL_FAMILY_NAME_PARAM", "ALL_MODEL_FAMILY_NAME")
            type_name = _param_string(etype, "SYMBOL_NAME_PARAM", "ALL_MODEL_TYPE_NAME")
            if family or type_name:
                return "{}: {}".format(family or "?", type_name or "?")
        return "<Unknown>"


def _param_string(element, *bip_names):
    for bip_name in bip_names:
        bip = _builtin_param(bip_name)
        if bip is None:
            continue
        param = element.get_Parameter(bip)
        if param is not None and param.AsString():
            return to_text(param.AsString())
    return ""


# ---------------------------------------------------------------------------
# Step 1 - element collection
# ---------------------------------------------------------------------------
def collect_audit_elements(doc, cfg, warnings):
    """Return ``[(category_label, element), ...]`` and per-category counts."""
    items = []
    counts = OrderedDict()
    for label, bic_name in (cfg.get("audit_categories") or {}).items():
        if to_text(label).startswith("_"):
            continue
        bic = resolve_builtin_category(bic_name)
        if bic is None:
            warnings.append("Unknown category '{}' for '{}' - skipped.".format(bic_name, label))
            continue
        collector = (
            FilteredElementCollector(doc)
            .OfCategory(bic)
            .WhereElementIsNotElementType()
        )
        count = 0
        for element in collector:
            if element.Category is None:
                continue
            items.append((label, element))
            count += 1
        counts[label] = count
    return items, counts


# ---------------------------------------------------------------------------
# Step 2 - IFC-SG parameter compliance
# ---------------------------------------------------------------------------
def find_parameters(element, name):
    """All parameters on ``element`` matching ``name`` (built-in alias first)."""
    found = []
    bip = BUILTIN_PARAM_ALIASES.get(name.lower())
    if bip is not None:
        param = element.get_Parameter(bip)
        if param is not None:
            found.append(param)
    try:
        # GetParameters returns every match, so a duplicate project/shared
        # parameter with the same name cannot hide a filled-in value.
        found.extend(list(element.GetParameters(name)))
    except Exception:
        param = element.LookupParameter(name)
        if param is not None:
            found.append(param)
    return found


def evaluate_parameter(param, placeholders):
    """Classify one parameter value as ok / placeholder / empty."""
    try:
        storage = param.StorageType
        if storage == StorageType.String:
            text = param.AsString()
            if text is None or not text.strip():
                return STATUS_EMPTY
            if text.strip().upper() in placeholders:
                return STATUS_PLACEHOLDER
            return STATUS_OK
        if not param.HasValue:
            return STATUS_EMPTY
        if storage == StorageType.ElementId:
            return STATUS_OK if is_valid_id(param.AsElementId()) else STATUS_EMPTY
        if storage in (StorageType.Integer, StorageType.Double):
            return STATUS_OK  # 0 is a legitimate numeric value
        return STATUS_EMPTY  # StorageType.None
    except Exception:
        logger.debug("Could not read parameter:\n%s", traceback.format_exc())
        return STATUS_EMPTY


def best_status(params, placeholders):
    best = STATUS_NOT_FOUND
    for param in params:
        status = evaluate_parameter(param, placeholders)
        if STATUS_RANK[status] > STATUS_RANK[best]:
            best = status
        if best == STATUS_OK:
            break
    return best


def check_element_parameters(ctx, element, required):
    """Return ``[(name, status), ...]`` for every required parameter that fails."""
    problems = []
    for name in required:
        status = best_status(find_parameters(element, name), ctx.placeholders)
        if status != STATUS_OK and ctx.check_type_params:
            etype = ctx.element_type(element)
            if etype is not None:
                type_status = best_status(find_parameters(etype, name), ctx.placeholders)
                if STATUS_RANK[type_status] > STATUS_RANK[status]:
                    status = type_status
        if status == STATUS_OK:
            continue
        if status == STATUS_NOT_FOUND and not ctx.flag_not_found:
            continue
        problems.append((name, status))
    return problems


def audit_parameters(ctx, items, warnings):
    records = []
    total = len(items)
    step = max(1, total // 100)
    with progress_bar("IFC-SG parameter check ... {value} of {max_value}") as progress:
        for index, (label, element) in enumerate(items, 1):
            tick(progress, index, total, step)
            try:
                record = AuditRecord(
                    element,
                    label,
                    ctx.family_and_type(element),
                    ctx.level_name(element),
                )
                record.missing = check_element_parameters(
                    ctx, element, ctx.required_parameters(label)
                )
                records.append(record)
            except Exception as err:
                warnings.append(
                    "Element {} could not be audited: {}".format(eid_int(element.Id), err)
                )
                logger.debug(traceback.format_exc())
    return records


# ---------------------------------------------------------------------------
# Step 3 - MEP vs. structure clash pre-check (bounding boxes)
# ---------------------------------------------------------------------------
def shrink_range(low, high, inset):
    """Shrink [low, high] by ``inset`` on both sides when it is large enough."""
    if inset > 0 and (high - low) > 2.0 * inset:
        return low + inset, high - inset
    return low, high


def outline_from_bbox(bbox, link_transform=None, inset_ft=0.0):
    """Axis-aligned host-coordinate Outline for a BoundingBoxXYZ, or None.

    Linked elements are reported in the link's coordinates, so all eight
    corners are transformed into the host model before taking min/max
    (this stays correct when the link is rotated).
    """
    if bbox is None:
        return None
    transforms = []
    bbox_transform = getattr(bbox, "Transform", None)
    if bbox_transform is not None and not bbox_transform.IsIdentity:
        transforms.append(bbox_transform)
    if link_transform is not None and not link_transform.IsIdentity:
        transforms.append(link_transform)

    low, high = bbox.Min, bbox.Max
    if transforms:
        xs, ys, zs = [], [], []
        for x in (low.X, high.X):
            for y in (low.Y, high.Y):
                for z in (low.Z, high.Z):
                    point = XYZ(x, y, z)
                    for transform in transforms:
                        point = transform.OfPoint(point)
                    xs.append(point.X)
                    ys.append(point.Y)
                    zs.append(point.Z)
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        z0, z1 = min(zs), max(zs)
    else:
        x0, x1 = low.X, high.X
        y0, y1 = low.Y, high.Y
        z0, z1 = low.Z, high.Z

    x0, x1 = shrink_range(x0, x1, inset_ft)
    y0, y1 = shrink_range(y0, y1, inset_ft)
    z0, z1 = shrink_range(z0, z1, inset_ft)
    outline = Outline(XYZ(x0, y0, z0), XYZ(x1, y1, z1))
    if outline.IsEmpty:
        return None
    return outline


def gather_structural_sources(doc, clash_cfg, warnings):
    """Return ``[(source label, element, link transform or None), ...]``."""
    categories = []
    for name in as_list(clash_cfg.get("structural_categories")):
        bic = resolve_builtin_category(name)
        if bic is None:
            warnings.append("Unknown structural category '{}' - skipped.".format(name))
        else:
            categories.append(bic)

    sources = []

    def collect_from(source_doc, label, transform):
        for bic in categories:
            collector = (
                FilteredElementCollector(source_doc)
                .OfCategory(bic)
                .WhereElementIsNotElementType()
            )
            for element in collector:
                sources.append((label, element, transform))

    collect_from(doc, "Host", None)

    linked_models = 0
    if clash_cfg.get("include_linked_models", True):
        for link in FilteredElementCollector(doc).OfClass(RevitLinkInstance):
            link_doc = link.GetLinkDocument()
            if link_doc is None:
                try:
                    link_name = to_text(link.Name)
                except Exception:
                    link_name = "Id {}".format(eid_int(link.Id))
                warnings.append(
                    "Link '{}' is not loaded - skipped in the clash pre-check.".format(link_name)
                )
                continue
            collect_from(link_doc, "Link: " + to_text(link_doc.Title), link.GetTotalTransform())
            linked_models += 1
    return sources, linked_models


def describe_structural(label, element):
    try:
        category = to_text(element.Category.Name)
    except Exception:
        category = "Structure"
    return "{} / {} / Id {}".format(label, category, eid_int(element.Id))


def run_clash_precheck(doc, records, cfg, warnings):
    """Flag ducts/pipes whose bounding box intersects structure.

    Each structural outline is tested with a BoundingBoxIntersectsFilter
    against the audited MEP element ids, so Revit does the heavy lifting
    natively instead of an N x M loop in Python.
    """
    clash_cfg = cfg.get("clash_check") or {}
    mep_labels = set(as_list(clash_cfg.get("mep_categories")))
    mep_ids = List[ElementId]()
    for record in records:
        if record.category in mep_labels:
            mep_ids.Add(record.element_id)

    stats = {"enabled": True, "structural_elements": 0, "linked_models": 0, "mep_checked": mep_ids.Count}
    hits = {}
    if mep_ids.Count == 0:
        return hits, stats

    sources, stats["linked_models"] = gather_structural_sources(doc, clash_cfg, warnings)
    stats["structural_elements"] = len(sources)
    if not sources:
        warnings.append("No structural framing/columns found - clash pre-check had nothing to test.")
        return hits, stats

    inset_ft = float(clash_cfg.get("bbox_inset_mm", 0) or 0) / MM_PER_FOOT
    total = len(sources)
    step = max(1, total // 100)
    with progress_bar("Structural clash pre-check ... {value} of {max_value}") as progress:
        for index, (label, struct_el, transform) in enumerate(sources, 1):
            tick(progress, index, total, step)
            try:
                outline = outline_from_bbox(struct_el.get_BoundingBox(None), transform, inset_ft)
                if outline is None:
                    continue
                clashing_ids = (
                    FilteredElementCollector(doc, mep_ids)
                    .WherePasses(BoundingBoxIntersectsFilter(outline))
                    .ToElementIds()
                )
                if clashing_ids.Count == 0:
                    continue
                description = describe_structural(label, struct_el)
                for clash_id in clashing_ids:
                    hits.setdefault(eid_int(clash_id), []).append(description)
            except Exception:
                logger.debug(
                    "Clash test failed for structural element %s:\n%s",
                    eid_int(struct_el.Id),
                    traceback.format_exc(),
                )
    return hits, stats


# ---------------------------------------------------------------------------
# Step 4 - QA 3D view with colour overrides
# ---------------------------------------------------------------------------
def find_view3d_by_name(doc, name):
    for view in FilteredElementCollector(doc).OfClass(View3D):
        if not view.IsTemplate and to_text(view.Name) == name:
            return view
    return None


def owned_by_other_user(doc, element_id):
    if not doc.IsWorkshared:
        return False
    try:
        status = WorksharingUtils.GetCheckoutStatus(doc, element_id)
        return status == CheckoutStatus.OwnedByOtherUser
    except Exception:
        return False


def resolve_qa_view_name(doc, base_name, warnings):
    """Use the shared QA view unless a colleague currently owns it.

    In a workshared central model a borrowed view cannot be edited, so we
    fall back to a personal copy instead of failing the whole run.
    """
    view = find_view3d_by_name(doc, base_name)
    if view is None or not owned_by_other_user(doc, view.Id):
        return base_name
    user = sanitize_filename(doc.Application.Username, fallback="user")
    personal = "{}_{}".format(base_name, user)
    warnings.append(
        "'{}' is borrowed by another user - updated '{}' instead.".format(base_name, personal)
    )
    return personal


def get_or_create_qa_view(doc, name):
    view = find_view3d_by_name(doc, name)
    if view is not None:
        return view, False
    view_type = None
    for vft in FilteredElementCollector(doc).OfClass(ViewFamilyType):
        if vft.ViewFamily == ViewFamily.ThreeDimensional:
            view_type = vft
            break
    if view_type is None:
        raise RuntimeError("No 3D view type exists in this model.")
    view = View3D.CreateIsometric(doc, view_type.Id)
    view.Name = name
    return view, True


def prepare_view(view):
    """Detach any view template and use a display that shows colours clearly."""
    try:
        if is_valid_id(view.ViewTemplateId):
            view.ViewTemplateId = ElementId.InvalidElementId
    except Exception:
        logger.debug("Could not remove view template:\n%s", traceback.format_exc())
    for attribute, value in (
        ("DetailLevel", ViewDetailLevel.Fine),
        ("DisplayStyle", DisplayStyle.Shading),
    ):
        try:
            setattr(view, attribute, value)
        except Exception:
            logger.debug("Could not set %s:\n%s", attribute, traceback.format_exc())


def solid_fill_pattern_id(doc):
    for pattern_element in FilteredElementCollector(doc).OfClass(FillPatternElement):
        pattern = pattern_element.GetFillPattern()
        if pattern.IsSolidFill and pattern.Target == FillPatternTarget.Drafting:
            return pattern_element.Id
    return None


def make_color(rgb):
    values = (as_list(rgb) + [0, 0, 0])[:3]
    red, green, blue = [max(0, min(255, int(v))) for v in values]
    return Color(red, green, blue)


def build_fail_override(doc, rgb):
    color = make_color(rgb)
    solid_id = solid_fill_pattern_id(doc)
    ogs = OverrideGraphicSettings()
    try:
        # Revit 2019+
        ogs.SetSurfaceForegroundPatternColor(color)
        ogs.SetCutForegroundPatternColor(color)
        if solid_id is not None:
            ogs.SetSurfaceForegroundPatternId(solid_id)
            ogs.SetCutForegroundPatternId(solid_id)
    except AttributeError:
        # Revit 2018 and older
        ogs.SetProjectionFillColor(color)
        ogs.SetCutFillColor(color)
        if solid_id is not None:
            ogs.SetProjectionFillPatternId(solid_id)
            ogs.SetCutFillPatternId(solid_id)
    ogs.SetProjectionLineColor(color)
    ogs.SetCutLineColor(color)
    return ogs


def build_pass_override(view_cfg):
    """Passing elements: optionally halftone/transparent so red stands out.

    An empty OverrideGraphicSettings also clears red left over from a
    previous run on elements that have since been fixed.
    """
    ogs = OverrideGraphicSettings()
    if view_cfg.get("halftone_passing_elements", True):
        ogs.SetHalftone(True)
    transparency = int(view_cfg.get("passing_transparency", 0) or 0)
    if transparency > 0:
        ogs.SetSurfaceTransparency(max(0, min(100, transparency)))
    return ogs


def update_qa_view(doc, records, cfg, warnings):
    """Create/refresh the QA 3D view in a single, rolled-back-on-error transaction."""
    view_cfg = cfg.get("qa_view") or {}
    view_name = resolve_qa_view_name(
        doc, to_text(view_cfg.get("name") or "QA_MEP_Compliance_Check"), warnings
    )
    result = {"view": None, "name": view_name, "created": False, "coloured": 0, "error": None}

    transaction = Transaction(doc, "IFC-SG MEP Audit - Update QA View")
    try:
        transaction.Start()
        view, created = get_or_create_qa_view(doc, view_name)
        prepare_view(view)
        fail_ogs = build_fail_override(doc, view_cfg.get("fail_color_rgb", [255, 0, 0]))
        pass_ogs = build_pass_override(view_cfg)
        coloured = 0
        for record in records:
            try:
                view.SetElementOverrides(
                    record.element_id, fail_ogs if record.failed else pass_ogs
                )
                if record.failed:
                    coloured += 1
            except Exception:
                logger.debug("Override failed for %s", record.id_int)
        if transaction.Commit() != TransactionStatus.Committed:
            raise RuntimeError("Revit did not commit the transaction.")
        result.update(view=view, created=created, coloured=coloured)
    except Exception as err:
        if transaction.HasStarted() and not transaction.HasEnded():
            transaction.RollBack()
        hint = ""
        if doc.IsWorkshared:
            hint = " If the model is workshared, try 'Reload Latest' and run again."
        result["error"] = "QA view was not updated: {}.{}".format(err, hint)
        warnings.append(result["error"])
        logger.debug(traceback.format_exc())
    finally:
        transaction.Dispose()
    return result


# ---------------------------------------------------------------------------
# Step 5 - CSV report, output window and summary
# ---------------------------------------------------------------------------
def report_folder(cfg):
    """Configured folder, else the (OneDrive-aware) Desktop, else %TEMP%."""
    candidates = []
    custom = to_text((cfg.get("report") or {}).get("output_folder")).strip()
    if custom:
        custom = os.path.expandvars(os.path.expanduser(custom))
        try:
            if not os.path.isdir(custom):
                os.makedirs(custom)
        except Exception:
            pass
        candidates.append(custom)
    try:
        candidates.append(Environment.GetFolderPath(Environment.SpecialFolder.Desktop))
    except Exception:
        pass
    candidates.append(os.path.join(os.path.expanduser("~"), "Desktop"))
    candidates.append(tempfile.gettempdir())
    for folder in candidates:
        if folder and os.path.isdir(folder) and os.access(folder, os.W_OK):
            return folder
    return tempfile.gettempdir()


def sort_records(records):
    return sorted(
        records,
        key=lambda r: (0 if r.failed else 1, r.category, r.level, r.id_int),
    )


def build_csv_rows(records, failures_only):
    rows = []
    for record in sort_records(records):
        if failures_only and not record.failed:
            continue
        rows.append([
            record.id_int,
            record.category,
            record.family_type,
            record.level,
            record.missing_text(),
            "Yes" if record.has_clash else "No",
            record.clash_text(),
            "FAIL" if record.failed else "PASS",
        ])
    return rows


def write_csv(path, header, rows):
    """Write UTF-8 CSV (with BOM so Excel shows non-ASCII names correctly)."""
    if PY2:
        def encode(value):
            return to_text(value).encode("utf-8")

        with open(path, "wb") as handle:
            handle.write(codecs.BOM_UTF8)
            writer = csv.writer(handle)
            writer.writerow([encode(c) for c in header])
            for row in rows:
                writer.writerow([encode(c) for c in row])
    else:
        with open(path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            for row in rows:
                writer.writerow([to_text(c) for c in row])


def export_report(doc, records, cfg):
    report_cfg = cfg.get("report") or {}
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    file_name = "IFCSG_MEP_Audit_{}_{}.csv".format(sanitize_filename(doc.Title), stamp)
    path = os.path.join(report_folder(cfg), file_name)
    write_csv(path, CSV_HEADER, build_csv_rows(records, report_cfg.get("failures_only", False)))
    return path


def parameter_breakdown(records):
    """``[(parameter, failing element count), ...]`` sorted by count."""
    counts = {}
    for record in records:
        for name, _status in record.missing:
            counts[name] = counts.get(name, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def print_output_report(doc, records, summary, cfg):
    """Detailed, clickable results in the pyRevit output window."""
    report_cfg = cfg.get("report") or {}
    output = script.get_output()
    output.set_title(TOOL_NAME)
    output.print_md("# {}".format(TOOL_NAME))
    output.print_md("**Model:** {}  \n**Run:** {}".format(
        to_text(doc.Title), summary["timestamp"]))
    output.print_md("\n".join("- " + line for line in summary["lines"]))

    breakdown = parameter_breakdown(records)
    if breakdown:
        output.print_table(
            table_data=[[name, "{:,}".format(count)] for name, count in breakdown],
            columns=["Parameter", "Elements failing"],
            title="Missing / empty / placeholder parameters",
        )

    failed = [r for r in sort_records(records) if r.failed]
    if not failed:
        output.print_md("### All audited elements passed.")
        return

    if len(failed) <= 1000:
        output.print_md(output.linkify(
            [r.element_id for r in failed],
            title="Select all {} failed elements".format(len(failed)),
        ))
    limit = int(report_cfg.get("max_rows_in_output_window", 300) or 300)
    table = []
    for record in failed[:limit]:
        table.append([
            output.linkify(record.element_id),
            record.category,
            record.family_type,
            record.level,
            record.missing_text() or "-",
            "Yes" if record.has_clash else "No",
        ])
    output.print_table(
        table_data=table,
        columns=["Element", "Category", "Family & Type", "Level", "Missing Parameters", "Clash"],
        title="Failed elements (first {} of {:,}; full list in the CSV)".format(
            min(limit, len(failed)), len(failed)),
    )
    if summary["warnings"]:
        output.print_md("### Warnings\n" + "\n".join("- " + w for w in summary["warnings"]))


def build_summary(records, category_counts, clash_stats, report_path, view_result,
                  warnings, elapsed):
    failed = [r for r in records if r.failed]
    param_fail = sum(1 for r in records if r.has_parameter_issue)
    clash_fail = sum(1 for r in records if r.has_clash)

    per_category = ", ".join(
        "{} {:,}".format(label, count) for label, count in category_counts.items()
    )
    lines = [
        "Elements audited: {:,} ({})".format(len(records), per_category),
        "Passed: {:,}  |  Failed: {:,}".format(len(records) - len(failed), len(failed)),
        "Missing / empty IFC-SG parameters: {:,} elements".format(param_fail),
    ]
    if clash_stats.get("enabled"):
        lines.append(
            "Potential structural clashes: {:,} of {:,} ducts/pipes "
            "(vs. {:,} structural elements, {} linked model(s))".format(
                clash_fail,
                clash_stats.get("mep_checked", 0),
                clash_stats.get("structural_elements", 0),
                clash_stats.get("linked_models", 0),
            )
        )
    else:
        lines.append("Structural clash pre-check: disabled in config")
    lines.append("Run time: {:.1f} s".format(elapsed))

    details = []
    breakdown = parameter_breakdown(records)
    if breakdown:
        details.append("Top gaps: " + ", ".join(
            "{} ({:,})".format(name, count) for name, count in breakdown[:5]))
    details.append("CSV report: {}".format(report_path or "NOT SAVED (see warnings)"))
    if view_result.get("error") is None:
        details.append("QA view '{}' {} - {:,} element(s) coloured red.".format(
            view_result["name"],
            "created" if view_result["created"] else "updated",
            view_result["coloured"],
        ))
    if warnings:
        shown = warnings[:6]
        details.append("Warnings:\n- " + "\n- ".join(shown))
        if len(warnings) > len(shown):
            details.append("(+{} more warnings in the output window)".format(
                len(warnings) - len(shown)))

    return {
        "lines": lines,
        "details": details,
        "failed": len(failed),
        "warnings": warnings,
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def activate_view(uidoc, view):
    try:
        uidoc.ActiveView = view
    except Exception:
        try:
            uidoc.RequestViewChange(view)
        except Exception as err:
            logger.warning("Could not open the QA view: %s", err)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def get_active_documents():
    try:
        uidoc = __revit__.ActiveUIDocument  # noqa: F821  -- injected by pyRevit
    except Exception:
        uidoc = None
    if uidoc is None:
        return None, None
    return uidoc.Document, uidoc


def main():
    started = time.time()
    doc, uidoc = get_active_documents()
    if doc is None:
        show_dialog("Open a Revit project model before running the audit.", warn=True)
        return
    if doc.IsFamilyDocument:
        show_dialog("This tool audits project models, not families.", warn=True)
        return

    warnings = []
    cfg = load_config(CONFIG_FILE, warnings)
    ctx = AuditContext(doc, cfg)

    # 1. Collect
    items, category_counts = collect_audit_elements(doc, cfg, warnings)
    if not items:
        show_dialog(
            "No elements were found in the audited categories.",
            sub_message="Categories: " + ", ".join(category_counts.keys() or ["none"]),
            warn=True,
        )
        return

    # 2. Parameter compliance
    records = audit_parameters(ctx, items, warnings)

    # 3. Clash pre-check
    clash_stats = {"enabled": False}
    if (cfg.get("clash_check") or {}).get("enabled", True):
        hits, clash_stats = run_clash_precheck(doc, records, cfg, warnings)
        for record in records:
            record.clashes = hits.get(record.id_int, [])

    # 4. CSV report (written before touching the model, so it survives
    #    even if the view update fails)
    report_path = None
    try:
        report_path = export_report(doc, records, cfg)
    except Exception as err:
        warnings.append("CSV export failed: {}".format(err))
        logger.debug(traceback.format_exc())

    # 5. QA view + colour overrides (single transaction)
    view_result = update_qa_view(doc, records, cfg, warnings)

    summary = build_summary(
        records, category_counts, clash_stats, report_path, view_result,
        warnings, time.time() - started,
    )

    if (cfg.get("report") or {}).get("show_output_window", True):
        try:
            print_output_report(doc, records, summary, cfg)
        except Exception:
            logger.debug("Output window report failed:\n%s", traceback.format_exc())

    options = []
    if view_result.get("view") is not None:
        options.append(OPTION_OPEN_VIEW)
    if report_path:
        options.append(OPTION_OPEN_REPORT)
    options.append(OPTION_CLOSE)

    headline = "All {:,} elements passed.".format(len(records)) if not summary["failed"] else \
        "{:,} of {:,} elements need attention.".format(summary["failed"], len(records))
    choice = show_dialog(
        headline + "\n\n" + "\n".join(summary["lines"]),
        sub_message="\n\n".join(summary["details"]),
        options=options,
        warn=bool(summary["failed"]),
    )
    if choice == OPTION_OPEN_VIEW:
        activate_view(uidoc, view_result["view"])
    elif choice == OPTION_OPEN_REPORT:
        open_with_shell(report_path)


if __name__ == "__main__":
    try:
        main()
    except AuditCancelled:
        show_dialog("Audit cancelled. No changes were made to the model.")
    except Exception as unexpected:
        logger.error(traceback.format_exc())
        show_dialog(
            "The audit stopped because of an unexpected error.",
            sub_message=to_text(unexpected),
            warn=True,
        )
