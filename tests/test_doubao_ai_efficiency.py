import json
from pathlib import Path
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import save_doubao_refs as saver


class ProductAiEfficiencyTests(unittest.TestCase):
    def test_product_prompt_and_cache_are_scoped_to_question(self):
        prompt = saver.build_product_prompt("same answer", "recommend a mask")
        self.assertEqual(prompt["question"], "recommend a mask")
        self.assertNotEqual(
            saver.product_ai_cache_key("same answer", "recommend a mask"),
            saver.product_ai_cache_key("same answer", "recommend a shampoo"),
        )

    def test_appearance_ranks_are_normalized_after_filtered_items(self):
        products = saver.normalize_ai_products({"products": [
            {"product_name": "A mask", "brand": "A", "evidence": "A mask", "rank": 1},
            {"product_name": "B mask", "brand": "B", "evidence": "B mask", "rank": 3},
        ]})
        self.assertEqual([item["rank"] for item in products], [1, 2])

    def test_commercial_recommendation_cannot_silently_verify_empty(self):
        answer = "推荐这款A品牌补水面膜，售价39元，支持7天退货。"
        self.assertFalse(saver.credible_empty_product_result(answer))
        self.assertTrue(saver.credible_empty_product_result("没有找到适合的产品。"))

    def test_structured_headings_prevent_false_empty_product_review(self):
        answer = (
            "先给结论：大油头选沙宣绿钻。\n"
            "1. Spes 诗裴丝海盐蓝胖子（细软塌首选）\n"
            "✅ 适合：细软扁塌、半天就油\n"
            "✅ 亮点：海盐加玻尿酸\n"
            "2. 沙宣绿钻控油瓶（重度大油头）\n"
            "适合：出油快、夏天易出汗\n"
        )
        products = saver.extract_structured_product_headings(
            answer, "控油蓬松洗发水推荐"
        )
        self.assertEqual(
            [item["product_name"] for item in products],
            ["Spes 诗裴丝海盐蓝胖子", "沙宣绿钻控油瓶"],
        )
        self.assertFalse(saver.credible_empty_product_result(answer))

    def test_emoji_product_heading_is_not_confused_with_price_section(self):
        answer = (
            "1）平价入门（学生党）\n"
            "✅ 欧莱雅小金瓶（全能基础款）\n"
            "适合：普通发质、轻微毛躁\n"
            "✅ 菲诗蔻（细软塌首选）\n"
            "质地：水感质地，吸收很快\n"
        )
        products = saver.extract_structured_product_headings(answer, "护发精油推荐")
        self.assertEqual(
            [item["product_name"] for item in products],
            ["欧莱雅 小金瓶", "菲诗蔻"],
        )

    def test_question_filter_removes_timestamped_and_wrong_category_items(self):
        products = saver.filter_products_for_question("睫毛增长液推荐", [
            {"product_name": "A睫毛精华液", "evidence": "推荐A睫毛精华液", "rank": 1, "rank_type": "appearance_order"},
            {"product_name": "星伊睫麦穗睫毛", "evidence": "星伊睫麦穗睫毛", "rank": 2, "rank_type": "appearance_order"},
            {"product_name": "B睫毛精华液", "evidence": "00:13 B睫毛精华液", "rank": 3, "rank_type": "appearance_order"},
        ])
        self.assertEqual([item["product_name"] for item in products], ["A睫毛精华液"])
        self.assertEqual(products[0]["rank"], 1)

    def test_product_ai_defaults_to_required_verification(self):
        with mock.patch.dict(saver.os.environ, {}, clear=True):
            self.assertEqual(saver.product_ai_mode(), "required")

    def test_payment_error_pauses_repeated_product_ai_calls(self):
        error = saver.urllib.error.HTTPError("https://example.com", 402, "Payment Required", {}, None)
        original_pause = saver._PRODUCT_AI_PAUSED_UNTIL
        try:
            saver._PRODUCT_AI_PAUSED_UNTIL = 0
            with mock.patch.dict(saver.os.environ, {"ANTHROPIC_API_KEY": "test"}, clear=True), \
                    mock.patch.object(saver.urllib.request, "urlopen", side_effect=error) as request:
                self.assertIsNone(saver.call_anthropic_product_extractor("answer"))
                self.assertIsNone(saver.call_anthropic_product_extractor("answer"))
            self.assertEqual(request.call_count, 1)
        finally:
            saver._PRODUCT_AI_PAUSED_UNTIL = original_pause

    def test_simple_extractors_disable_expensive_thinking_mode(self):
        self.assertEqual(saver.DEEPSEEK_THINKING_MODE, {"type": "disabled"})
        self.assertEqual(
            saver.deepseek_thinking_options("https://api.deepseek.com/anthropic"),
            {"thinking": {"type": "disabled"}},
        )
        self.assertEqual(
            saver.deepseek_thinking_options("https://api.openai.com"),
            {},
        )

    def test_product_prompt_is_compact_and_keeps_complete_body(self):
        answer = (
            "推荐三款：\n"
            "1. 欧莱雅小金瓶，适合干枯发质。\n"
            "2. 卡诗山茶花护发油，适合烫染受损。\n"
            "3. 且初护发精油，适合细软发。"
        )
        encoded = json.dumps(saver.build_product_prompt(answer), ensure_ascii=False)
        self.assertIn("欧莱雅小金瓶", encoded)
        self.assertLess(len(encoded), 1100)
        self.assertLessEqual(saver.product_ai_max_tokens(answer), 1200)

    def test_product_prompt_does_not_truncate_long_answers(self):
        marker = "UNIQUE_PRODUCT_AT_THE_END"
        answer = "正文推荐信息。" * 1000 + marker
        self.assertTrue(saver.build_product_prompt(answer)["text"].endswith(marker))

    def test_output_budget_expands_for_long_numbered_lists(self):
        answer = "\n".join(
            "%d. 品牌%d产品，推荐理由。" % (index, index)
            for index in range(1, 11)
        )
        self.assertGreaterEqual(saver.product_ai_max_tokens(answer), 1000)

    def test_unnumbered_product_lists_have_safe_json_budget(self):
        answer = "推荐几款不同定位的产品，可以根据发质和预算选择。" * 20
        self.assertGreaterEqual(saver.product_ai_max_tokens(answer), 500)
        self.assertLessEqual(saver.product_ai_max_tokens(answer), 680)

    def test_ungrounded_brand_is_cleared_instead_of_invented(self):
        products = saver.ground_product_brands("正文只推荐A面膜", [{
            "product_name": "A面膜", "brand_name": "不存在品牌", "evidence": "A面膜"
        }])
        self.assertEqual(products[0]["brand_name"], "")
        self.assertFalse(products[0]["brand_identified"])

    def test_repeated_contiguous_brand_prefix_is_collapsed(self):
        self.assertEqual(
            saver.normalize_ai_product_brand_prefix("儒曼 儒曼控油蓬松洗发水", "儒曼"),
            "儒曼 控油蓬松洗发水",
        )
        self.assertEqual(
            saver.normalize_ai_product_brand_prefix("JohnJeffJeff二硫化硒", "John Jeff"),
            "John Jeff 二硫化硒",
        )

    def test_grounding_rejects_evidence_not_present_in_answer(self):
        with self.assertRaises(ValueError):
            saver.validate_grounded_ai_products(
                "正文只推荐欧莱雅小金瓶。",
                [{
                    "product_name": "虚构产品",
                    "brand_name": "虚构",
                    "evidence": "这是正文中不存在的证据",
                }],
            )

    def test_grounding_rejects_product_name_unrelated_to_evidence(self):
        with self.assertRaises(ValueError):
            saver.validate_grounded_ai_products(
                "推荐欧莱雅小金瓶，适合干枯发质。",
                [{
                    "product_name": "虚构品牌神奇精华液",
                    "brand_name": "虚构品牌",
                    "evidence": "推荐欧莱雅小金瓶",
                }],
            )

    def test_grounding_rejects_negated_product(self):
        with self.assertRaises(ValueError):
            saver.validate_grounded_ai_products(
                "不推荐欧莱雅小金瓶，推荐卡诗小金瓶。",
                [{
                    "product_name": "欧莱雅小金瓶",
                    "brand_name": "欧莱雅",
                    "evidence": "不推荐欧莱雅小金瓶",
                }],
            )

    def test_grounding_does_not_transfer_negation_to_next_product(self):
        products = saver.validate_grounded_ai_products(
            "不推荐欧莱雅小金瓶，推荐卡诗小金瓶。",
            [{
                "product_name": "卡诗小金瓶",
                "brand_name": "卡诗",
                "evidence": "不推荐欧莱雅小金瓶，推荐卡诗小金瓶",
            }],
        )
        self.assertEqual(products[0]["product_name"], "卡诗小金瓶")

    def test_complete_check_allows_duplicate_model_items_after_deduplication(self):
        parsed = {"products": [
            {"product_name": "欧莱雅小金瓶", "evidence": "推荐欧莱雅小金瓶"},
            {"product_name": "欧莱雅小金瓶", "evidence": "推荐欧莱雅小金瓶"},
        ]}
        products = [{"product_name": "欧莱雅小金瓶", "evidence": "推荐欧莱雅小金瓶"}]
        self.assertEqual(
            saver.ensure_complete_ai_products("推荐欧莱雅小金瓶", parsed, products),
            products,
        )

    def test_verified_result_uses_fingerprint_cache(self):
        answer = "推荐欧莱雅小金瓶，适合干枯发质。"
        products = [{
            "product_name": "欧莱雅小金瓶",
            "brand_name": "欧莱雅",
            "evidence": "推荐欧莱雅小金瓶",
            "rank": 1,
            "rank_type": "appearance_order",
        }]
        cache = {
            saver.product_ai_cache_key(answer): {
                "products": products,
                "method": "anthropic",
                "model": "test",
            }
        }
        with mock.patch.object(saver, "load_json_cache", return_value=cache):
            hit = saver.cached_product_ai_result(answer)
            self.assertEqual(hit["products"][0]["product_name"], products[0]["product_name"])
            self.assertTrue(hit["products"][0]["brand_identified"])


if __name__ == "__main__":
    unittest.main()
