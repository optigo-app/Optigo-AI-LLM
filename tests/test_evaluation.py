import importlib.util
import unittest
from pathlib import Path


_SPEC = importlib.util.spec_from_file_location(
    "evaluation_script", Path(__file__).resolve().parents[1] / "scripts" / "evaluate.py"
)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


class EvaluationDatasetTests(unittest.TestCase):
    def test_production_dataset_is_valid(self):
        import json
        cases = json.loads(_MODULE.DATASET.read_text(encoding="utf-8"))
        self.assertEqual(_MODULE.validate_dataset(cases), [])
        self.assertGreaterEqual(len(cases), 20)

    def test_duplicate_questions_are_rejected(self):
        cases = [
            {"question": "total sales", "report_key": "sales_report", "metric": "Amount"},
            {"question": "Total Sales", "report_key": "sales_report", "metric": "Amount"},
        ]
        self.assertTrue(any("duplicate" in error for error in _MODULE.validate_dataset(cases)))

    def test_plan_expectation_is_required(self):
        errors = _MODULE.validate_dataset([{"question": "hello", "report_key": "sales_report"}])
        self.assertTrue(any("plan expectation" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
