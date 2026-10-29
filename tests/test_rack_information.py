"""Check portable family defaults and equipment data readback without Revit."""

import csv
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from test_rack_families import ROOT, load_functions


PREFIX = "SCADA_"
PLACEMENT = PREFIX + "定位來源"


def data_api(*names):
    return load_functions(*names, PARAM_PREFIX=PREFIX, PLACEMENT_PARAM=PLACEMENT, CODE_COLUMN="編號",
                          String=str)


class FamilyDefaultsTest(unittest.TestCase):
    def setUp(self):
        with (ROOT / "SCADA_Racks_SoW_V6.csv").open(encoding="utf-8-sig", newline="") as stream:
            self.rows = list(csv.DictReader(stream))
        self.api = data_api("family_defaults")

    def test_reusable_family_has_size_and_source_without_station_identity(self):
        rows = [r for r in self.rows if r["機櫃類型"] == "RTU 與網路機櫃"]
        columns = ["編號", "站點", "機櫃類型", "寬mm", "來源文件", "UPS電源110V"]
        values = self.api.family_defaults(rows, columns)
        self.assertEqual(values[PREFIX + "寬mm"], "600")
        self.assertEqual(values[PREFIX + "機櫃類型"], "RTU 與網路機櫃")
        self.assertEqual(values[PREFIX + "來源文件"], rows[0]["來源文件"])
        for column in ("編號", "站點", "UPS電源110V"):
            self.assertIn("待填", values[PREFIX + column])
        self.assertIn("手動放置", values[PLACEMENT])

    def test_type_used_once_still_does_not_default_to_unique_equipment_identity(self):
        columns = ["編號", "期別", "站點", "建物", "房間", "附錄D項次", "附錄F項次"]
        values = self.api.family_defaults(self.rows[:1], columns)
        self.assertTrue(all("待填" in values[PREFIX + c] for c in columns))


class EquipmentDataTest(unittest.TestCase):
    def setUp(self):
        self.api = data_api("rack_values", "write_rack_data", "verify_rack_data")
        self.row = {"編號": "SB10-COMS-RTU-01", "站點": "SB10", "備註": ""}
        self.columns = list(self.row)
        self.source = "房間 F201 通訊/SCADA設備室"
        self.values = self.api.rack_values(self.row, self.source, self.columns)
        self.definitions = {name: SimpleNamespace(GUID=str(index))
                            for index, name in enumerate(self.values)}
        self.parameters = {}
        for name, value in self.values.items():
            parameter = Mock(IsReadOnly=False)
            parameter.Set.Overloads = {str: parameter.Set}
            parameter.AsString.return_value = value
            self.parameters[self.definitions[name].GUID] = parameter
        self.instance = Mock()
        self.instance.get_Parameter.side_effect = self.parameters.get

    def write(self):
        self.api.write_rack_data(self.instance, self.row, self.source, self.columns, self.definitions)

    def verify(self):
        self.api.verify_rack_data(self.instance, self.row, self.source, self.columns, self.definitions)

    def test_write_and_read_by_guid_with_all_csv_values_and_location(self):
        self.write()
        self.verify()
        for name, value in self.values.items():
            self.parameters[self.definitions[name].GUID].Set.assert_called_once_with(value)
        self.instance.LookupParameter.assert_not_called()

    def test_set_reports_no_change_but_correct_value_is_accepted(self):
        for parameter in self.parameters.values():
            parameter.Set.return_value = False
        self.write()

    def test_set_reports_success_but_loses_value_is_rejected(self):
        parameter = self.parameters[self.definitions[PREFIX + "站點"].GUID]
        parameter.Set.return_value = True
        parameter.AsString.return_value = ""
        with self.assertRaisesRegex(Exception, "SB10-COMS-RTU-01.*SCADA_站點"):
            self.write()

    def test_missing_or_readonly_parameter_is_rejected(self):
        guid = self.definitions[PREFIX + "編號"].GUID
        self.parameters[guid].IsReadOnly = True
        with self.assertRaisesRegex(Exception, "無法寫入參數 SCADA_編號"):
            self.write()
        self.parameters.pop(guid)
        with self.assertRaisesRegex(Exception, "無法寫入參數 SCADA_編號"):
            self.write()

    def test_post_commit_readback_detects_lost_data(self):
        self.write()
        self.parameters[self.definitions[PREFIX + "編號"].GUID].AsString.return_value = ""
        with self.assertRaisesRegex(Exception, "輸出前資料驗證失敗 SCADA_編號"):
            self.verify()

    def test_blank_note_is_a_valid_empty_value(self):
        self.parameters[self.definitions[PREFIX + "備註"].GUID].AsString.return_value = None
        self.write()
        self.verify()


class ExportGateTest(unittest.TestCase):
    def test_failed_readback_blocks_both_exports_and_closes_project(self):
        project = Mock()
        export = Mock()
        verify = Mock(side_effect=ValueError("lost equipment data"))
        api = load_functions(
            "build_group", new_project_document=lambda: project,
            run_transaction=lambda d, name, action: action(),
            load_symbols=lambda d, families: {}, lowest_level=lambda d: None,
            activate_symbols=lambda symbols: None, create_rack=lambda *args: 42,
            get_3d_view=lambda d: None, verify_rack_data=verify, export_nwc=export,
        )
        with self.assertRaisesRegex(ValueError, "lost equipment data"):
            api.build_group("SB10", None, [({}, None, 0, "room")], {}, [], {},
                            "NWC", True, "RVT", True)
        verify.assert_called_once()
        export.assert_not_called()
        project.SaveAs.assert_not_called()
        project.Close.assert_called_once_with(False)


if __name__ == "__main__":
    unittest.main()
