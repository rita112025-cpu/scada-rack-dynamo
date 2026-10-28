# Dynamo for Revit 2025 (Dynamo 3.3, CPython3) Python Script node.
# Creates SCADA racks (SoW V6 Appendix D) as Electrical Equipment family
# instances (one box family per rack size, one type per rack type, saved to
# 族群\) carrying shared parameters, one new Revit document per file group.
# Racks are placed in the
# matching rooms of each station's architecture model (same shared coordinates),
# then fine-tuned by the CSV offset / rotation columns, and exported to NWC
# (shared coordinates) and/or saved as RVT.
#
# IN[0]  CSV path (SCADA_Racks_SoW_V6.csv)
# IN[1]  output folder for NWC files and the shared parameter file
# IN[2]  True = export NWC files
# IN[3]  output folder for per-station RVT files
# IN[4]  True = save one RVT per file group
# OUT    list of report lines

import clr
import csv
import json
import math
import os
import re

clr.AddReference("RevitAPI")
clr.AddReference("RevitServices")
from Autodesk.Revit.DB import (
    BasicFileInfo, BuiltInCategory, BuiltInParameter, Category, CurveArrArray,
    CurveArray, DetachFromCentralOption, DirectShape, ElementId,
    ElementTransformUtils, ExternalDefinitionCreationOptions,
    FilteredElementCollector, GroupTypeId, Level, Line, ModelPathUtils,
    NavisworksCoordinates, NavisworksExportOptions, NavisworksExportScope,
    NavisworksParameters, OpenOptions, OptionalFunctionalityUtils, Plane,
    ProjectPosition, SaveAsOptions, SketchPlane, SpecTypeId, Transaction,
    UnitSystem, View3D, ViewFamily, ViewFamilyType, XYZ,
)
from Autodesk.Revit.DB.Structure import StructuralType
from RevitServices.Persistence import DocumentManager
from RevitServices.Transactions import TransactionManager
from System.Collections.Generic import List

doc = DocumentManager.Instance.CurrentDBDocument
app = doc.Application

APP_ID = "SCADA_SoW_V6_Rack"  # DirectShape racks from earlier versions, cleaned up on run
RACK_CATEGORY = BuiltInCategory.OST_ElectricalEquipment
FAMILY_DIR = "族群"
PARAM_GROUP = "SCADA_SoW_V6"
PARAM_PREFIX = "SCADA_"
GROUP_COLUMN = "檔案群組"
CODE_COLUMN = "編號"
MODEL_COLUMN = "建築模型"
ROOM_COLUMN = "房間關鍵字"
OFFSET_X_COLUMN = "X偏移mm"
OFFSET_Y_COLUMN = "Y偏移mm"
ROTATION_COLUMN = "旋轉度"
CONTROL_COLUMNS = (GROUP_COLUMN, MODEL_COLUMN, ROOM_COLUMN, OFFSET_X_COLUMN, OFFSET_Y_COLUMN, ROTATION_COLUMN)
PLACEMENT_PARAM = PARAM_PREFIX + "定位來源"
ROOM_CACHE_FILE = "建築模型房間快取.json"
VIEW_NAME = "SCADA_機櫃_3D"
MM = 1.0 / 304.8
RACK_GAP_MM = 1000.0
ROOM_RACK_GAP_MM = 200.0


