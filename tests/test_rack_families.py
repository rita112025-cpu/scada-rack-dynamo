"""Exercise error handling without loading Revit; these are not API integration tests."""

import ast
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "scada_racks.py").read_text(encoding="utf-8")


def load_functions(*names, **globals_):
    tree = ast.parse(SOURCE)
    tree.body = [node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name in names]
    exec(compile(tree, "scada_racks.py", "exec"), globals_)
    return SimpleNamespace(**globals_)


class TransactionTest(unittest.TestCase):
    def setUp(self):
        self.status = SimpleNamespace(Started="Started", Committed="Committed", RolledBack="RolledBack")
        self.transaction = Mock()
        self.transaction.Start.return_value = self.status.Started
        self.transaction.Commit.return_value = self.status.Committed
        self.api = load_functions("run_transaction", Transaction=Mock(return_value=self.transaction),
                                  TransactionStatus=self.status)

    def test_success_returns_action_result_and_disposes(self):
        self.assertEqual(self.api.run_transaction(None, "racks", lambda: 36), 36)
        self.transaction.RollBack.assert_not_called()
        self.transaction.Dispose.assert_called_once()

    def test_revit_rollback_is_failure_without_second_rollback(self):
        self.transaction.Commit.return_value = self.status.RolledBack
        self.transaction.GetStatus.return_value = self.status.RolledBack
        with self.assertRaisesRegex(Exception, "交易未提交"):
            self.api.run_transaction(None, "racks", lambda: 36)
        self.transaction.RollBack.assert_not_called()
        self.transaction.Dispose.assert_called_once()

    def test_action_failure_rolls_back_and_preserves_error(self):
        self.transaction.GetStatus.return_value = self.status.Started
        with self.assertRaisesRegex(ValueError, "bad rack"):
            self.api.run_transaction(None, "racks", Mock(side_effect=ValueError("bad rack")))
        self.transaction.RollBack.assert_called_once()
        self.transaction.Commit.assert_not_called()
        self.transaction.Dispose.assert_called_once()

    def test_failed_start_does_not_run_action(self):
        self.transaction.Start.return_value = self.status.RolledBack
        action = Mock()
        with self.assertRaisesRegex(Exception, "無法開始交易"):
            self.api.run_transaction(None, "racks", action)
        action.assert_not_called()
        self.transaction.Dispose.assert_called_once()


class FamilyLoadTest(unittest.TestCase):
    def setUp(self):
        self.api = load_functions("load_symbols", BuiltInParameter=SimpleNamespace(SYMBOL_NAME_PARAM=1))
        self.project = Mock()
        self.fdoc = Mock()
        self.fdoc.LoadFamily.return_value.GetFamilySymbolIds.return_value = [42]

    def test_type_name_uses_parameter_when_name_getter_is_unreadable(self):
        class Symbol:
            @property
            def Name(self):
                raise AttributeError("unreadable property")

            def get_Parameter(self, parameter):
                return SimpleNamespace(AsString=lambda: "RTU")

        symbol = Symbol()
        self.project.GetElement.return_value = symbol
        result = self.api.load_symbols(self.project, {"rack": self.fdoc})
        self.assertIs(result[("rack", "RTU")], symbol)

    def test_load_failure_names_family(self):
        self.fdoc.LoadFamily.return_value = None
        with self.assertRaisesRegex(Exception, "無法載入族群 rack"):
            self.api.load_symbols(self.project, {"rack": self.fdoc})

    def test_missing_type_name_is_explicit_failure(self):
        self.project.GetElement.return_value.get_Parameter.return_value = None
        with self.assertRaisesRegex(Exception, "無法讀取族群類型名稱 rack"):
            self.api.load_symbols(self.project, {"rack": self.fdoc})

    def test_empty_family_is_explicit_failure(self):
        self.fdoc.LoadFamily.return_value.GetFamilySymbolIds.return_value = []
        with self.assertRaisesRegex(Exception, "族群沒有可用類型 rack"):
            self.api.load_symbols(self.project, {"rack": self.fdoc})


class DynamoSyncTest(unittest.TestCase):
    def test_embedded_python_matches_source(self):
        graph = json.loads((ROOT / "SCADA_Racks_SoW_V6.dyn").read_text(encoding="utf-8-sig"))
        scripts = [node["Code"] for node in graph["Nodes"] if "Code" in node]
        self.assertEqual(scripts, [SOURCE])


if __name__ == "__main__":
    unittest.main()
