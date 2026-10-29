"""Exercise explicit text overload selection and refreshing stale parameter handles."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from test_rack_information import data_api


class AmbiguousSetter:
    def __init__(self, text_setter):
        self.Overloads = {str: text_setter}

    def __call__(self, value):
        raise AssertionError("the runtime must not select the overload implicitly")


class TextParameterTest(unittest.TestCase):
    def setUp(self):
        self.api = data_api("rack_values", "write_rack_data")
        self.row = {"編號": "SB10-COMS-RTU-01"}
        self.definitions = {"SCADA_編號": SimpleNamespace(GUID="code-guid"),
                            "SCADA_定位來源": SimpleNamespace(GUID="location-guid")}
        self.state = {"code-guid": "待填", "location-guid": "手動放置"}
        self.parameters = {}
        for guid in self.state:
            def set_value(value, key=guid):
                self.state[key] = value
                return True

            self.parameters[guid] = SimpleNamespace(
                IsReadOnly=False, Set=AmbiguousSetter(set_value),
                AsString=lambda key=guid: self.state[key],
            )
        self.document = SimpleNamespace(Regenerate=Mock())
        self.instance = SimpleNamespace(get_Parameter=self.parameters.get, Document=self.document)

    def write(self):
        self.api.write_rack_data(self.instance, self.row, "room", ["編號"], self.definitions)

    def test_explicit_text_overload_sets_actual_equipment_code(self):
        self.write()
        self.assertEqual(self.state, {"code-guid": "SB10-COMS-RTU-01", "location-guid": "room"})
        self.document.Regenerate.assert_not_called()

    def test_reacquires_parameter_after_regeneration_before_rejecting(self):
        fresh = self.parameters["code-guid"]
        stale = SimpleNamespace(IsReadOnly=False, Set=fresh.Set, AsString=lambda: "待填")
        self.parameters["code-guid"] = stale

        def regenerate():
            self.parameters["code-guid"] = fresh

        self.document.Regenerate.side_effect = regenerate
        self.write()
        self.document.Regenerate.assert_called_once()
        self.assertEqual(fresh.AsString(), self.row["編號"])

    def test_persistent_failure_reports_expected_actual_return_and_guid(self):
        parameter = self.parameters["code-guid"]
        parameter.Set = AmbiguousSetter(lambda value: False)
        with self.assertRaises(Exception) as caught:
            self.write()
        message = str(caught.exception)
        for detail in ("SCADA_編號", "SB10-COMS-RTU-01", "待填", "False", "code-guid"):
            self.assertIn(detail, message)
        self.document.Regenerate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
