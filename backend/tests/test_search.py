import json
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from src.agent.tools import search as search_module
from src.agent.tools.search import SearchQuery


class SearchTests(unittest.TestCase):
    def test_hit_shape(self):
        payload = json.loads(search_module.search(SearchQuery(query="退货")))
        self.assertEqual(payload["source"], "mock")
        self.assertGreaterEqual(len(payload["results"]), 2)
        for item in payload["results"]:
            self.assertEqual(set(item), {"title", "url", "snippet"})
            self.assertTrue(item["title"] and item["url"] and item["snippet"])

    def test_limit_truncates(self):
        self.assertEqual(len(json.loads(search_module.search(SearchQuery(query="退货", limit=1)))["results"]), 1)
        self.assertEqual(len(json.loads(search_module.search(SearchQuery(query="退货", limit=10)))["results"]), 2)
        self.assertEqual(len(json.loads(search_module.search(SearchQuery(query="退货")))["results"]), 2)

    def test_limit_out_of_range_fails(self):
        for limit in (0, 11):
            with self.subTest(limit=limit), self.assertRaises(ValidationError):
                SearchQuery(query="退货", limit=limit)

    def test_no_match_returns_empty(self):
        payload = json.loads(search_module.search(SearchQuery(query="不存在的关键词")))
        self.assertEqual(payload, {"source": "mock", "results": []})

    def test_match_ignores_case(self):
        corpus = ({"title": "Refund Policy", "url": "https://example.com/a", "snippet": "Return in 7 Days"},)
        with patch.object(search_module, "SEARCH_CORPUS", corpus):
            for query in ("REFUND", "refund", "7 days", "ReFuNd PoLiCy"):
                with self.subTest(query=query):
                    payload = json.loads(search_module.search(SearchQuery(query=query)))
                    self.assertEqual(len(payload["results"]), 1)

    def test_invalid_arguments_fail(self):
        with self.assertRaises(ValidationError):
            SearchQuery(query="")
        with self.assertRaises(ValueError):
            search_module.search(SearchQuery(query="   "))
        with self.assertRaises(ValidationError):
            SearchQuery.model_validate_json('{"query": "退货", "extra": 1}')


if __name__ == "__main__":
    unittest.main()
