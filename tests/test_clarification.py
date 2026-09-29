import unittest

from app.services.context_resolver import ambiguous_entity_value, needs_date_clarification


class DateClarificationTests(unittest.TestCase):
    def test_different_date_range_requires_clarification(self):
        self.assertTrue(needs_date_clarification("Try a different date range"))

    def test_change_period_requires_clarification(self):
        self.assertTrue(needs_date_clarification("Please change the period"))

    def test_explicit_preset_does_not_require_clarification(self):
        self.assertFalse(needs_date_clarification("Try a different date range: last month"))

    def test_explicit_dates_do_not_require_clarification(self):
        self.assertFalse(needs_date_clarification("Change date range to 2026-09-01 through 2026-09-22"))

    def test_normal_question_does_not_require_clarification(self):
        self.assertFalse(needs_date_clarification("What are total sales this month?"))

    def test_unqualified_entity_requires_field_confirmation(self):
        self.assertEqual(ambiguous_entity_value("ThGems total sale this year"), "ThGems")

    def test_explicit_customer_does_not_require_field_confirmation(self):
        self.assertIsNone(ambiguous_entity_value("customer ThGems total sale this year"))

    def test_explicit_brand_does_not_require_field_confirmation(self):
        self.assertIsNone(ambiguous_entity_value("brand ThGems total sales this year"))

    def test_dimension_words_do_not_require_field_confirmation(self):
        for question in (
            "manufacturer wise sales",
            "supplier wise sales",
            "customer type Retailer total sales",
            "customer segment generates the most revenue",
        ):
            self.assertIsNone(ambiguous_entity_value(question), question)

    def test_normal_total_sales_is_not_an_entity(self):
        self.assertIsNone(ambiguous_entity_value("total sales this year"))

    def test_question_phrases_are_not_entities(self):
        for question in (
            "What is the total sales value this month?",
            "What is the sales growth % compared with the previous period?",
            "What is the total sales value of diamond in this year?",
            "Show me the total sales value today",
            "Tell me the total revenue this year",
            "How much sales value today?",
            "What is the total tax value this year?",
        ):
            self.assertIsNone(ambiguous_entity_value(question), question)


if __name__ == "__main__":
    unittest.main()
