import contextlib
import csv
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

TOOL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, TOOL_DIR)

import report_racks as rr  # noqa: E402
import validate_racks as v  # noqa: E402
from openpyxl import load_workbook  # noqa: E402

COLUMNS = list(v.REQUIRED_COLUMNS) + ["尺寸", "附錄D項次", "備註"]


def row(**overrides):
    r = {c: "" for c in COLUMNS}
    r.update({"編號": "SB10-COMS-RTU-01", "檔案群組": "03_P1_SB10", "站點": "SB10", "房間": "COMS/SCADA 設備室",
              "機櫃類型": "RTU 與網路機櫃", "寬mm": "600", "深mm": "1000", "高mm": "2100",
              "X偏移mm": "0", "Y偏移mm": "-150.5", "旋轉度": "0", "尺寸": "600W×1000D×2100H", "附錄D項次": "4"})
    r.update(overrides)
    return r


def write_csv(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)


class ReportCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.csv = os.path.join(self.dir, "racks.csv")
        self.out = os.path.join(self.dir, "報表")

    def tearDown(self):
        self._tmp.cleanup()

    def run_main(self, rows, *flags, csv_path=None):
        if rows is not None:
            write_csv(self.csv, rows)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = rr.main([csv_path or self.csv, "--out", self.out] + list(flags))
        self.output = buf.getvalue()
        return code

    def xlsx(self):
        return load_workbook(os.path.join(self.out, rr.XLSX_NAME))

    def html(self):
        with open(os.path.join(self.out, rr.HTML_NAME), encoding="utf-8") as f:
            return f.read()


class RefuseOnErrorTest(ReportCase):
    def test_errors_write_nothing_and_create_no_folder(self):
        self.assertEqual(self.run_main([row(寬mm="nan")]), 1)
        self.assertFalse(os.path.exists(self.out))
        self.assertEqual(os.listdir(self.dir), ["racks.csv"])

    def test_force_writes_both_with_error_marks(self):
        self.assertEqual(self.run_main([row(寬mm="nan")], "--force"), 1)
        self.assertIn(rr.ERROR_BANNER, self.html())
        self.assertIn("class=\"banner\"", self.html())
        ws = self.xlsx()["檢查結果"]
        self.assertEqual(ws["A2"].value, rr.ERROR_BANNER)
        self.assertEqual(ws["A2"].fill.fgColor.rgb[-6:], "F4CCCC")

    def test_force_keeps_unparsable_number_as_text(self):
        self.run_main([row(寬mm="nan")], "--force")
        ws = self.xlsx()["設備清冊"]
        self.assertEqual(ws.cell(2, COLUMNS.index("寬mm") + 1).value, "nan")


class ExcelTest(ReportCase):
    def test_numbers_are_numbers(self):
        self.assertEqual(self.run_main([row()]), 0)
        ws = self.xlsx()["設備清冊"]
        width = ws.cell(2, COLUMNS.index("寬mm") + 1).value
        self.assertEqual((width, type(width)), (600, int))
        self.assertEqual(ws.cell(2, COLUMNS.index("Y偏移mm") + 1).value, -150.5)
        self.assertEqual(ws.cell(2, COLUMNS.index("附錄D項次") + 1).value, 4)

    def test_appendix_text_stays_text(self):
        self.run_main([row(附錄D項次="12（無獨立項次）")])
        self.assertEqual(self.xlsx()["設備清冊"].cell(2, COLUMNS.index("附錄D項次") + 1).value, "12（無獨立項次）")

    def test_formula_text_is_not_a_formula(self):
        note = '=HYPERLINK("http://x")'
        self.run_main([row(備註=note, 房間="=1+1")])
        ws = self.xlsx()["設備清冊"]
        for column, expected in (("備註", note), ("房間", "=1+1")):
            cell = ws.cell(2, COLUMNS.index(column) + 1)
            self.assertEqual((cell.data_type, cell.value), ("s", expected))

    def test_header_frozen_and_filtered(self):
        self.run_main([row()])
        ws = self.xlsx()["設備清冊"]
        self.assertEqual(ws.freeze_panes, "A2")
        self.assertTrue(ws.auto_filter.ref.startswith("A1:"))
        self.assertEqual(ws.cell(1, len(COLUMNS) + 1).value, rr.EXPECTED_ROOM_COLUMN)

    def test_station_counts(self):
        rows = [row(), row(編號="SB10-TSS-PLC-01", 機櫃類型="PLC 機櫃"), row(編號="SB11-A", 檔案群組="04_P1_SB11")]
        self.run_main(rows)
        ws = self.xlsx()["各站統計"]
        self.assertEqual([c.value for c in ws[1]], ["檔案群組", "PLC 機櫃", "RTU 與網路機櫃", "合計"])
        self.assertEqual([c.value for c in ws[2]], ["03_P1_SB10", 1, 1, 2])
        self.assertEqual([c.value for c in ws[4]], ["合計", 1, 2, 3])


