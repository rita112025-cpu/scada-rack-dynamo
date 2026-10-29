import ast
import contextlib
import csv
import io
import json
import os
import sys
import tempfile
import unittest

TOOL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, TOOL_DIR)

import validate_racks as v  # noqa: E402

ROOMS = [{"name": "通訊/SCADA設備室", "number": "F201"}, {"name": "牽引動力配電室", "number": "F351"}]


def row(**overrides):
    r = {c: "" for c in v.REQUIRED_COLUMNS}
    r.update({"編號": "SB10-COMS-RTU-01", "檔案群組": "03_P1_SB10", "站點": "SB10", "房間": "COMS/SCADA 設備室",
              "機櫃類型": "RTU 與網路機櫃", "寬mm": "600", "深mm": "1000", "高mm": "2100",
              "X偏移mm": "0", "Y偏移mm": "0", "旋轉度": "0", "備註": ""})
    r.update(overrides)
    return r


def check(rows, base_dir=".", room_cache=None):
    return [(i.level, i.column) for i in v.validate(rows, base_dir, room_cache if room_cache is not None else {})]


def write_csv(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


class RowValueTest(unittest.TestCase):
    def test_clean_row_has_no_issues(self):
        self.assertEqual(check([row()]), [])

    def test_empty_csv(self):
        self.assertEqual(check([]), [(v.ERROR, "")])

    def test_blank_code_reports_line_number(self):
        issues = v.validate([row(編號="")], ".", {})
        self.assertEqual((issues[0].code, issues[0].column), ("第 2 列", "編號"))

    def test_blank_group(self):
        self.assertIn((v.ERROR, "檔案群組"), check([row(檔案群組="")]))

    def test_size_must_be_positive_finite_number(self):
        for bad in ("", "0", "-600", "六百", "nan", "inf", "1e999"):
            with self.subTest(bad=bad):
                self.assertEqual(check([row(寬mm=bad)]), [(v.ERROR, "寬mm")])

    def test_offset_blank_is_ok(self):
        self.assertEqual(check([row(X偏移mm="", Y偏移mm="", 旋轉度="")]), [])

    def test_offset_must_be_finite_number(self):
        for bad in ("200mm", "nan", "-inf", "1e999"):
            with self.subTest(bad=bad):
                self.assertEqual(check([row(Y偏移mm=bad)]), [(v.ERROR, "Y偏移mm")])

    def test_pending_note_is_warning(self):
        self.assertEqual(check([row(備註="PLC 機櫃為 1 座，明細為 17 座，數量待確認")]), [(v.WARNING, "備註")])


class StructureTest(unittest.TestCase):
    def test_missing_header_column_stops_further_checks(self):
        r = row(編號="")
        del r["旋轉度"]
        self.assertEqual(check([r]), [(v.ERROR, "旋轉度")])

    def test_row_with_extra_fields(self):
        r = row()
        r[None] = ["多出來"]
        self.assertEqual(check([r]), [(v.ERROR, "")])

    def test_row_with_missing_fields(self):
        self.assertEqual(check([row(備註=None)]), [(v.ERROR, "備註")])

    def test_extra_and_short_rows_from_real_csv_text(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "racks.csv")
            write_csv(path, [row()])
            with open(path, encoding="utf-8-sig") as f:
                header, line = f.read().splitlines()
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join([header, line + ",x", line.rsplit(",", 2)[0]]) + "\n")
            issues = v.validate(v.read_rows(path), d, {})
        structure = [i.message.split("（")[0] for i in issues if i.message.startswith("欄位數")]
        self.assertEqual(structure, ["欄位數多於表頭", "欄位數少於表頭"])

    def test_code_and_group_with_surrounding_spaces(self):
        self.assertEqual(sorted(check([row(編號=" SB10-COMS-RTU-01", 檔案群組="03_P1_SB10 ")])),
                         sorted([(v.ERROR, "編號"), (v.ERROR, "檔案群組")]))

    def test_control_characters_are_errors(self):
        for bad in ("a\x0bb", "\x00", "x\x1f"):
            with self.subTest(bad=repr(bad)):
                self.assertEqual(check([row(備註=bad)]), [(v.ERROR, "備註")])
        self.assertEqual(check([row(備註="第一行\r\n第二行\t縮排")]), [])

    def test_duplicate_code(self):
        issues = v.validate([row(), row()], ".", {})
        self.assertEqual([(i.column, "第 2、3 列" in i.message) for i in issues], [("編號", True)])

    def test_duplicate_code_ignores_case_and_spaces(self):
        issues = check([row(), row(編號="sb10-coms-rtu-01"), row(編號="SB10-COMS-RTU-01 ")])
        self.assertEqual(issues.count((v.ERROR, "編號")), 3)  # two duplicates + one spacing error