def read_rows(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def bound_param_names(d):
    names = set()
    it = d.ParameterBindings.ForwardIterator()
    while it.MoveNext():
        names.add(it.Key.Name)
    return names


def ensure_shared_params(d, names, bic, out_dir):
    missing = [n for n in names if n not in bound_param_names(d)]
    if not missing:
        return []
    original = app.SharedParametersFilename
    sp_path = os.path.join(out_dir, "SCADA_SoW_V6_SharedParameters.txt")
    if not os.path.exists(sp_path):
        open(sp_path, "w").close()
    try:
        app.SharedParametersFilename = sp_path
        def_file = app.OpenSharedParameterFile()
        group = def_file.Groups.get_Item(PARAM_GROUP) or def_file.Groups.Create(PARAM_GROUP)
        cats = app.Create.NewCategorySet()
        cats.Insert(Category.GetCategory(d, bic))
        binding = app.Create.NewInstanceBinding(cats)
        for name in missing:
            definition = group.Definitions.get_Item(name)
            if definition is None:
                definition = group.Definitions.Create(
                    ExternalDefinitionCreationOptions(name, SpecTypeId.String.Text))
            if not d.ParameterBindings.Insert(definition, binding, GroupTypeId.Data):
                raise Exception("無法綁定共用參數 " + name)
    finally:
        # Restore whatever was set before, including "none".
        try:
            app.SharedParametersFilename = original or ""
        except Exception:
            pass
    return missing


def delete_previous_racks(d):
    old = [e.Id for e in FilteredElementCollector(d).OfClass(DirectShape)
           if e.ApplicationId == APP_ID]
    if old:
        d.Delete(List[ElementId](old))
    return len(old)


def get_3d_view(d):
    for v in FilteredElementCollector(d).OfClass(View3D):
        if not v.IsTemplate and v.Name == VIEW_NAME:
            return v
    vft = next(t for t in FilteredElementCollector(d).OfClass(ViewFamilyType)
               if t.ViewFamily == ViewFamily.ThreeDimensional)
    view = View3D.CreateIsometric(d, vft.Id)
    view.Name = VIEW_NAME
    return view


def run_transaction(d, name, action):
    t = Transaction(d, name)
    t.Start()
    try:
        result = action()
        t.Commit()
        return result
    except Exception:
        t.RollBack()
        raise


# ---------- architecture model rooms ----------

def open_model(path):
    opts = OpenOptions()
    if BasicFileInfo.Extract(path).IsWorkshared:
        opts.DetachFromCentralOption = DetachFromCentralOption.DetachAndPreserveWorksets
    return app.OpenDocumentFile(ModelPathUtils.ConvertUserVisiblePathToModelPath(path), opts)


def read_model_rooms(path):
    d = open_model(path)
    try:
        pos = d.ActiveProjectLocation.GetProjectPosition(XYZ.Zero)
        rooms = []
        for r in FilteredElementCollector(d).OfCategory(BuiltInCategory.OST_Rooms).WhereElementIsNotElementType():
            if r.Location is None or r.Area <= 0:
                continue
            p = r.Location.Point
            rooms.append({
                "name": r.get_Parameter(BuiltInParameter.ROOM_NAME).AsString() or "",
                "number": r.get_Parameter(BuiltInParameter.ROOM_NUMBER).AsString() or "",
                "x": p.X, "y": p.Y, "z": p.Z,
            })
        return {"position": [pos.EastWest, pos.NorthSouth, pos.Elevation, pos.Angle], "rooms": rooms}
    finally:
        d.Close(False)


class RoomCache:
    """Room data per architecture model, reused until the model file changes."""

    def __init__(self, path):
        self.path = path
        self.data = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)

    def get(self, model_path):
        mtime = os.path.getmtime(model_path)
        entry = self.data.get(model_path)
        if entry and entry.get("mtime") == mtime:
            return entry, False
        entry = read_model_rooms(model_path)
        entry["mtime"] = mtime
        self.data[model_path] = entry
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        return entry, True


def find_room(rooms, keyword):
    """First room matching the earliest keyword in 'a|b|c' (name or number, case-insensitive)."""
    for key in (k.strip().lower() for k in (keyword or "").split("|")):
        if not key:
            continue
        for room in rooms:
            if key in (room["name"] + " " + room["number"]).lower():
                return room
    return None


