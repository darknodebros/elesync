import unittest

from elesync.queries import SearchQuery


class TestSearchQuery(unittest.TestCase):
    def test_defaults_and_weight_clamping(self):
        query = SearchQuery("flight", semantic_weight=4)
        self.assertEqual(query.limit, 10)
        self.assertEqual(query.semantic_weight, 1.0)

    def test_limit_is_bounded(self):
        self.assertEqual(SearchQuery("anything", limit=999).limit, 500)

    def test_invalid_limit_is_rejected(self):
        with self.assertRaises(ValueError):
            SearchQuery("anything", limit=0)


if __name__ == "__main__":
    unittest.main()
