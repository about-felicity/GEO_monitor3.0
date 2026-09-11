from __future__ import annotations

import os
import tempfile
import unittest
import json
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from windows_enterprise_worker.analyzer import DeepSeekAnalyzer, _plausible_commercial_brand
from windows_enterprise_worker.collectors import (
    DoubaoCollector, KimiExtensionCollector, YuanbaoCollector, _captured, _sanitize_provider_result,
    _validate_collected_question, _validate_provider_answer,
    _yuanbao_capture_incomplete_reason, create_collector,
)
from windows_enterprise_worker.yuanbao_burst import _install_complete_body_extractor
from windows_enterprise_worker.supervisor import SUPERVISOR, parse_listening_pids, parse_meminfo
from windows_worker_sdk.contracts import CapturedAnswer
from windows_worker_sdk.runner import MODEL_ORDER


class EnterpriseWorkerTests(unittest.TestCase):
    def test_captured_question_gate_runs_before_answer_topic_gate(self):
        actual = _validate_collected_question(
            "doubao", "推荐一款孕妇喝的酸奶", {"actual_question": "推荐一款孕妇喝的酸奶"},
        )
        self.assertEqual(actual, "推荐一款孕妇喝的酸奶")
        with self.assertRaisesRegex(RuntimeError, "网页会话中的问题与本轮任务不一致"):
            _validate_collected_question(
                "yuanbao", "推荐一款孕妇喝的酸奶", {"question": "推荐一款机械键盘"},
            )

    def test_competitor_entity_filter_keeps_companies_and_rejects_equipment_terms(self):
        for name in ("北京碧水源科技股份有限公司", "潍坊恒远环保", "汐汐", "蓝珊瑚", "北排装备"):
            with self.subTest(name=name):
                self.assertTrue(_plausible_commercial_brand(name))
        for name in ("MBR", "MBBR", "A/O/A²/O", "机械格栅", "溶气气浮机", "板框", "芬顿", "TS水处理设备经营部"):
            with self.subTest(name=name):
                self.assertFalse(_plausible_commercial_brand(name))

    def test_provider_quality_gate_rejects_busy_kimi_and_quark_page_shell(self):
        with self.assertRaisesRegex(RuntimeError, "Kimi"):
            _validate_provider_answer("kimi", "问题", "不好意思，Kimi有点累了，可以晚点再问我一遍。")
        with self.assertRaisesRegex(RuntimeError, "Kimi"):
            _validate_provider_answer("kimi", "问题", "和Kimi聊天的人太多了，订阅会员可进入优先队列")
        with self.assertRaisesRegex(RuntimeError, "千问"):
            _validate_provider_answer("quark", "问题", "新对话\n近期对话\n问题\n问题\n问题")
        with self.assertRaisesRegex(RuntimeError, "千问"):
            _validate_provider_answer("quark", "问题", "正常回答\n近期对话\n其他会话标题")
        _validate_provider_answer("kimi", "推荐护发精油", "推荐一款温和滋润的护发精油，适合日常使用。")
        _validate_provider_answer("quark", "推荐电解质饮料", "推荐一款适合运动后的电解质饮料。")

    def test_every_provider_rejects_cross_topic_answer_before_upload(self):
        wrong = (
            "2025 年国内新能源车市，比亚迪一家独大，吉利、长安紧随其后；"
            "车型端比亚迪海鸥夺冠，特斯拉 Model Y 排第二。"
        )
        for model in ("doubao", "yuanbao", "wenxin", "quark", "deepseek", "kimi"):
            with self.subTest(model=model), self.assertRaisesRegex(RuntimeError, "一致性校验"):
                _validate_provider_answer(model, "推荐一款耐用的无线鼠标", wrong)

    def test_every_provider_accepts_matching_product_answer(self):
        answer = "推荐罗技 G304 无线鼠标，连接稳定，续航长，按键和滚轮也比较耐用。"
        for model in ("doubao", "yuanbao", "wenxin", "quark", "deepseek", "kimi"):
            with self.subTest(model=model):
                _validate_provider_answer(model, "推荐一款耐用的无线鼠标", answer)

    def test_provider_sanitizer_removes_source_titles_before_validation(self):
        result = _sanitize_provider_result("doubao", "推荐无线鼠标", {
            "body": "推荐罗技 G304 无线鼠标，续航和连接稳定。\n相关视频\n无线鼠标品牌排行榜",
            "sources": [{"title": "无线鼠标品牌排行榜", "url": "https://example.com/rank"}],
        })
        self.assertNotIn("无线鼠标品牌排行榜", result["body"])
        _validate_provider_answer("doubao", "推荐无线鼠标", result["body"])

    def test_provider_sanitizer_cuts_unindexed_related_video_section(self):
        result = _sanitize_provider_result("doubao", "推荐薄膜键盘", {
            "body": (
                "推荐罗技 K120 薄膜键盘，按键安静，适合日常办公。\n"
                "你可以根据是否需要数字键区选择具体配列。\n"
                "相关视频\n"
                "苹果 Magic Keyboard 上手体验 #Apple #键盘"
            ),
            "sources": [{"title": "另一条未显示在正文中的信源", "url": "https://example.com/other"}],
        })
        self.assertNotIn("相关视频", result["body"])
        self.assertNotIn("Magic Keyboard", result["body"])
        _validate_provider_answer("doubao", "推荐薄膜键盘", result["body"])

    def test_provider_sanitizer_repairs_visual_word_fragments(self):
        result = _sanitize_provider_result("wenxin", "推荐薄膜键盘", {
            "body": (
                "薄膜键盘推荐主要看\n\n使用场景和预算\n\n，办公静音选\n"
                "罗\n技\n或\nCherr\ny\n，高性价比选\n狼\n途。\n\n"
                "罗技 MX Key\ns\n\n高端办公首选，支持多设备切换。"
            ),
        })
        self.assertIn("薄膜键盘推荐主要看使用场景和预算，办公静音选罗技或Cherry", result["body"])
        self.assertIn("罗技 MX Keys\n\n高端办公首选", result["body"])
        self.assertNotIn("\n罗\n技", result["body"])

    def test_provider_sanitizer_preserves_normal_short_lines_from_other_models(self):
        body = "推荐型号\n1\n\n产品说明完整，适合安静办公和日常输入。\n\n选择建议"
        for model in ("doubao", "yuanbao", "quark", "deepseek", "kimi"):
            with self.subTest(model=model):
                result = _sanitize_provider_result(model, "推荐薄膜键盘", {"body": body})
                self.assertEqual(result["body"], body)

    def test_quark_result_removes_prompt_echo_and_conversation_navigation(self):
        result = _sanitize_provider_result("quark", "推荐显示屏", {
            "body": "推荐显示屏\n这是千问返回的完整建议正文，包含产品类型、选择依据和注意事项。\n近期对话\n推荐染发剂\n历史标题",
        })
        self.assertNotIn("推荐染发剂", result["body"])
        self.assertNotIn("近期对话", result["body"])
        self.assertTrue(result["body"].startswith("这是千问返回"))

    def test_quark_prompt_echo_plus_navigation_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "未抓到完整回答正文"):
            _sanitize_provider_result("quark", "推荐显示屏", {
                "body": "推荐显示屏\n近期对话\n推荐染发剂\n其他历史标题",
            })

    def test_every_provider_removes_prompt_echo_and_navigation(self):
        for model in ("doubao", "yuanbao", "wenxin", "quark", "deepseek", "kimi"):
            with self.subTest(model=model):
                result = _sanitize_provider_result(model, "推荐显示屏", {
                    "body": "用户\n推荐显示屏\n助手\n这是模型返回的完整产品建议，包含选择依据和具体注意事项。\n历史会话\n其他问题标题",
                })
                self.assertEqual(
                    result["body"],
                    "这是模型返回的完整产品建议，包含选择依据和具体注意事项。",
                )
                self.assertTrue(result["answer_sanitized"])

    def test_every_provider_rejects_page_shell_without_answer(self):
        for model in ("doubao", "yuanbao", "wenxin", "quark", "deepseek", "kimi"):
            with self.subTest(model=model), self.assertRaisesRegex(RuntimeError, "未抓到完整回答正文"):
                _sanitize_provider_result(model, "推荐显示屏", {
                    "body": "推荐显示屏\n最近对话\n其他问题标题",
                })

    @classmethod
    def tearDownClass(cls) -> None:
        SUPERVISOR.close()

    def test_six_diagnosis_collectors_are_enabled(self):
        self.assertEqual(
            MODEL_ORDER,
            ("doubao", "yuanbao", "wenxin", "quark", "deepseek", "kimi"),
        )

    def test_kimi_factory_uses_independent_chrome_extension(self):
        collector = create_collector("kimi")
        self.assertIsInstance(collector, KimiExtensionCollector)

    def test_kimi_extension_batch_maps_real_rounds_and_sources(self):
        question = "推荐一款耐用的无线鼠标"

        class Client:
            def run_job(self, **kwargs):
                self.kwargs = kwargs
                return [
                    {
                        "prompt": question,
                        "reply": f"推荐罗技 G304 无线鼠标，第 {number} 轮续航稳定。",
                        "sources": [{"title": f"来源{number}", "url": f"https://example.com/{number}"}],
                        "expected_source_count": 1,
                        "source_capture_complete": True,
                        "page_url": f"https://www.kimi.com/chat/{number}",
                    }
                    for number in (1, 2)
                ]

        collector = KimiExtensionCollector()
        collector.client = Client()
        collector.prepare_task("diagnosis-1", "diagnosis")
        with patch("windows_enterprise_worker.collectors._activity", return_value=nullcontext()):
            output = collector.collect_batch(
                [(1, question), (2, question)], lambda _message: None
            )
        self.assertEqual(sorted(output), [1, 2])
        self.assertEqual(collector.client.kwargs["job_id"], "geo-diagnosis-1-kimi-batch")
        self.assertEqual(collector.client.kwargs["questions"], [question])
        self.assertEqual(collector.client.kwargs["rounds"], 2)
        self.assertEqual(output[1].capture_mode, "kimi_chrome_extension")
        self.assertEqual(output[2].sources[0]["title"], "来源2")

    def test_capture_mapping_preserves_complete_sources(self):
        result = _captured({
            "answerText": "complete answer",
            "page_navigation_id": "loader:fresh-document",
            "items": [{"title": "one", "href": "https://example.com/1"}],
            "count": 1,
            "complete": True,
        }, mode="test", started="2026-01-01T00:00:00+08:00")
        result.validate()
        self.assertEqual(result.expected_source_count, 1)
        self.assertTrue(result.source_capture_complete)
        self.assertEqual(result.capture_identity, "loader:fresh-document")

    def test_deepseek_cannot_invent_target_recommendation(self):
        with tempfile.TemporaryDirectory() as folder:
            key = Path(folder) / "key.txt"
            key.write_text("test-key", encoding="utf-8")
            with patch.dict(os.environ, {"GEO_DEEPSEEK_KEY_FILE": str(key)}, clear=False):
                analyzer = DeepSeekAnalyzer()
            analyzer._request = lambda payload: {  # type: ignore[method-assign]
                "recommended": True, "rank": 1, "matched_terms": ["不存在品牌"],
                "products": [], "brands": ["不存在品牌"],
            }
            result = analyzer.analyze(
                "quark", "recommend", "目标品牌", "目标产品",
                CapturedAnswer(body="这是一段没有目标的回答", body_capture_complete=True),
            )
            self.assertFalse(result.recommended)
            self.assertIsNone(result.rank)
            self.assertEqual(result.matched_terms, [])

    def test_literal_target_mention_is_not_automatically_a_recommendation(self):
        with tempfile.TemporaryDirectory() as folder:
            key = Path(folder) / "key.txt"
            key.write_text("test-key", encoding="utf-8")
            with patch.dict(os.environ, {"GEO_DEEPSEEK_KEY_FILE": str(key)}, clear=False):
                analyzer = DeepSeekAnalyzer()
            analyzer._request = lambda payload: {  # type: ignore[method-assign]
                "recommended": False, "rank": None, "matched_terms": ["目标品牌"],
                "products": [], "brands": ["目标品牌"],
            }
            result = analyzer.analyze(
                "deepseek", "推荐相关产品", "目标品牌", "目标产品",
                CapturedAnswer(body="目标品牌只是市场背景之一，并不作为本次推荐。", body_capture_complete=True),
            )
            self.assertFalse(result.recommended)
            self.assertTrue(result.matched_terms)

    def test_android_meminfo_parser_supports_real_memu_format(self):
        self.assertEqual(
            parse_meminfo("TOTAL  1085177  1008348\nJava Heap: 110772\nTOTAL: 1085177"),
            (1085177, 110772),
        )

    def test_yuanbao_burst_has_no_undeployed_device_lock_dependency(self):
        source = (Path(__file__).resolve().parents[1] / "windows_enterprise_worker" / "yuanbao_burst.py").read_text(encoding="utf-8")
        self.assertNotIn("monitor_core.device_lock", source)
        self.assertIn("问题已发送，等待回答完成", source)
        self.assertIn("flush=True", source)

    def test_netstat_parser_limits_reclaim_to_managed_browser_ports(self):
        value = parse_listening_pids(
            """
  TCP    127.0.0.1:9222       0.0.0.0:0       LISTENING       14256
  TCP    127.0.0.1:9301       0.0.0.0:0       LISTENING       9644
  TCP    127.0.0.1:8765       0.0.0.0:0       LISTENING       5332
            """
        )
        self.assertEqual(value, {9222: 14256, 9301: 9644})

    def test_yuanbao_single_round_uses_plus_signal_burst_harvester(self):
        answer = "这是本轮完整的洋芋片推荐回答正文，包含口味、规格、价格与适用场景等清晰建议。"

        def fake_run(command, progress, *, timeout, cwd):
            self.assertIn("windows_enterprise_worker.yuanbao_burst", command)
            payload = {"results": [{
                "question": "推荐洋芋片", "body": answer,
                "sources": [{"title": "来源一", "url": "https://example.com/one"}],
                "expected_source_count": 1, "source_capture_complete": True,
            }]}
            return ["GEO_YUANBAO_BURST_JSON=" + json.dumps(payload, ensure_ascii=False)]

        with (
            patch("windows_enterprise_worker.collectors._run", side_effect=fake_run),
            patch("windows_enterprise_worker.collectors._activity", return_value=nullcontext()),
        ):
            captured = YuanbaoCollector().collect_new_conversation(
                "推荐洋芋片", 1, lambda _message: None
            )
        captured.validate()
        self.assertEqual(captured.body, answer)
        self.assertTrue(captured.body_capture_complete)

    def test_yuanbao_skips_the_slow_speculative_multi_round_attempt(self):
        self.assertFalse(YuanbaoCollector.batch_collection_enabled)

    def test_doubao_batch_publishes_each_jsonl_round_immediately(self):
        question = "推荐一款耐用的无线鼠标"
        callback_rounds: list[int] = []

        def fake_run(command, progress, *, timeout, cwd, tick=None):
            results = Path(command[command.index("--results") + 1])
            for number in (1, 2, 3):
                row = {
                    "ok": True,
                    "round": number,
                    "question": question,
                    "chat_url": f"https://example.com/chat/{number}",
                    "capture_payload": {
                        "answerText": f"推荐罗技 G304 无线鼠标，第 {number} 轮连接稳定、续航持久。",
                        "body_capture_complete": True,
                        "source_capture_complete": True,
                    },
                }
                with results.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                self.assertIsNotNone(tick)
                tick()
                self.assertEqual(callback_rounds, list(range(1, number + 1)))
            return []

        with (
            patch("windows_enterprise_worker.collectors._run", side_effect=fake_run),
            patch("windows_enterprise_worker.collectors._activity", return_value=nullcontext()),
        ):
            output = DoubaoCollector().collect_batch_progressive(
                [(1, question), (2, question), (3, question)],
                lambda _message: None,
                lambda number, _captured: callback_rounds.append(number),
            )
        self.assertEqual(callback_rounds, [1, 2, 3])
        self.assertEqual(sorted(output), [1, 2, 3])
        for captured in output.values():
            captured.validate()

    def test_yuanbao_truncation_gate_rejects_partial_dom_fragments(self):
        self.assertIn("未闭合", _yuanbao_capture_incomplete_reason({
            "body": "ikbc C108（手感", "sources": [],
        }))
        self.assertIn("疑似", _yuanbao_capture_incomplete_reason({
            "body": "推荐 EK815，作为入门首选，做工扎实，手感清脆。",
            "sources": [{} for _ in range(38)], "expected_source_count": 38,
        }))
        self.assertIn("转折", _yuanbao_capture_incomplete_reason({
            "body": "狼蛛按键反馈清晰，功能丰富，但 ABS", "sources": [],
        }))
        self.assertEqual("", _yuanbao_capture_incomplete_reason({
            "body": "推荐达尔优 EK815。它采用 108 键布局，青轴反馈清晰，适合首次购买机械键盘的用户。",
            "sources": [{"title": "来源", "url": "https://example.com"}],
            "expected_source_count": 1,
        }))

    def test_yuanbao_browser_capture_merges_all_markdown_segments(self):
        class Driver:
            @staticmethod
            def execute_script(_script, _message):
                return ["第一段完整选购建议。", "第二段包含其余品牌和产品。"]

        class Collector:
            driver = Driver()

            @staticmethod
            def extract_body(_message):
                return "第一段完整选购建议。"

        collector = Collector()
        _install_complete_body_extractor(collector)
        self.assertEqual(
            collector.extract_body(object()),
            "第一段完整选购建议。\n第二段包含其余品牌和产品。",
        )


if __name__ == "__main__":
    unittest.main()
