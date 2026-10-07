"""Test registry.json and report_columns.json synchronization.

Verifies:
1. All reports in registry.json exist in report_columns.json
2. All reports in report_columns.json exist in registry.json
3. filter_schema is auto-derived (not manually in registry.json)
4. Auto-derived filter_schema matches expected filters
5. Column types are correctly mapped to filter types
"""
import json
import os
import unittest

import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.column_registry import (
    _COLUMNS_DIR,
    _REGISTRY_PATH,
    _load_registry as _load_cols,
    check_registry_sync,
    derive_filter_schema,
)


class TestRegistrySync(unittest.TestCase):
    """Ensure registry and report columns stay in sync."""

    @classmethod
    def setUpClass(cls):
        with open(_REGISTRY_PATH, "r", encoding="utf-8") as f:
            cls.reg_data = json.load(f)
        cls.cols_data = _load_cols()

    def test_no_sync_warnings(self):
        warnings = check_registry_sync()
        self.assertEqual(len(warnings), 0, f"Registry sync warnings: {warnings}")

    def test_sales_report_filter_schema(self):
        schema = derive_filter_schema("sales_report")
        self.assertIn("start_date", schema)
        self.assertIn("end_date", schema)
        self.assertEqual(schema["start_date"]["type"], "date")
        self.assertIn("category", schema)
        self.assertIn("metal_type", schema)
        self.assertIn("brand", schema)
        self.assertIn("stockdocumentno", schema)
        self.assertEqual(schema["stockdocumentno"]["type"], "string")
        self.assertIn("stockmanagement_statusid", schema)
        self.assertEqual(schema["stockmanagement_statusid"]["type"], "integer")
        self.assertGreaterEqual(len(schema), 20)

    def test_tax_report_filter_schema(self):
        schema = derive_filter_schema("tax_report")
        self.assertIn("start_date", schema)
        self.assertIn("end_date", schema)
        for key in ("bill_mode", "billmode", "sale_mode", "tax_mode", "taxfilter", "tax_status"):
            self.assertIn(key, schema)
        self.assertEqual(len(schema), 10)

    def test_no_manual_filter_schema_in_registry(self):
        for entry in self.reg_data.get("reports", []):
            rk = entry["report_key"]
            self.assertNotIn(
                "filter_schema", entry,
                f"{rk} should not have a manually-defined filter_schema in registry.json"
            )

    def test_filter_schema_auto_derived(self):
        from app.services.validator import load_registry
        reg = load_registry(_REGISTRY_PATH)
        for rk, entry in reg.items():
            self.assertGreater(
                len(entry.filter_schema), 0,
                f"{rk} should have an auto-derived filter_schema"
            )

    def test_report_keys_match(self):
        reg_keys = {r["report_key"] for r in self.reg_data.get("reports", [])}
        col_keys = set(self.cols_data.keys())
        self.assertEqual(
            reg_keys, col_keys,
            f"Registry keys {reg_keys} do not match column keys {col_keys}"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