class GroupTest(unittest.TestCase):
    def test_model_without_room_keyword(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "a.rvt"), "w").close()
            self.assertEqual(check([row(建築模型="a.rvt", 房間關鍵字="")], d), [(v.ERROR, "房間關鍵字")])

    def test_inconsistent_model_within_group(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("a.rvt", "b.rvt"):
                open(os.path.join(d, name), "w").close()
            issues = v.validate([row(編號="A", 建築模型="a.rvt", 房間關鍵字="x"),
                                 row(編號="B", 建築模型=" a.rvt ", 房間關鍵字="x"),
                                 row(編號="C", 建築模型="b.rvt", 房間關鍵字="x")], d, {})
        self.assertEqual([(i.level, i.column) for i in issues], [(v.ERROR, "建築模型")])
        self.assertIn("第 2、3 列", issues[0].message)
        self.assertIn("第 4 列", issues[0].message)

    def test_missing_model_warns_once_per_model(self):
        with tempfile.TemporaryDirectory() as d:
            issues = check([row(編號="A", 建築模型="missing.rvt", 房間關鍵字="SCADA"),
                            row(編號="B", 建築模型="missing.rvt", 房間關鍵字="牽引")], d)
        self.assertEqual(issues, [(v.WARNING, "建築模型")])

    def test_room_keyword_checked_against_cache(self):
        d = os.path.abspath("cache-only")  # does not exist: cache alone is enough
        cache = {os.path.normcase(os.path.join(d, "a.rvt")): ROOMS}
        rows = [row(編號="A", 建築模型="a.rvt", 房間關鍵字="不存在|scada"),
                row(編號="B", 建築模型="a.rvt", 房間關鍵字="f351"),
                row(編號="C", 建築模型="a.rvt", 房間關鍵字="電腦|機房")]
        issues = v.validate(rows, d, cache)
        self.assertEqual([(i.level, i.code, i.column) for i in issues], [(v.WARNING, "C", "房間關鍵字")])

    def test_match_room_prefers_earlier_keyword(self):
        self.assertEqual(v.match_room(ROOMS, "牽引|SCADA")["number"], "F351")
        self.assertEqual(v.match_room(ROOMS, "SCADA|牽引")["number"], "F201")
        self.assertEqual(v.match_room(ROOMS, " |f351")["number"], "F351")
        self.assertIsNone(v.match_room(ROOMS, "電腦"))
        self.assertIsNone(v.match_room(ROOMS, ""))

    def test_room_cache_file_is_read(self):
        with tempfile.TemporaryDirectory() as d:
            key = os.path.normpath(os.path.join(d, "a.rvt"))
            with open(os.path.join(d, v.ROOM_CACHE_FILE), "w", encoding="utf-8") as f:
                f.write('{"%s": {"rooms": [{"name": "SCADA", "number": "F1"}]}}' % key.replace("\\", "\\\\"))
            self.assertEqual(list(v.load_room_cache(d).values()), [[{"name": "SCADA", "number": "F1"}]])

    def test_broken_room_cache_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, v.ROOM_CACHE_FILE), "w", encoding="utf-8") as f:
                f.write("not json")
            self.assertEqual(v.load_room_cache(d), {})

    def test_room_cache_entries_without_name_or_number_are_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            key = os.path.normpath(os.path.join(d, "a.rvt"))
            with open(os.path.join(d, v.ROOM_CACHE_FILE), "w", encoding="utf-8") as f:
                json.dump({key: {"rooms": [{"name": "SCADA"}, {"name": None, "number": "F1"}, "x",
                                           {"name": "通訊", "number": "F2"}]}}, f)
            open(key, "w").close()
            issues = v.validate([row(建築模型="a.rvt", 房間關鍵字="SCADA")], d)
        self.assertEqual(issues, [v.Issue(v.WARNING, "SB10-COMS-RTU-01", "房間關鍵字",
                                          "房間關鍵字「SCADA」在快取房間中沒有命中，會放在原點")])


class CliTest(unittest.TestCase):
    def run_cli(self, rows):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "racks.csv")
            write_csv(path, rows)
            before = sorted(os.listdir(d))
            with contextlib.redirect_stdout(io.StringIO()):
                code = v.main(["validate_racks.py", path])
            self.assertEqual(sorted(os.listdir(d)), before)  # read-only
        return code

    def test_warnings_only_exit_0(self):
        self.assertEqual(self.run_cli([row(備註="數量待確認")]), 0)

    def test_errors_exit_1(self):
        self.assertEqual(self.run_cli([row(寬mm="nan")]), 1)

    def test_cp950_console_does_not_crash(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "racks.csv")
            write_csv(path, [row(備註="⚠ 數量待確認")])
            out = io.TextIOWrapper(io.BytesIO(), encoding="cp950")
            with contextlib.redirect_stdout(out):
                code = v.main(["validate_racks.py", path])
                out.flush()
        self.assertEqual(code, 0)

    def test_unreadable_file_exit_1(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(v.main(["validate_racks.py", os.path.join(TOOL_DIR, "no-such.csv")]), 1)


class ProjectDataTest(unittest.TestCase):
    def test_project_csv_has_no_errors(self):
        path = os.path.join(TOOL_DIR, v.DEFAULT_CSV)
        issues = v.validate(v.read_rows(path), TOOL_DIR)
        self.assertEqual([i for i in issues if i.level == v.ERROR], [])
        self.assertEqual(sum(1 for i in issues if i.column == "備註"), 17)


class SyncWithScadaRacksTest(unittest.TestCase):
    """Remove once the column names live in a module shared with scada_racks.py.

    Covers constant subscripts only; scada_racks.py must guard variable ones (r[column]) itself.
    """

    def test_columns_used_by_scada_racks_are_required(self):
        with open(os.path.join(TOOL_DIR, "scada_racks.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        used = set()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                    and any(isinstance(t, ast.Name) and t.id.endswith("_COLUMN") for t in node.targets)):
                used.add(node.value.value)
            elif (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                  and node.value.id in ("r", "row") and isinstance(node.slice, ast.Constant)):
                used.add(node.slice.value)
        self.assertIn("寬mm", used)
        self.assertEqual(used - set(v.REQUIRED_COLUMNS), set())


if __name__ == "__main__":
    unittest.main()