def plan_group(group_rows, base_dir, cache):
    """Return (project position or None, [(row, centre, angle, source)], notes)."""
    model = (group_rows[0].get(MODEL_COLUMN) or "").strip()
    entry, notes, model_name = None, [], ""
    if model:
        model_path = os.path.normpath(os.path.join(base_dir, model))
        model_name = os.path.basename(model_path)
        if not os.path.exists(model_path):
            notes.append("找不到建築模型 " + model)
        else:
            try:
                entry, opened = cache.get(model_path)
                notes.append(("已讀取 " if opened else "使用快取 ") + model_name)
            except Exception as ex:
                notes.append("讀取建築模型失敗: " + str(ex))

    placements, room_counts, origin_x = [], {}, 0.0
    for r in group_rows:
        w, dep = num(r["寬mm"]) * MM, num(r["深mm"]) * MM
        room = find_room(entry["rooms"], r.get(ROOM_COLUMN)) if entry else None
        if room:
            key = room["name"] + room["number"]
            k = room_counts.get(key, 0)
            room_counts[key] = k + 1
            centre = XYZ(room["x"] + k * (w + ROOM_RACK_GAP_MM * MM), room["y"], room["z"])
            source = "房間 " + (room["number"] + " " + room["name"]).strip() + "（" + model_name + "）"
        else:
            centre = XYZ(origin_x + w / 2, dep / 2, 0)
            origin_x += w + RACK_GAP_MM * MM
            if entry:
                reason = "找不到房間 " + (r.get(ROOM_COLUMN) or "")
            elif model:
                reason = "建築模型無法使用"
            else:
                reason = "無建築模型"
            source = "原點（" + reason + "）"
        # pythonnet does not map XYZ's + operator, so use XYZ.Add.
        centre = centre.Add(XYZ(num(r.get(OFFSET_X_COLUMN)) * MM, num(r.get(OFFSET_Y_COLUMN)) * MM, 0))
        placements.append((r, centre, math.radians(num(r.get(ROTATION_COLUMN))), source))
    position = entry["position"] if entry else None
    return position, placements, notes


# ---------- rack families ----------

def family_template_path():
    roots = [app.FamilyTemplatePath or "", r"C:\ProgramData\Autodesk\RVT 2025\Family Templates"]
    for root in roots:
        if not root:
            continue
        base = root if os.path.basename(root) == "Family Templates" else os.path.dirname(root)
        for rel in (r"Traditional Chinese\公制電氣設備.rft", r"English\Metric Electrical Equipment.rft"):
            path = os.path.join(base, rel)
            if os.path.exists(path):
                return path
    raise Exception("找不到電氣設備族群樣板（公制電氣設備.rft）")


def family_name(row):
    return "SCADA_機櫃_" + "x".join(str(int(num(row[c]))) for c in ("寬mm", "深mm", "高mm"))


def build_family(name, w, dep, h, type_names, family_dir):
    """Create a box family (base centre at origin) with one type per rack type, saved as .rfa."""
    fdoc = app.NewFamilyDocument(family_template_path())

    def create():
        profile = CurveArray()
        p = [XYZ(-w / 2, -dep / 2, 0), XYZ(w / 2, -dep / 2, 0), XYZ(w / 2, dep / 2, 0), XYZ(-w / 2, dep / 2, 0)]
        for i in range(4):
            profile.Append(Line.CreateBound(p[i], p[(i + 1) % 4]))
        loops = CurveArrArray()
        loops.Append(profile)
        plane = SketchPlane.Create(fdoc, Plane.CreateByNormalAndOrigin(XYZ.BasisZ, XYZ.Zero))
        fdoc.FamilyCreate.NewExtrusion(True, loops, plane, h)
        existing = {t.Name for t in fdoc.FamilyManager.Types}
        for t in type_names:
            if t not in existing:
                fdoc.FamilyManager.NewType(t)

    try:
        run_transaction(fdoc, "SCADA rack family", create)
        opts = SaveAsOptions()
        opts.OverwriteExistingFile = True
        opts.MaximumBackups = 1
        fdoc.SaveAs(os.path.join(family_dir, name + ".rfa"), opts)
        remove_backups(family_dir, name, ".rfa")
    except Exception:
        fdoc.Close(False)
        raise
    return fdoc


def build_families(rows, family_dir):
    """Return {family name: open family document}; caller closes them."""
    sizes = {}
    for r in rows:
        sizes.setdefault(family_name(r), (r, set()))[1].add(r["機櫃類型"])
    families = {}
    try:
        for name, (r, types) in sizes.items():
            families[name] = build_family(name, num(r["寬mm"]) * MM, num(r["深mm"]) * MM,
                                          num(r["高mm"]) * MM, sorted(types), family_dir)
    except Exception:
        for fdoc in families.values():
            fdoc.Close(False)
        raise
    return families


