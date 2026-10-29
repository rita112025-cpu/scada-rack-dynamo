# Completeness check for SCADA_Racks_SoW_V6.csv, runnable without Revit.
# Read-only: reads the CSV and 建築模型房間快取.json, writes nothing.
#
# Usage: python validate_racks.py [csv path]
# Prints one line per issue; exits 1 when any [錯誤] is found.

import csv
import json
import math
import os
import re
import sys
from collections import namedtuple

# Column names are copied from scada_racks.py, which cannot be imported outside
# Revit; tests/test_validate_racks.py checks that the two stay in sync.
CODE_COLUMN = "編號"
GROUP_COLUMN = "檔案群組"
MODEL_COLUMN = "建築模型"
ROOM_COLUMN = "房間關鍵字"
SIZE_COLUMNS = ("寬mm", "深mm", "高mm")
OPTIONAL_NUMBER_COLUMNS = ("X偏移mm", "Y偏移mm", "旋轉度")
REQUIRED_COLUMNS = ((CODE_COLUMN, GROUP_COLUMN, MODEL_COLUMN, ROOM_COLUMN, "站點", "房間", "機櫃類型")
                    + SIZE_COLUMNS + OPTIONAL_NUMBER_COLUMNS)
NOTE_COLUMN = "備註"
PENDING_MARK = "待確認"
DEFAULT_CSV = "SCADA_Racks_SoW_V6.csv"
ROOM_CACHE_FILE = "建築模型房間快取.json"

# Control characters Excel cannot store (tab, CR and LF are allowed).
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

ERROR = "錯誤"
WARNING = "警告"

Issue = namedtuple("Issue", "level code column message")


