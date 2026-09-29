import unittest
from unittest.mock import AsyncMock, patch

from app.services.retrieval import retrieve_examples
from app.services.verified_queries import load_examples


class RetrievalTests(unittest.IsolatedAsyncioTestCase):
    def test_seed_repository_has_verified_examples(self):
        self.assertTrue(load_examples())
        self.assertTrue(all(item["verified"] for item in load_examples()))

    @patch("app.services.retrieval.llm_gateway.get_embeddings", new_callable=AsyncMock)
    async def test_exact_terms_rank_sales_example(self, embeddings):
        embeddings.side_effect = RuntimeError("offline")
        results = await retrieve_examples("top customers by sales", "sales_report", top_k=1)
        self.assertEqual(results[0]["question"], "Show top 5 customers by sales")

    async def test_report_scope_excludes_other_reports(self):
        results = await retrieve_examples("sales", "wip_report")
        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