def load_symbols(d, families):
    """Load every rack family into d (outside a transaction); return {(family, type): FamilySymbol}."""
    symbols = {}
    for name, fdoc in families.items():
        fam = fdoc.LoadFamily(d)
        for sid in fam.GetFamilySymbolIds():
            s = d.GetElement(sid)
            symbols[(name, s.Name)] = s
    return symbols


def activate_symbols(symbols):
    for s in symbols.values():
        if not s.IsActive:
            s.Activate()


# ---------- rack placement and output ----------

def create_rack(d, row, centre, angle, source, symbols, level, data_columns):
    symbol = symbols[(family_name(row), row["機櫃類型"])]
    inst = d.Create.NewFamilyInstance(XYZ(centre.X, centre.Y, 0), symbol, level, StructuralType.NonStructural)
    d.Regenerate()
    # Level-based placement ignores Z; move the rack to the room floor elevation.
    dz = centre.Z - inst.Location.Point.Z
    if abs(dz) > 1e-9:
        ElementTransformUtils.MoveElement(d, inst.Id, XYZ(0, 0, dz))
    if abs(angle) > 1e-9:
        axis = Line.CreateBound(XYZ(centre.X, centre.Y, centre.Z), XYZ(centre.X, centre.Y, centre.Z + 1))
        ElementTransformUtils.RotateElement(d, inst.Id, axis, angle)
    values = {PARAM_PREFIX + c: row[c] or "" for c in data_columns}
    values[PLACEMENT_PARAM] = source
    builtin_values = {
        BuiltInParameter.ALL_MODEL_MARK: row[CODE_COLUMN],
        BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS: row["站點"] + " " + row["房間"] + " " + row["機櫃類型"],
    }
    for bip, value in builtin_values.items():
        p = inst.get_Parameter(bip)
        if p is not None and not p.IsReadOnly:
            p.Set(value)
    for name, value in values.items():
        p = inst.LookupParameter(name)
        if p is None or p.IsReadOnly or not p.Set(value):
            raise Exception(row[CODE_COLUMN] + " 無法寫入參數 " + name)
    return inst.Id


def lowest_level(d):
    return min(FilteredElementCollector(d).OfClass(Level), key=lambda lv: lv.Elevation)


def export_nwc(d, out_dir, name, ids, view):
    opts = NavisworksExportOptions()
    opts.ExportScope = NavisworksExportScope.SelectedElements
    opts.SetSelectedElementIds(List[ElementId](ids))
    opts.ViewId = view.Id
    opts.Coordinates = NavisworksCoordinates.Shared
    opts.ConvertElementProperties = True
    opts.Parameters = NavisworksParameters.All
    opts.ExportElementIds = True
    opts.ExportLinks = False
    opts.ExportRoomGeometry = False
    opts.ExportUrls = False
    d.Export(out_dir, name, opts)


def remove_backups(rvt_dir, name, ext=".rvt"):
    # Revit keeps at least one backup (name.0001.rvt) when overwriting; drop it.
    pattern = re.compile(re.escape(name) + r"\.\d{4}" + re.escape(ext) + "$", re.IGNORECASE)
    for f in os.listdir(rvt_dir):
        if pattern.match(f):
            try:
                os.remove(os.path.join(rvt_dir, f))
            except OSError:
                pass  # a locked backup is harmless; the RVT itself is already saved


def new_project_document():
    template = app.DefaultProjectTemplate
    if template and os.path.exists(template):
        return app.NewProjectDocument(template)
    return app.NewProjectDocument(UnitSystem.Metric)


