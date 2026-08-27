import json
import unittest
from unittest.mock import patch

from product_ai_worker import provider_unavailable_error
from monitor_core.product_analysis import (
    answer_key,
    build_knowledge,
    compact_model_text,
    batch_model_review,
    deterministic_review,
)


class ProductAnalysisEfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.verified = [{
            "question": "推荐一款祛痘精华液",
            "answer": "首选：兰芝祛痘精华液\n核心成分：水杨酸",
            "products": [{
                "product_name": "兰芝祛痘精华液",
                "brand_name": "兰芝",
                "evidence": "首选：兰芝祛痘精华液",
            }],
        }]
        self.knowledge = build_knowledge(self.verified)

    def test_invisible_ui_characters_do_not_break_exact_reuse(self):
        answer = "首选：兰芝\u200b祛痘精华液\n核心成分：水杨酸"
        products, method = deterministic_review(
            answer, "推荐一款祛痘精华液", self.knowledge, [], 0,
        )
        self.assertEqual(method, "verified_answer_reuse")
        self.assertEqual(products[0]["brand_name"], "兰芝")

    def test_algorithm_refuses_single_parser_guess(self):
        products, method = deterministic_review(
            "首选：兰芝祛痘精华液", "推荐一款祛痘精华液",
            self.knowledge, [], 0,
        )
        self.assertIsNone(products)
        self.assertEqual(method, "ambiguous")

    def test_consensus_accepts_grounded_known_product(self):
        rule = [{"product_name": "兰芝祛痘精华液", "evidence": "首选：兰芝祛痘精华液"}]
        products, method = deterministic_review(
            "新的回答\n首选：兰芝祛痘精华液", "推荐一款祛痘精华液",
            self.knowledge, rule, 0,
        )
        self.assertEqual(method, "dual_parser_consensus")
        self.assertEqual(len(products), 1)

    def test_compaction_preserves_product_heading(self):
        answer = "普通说明。" * 1000 + "\n1. 首选：兰芝祛痘精华液\n核心成分：水杨酸"
        excerpt = compact_model_text(answer, "推荐一款祛痘精华液")
        self.assertIn("兰芝祛痘精华液", excerpt)
        self.assertLess(len(excerpt), len(answer))

    def test_answer_key_normalizes_whitespace(self):
        self.assertEqual(answer_key("问题", "A B"), answer_key(" 问题 ", "A\nB"))

    def test_provider_billing_errors_trigger_global_backoff(self):
        for message in (
            "HTTP Error 402: Payment Required",
            "insufficient balance",
            "账户欠费",
            "quota exceeded",
            "invalid api key",
        ):
            with self.subTest(message=message):
                self.assertTrue(provider_unavailable_error(RuntimeError(message)))
        self.assertFalse(provider_unavailable_error(RuntimeError("temporary network timeout")))

    def test_batch_validation_rejects_only_the_bad_row(self):
        response_data = {
            "content": [{"type": "text", "text": json.dumps({"results": [
                {"id": "0", "products": [{
                    "product_name": "甲牌控油洗发水",
                    "brand": "甲牌",
                    "evidence": "推荐甲牌控油洗发水",
                }]},
                {"id": "1", "products": [{
                    "product_name": "乙牌控油洗发水",
                    "brand": "乙牌",
                    "evidence": "正文里不存在的推荐证据",
                }]},
            ]}, ensure_ascii=False)}],
            "usage": {"input_tokens": 100, "output_tokens": 40},
        }

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(response_data, ensure_ascii=False).encode("utf-8")

        items = [
            {"id": "0", "question": "控油蓬松洗发水", "answer": "推荐甲牌控油洗发水。"},
            {"id": "1", "question": "控油蓬松洗发水", "answer": (
                "1. 推荐乙牌控油洗发水\n2. 推荐丙牌控油洗发水"
            )},
        ]
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test"}), patch(
            "monitor_core.product_analysis.urllib.request.urlopen",
            return_value=FakeResponse(),
        ):
            results, usage = batch_model_review(items, self.knowledge)
        self.assertIn("0", results)
        self.assertNotIn("1", results)
        self.assertEqual(usage["accepted_items"], 1)
        self.assertEqual(usage["rejected_items"], 1)


if __name__ == "__main__":
    unittest.main()
