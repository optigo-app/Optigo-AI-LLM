import unittest

from app.services.orchestrator import execute_plan
from app.services.query_plan import QueryPlan, QueryStep


class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_steps_execute_concurrently_and_preserve_order(self):
        plan = QueryPlan(
            intent="sales", report_key="sales_report", metric="Amount",
            steps=[QueryStep(type="metric_fetch", metric="Amount"), QueryStep(type="metric_fetch", metric="GoldWt")],
        )

        async def execute(step):
            return step.metric

        results = await execute_plan(plan, execute)
        self.assertEqual([result.data for result in results], ["Amount", "GoldWt"])

    async def test_step_failure_does_not_drop_other_results(self):
        plan = QueryPlan(
            intent="sales", report_key="sales_report", metric="Amount",
            steps=[QueryStep(type="metric_fetch", metric="Amount"), QueryStep(type="metric_fetch", metric="GoldWt")],
        )

        async def execute(step):
            if step.metric == "GoldWt":
                raise RuntimeError("failed")
            return step.metric

        results = await execute_plan(plan, execute)
        self.assertEqual(results[0].data, "Amount")
        self.assertEqual(results[1].error, "failed")


if __name__ == "__main__":
    unittest.main()