def build_group(name, position, placements, families, data_columns, param_names, out_dir, do_export, rvt_dir, do_rvt):
    results = []
    new_doc = new_project_document()
    try:
        bic = RACK_CATEGORY
        if position:
            ew, ns, elev, angle = position
            run_transaction(new_doc, "SCADA shared coordinates", lambda: new_doc.ActiveProjectLocation.SetProjectPosition(
                XYZ.Zero, ProjectPosition(ew, ns, elev, angle)))
        # Bind parameters in their own transaction so the new racks pick them up.
        run_transaction(new_doc, "SCADA parameters",
                        lambda: ensure_shared_params(new_doc, param_names, bic, out_dir))
        symbols = load_symbols(new_doc, families)
        level = lowest_level(new_doc)

        def place():
            activate_symbols(symbols)
            ids = [create_rack(new_doc, r, c, a, s, symbols, level, data_columns) for r, c, a, s in placements]
            return ids, get_3d_view(new_doc)

        ids, view = run_transaction(new_doc, "SCADA racks " + name, place)
        if do_export:
            try:
                export_nwc(new_doc, out_dir, name, ids, view)
                results.append("NWC")
            except Exception as ex:
                results.append("NWC 失敗: " + str(ex))
        if do_rvt:
            try:
                opts = SaveAsOptions()
                opts.OverwriteExistingFile = True
                opts.MaximumBackups = 1
                new_doc.SaveAs(os.path.join(rvt_dir, name + ".rvt"), opts)
                remove_backups(rvt_dir, name)
                results.append("RVT")
            except Exception as ex:
                results.append("RVT 失敗: " + str(ex))
    finally:
        new_doc.Close(False)
    return results


def main(csv_path, out_dir, do_export, rvt_dir, do_rvt):
    report = []
    rows = read_rows(csv_path)
    if not rows:
        return ["CSV 沒有資料: " + csv_path]
    base_dir = os.path.dirname(os.path.abspath(csv_path))
    os.makedirs(out_dir, exist_ok=True)
    if do_rvt:
        os.makedirs(rvt_dir, exist_ok=True)
    data_columns = [c for c in rows[0].keys() if c not in CONTROL_COLUMNS]
    param_names = [PARAM_PREFIX + c for c in data_columns] + [PLACEMENT_PARAM]
    groups = {}
    for r in rows:
        groups.setdefault(r[GROUP_COLUMN], []).append(r)

    if do_export and not OptionalFunctionalityUtils.IsNavisworksExporterAvailable():
        report.append("找不到 Navisworks Exporter，不匯出 NWC")
        do_export = False

    # Racks from earlier versions of this script lived in the current document.
    TransactionManager.Instance.EnsureInTransaction(doc)
    removed = delete_previous_racks(doc)
    TransactionManager.Instance.TransactionTaskDone()
    TransactionManager.Instance.ForceCloseTransaction()
    report.append("目前專案刪除舊機櫃 " + str(removed) + " 台")

    family_dir = os.path.join(base_dir, FAMILY_DIR)
    os.makedirs(family_dir, exist_ok=True)
    families = build_families(rows, family_dir)
    report.append("族群: " + "、".join(sorted(f + ".rfa" for f in families)))
    try:
        report.extend(build_all_groups(groups, families, base_dir, data_columns, param_names,
                                       out_dir, do_export, rvt_dir, do_rvt))
    finally:
        for fdoc in families.values():
            fdoc.Close(False)
    return report


def build_all_groups(groups, families, base_dir, data_columns, param_names, out_dir, do_export, rvt_dir, do_rvt):
    report = []
    cache = RoomCache(os.path.join(base_dir, ROOM_CACHE_FILE))
    for group, group_rows in sorted(groups.items()):
        try:
            position, placements, notes = plan_group(group_rows, base_dir, cache)
            results = build_group(group, position, placements, families, data_columns, param_names,
                                  out_dir, do_export, rvt_dir, do_rvt)
            in_room = sum(1 for p in placements if not p[3].startswith("原點"))
            line = group + ": " + str(len(placements)) + " 台（房間 " + str(in_room) + "）"
            if notes:
                line += "；" + "；".join(notes)
            if results:
                line += " -> " + " -> ".join(results)
            report.append(line)
            for r, _, _, source in placements:
                if source.startswith("原點") and (r.get(MODEL_COLUMN) or "").strip():
                    report.append("    " + r[CODE_COLUMN] + ": " + source)
        except Exception as ex:
            report.append(group + ": 失敗: " + str(ex))
    return report


try:
    OUT = main(IN[0], IN[1], bool(IN[2]), IN[3], bool(IN[4]))
except Exception as ex:
    TransactionManager.Instance.ForceCloseTransaction()
    OUT = ["執行失敗: " + str(ex)]
