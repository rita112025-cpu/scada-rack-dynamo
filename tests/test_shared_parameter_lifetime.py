"""Guard shared parameter source lifetime and actionable family creation errors."""

from contextlib import contextmanager
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from test_rack_families import load_functions


class SharedParameterLifetimeTest(unittest.TestCase):
    def setUp(self):
        self.definition = SimpleNamespace(GUID="rack-guid")
        definitions = Mock()
        definitions.get_Item.return_value = self.definition
        self.group = SimpleNamespace(Definitions=definitions)
        groups = Mock()
        groups.get_Item.return_value = self.group
        self.source_file = SimpleNamespace(Groups=groups)
        self.app = SimpleNamespace(SharedParametersFilename="original.txt",
                                   OpenSharedParameterFile=Mock(return_value=self.source_file))
        self.api = load_functions(
            "shared_parameter_definitions", "add_family_parameters", contextmanager=contextmanager,
            app=self.app, os=SimpleNamespace(path=SimpleNamespace(join=os.path.join, exists=lambda p: True)),
            PARAM_GROUP="SCADA", GroupTypeId=SimpleNamespace(Data="data"),
        )

    def test_source_remains_selected_during_family_creation_and_restores_afterwards(self):
        manager = Mock()

        def add_parameter(definition, group, instance):
            self.assertEqual(self.app.SharedParametersFilename,
                             os.path.join("output", "SCADA_SoW_V6_SharedParameters.txt"))
            self.assertIs(definition, self.definition)
            self.assertEqual((group, instance), ("data", True))
            return "family-parameter"

        manager.AddParameter.side_effect = add_parameter
        with self.api.shared_parameter_definitions(["SCADA_code"], "output") as definitions:
            result = self.api.add_family_parameters(manager, definitions, "rack")
            self.assertEqual(result, {"SCADA_code": "family-parameter"})
        self.assertEqual(self.app.SharedParametersFilename, "original.txt")

    def test_add_failure_names_parameter_and_guid_and_restores_original(self):
        manager = Mock()
        manager.AddParameter.side_effect = ValueError("Shared parameter creation failed")
        with self.assertRaisesRegex(Exception, "rack.*SCADA_code.*rack-guid.*Shared parameter creation failed"):
            with self.api.shared_parameter_definitions(["SCADA_code"], "output") as definitions:
                self.api.add_family_parameters(manager, definitions, "rack")
        self.assertEqual(self.app.SharedParametersFilename, "original.txt")

    def test_empty_original_setting_is_restored_on_failure(self):
        self.app.SharedParametersFilename = ""
        with self.assertRaisesRegex(ValueError, "family failed"):
            with self.api.shared_parameter_definitions(["SCADA_code"], "output"):
                raise ValueError("family failed")
        self.assertEqual(self.app.SharedParametersFilename, "")

    def test_open_failure_also_restores_original(self):
        self.app.OpenSharedParameterFile.return_value = None
        with self.assertRaisesRegex(Exception, "無法開啟共用參數檔"):
            with self.api.shared_parameter_definitions(["SCADA_code"], "output"):
                self.fail("invalid source must not be used")
        self.assertEqual(self.app.SharedParametersFilename, "original.txt")


if __name__ == "__main__":
    unittest.main()
