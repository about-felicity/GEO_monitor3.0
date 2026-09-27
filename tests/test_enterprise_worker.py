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
    DoubaoCollector, YuanbaoCollector, _captured, _sanitize_provider_result,
    _validate_collected_question, _validate_provider_answer,
    _yuanbao_capture_incomplete_reason, _matching_bundled_chromedriver, create_collector,
)
from windows_enterprise_worker.yuanbao_burst import _install_complete_body_extractor
from windows_enterprise_worker.doubao_resilient_pipeline import (
    patch_appium_status_probe, patch_new_chat_navigation,
    recover_partial_payload, wrap_grabber,
)
from windows_enterprise_worker.supervisor import SUPERVISOR, parse_listening_pids, parse_meminfo
from windows_worker_sdk.contracts import CapturedAnswer
from windows_worker_sdk.runner import MODEL_ORDER


class EnterpriseWorkerTests(unittest.TestCase):
    def test_doubao_uses_resilient_pipeline_owned_by_production_project(self):
        command = DoubaoCollector()._command(
            "推荐一家重庆中央空调服务商", 1, Path("result.jsonl"), Path("temp")
        )
        self.assertIn("doubao_resilient_pipeline.py", command[1])
        self.assertNotIn("DouBao_Monitor_v2.0", command[1])
        wrapper = (
            Path(__file__).resolve().parents[1]
            / "windows_enterprise_worker" / "doubao_resilient_pipeline.py"
        ).read_text(encoding="utf-8")
        self.assertIn("pipeline.MONITOR_DIR = ROOT", wrapper)
        self.assertIn("pipeline.launch_account_browser =", wrapper)

    def test_doubao_accepts_stable_high_coverage_partial_sources(self):
        payload = {
            "count": 16,
            "expectedCount": 22,
            "complete": False,
            "url": "https://www.doubao.com/chat/123",
            "answerText": "完整回答正文" * 100,
            "items": [
                {"title": f"信源 {index}", "href": f"https://example.com/{index}"}
                for index in range(16)
            ],
        }
        recovered = recover_partial_payload(
            RuntimeError("抓取未完整：" + json.dumps(payload, ensure_ascii=False)),
            "https://www.doubao.com/chat/123",
        )
        self.assertIsNotNone(recovered)
        self.assertTrue(recovered["partialAccepted"])
        self.assertFalse(recovered["source_capture_complete"])
        self.assertFalse(recovered["complete"])
        self.assertEqual(recovered["missingCount"], 6)
        self.assertAlmostEqual(recovered["sourceCoverage"], 16 / 22, places=4)

    def test_doubao_partial_recovery_rejects_low_quality_or_wrong_chat(self):
        base = {
            "count": 2, "expectedCount": 22, "complete": False,
            "url": "https://www.doubao.com/chat/123",
            "answerText": "正文" * 200,
            "items": [{"title": "A", "href": "https://example.com/a"}] * 2,
        }
        error = RuntimeError("抓取未完整：" + json.dumps(base, ensure_ascii=False))
        self.assertIsNone(recover_partial_payload(error, base["url"]))
        high_coverage = {**base, "count": 16, "items": base["items"] * 8}
        high_error = RuntimeError(
            "抓取未完整：" + json.dumps(high_coverage, ensure_ascii=False)
        )
        self.assertIsNone(
            recover_partial_payload(high_error, "https://www.doubao.com/chat/other")
        )

    def test_enterprise_doubao_capture_skips_legacy_persistence_workers(self):
        class Grabber:
            @staticmethod
            def grab_with_retry(_ws_url, _latest_href=""):
                return {"answerText": "完整回答"}

            @staticmethod
            def save_payload(_payload):
                raise AssertionError("legacy persistence must not run")

            @staticmethod
            def start_source_ai_worker():
                raise AssertionError("legacy source worker must not run")

            @staticmethod
            def start_product_ai_worker():
                raise AssertionError("legacy product worker must not run")

        wrapped = wrap_grabber(Grabber)
        self.assertTrue(wrapped.save_payload({})["enterprise_worker_owned"])
        self.assertEqual(wrapped.start_source_ai_worker(), "enterprise-worker")
        self.assertEqual(wrapped.start_product_ai_worker(), "enterprise-worker")

    def test_appium_ready_message_is_not_misclassified_as_an_error(self):
        class Response:
            status_code = 200

            @staticmethod
            def json():
                return {
                    "value": {
                        "ready": True,
                        "message": "The server is ready to accept new connections",
                    }
                }

        class Http:
            def get(self, url, timeout):
                self.request = (url, timeout)
                return Response()

        class AppiumClient:
            def __init__(self):
                self.http = Http()

            @staticmethod
            def _url(path):
                return f"http://127.0.0.1:4723/wd/hub/{path}"

        class Mumu:
            pass

        class Pipeline:
            pass

        Mumu.AppiumClient = AppiumClient
        Pipeline.mumu = Mumu
        patch_appium_status_probe(Pipeline)
        client = AppiumClient()
        self.assertTrue(client.server_ready())
        self.assertEqual(
            client.http.request,
            ("http://127.0.0.1:4723/wd/hub/status", 5),
        )

    def test_current_doubao_direct_new_chat_control_is_preferred(self):
        clicked = []

        class AutomationError(RuntimeError):
            pass

        class Automation:
            def source_root(self):
                return "xml", {"page": "chat", "ids": {"com.larus.nova:id/larus_chat_top_left_create_new_cvs"}}

            class Appium:
                @staticmethod
                def click_id(resource_id, timeout):
                    clicked.append((resource_id, timeout))

            class Logger:
                @staticmethod
                def info(*args):
                    return None

                @staticmethod
                def warning(*args):
                    return None

            appium = Appium()
            logger = Logger()

            def wait_until(self, predicate, **kwargs):
                self.waited = kwargs
                self.assertion = predicate({"page": "chat", "ids": {"input"}})

            def create_new_chat(self):
                raise AssertionError("legacy navigation should not run")

        class Mumu:
            DoubaoAutomation = Automation
            INPUT_ID = "input"

            @staticmethod
            def page_name(root):
                return root["page"]

            @staticmethod
            def has_id(root, resource_id):
                return resource_id in root["ids"]

        Mumu.AutomationError = AutomationError

        class Pipeline:
            mumu = Mumu

        patch_new_chat_navigation(Pipeline)
        instance = Automation()
        with patch("windows_enterprise_worker.doubao_resilient_pipeline.time.sleep"):
            instance.create_new_chat()
        self.assertEqual(
            clicked,
            [("com.larus.nova:id/larus_chat_top_left_create_new_cvs", 5)],
        )
        self.assertTrue(instance.assertion)

    def test_watchdog_repairs_quark_receiver_before_opening_a_page(self):
        script = (
            Path(__file__).resolve().parents[1]
            / "scripts" / "watch_windows_enterprise_worker.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn('$quarkReceiverStart = Join-Path $quarkRoot "start_monitor.ps1"', script)
        self.assertIn("if (-not $quarkHealth -and (Test-Path -LiteralPath $quarkReceiverStart))", script)
        self.assertIn("$quarkPageCooldownElapsed", script)
        self.assertIn("waiting_for_extension", script)
        self.assertLess(
            script.index("& $quarkReceiverStart"),
            script.index('Start-Process -FilePath $quarkExe -WindowStyle Normal'),
        )

    def test_watchdog_restores_yuanbao_debug_browser_and_collection_page(self):
        script = (
            Path(__file__).resolve().parents[1]
            / "scripts" / "watch_windows_enterprise_worker.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn('http://127.0.0.1:9222/json/list', script)
        self.assertIn('$yuanbaoProfile = Join-Path $env:USERPROFILE "ChromeSourceDebug"', script)
        self.assertIn('"--remote-debugging-port=9222"', script)
        self.assertIn('$yuanbaoPageCooldownElapsed', script)
        self.assertIn('"*yuanbao.tencent.com*"', script)
        self.assertIn('yuanbao_debug_ready = $yuanbaoDebugReady', script)
        self.assertIn('yuanbao_page_ready = $yuanbaoPageReady', script)

    def test_chromedriver_must_match_installed_chrome_major(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            matching = root / "runtime" / "chromedriver-153" / "chromedriver-win64" / "chromedriver.exe"
            stale = root / "runtime" / "chromedriver-151" / "chromedriver-win64" / "chromedriver.exe"
            matching.parent.mkdir(parents=True)
            stale.parent.mkdir(parents=True)
            matching.touch()
            stale.touch()
            with patch("windows_enterprise_worker.collectors.ROOT", root), patch(
                "windows_enterprise_worker.collectors._installed_chrome_major", return_value=153,
            ):
                self.assertEqual(_matching_bundled_chromedriver(), matching)
            matching.unlink()
            with patch("windows_enterprise_worker.collectors.ROOT", root), patch(
                "windows_enterprise_worker.collectors._installed_chrome_major", return_value=153,
            ):
                self.assertIsNone(_matching_bundled_chromedriver())

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
        with patch(
            "windows_enterprise_worker.collectors.judge_answer_relevance",
            return_value=False,
        ):
            for model in ("doubao", "yuanbao", "wenxin", "quark", "deepseek", "kimi"):
                with self.subTest(model=model), self.assertRaisesRegex(RuntimeError, "一致性校验"):
                    _validate_provider_answer(model, "推荐一款耐用的无线鼠标", wrong)

    def test_verified_provider_question_fails_open_only_when_judge_is_unavailable(self):
        question = "适合长途差旅的降噪耳机怎么选"
        paraphrase = "经常坐飞机可优先看主动消除环境声、佩戴舒适度和续航表现。"
        with patch(
            "windows_enterprise_worker.collectors.judge_answer_relevance",
            return_value=None,
        ):
            _validate_provider_answer(
                "doubao", question, paraphrase, captured_question=question,
            )
            with self.assertRaisesRegex(RuntimeError, "一致性校验"):
                _validate_provider_answer("doubao", question, paraphrase)

    def test_semantic_rejection_still_wins_over_verified_question(self):
        question = "适合长途差旅的降噪耳机怎么选"
        wrong = "这几款儿童牙膏含氟量适中，刷牙时注意不要吞咽。"
        with patch(
            "windows_enterprise_worker.collectors.judge_answer_relevance",
            return_value=False,
        ), self.assertRaisesRegex(RuntimeError, "一致性校验"):
            _validate_provider_answer(
                "doubao", question, wrong, captured_question=question,
            )

    def test_deepseek_relevance_judge_uses_confidence_thresholds(self):
        analyzer = object.__new__(DeepSeekAnalyzer)
        analyzer.model = "test-model"
        with patch.object(
            analyzer, "_request",
            return_value={"relevant": True, "confidence": 0.91, "reason": "同义表达"},
        ):
            self.assertIs(analyzer.judge_relevance("问题", "回答"), True)
        with patch.object(
            analyzer, "_request",
            return_value={"relevant": False, "confidence": 0.91, "reason": "不同品类"},
        ):
            self.assertIs(analyzer.judge_relevance("问题", "回答"), False)
        with patch.object(
            analyzer, "_request",
            return_value={"relevant": False, "confidence": 0.7, "reason": "不确定"},
        ):
            self.assertIsNone(analyzer.judge_relevance("问题", "回答"))

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

    def test_six_report_dimensions_are_enabled(self):
        self.assertEqual(
            MODEL_ORDER,
            ("doubao", "yuanbao", "wenxin", "quark", "deepseek", "kimi"),
        )

    def test_kimi_direct_collector_is_retired(self):
        with self.assertRaisesRegex(ValueError, "不支持的模型"):
            create_collector("kimi")

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
        self.assertIn("yuanbao_web_identity(args.chrome_port)", source)
        self.assertLess(
            source.index("yuanbao_web_identity(args.chrome_port)"),
            source.index("YuanbaoController(serial=args.serial)"),
        )

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