class ExpectedRoomTest(unittest.TestCase):
    ROOMS = [{"name": "通訊/SCADA設備室", "number": "F201"}, {"name": "牽引動力配電室", "number": "F351"}]

    def room(self, **overrides):
        base = os.path.abspath("x")
        cache = {os.path.normcase(os.path.join(base, "a.rvt")): self.ROOMS}
        return rr.expected_room(row(**overrides), base, cache)

    def test_cases(self):
        self.assertEqual(self.room(建築模型="a.rvt", 房間關鍵字="牽引|SCADA"), "F351 牽引動力配電室")
        self.assertEqual(self.room(建築模型="a.rvt", 房間關鍵字="電腦"), "原點（房間關鍵字未命中）")
        self.assertEqual(self.room(建築模型="b.rvt", 房間關鍵字="SCADA"), "未知（房間快取沒有此模型）")
        self.assertEqual(self.room(), "原點（無建築模型）")


class PendingCountTest(unittest.TestCase):
    def test_short_row_error_on_note_column_is_not_pending(self):
        rows = [row(備註=None), row(編號="B", 備註="數量待確認")]
        issues = v.validate(rows, ".", {})
        self.assertIn(v.ERROR, [i.level for i in issues if i.column == "備註"])
        self.assertEqual(rr.Report(rows, issues, ".", {}, "now").pending, 1)


class HtmlTest(ReportCase):
    def test_csv_values_are_escaped(self):
        self.run_main([row(備註="</script><img src=x onerror=alert(1)>", 房間='"><b>')])
        page = self.html()
        self.assertNotIn("<img", page)
        self.assertIn("&quot;&gt;&lt;b&gt;", page)
        self.assertIn("&lt;/script&gt;&lt;img src=x onerror=alert(1)&gt;", page)
        self.assertNotIn("<script", page)

    def test_no_external_resources(self):
        self.run_main([row()])
        page = self.html()
        for needle in ("http://", "https://", "<script", "<link"):
            self.assertNotIn(needle, page)


class FailureTest(ReportCase):
    def test_replace_failure_keeps_old_file_and_cleans_tmp(self):
        self.assertEqual(self.run_main([row()]), 0)
        xlsx_path = os.path.join(self.out, rr.XLSX_NAME)
        with open(xlsx_path, "rb") as f:
            old = f.read()
        old_html = self.html()
        real_replace = os.replace

        def fake_replace(src, dst):
            if dst.endswith(".xlsx"):
                raise PermissionError(13, "檔案被 Excel 開啟中")
            real_replace(src, dst)

        with mock.patch.object(rr.os, "replace", side_effect=fake_replace):
            code = self.run_main([row(編號="SB10-NEW-01")])
        self.assertEqual(code, 1)
        with open(xlsx_path, "rb") as f:
            self.assertEqual(f.read(), old)
        self.assertNotEqual(self.html(), old_html)
        self.assertIn("SB10-NEW-01", self.html())
        self.assertEqual(sorted(os.listdir(self.out)), sorted([rr.XLSX_NAME, rr.HTML_NAME]))
        self.assertIn("保留舊檔", self.output)
        self.assertIn("%s: 已更新" % rr.HTML_NAME, self.output)

    def test_missing_openpyxl_still_writes_html(self):
        with mock.patch.dict(sys.modules, {"openpyxl": None}):
            code = self.run_main([row()])
        self.assertEqual(code, 1)
        self.assertEqual(os.listdir(self.out), [rr.HTML_NAME])
        self.assertIn("未安裝 openpyxl", self.output)

    def test_control_character_refused_then_forced(self):
        self.assertEqual(self.run_main([row(備註="a\x0bb")]), 1)
        self.assertFalse(os.path.exists(self.out))
        self.assertEqual(self.run_main(None, "--force"), 1)
        self.assertEqual(sorted(os.listdir(self.out)), sorted([rr.XLSX_NAME, rr.HTML_NAME]))
        self.assertEqual(self.xlsx()["設備清冊"].cell(2, COLUMNS.index("備註") + 1).value, "a�b")

    def test_any_writer_exception_keeps_other_file(self):
        with mock.patch.object(rr, "write_xlsx", side_effect=ValueError("壞資料")):
            code = self.run_main([row()])
        self.assertEqual(code, 1)
        self.assertEqual(os.listdir(self.out), [rr.HTML_NAME])
        self.assertIn("保留舊檔：ValueError: 壞資料", self.output)
        self.assertIn("報表 已更新 1／失敗 1", self.output)

    def test_unreadable_csv(self):
        self.assertEqual(self.run_main(None, csv_path=os.path.join(self.dir, "no.csv")), 1)
        self.assertFalse(os.path.exists(self.out))


class ProjectDataTest(ReportCase):
    def test_real_csv(self):
        code = self.run_main(None, csv_path=os.path.join(TOOL_DIR, v.DEFAULT_CSV))
        self.assertEqual(code, 0)
        wb = self.xlsx()
        self.assertEqual(wb["設備清冊"].max_row - 1, 36)
        stations = wb["各站統計"]
        groups = [r[0].value for r in stations.iter_rows(min_row=2) if r[0].value != "合計"]
        self.assertEqual(len(groups), 18)
        self.assertEqual(stations.cell(stations.max_row, stations.max_column).value, 36)
        pending = [r for r in wb["檢查結果"].iter_rows(min_row=4, values_only=True) if r[2] == "備註"]
        self.assertEqual(len(pending), 17)
        self.assertEqual(sorted(os.listdir(self.out)), sorted([rr.XLSX_NAME, rr.HTML_NAME]))
        self.assertIn("結果：資料錯誤 0、警告 17；報表 已更新 2／失敗 0", self.output)
        self.assertIn("<b>17</b><span>備註待確認</span>", self.html())


if __name__ == "__main__":
    unittest.main()
