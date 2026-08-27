import unittest

from monitor_core.supervised_product_model import (
    grounded_span,
    merge_spans,
    split_bucket,
    verified_spans,
)


class SupervisedProductModelTests(unittest.TestCase):
    def test_grounded_span_maps_full_width_and_invisible_characters(self):
        text = "推荐：依思佩尔\u200b(EASPEER)睫毛增长液"
        span = grounded_span(text, "依思佩尔（EASPEER）睫毛增长液")
        self.assertIsNotNone(span)
        self.assertIn("依思佩尔", text[slice(*span)])

    def test_product_name_is_preferred_over_long_evidence(self):
        answer = "高端款：梵玢FBCY睫毛精华液，适合敏感肌。"
        spans = verified_spans(answer, [{
            "product_name": "梵玢FBCY睫毛精华液",
            "evidence": "高端款：梵玢FBCY睫毛精华液，适合敏感肌。",
        }])
        self.assertEqual(answer[slice(*spans[0])], "梵玢FBCY睫毛精华液")

    def test_overlapping_spans_are_merged(self):
        self.assertEqual(merge_spans([(2, 8), (5, 10), (20, 21)]), [(2, 10), (20, 21)])

    def test_equal_answers_never_cross_splits(self):
        self.assertEqual(split_bucket("问题", "相同回答"), split_bucket("问题", "相同回答"))


if __name__ == "__main__":
    unittest.main()