def read_rows(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load_room_cache(base_dir):
    """Return {normcase(model path): [room, ...]} from the room cache, or {} when unavailable."""
    path = os.path.join(base_dir, ROOM_CACHE_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {os.path.normcase(k): [r for r in v["rooms"] if isinstance(r, dict)
                                      and isinstance(r.get("name"), str) and isinstance(r.get("number"), str)]
                for k, v in data.items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def _value(row, column):
    return (row.get(column) or "").strip()


def _finite(text):
    try:
        n = float(text)
    except ValueError:
        return None
    return n if math.isfinite(n) else None


def match_room(rooms, keyword):
    """Same semantics as scada_racks.find_room: first room matching the earliest keyword in 'a|b|c'
    (name or number, case-insensitive); None when nothing matches."""
    for key in (k.strip().lower() for k in (keyword or "").split("|")):
        if not key:
            continue
        for room in rooms:
            if key in (room["name"] + " " + room["number"]).lower():
                return room
    return None


def keyword_matches(rooms, keyword):
    return match_room(rooms, keyword) is not None


def check_structure(row, code):
    issues = []
    if None in row:
        issues.append(Issue(ERROR, code, "", "欄位數多於表頭（多出 %d 欄）" % len(row[None])))
    short = [c for c, val in row.items() if c is not None and val is None]
    if short:
        issues.append(Issue(ERROR, code, short[0], "欄位數少於表頭（缺 %d 欄）" % len(short)))
    for c in (CODE_COLUMN, GROUP_COLUMN):
        raw = row.get(c) or ""
        if raw != raw.strip():
            issues.append(Issue(ERROR, code, c, "前後有空白「%s」，下游不會自動去除" % raw))
    for c, val in row.items():
        if c is not None and isinstance(val, str) and CONTROL_CHARS.search(val):
            issues.append(Issue(ERROR, code, c, "含控制字元 %r，Excel 無法儲存" % CONTROL_CHARS.search(val).group()))
    return issues


def check_values(row, code):
    issues = []
    if not _value(row, CODE_COLUMN):
        issues.append(Issue(ERROR, code, CODE_COLUMN, "編號空白"))
    if not _value(row, GROUP_COLUMN):
        issues.append(Issue(ERROR, code, GROUP_COLUMN, "檔案群組空白"))
    for c in SIZE_COLUMNS:
        n = _finite(_value(row, c))
        if n is None or n <= 0:
            issues.append(Issue(ERROR, code, c, "尺寸須為正數，目前為「%s」" % _value(row, c)))
    for c in OPTIONAL_NUMBER_COLUMNS:
        text = _value(row, c)
        if text and _finite(text) is None:
            issues.append(Issue(ERROR, code, c, "不是有限數字「%s」" % text))
    if _value(row, MODEL_COLUMN) and not _value(row, ROOM_COLUMN):
        issues.append(Issue(ERROR, code, ROOM_COLUMN, "有建築模型但沒有房間關鍵字，會放在原點"))
    if PENDING_MARK in _value(row, NOTE_COLUMN):
        issues.append(Issue(WARNING, code, NOTE_COLUMN, _value(row, NOTE_COLUMN)))
    return issues


def check_groups(numbered, base_dir, room_cache):
    """Per file group: one architecture model (only the first row's is used), and it resolves."""
    issues, groups, missing_models = [], {}, set()
    for line, code, row in numbered:
        groups.setdefault(row.get(GROUP_COLUMN) or "", []).append((line, code, row))
    for group, members in groups.items():
        models = {}
        for line, _, row in members:
            models.setdefault(_value(row, MODEL_COLUMN), []).append(line)
        if len(models) > 1:
            detail = "；".join("「%s」第 %s 列" % (m, "、".join(map(str, ls))) for m, ls in models.items())
            issues.append(Issue(ERROR, group, MODEL_COLUMN, "同一檔案群組的建築模型不一致，只會用第一列：" + detail))
        model = _value(members[0][2], MODEL_COLUMN)
        if not model:
            continue
        path = os.path.normpath(os.path.join(base_dir, model))
        rooms = room_cache.get(os.path.normcase(path))
        if rooms is None:
            if not os.path.exists(path) and model not in missing_models:
                missing_models.add(model)
                issues.append(Issue(WARNING, group, MODEL_COLUMN, "找不到建築模型且快取沒有資料：" + model))
            continue
        for _, code, row in members:
            keyword = _value(row, ROOM_COLUMN)
            if keyword and not keyword_matches(rooms, keyword):
                issues.append(Issue(WARNING, code, ROOM_COLUMN, "房間關鍵字「%s」在快取房間中沒有命中，會放在原點" % keyword))
    return issues


def validate(rows, base_dir, room_cache=None):
    """Return a list of Issue for the CSV rows; base_dir resolves relative model paths."""
    if not rows:
        return [Issue(ERROR, "", "", "CSV 沒有資料")]
    missing = [c for c in REQUIRED_COLUMNS if c not in rows[0]]
    if missing:
        return [Issue(ERROR, "", c, "CSV 缺少欄位") for c in missing]
    if room_cache is None:
        room_cache = load_room_cache(base_dir)
    issues, numbered, seen = [], [], {}
    for line, row in enumerate(rows, start=2):  # line 1 is the header
        code = _value(row, CODE_COLUMN) or "第 %d 列" % line
        issues.extend(check_structure(row, code))
        issues.extend(check_values(row, code))
        numbered.append((line, code, row))
        # Codes differing only in case or surrounding spaces name the same rack.
        key = _value(row, CODE_COLUMN).upper()
        if key in seen:
            issues.append(Issue(ERROR, code, CODE_COLUMN, "編號重複（第 %d、%d 列）" % (seen[key], line)))
        elif key:
            seen[key] = line
    issues.extend(check_groups(numbered, base_dir, room_cache))
    return issues


def main(argv):
    # A cp950 console cannot print every character in the CSV; never crash on output.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    path =argv[1] if len(argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), DEFAULT_CSV)
    try:
        rows = read_rows(path)
    except (OSError, UnicodeDecodeError, csv.Error) as ex:
        print("[錯誤] 無法讀取 %s: %s" % (path, ex))
        return 1
    issues = validate(rows, os.path.dirname(os.path.abspath(path)))
    for i in issues:
        print("[%s] %s %s: %s" % (i.level, i.code, i.column, i.message))
    errors = sum(1 for i in issues if i.level == ERROR)
    print("共 %d 台；錯誤 %d、警告 %d" % (len(rows), errors, len(issues) - errors))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
