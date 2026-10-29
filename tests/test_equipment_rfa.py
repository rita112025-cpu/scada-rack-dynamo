"""Verify one RFA per CSV equipment row, with exact equipment-specific defaults."""

import csv
import re
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock
import unittest

from test_rack_families import ROOT, load_functions


class EquipmentRfaTest(unittest.TestCase):
    def setUp(self):
        with (ROOT / "SCADA_Racks_SoW_V6.csv").open(encoding="utf-8-sig", newline="") as stream:
            self.rows = list(csv.DictReader(stream))
        controls = {"檔案群組", "建築模型", "房間關鍵字", "X偏移mm", "Y偏移mm", "旋轉度"}
        self.columns = [c for c in self.rows[0] if c not in controls]
        self.build = Mock(side_effect=lambda *args, **kwargs: Mock())
        self.api = load_functions(
            "family_name", "family_defaults", "rack_values", "build_families", "num",
            CODE_COLUMN="編號", PARAM_PREFIX="SCADA_", PLACEMENT_PARAM="SCADA_定位來源",
            re=re, MM=1 / 304.8, build_family=self.build,
        )

    def test_project_csv_produces_36_distinct_families_even_for_same_size(self):
        families = self.api.build_families(self.rows, "output", self.columns, {})
        self.assertEqual(len(families), 36)
        self.assertEqual(self.build.call_count, 36)
        self.assertEqual(set(families), {"SCADA_機櫃_" + r["編號"] for r in self.rows})
        for call, row in zip(self.build.call_args_list, self.rows):
            self.assertIs(call.kwargs["equipment_row"], row)
            self.assertEqual(call.args[4], {row["機櫃類型"]: [row]})

    def test_every_equipment_rfa_has_its_exact_15_csv_values_and_location_note(self):
        self.assertEqual(len(self.columns), 15)
        for row in self.rows:
            with self.subTest(code=row["編號"]):
                defaults = self.api.family_defaults([row], self.columns, equipment_row=row)
                self.assertEqual(len(defaults), 16)
                for column in self.columns:
                    self.assertEqual(defaults["SCADA_" + column], row[column] or "")
                self.assertIn("載入專案後定位", defaults["SCADA_定位來源"])

    def test_same_size_equipment_retains_different_station_and_ups_data(self):
        rows = [r for r in self.rows if r["機櫃類型"] == "RTU 與網路機櫃"]
        defaults = [self.api.family_defaults([r], self.columns, r) for r in rows]
        self.assertGreater(len({v["SCADA_站點"] for v in defaults}), 1)
        self.assertGreater(len({v["SCADA_UPS電源110V"] for v in defaults}), 1)
        self.assertEqual(len({v["SCADA_編號"] for v in defaults}), len(rows))

    def test_duplicate_file_names_are_rejected_before_creating_any_family(self):
        duplicate = dict(self.rows[0], 編號=self.rows[0]["編號"].lower())
        with self.assertRaisesRegex(Exception, "設備族群檔名重複"):
            self.api.build_families([self.rows[0], duplicate], "output", self.columns, {})
        self.build.assert_not_called()

    def test_invalid_equipment_code_cannot_escape_output_directory(self):
        for code in ("", "../outside", "A\\B", "A:B", "A*B", "A.", " A"):
            with self.subTest(code=code), self.assertRaisesRegex(Exception, "族群檔名"):
                self.api.family_name({"編號": code})


class RfaOnlyModeTest(unittest.TestCase):
    def test_rfa_only_mode_skips_station_projects_and_closes_family_documents(self):
        document = Mock()
        station_export = Mock()

        @contextmanager
        def definitions(names, output):
            yield {name: Mock() for name in names}

        api = load_functions(
            "main", read_rows=lambda path: [{"編號": "A", "檔案群組": "station"}],
            os=SimpleNamespace(path=SimpleNamespace(dirname=lambda p: "root", abspath=lambda p: p,
                                                   join=lambda *parts: "/".join(parts)), makedirs=Mock()),
            CONTROL_COLUMNS=("檔案群組",), PARAM_PREFIX="SCADA_", PLACEMENT_PARAM="SCADA_定位來源",
            GROUP_COLUMN="檔案群組", FAMILY_DIR="families", doc=Mock(),
            TransactionManager=Mock(), delete_previous_racks=lambda d: 0,
            shared_parameter_definitions=definitions, build_families=lambda *args: {"rack": document},
            build_all_groups=station_export,
        )
        report = api.main("racks.csv", "NWC", False, "RVT", False)
        self.assertEqual(report[-1], "僅輸出 RFA 完成")
        self.assertIn("RFA: root/families/rack.rfa", report)
        station_export.assert_not_called()
        document.Close.assert_called_once_with(False)


if __name__ == "__main__":
    unittest.main()
