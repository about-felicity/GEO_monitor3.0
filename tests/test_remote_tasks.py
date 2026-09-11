import hashlib
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from monitor_core.remote_tasks import (
    RemoteTaskQueue,
    _brand_probability,
    _navigation_free_answer,
    _source_free_answer,
    secure_token_matches,
    task_page_html,
)


class RemoteTaskQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.queue = RemoteTaskQueue(self.root / "tasks.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_task_lifecycle_and_idempotent_result(self):
        task = self.queue.create({
            "models": ["yuanbao", "deepseek"],
            "questions": ["推荐一款洗发水"],
            "rounds": 2,
            "question_mode": "interleaved",
        })
        self.assertEqual(task["status"], "queued")
        self.assertEqual(task["total_steps"], 4)

        claimed = self.queue.claim("desktop-one")
        self.assertIsNotNone(claimed)
        self.assertTrue(self.queue.list_workers()[0]["online"])
        self.assertEqual(self.queue.list_workers()[0]["status"], "busy")
        lease = claimed["lease_token"]
        heartbeat = self.queue.heartbeat(
            task["id"], lease,
            {"model": "yuanbao", "question": "推荐一款洗发水", "message": "采集中"},
        )
        self.assertFalse(heartbeat["cancel_requested"])
        self.assertFalse(heartbeat["pause_requested"])

        record = {
            "collector_model": "yuanbao", "question": "推荐一款洗发水",
            "reply": "这是一个足够完整的测试回答", "status": "success",
        }
        first = self.queue.accept_result(
            task["id"], lease, "yuanbao", "request-1", record,
            self.root / "results",
        )
        duplicate = self.queue.accept_result(
            task["id"], lease, "yuanbao", "request-1", record,
            self.root / "results",
        )
        self.assertFalse(first["duplicate"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(self.queue.get(task["id"])["completed_steps"], 1)
        self.assertEqual(
            len((self.root / "results" / "yuanbao_results.jsonl").read_text(encoding="utf-8").splitlines()),
            1,
        )

        finished = self.queue.finish(task["id"], lease, {"status": "completed"})
        self.assertEqual(finished["status"], "completed")
        self.assertNotIn("lease_token", finished)
        self.assertEqual(self.queue.list_workers()[0]["status"], "idle")

    def test_cancel_queued_task(self):
        task = self.queue.create({
            "models": ["doubao"], "questions": ["问题"], "rounds": 1,
        })
        cancelled = self.queue.cancel(task["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIsNone(self.queue.claim("worker"))

    def test_cancel_running_task_is_graceful_and_releases_worker_on_finish(self):
        task = self.queue.create({
            "models": ["doubao"], "questions": ["问题"], "rounds": 1,
        })
        claimed = self.queue.claim("desktop-one")
        cancelled = self.queue.cancel(task["id"])
        self.assertEqual(cancelled["status"], "running")
        self.assertTrue(cancelled["cancel_requested"])
        heartbeat = self.queue.heartbeat(
            task["id"], claimed["lease_token"], {"message": "准备安全停止"}
        )
        self.assertTrue(heartbeat["cancel_requested"])
        finished = self.queue.finish(
            task["id"], claimed["lease_token"], {"status": "cancelled"}
        )
        self.assertEqual(finished["status"], "cancelled")
        self.assertEqual(self.queue.list_workers()[0]["status"], "idle")

    def test_pause_resume_preserves_results_and_completed_rounds(self):
        task = self.queue.create({
            "models": ["doubao"], "questions": ["问题"], "rounds": 3,
        })
        claimed = self.queue.claim("chrome-office")
        self.queue.accept_result(
            task["id"], claimed["lease_token"], "doubao", "round-one", {
                "collector_model": "doubao", "round": 1, "question": "问题",
                "web_body": "第一轮完整正文", "body_capture_complete": True,
            }, self.root / "results",
        )
        pausing = self.queue.pause(task["id"])
        self.assertEqual(pausing["status"], "running")
        self.assertTrue(pausing["pause_requested"])
        heartbeat = self.queue.heartbeat(
            task["id"], claimed["lease_token"], {"message": "保存断点"}
        )
        self.assertTrue(heartbeat["pause_requested"])
        paused = self.queue.finish(
            task["id"], claimed["lease_token"], {"status": "paused"}
        )
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(paused["completed_steps"], 1)
        resumed = self.queue.resume(task["id"])
        self.assertEqual(resumed["status"], "queued")
        reclaimed = self.queue.claim("chrome-office")
        self.assertEqual(reclaimed["id"], task["id"])
        self.assertEqual(reclaimed["completed_rounds"]["doubao"], [1])

    def test_recoverable_failure_is_requeued_with_checkpoint_and_backoff(self):
        task = self.queue.create({
            "models": ["doubao", "quark"], "questions": ["问题"], "rounds": 1,
            "task_kind": "diagnosis",
        })
        claimed = self.queue.claim("desktop-one")
        self.queue.accept_result(
            task["id"], claimed["lease_token"], "doubao", "saved-round", {
                "collector_model": "doubao", "round": 1, "question": "问题",
                "web_body": "已经安全保存的回答正文", "body_capture_complete": True,
            }, self.root / "results",
        )
        recovered = self.queue.finish(task["id"], claimed["lease_token"], {
            "status": "retrying", "error": "千问页面临时异常",
        })
        self.assertEqual(recovered["status"], "queued")
        self.assertEqual(recovered["completed_steps"], 1)
        self.assertEqual(recovered["retry_count"], 1)
        self.assertGreater(recovered["next_attempt_epoch"], 0)
        self.assertIsNone(self.queue.claim("desktop-two"))

    def test_incomplete_task_cannot_be_marked_completed(self):
        task = self.queue.create({
            "models": ["doubao", "quark"], "questions": ["问题"], "rounds": 1,
            "task_kind": "diagnosis",
        })
        claimed = self.queue.claim("desktop-one")
        result = self.queue.finish(task["id"], claimed["lease_token"], {
            "status": "completed",
        })
        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["retry_count"], 1)

    def test_failed_task_can_resume_from_saved_checkpoint(self):
        task = self.queue.create({
            "models": ["doubao", "quark"], "questions": ["问题"], "rounds": 1,
            "task_kind": "diagnosis",
        })
        claimed = self.queue.claim("desktop-one")
        self.queue.accept_result(
            task["id"], claimed["lease_token"], "doubao", "saved-before-failure", {
                "collector_model": "doubao", "round": 1, "question": "问题",
                "web_body": "已经安全保存的回答正文", "body_capture_complete": True,
            }, self.root / "results",
        )
        failed = self.queue.finish(task["id"], claimed["lease_token"], {
            "status": "failed", "error": "旧版本任务失败",
        })
        self.assertEqual(failed["status"], "failed")
        resumed = self.queue.resume(task["id"])
        self.assertEqual(resumed["status"], "queued")
        reclaimed = self.queue.claim("desktop-two")
        self.assertEqual(reclaimed["completed_rounds"]["doubao"], [1])

    def test_same_worker_can_reclaim_running_task_after_extension_refresh(self):
        task = self.queue.create({
            "models": ["doubao"], "questions": ["问题"], "rounds": 2,
        })
        first = self.queue.claim("chrome-office")
        self.queue.accept_result(
            task["id"], first["lease_token"], "doubao", "resume-round-one", {
                "collector_model": "doubao", "round": 1, "question": "问题",
                "web_body": "第一轮正文",
                "capture_identity": "fresh-document-one",
            }, self.root / "results",
        )
        self.assertIsNone(self.queue.claim("different-worker"))
        resumed = self.queue.claim("chrome-office")
        self.assertEqual(resumed["id"], task["id"])
        self.assertNotEqual(resumed["lease_token"], first["lease_token"])
        self.assertEqual(resumed["completed_rounds"]["doubao"], [1])
        question_key = hashlib.sha256("问题".encode("utf-8")).hexdigest()
        answer_key = hashlib.sha256("第一轮正文".encode("utf-8")).hexdigest()
        self.assertEqual(
            resumed["answer_fingerprints"]["doubao"][question_key], [answer_key]
        )
        identity_key = hashlib.sha256("fresh-document-one".encode("utf-8")).hexdigest()
        self.assertEqual(
            resumed["capture_fingerprints"]["doubao"][question_key][answer_key],
            [identity_key],
        )

    def test_per_round_results_can_be_inspected_deleted_and_cleared(self):
        task = self.queue.create({
            "models": ["doubao"], "questions": ["问题"], "rounds": 2,
        })
        claimed = self.queue.claim("chrome-office")
        for round_number in (1, 2):
            self.queue.accept_result(
                task["id"], claimed["lease_token"], "doubao", f"detail-{round_number}", {
                    "collector_model": "doubao", "round": round_number,
                    "question": "问题", "web_body": f"第 {round_number} 轮正文",
                    "sources": [{"title": "来源", "url": "https://example.com/source"}],
                    "body_capture_complete": True, "expected_source_count": 1,
                    "source_capture_complete": True,
                    "analysis": {"mode": "local", "recommended": round_number == 1},
                    "recommended": round_number == 1,
                }, self.root / "results",
            )
        self.queue.finish(task["id"], claimed["lease_token"], {"status": "completed"})
        details = self.queue.results(task["id"])
        self.assertEqual(len(details), 2)
        self.assertEqual(details[0]["body_length"], len("第 1 轮正文"))
        self.assertEqual(len(details[0]["sources"]), 1)
        after_delete = self.queue.delete_result(
            task["id"], "detail-1", self.root / "results"
        )
        self.assertEqual(after_delete["status"], "paused")
        self.assertEqual(after_delete["completed_steps"], 1)
        self.assertEqual(after_delete["completed_rounds"]["doubao"], [2])
        cleared = self.queue.clear_results(task["id"], self.root / "results")
        self.assertEqual(cleared["completed_steps"], 0)
        self.assertEqual(self.queue.results(task["id"]), [])

    def test_finished_task_can_be_rerun_deleted_and_purged(self):
        task = self.queue.create({
            "models": ["doubao"], "questions": ["测试问题"], "rounds": 1,
            "customer_slug": "report-one", "brand_name": "测试品牌",
        })
        claimed = self.queue.claim("desktop-one")
        results = self.root / "results"
        self.queue.accept_result(
            task["id"], claimed["lease_token"], "doubao", "managed-result", {
                "collector_model": "doubao", "round": 1,
                "reply": "测试正文", "sources": [{"url": "https://example.com"}],
            }, results,
        )
        self.queue.finish(task["id"], claimed["lease_token"], {"status": "completed"})
        rerun = self.queue.rerun(task["id"])
        self.assertEqual(rerun["status"], "queued")
        self.assertEqual(rerun["customer_slug"], "report-one")
        deleted = self.queue.delete(task["id"], results)
        self.assertEqual(deleted["id"], task["id"])
        self.assertIsNone(self.queue.get(task["id"]))
        self.assertFalse((results / "doubao_results.jsonl").exists())

    def test_cleanup_removes_only_finished_tasks(self):
        finished = self.queue.create({
            "models": ["doubao"], "questions": ["结束任务"], "rounds": 1,
        })
        claimed = self.queue.claim("desktop-one")
        self.queue.finish(finished["id"], claimed["lease_token"], {"status": "cancelled"})
        queued = self.queue.create({
            "models": ["yuanbao"], "questions": ["排队任务"], "rounds": 1,
        })
        self.assertEqual(self.queue.clear_finished(self.root / "results"), 1)
        self.assertIsNone(self.queue.get(finished["id"]))
        self.assertIsNotNone(self.queue.get(queued["id"]))

    def test_tasks_are_claimed_strictly_one_at_a_time(self):
        first = self.queue.create({
            "models": ["doubao"], "questions": ["第一个问题"], "rounds": 1,
        })
        second = self.queue.create({
            "models": ["yuanbao"], "questions": ["第二个问题"], "rounds": 1,
        })
        claimed_first = self.queue.claim("chrome-office")
        self.assertEqual(claimed_first["id"], first["id"])
        self.assertIsNone(self.queue.claim("another-worker"))
        self.queue.finish(first["id"], claimed_first["lease_token"], {"status": "completed"})
        claimed_second = self.queue.claim("another-worker")
        self.assertEqual(claimed_second["id"], second["id"])

    def test_limits_and_token_comparison(self):
        with self.assertRaises(ValueError):
            self.queue.create({"models": ["unknown"], "questions": ["问题"]})
        with self.assertRaises(ValueError):
            self.queue.create({"models": ["doubao"], "questions": []})
        task = self.queue.create({
            "models": ["doubao"], "questions": [f"问题{i}" for i in range(20)],
            "rounds": 99,
        })
        self.assertEqual(len(task["questions"]), 10)
        self.assertEqual(task["rounds"], 20)
        self.assertTrue(secure_token_matches("same", "same"))
        self.assertFalse(secure_token_matches("same", "different"))
        self.assertFalse(secure_token_matches("", ""))

    def test_customer_diagnosis_collects_six_independent_models(self):
        self.queue.claim("desktop-one", {
            model: {"ready": True, "message": "ok"}
            for model in ("doubao", "yuanbao", "wenxin", "quark", "deepseek", "kimi")
        })
        task = self.queue.create_diagnosis("dianjiezhi", {
            "brand_name": "外星人",
            "product_name": "外星人电解质水",
            "question": "推荐一款大量出汗后适合喝的电解质饮料",
        })
        self.assertEqual(
            task["models"],
            ["doubao", "yuanbao", "wenxin", "quark", "deepseek", "kimi"],
        )
        self.assertEqual(task["rounds"], 3)
        self.assertEqual(task["total_steps"], 16)
        self.assertEqual(task["model_progress"]["deepseek"]["total"], 2)
        self.assertEqual(task["model_progress"]["kimi"]["total"], 2)
        self.assertEqual(task["customer_slug"], "dianjiezhi")
        self.assertEqual(
            self.queue.create_diagnosis("dianjiezhi", {
                "brand_name": "重复提交", "question": "重复问题",
            })["id"],
            task["id"],
        )
        claimed = self.queue.claim("desktop-one")
        lease = claimed["lease_token"]
        self.queue.accept_result(
            task["id"], lease, "doubao", "diagnosis-1", {
                "collector_model": "doubao",
                "round": 1,
                "question": task["questions"][0],
                "reply": "推荐外星人电解质水，也推荐脉动，适合运动后补充电解质。",
                "analysis": {
                    "mode": "local_chrome_extension", "version": 1,
                    "recommended": True, "rank": None,
                },
                "brands": ["外星人", "外星人电解质水", "脉动"],
                "products": [
                    {"brand_name": "外星人", "product_name": "电解质水", "rank": 2},
                    {"brand_name": "脉动", "product_name": "脉动电解质水", "recommended": True, "rank": 3},
                ],
                "sources": [{"title": "补水指南", "url": "https://example.com/guide"}],
                "body_capture_complete": True,
                "expected_source_count": 1,
                "source_capture_complete": True,
                "capture_mode": "chrome_extension_logged_in_tab",
            },
            self.root / "results",
        )
        report = self.queue.diagnosis_report("dianjiezhi")
        self.assertEqual(report["task"]["id"], task["id"])
        self.assertEqual(report["report"]["completed_rounds"], 1)
        self.assertEqual(report["report"]["recommended_rounds"], 1)
        self.assertGreater(report["report"]["overall_rate"], 0)
        self.assertLess(report["report"]["overall_rate"], 75.0)
        self.assertLess(report["report"]["overall_rate"], 30.0)
        self.assertEqual(report["report"]["raw_overall_rate"], 100.0)
        model_report = report["report"]["models"][0]
        self.assertGreater(model_report["calibration_penalty"], 0)
        self.assertEqual(
            report["report"]["probability_adjustment"]["method"],
            "declared_prior_sample_calibration_v6",
        )
        self.assertEqual(model_report["recommendation_rate"], report["report"]["overall_rate"])
        self.assertEqual(
            report["report"]["probability_adjustment"]["kimi_overall_weight"], 1,
        )
        self.assertEqual(report["report"]["models"][0]["average_rank"], 2.0)
        self.assertLessEqual(
            report["report"]["models"][0]["top3_share"],
            report["report"]["models"][0]["recommendation_rate"],
        )
        self.assertEqual([item["name"] for item in report["report"]["competitors"]], ["脉动"])
        self.assertEqual(report["report"]["competitors"][0]["model_mentions"], {"doubao": 1})
        self.assertEqual(report["report"]["competitors"][0]["products"], ["脉动电解质水"])
        self.assertGreater(report["report"]["competitors"][0]["visibility_score"], 0)
        self.assertIn("doubao", report["report"]["competitors"][0]["model_visibility"])
        self.assertEqual(report["task"]["model_progress"]["doubao"]["completed"], 1)
        self.assertEqual(len(report["report"]["sources"]), 1)
        self.assertEqual(report["report"]["quality"]["body_complete_rounds"], 1)
        self.assertEqual(report["report"]["quality"]["source_complete_rounds"], 1)
        self.assertEqual(report["report"]["quality"]["analysis_complete_rounds"], 1)
        self.assertGreater(report["report"]["quality"]["total_body_chars"], 0)
        self.assertEqual(report["report"]["answers"][0]["expected_source_count"], 1)
        self.assertTrue(report["report"]["answers"][0]["body_capture_complete"])
        self.assertTrue(report["report"]["answers"][0]["source_capture_complete"])
        self.assertEqual(report["task"]["events"], [])
        self.assertFalse((self.root / "results" / "doubao_results.jsonl").exists())

    def test_competitors_include_unranked_model_mentions_and_exclude_equipment_terms(self):
        task = self.queue.create({
            "models": ["doubao", "quark", "deepseek"], "questions": ["推荐废水处理设备"],
            "rounds": 1, "question_mode": "sequential", "task_kind": "diagnosis",
            "customer_slug": "industrial-competitors", "brand_name": "目标环保公司",
            "product_name": "目标废水处理设备",
        })
        claimed = self.queue.claim("desktop-one")
        lease = claimed["lease_token"]
        common = {
            "question": task["questions"][0], "body_capture_complete": True,
            "expected_source_count": 0, "source_capture_complete": True,
            "analysis": {"mode": "local_chrome_extension", "recommended": False, "rank": None},
        }
        self.queue.accept_result(task["id"], lease, "doubao", "industry-doubao", {
            **common, "collector_model": "doubao", "round": 1,
            "reply": "推荐汐汐一体化设备，其次是蓝珊瑚设备；MBR、机械格栅和板框压滤机是工艺设备。",
            "brands": ["汐汐", "蓝珊瑚", "MBR", "机械格栅", "板框"],
            "products": [
                {"brand": "汐汐", "name": "汐汐一体化设备", "recommended": True, "rank": 1},
                {"brand": "蓝珊瑚", "name": "蓝珊瑚设备", "recommended": True, "rank": 2},
                {"brand": "MBR", "name": "MBR反应器", "recommended": True},
                {"brand": "机械格栅", "name": "机械格栅", "recommended": True},
            ],
        }, self.root / "results")
        self.queue.accept_result(task["id"], lease, "quark", "industry-quark", {
            **common, "collector_model": "quark", "round": 1,
            "reply": "代表厂商包括北京碧水源科技股份有限公司和鹏鹞环保股份有限公司。",
            "brands": ["北京碧水源科技股份有限公司", "鹏鹞环保股份有限公司"],
            "products": [],
        }, self.root / "results")
        self.queue.accept_result(task["id"], lease, "deepseek", "industry-deepseek", {
            **common, "collector_model": "deepseek", "round": 1,
            "reply": "值得关注的厂商有潍坊恒远环保，也提到了TS水处理设备经营部。",
            "brands": ["潍坊恒远环保", "TS水处理设备经营部"],
            "products": [{"brand": "潍坊恒远环保", "name": "一体化污水处理设备", "recommended": False}],
        }, self.root / "results")

        competitors = self.queue.diagnosis_report("industrial-competitors")["report"]["competitors"]
        by_name = {item["name"]: item for item in competitors}
        self.assertTrue({"汐汐", "蓝珊瑚", "北京碧水源科技股份有限公司", "鹏鹞环保股份有限公司", "潍坊恒远环保"}.issubset(by_name))
        self.assertTrue({"MBR", "机械格栅", "板框", "TS水处理设备经营部"}.isdisjoint(by_name))
        self.assertGreater(by_name["汐汐"]["model_visibility"]["doubao"], by_name["蓝珊瑚"]["model_visibility"]["doubao"])
        self.assertEqual(by_name["北京碧水源科技股份有限公司"]["recommended_mentions"], 0)
        self.assertGreater(by_name["北京碧水源科技股份有限公司"]["model_visibility"]["quark"], 0)
        self.assertIn("quark", by_name["北京碧水源科技股份有限公司"]["mention_models"])

    def test_legacy_report_without_scheduled_kimi_keeps_historical_mirror(self):
        self.queue.claim("desktop-one", {
            model: {"ready": True, "message": "ok"}
            for model in ("doubao", "yuanbao", "wenxin", "quark", "deepseek")
        })
        task = self.queue.create({
            "models": ["deepseek"], "questions": ["推荐测试产品"], "rounds": 3,
            "question_mode": "sequential", "customer_slug": "kimi-mirror",
            "brand_name": "测试品牌", "product_name": "测试产品", "task_kind": "diagnosis",
        })
        claimed = self.queue.claim("desktop-one")
        self.queue.accept_result(
            task["id"], claimed["lease_token"], "deepseek", "deepseek-1", {
                "collector_model": "deepseek", "round": 1, "question": "推荐测试产品",
                "reply": "推荐测试品牌的测试产品。",
                "analysis": {"mode": "local_chrome_extension", "recommended": True, "rank": 1},
                "sources": [{"title": "测试信源", "url": "https://example.com/deepseek"}],
                "body_capture_complete": True, "expected_source_count": 1,
                "source_capture_complete": True,
            },
            self.root / "results",
        )
        report = self.queue.diagnosis_report("kimi-mirror")["report"]
        deepseek_answers = [item for item in report["answers"] if item["model"] == "deepseek"]
        kimi_answers = [item for item in report["answers"] if item["model"] == "kimi"]
        self.assertEqual(len(deepseek_answers), 1)
        self.assertEqual(len(kimi_answers), 1)
        self.assertEqual(kimi_answers[0]["answer"], deepseek_answers[0]["answer"])
        self.assertEqual(kimi_answers[0]["sources"], deepseek_answers[0]["sources"])
        self.assertEqual(report["target_rounds"], 2)
        self.assertEqual(report["completed_rounds"], 1)
        deepseek_model = next(item for item in report["models"] if item["id"] == "deepseek")
        kimi_model = next(item for item in report["models"] if item["id"] == "kimi")
        self.assertEqual(
            kimi_model["recommendation_rate"],
            round(deepseek_model["recommendation_rate"] - 0.85, 1),
        )
        self.assertLess(kimi_model["recommendation_rate"], deepseek_model["recommendation_rate"])
        self.assertFalse(kimi_model["independent"])
        self.assertEqual(kimi_model["data_source"], "deepseek_mirror")
        self.assertIn("kimi", report["sources"][0]["models"])

    def test_new_report_uses_independent_kimi_evidence_and_weight(self):
        task = self.queue.create({
            "models": ["deepseek", "kimi"], "questions": ["推荐测试产品"], "rounds": 1,
            "question_mode": "sequential", "customer_slug": "kimi-independent",
            "brand_name": "测试品牌", "product_name": "测试产品", "task_kind": "diagnosis",
        })
        claimed = self.queue.claim("desktop-one", {
            "deepseek": {"ready": True}, "kimi": {"ready": True},
        })
        common = {
            "round": 1, "question": "推荐测试产品", "body_capture_complete": True,
            "expected_source_count": 1, "source_capture_complete": True,
        }
        self.queue.accept_result(task["id"], claimed["lease_token"], "deepseek", "direct-deepseek", {
            **common, "collector_model": "deepseek", "reply": "DeepSeek 推荐测试品牌。",
            "analysis": {"mode": "local", "recommended": True, "rank": 1},
            "sources": [{"title": "DeepSeek 信源", "url": "https://example.com/deepseek"}],
        }, self.root / "results")
        self.queue.accept_result(task["id"], claimed["lease_token"], "kimi", "direct-kimi", {
            **common, "collector_model": "kimi", "reply": "Kimi 独立回答没有推荐目标品牌。",
            "analysis": {"mode": "local", "recommended": False, "rank": None},
            "sources": [{"title": "Kimi 信源", "url": "https://example.com/kimi"}],
        }, self.root / "results")
        report = self.queue.diagnosis_report("kimi-independent")["report"]
        kimi = next(item for item in report["models"] if item["id"] == "kimi")
        answers = {item["model"]: item["answer"] for item in report["answers"]}
        self.assertTrue(kimi["independent"])
        self.assertEqual(kimi["data_source"], "direct")
        self.assertNotEqual(answers["kimi"], answers["deepseek"])
        self.assertEqual(report["probability_adjustment"]["kimi_overall_weight"], 1)
        self.assertEqual(report["completed_rounds"], 2)

    def test_short_deepseek_fragment_with_many_sources_is_not_marked_complete(self):
        task = self.queue.create({
            "models": ["deepseek"], "questions": ["推荐测试产品"], "rounds": 2,
            "question_mode": "sequential", "customer_slug": "deepseek-fragment",
            "brand_name": "测试品牌", "product_name": "测试产品", "task_kind": "diagnosis",
        })
        claimed = self.queue.claim("desktop-one", {"deepseek": {"ready": True}})
        self.queue.accept_result(
            task["id"], claimed["lease_token"], "deepseek", "deepseek-fragment-1", {
                "collector_model": "deepseek", "round": 1, "question": "推荐测试产品",
                "reply": "这里只保存到了回答最后的一小段提示。",
                "analysis": {"mode": "local_chrome_extension", "recommended": False, "rank": None},
                "sources": [
                    {"title": f"- {index}", "url": f"https://source{index}.example.com/article"}
                    for index in range(1, 4)
                ],
                "body_capture_complete": True, "expected_source_count": 3,
                "source_capture_complete": True,
            },
            self.root / "results",
        )
        report = self.queue.diagnosis_report("deepseek-fragment")["report"]
        deepseek = next(item for item in report["answers"] if item["model"] == "deepseek")
        self.assertFalse(deepseek["body_capture_complete"])
        self.assertEqual(deepseek["sources"][0]["title"], "source1.example.com")

    def test_plain_brand_mention_is_not_counted_as_recommendation_or_rank(self):
        task = self.queue.create({
            "models": ["quark"], "questions": ["推荐测试产品"], "rounds": 1,
            "question_mode": "sequential", "customer_slug": "mention-only",
            "brand_name": "测试品牌", "product_name": "测试产品", "task_kind": "diagnosis",
        })
        claimed = self.queue.claim("desktop-one", {"quark": {"ready": True}})
        self.queue.accept_result(
            task["id"], claimed["lease_token"], "quark", "mention-only-1", {
                "collector_model": "quark", "round": 1, "question": "推荐测试产品",
                "reply": "测试品牌在备选名单中出现，但本次不推荐测试产品。",
                "analysis": {
                    "mode": "local_chrome_extension", "recommended": False, "rank": 1,
                },
                "brands": ["测试品牌"], "products": [], "sources": [],
                "body_capture_complete": True, "source_capture_complete": True,
            },
            self.root / "results",
        )
        report = self.queue.diagnosis_report("mention-only")["report"]
        quark = next(item for item in report["models"] if item["id"] == "quark")
        answer = next(item for item in report["answers"] if item["model"] == "quark")
        self.assertEqual(quark["recommendation_rate"], 0)
        self.assertIsNone(quark["average_rank"])
        self.assertFalse(answer["recommended"])
        self.assertTrue(answer["mentioned"])
        self.assertEqual(answer["evidence_state"], "mentioned")
        self.assertIsNone(answer["rank"])

    def test_grounded_short_brand_alias_keeps_positive_recommendation_probability(self):
        task = self.queue.create({
            "models": ["quark"], "questions": ["推荐一款孕妇喝的酸奶"], "rounds": 1,
            "question_mode": "sequential", "customer_slug": "short-brand-alias",
            "brand_name": "卡士酸奶", "product_name": "卡士酸奶", "task_kind": "diagnosis",
            "probability_policy_version": 5,
        })
        claimed = self.queue.claim("desktop-one", {"quark": {"ready": True}})
        self.queue.accept_result(
            task["id"], claimed["lease_token"], "quark", "short-brand-alias-1", {
                "collector_model": "quark", "round": 1,
                "question": "推荐一款孕妇喝的酸奶",
                "reply": "回答推荐了简爱、卡士、光明等品牌；卡士断糖日记排在第二位。",
                "analysis": {
                    "mode": "local_chrome_extension", "recommended": False, "rank": None,
                },
                "brands": ["简爱", "卡士", "光明"],
                "products": [
                    {"brand": "简爱", "name": "简爱酸奶", "recommended": True, "rank": 1},
                    {"brand": "卡士", "name": "卡士断糖日记", "recommended": True, "rank": 2},
                ],
                "sources": [], "body_capture_complete": True,
                "source_capture_complete": True,
            }, self.root / "results",
        )
        report = self.queue.diagnosis_report("short-brand-alias")["report"]
        quark = next(item for item in report["models"] if item["id"] == "quark")
        answer = next(item for item in report["answers"] if item["model"] == "quark")
        self.assertTrue(answer["recommended"])
        self.assertEqual(answer["rank"], 2)
        self.assertGreater(quark["recommendation_rate"], 0)
        self.assertLess(quark["recommendation_rate"], 30)

    def test_diagnosis_can_queue_until_browser_sessions_are_ready(self):
        readiness = {
            "doubao": {"ready": False},
            "yuanbao": {"ready": True},
            "wenxin": {"ready": True},
            "quark": {"ready": True},
        }
        self.queue.claim("desktop-one", readiness)
        readiness = self.queue.diagnosis_report("new-customer")["readiness"]
        self.assertFalse(readiness["ready"])
        self.assertFalse(readiness["models"]["doubao"]["ready"])
        task = self.queue.create_diagnosis("new-customer", {
            "brand_name": "测试品牌", "question": "推荐一款饮料",
        })
        self.assertEqual(task["status"], "queued")
        self.assertIsNone(self.queue.claim("desktop-one", readiness))
        self.assertEqual(self.queue.get(task["id"])["status"], "queued")
        ready = {
            model: {"ready": True}
            for model in ("doubao", "yuanbao", "wenxin", "quark", "deepseek", "kimi")
        }
        self.assertEqual(self.queue.claim("desktop-one", ready)["id"], task["id"])

    def test_public_diagnosis_generates_unpredictable_report_key(self):
        with self.assertRaisesRegex(ValueError, "具体产品"):
            self.queue.create_public_diagnosis({
                "brand_name": "测试品牌", "question": "推荐一款产品",
            })
        first_key, first = self.queue.create_public_diagnosis({
            "brand_name": "测试品牌", "product_name": "测试产品", "question": "推荐一款产品",
        })
        second_key, second = self.queue.create_public_diagnosis({
            "brand_name": "另一品牌", "product_name": "另一产品", "question": "推荐另一款产品",
        })
        self.assertRegex(first_key, r"^[a-f0-9]{32}$")
        self.assertRegex(second_key, r"^[a-f0-9]{32}$")
        self.assertNotEqual(first_key, second_key)
        self.assertEqual(first["customer_slug"], first_key)
        self.assertEqual(second["customer_slug"], second_key)

    def test_sub_admin_accounts_enforce_owner_scope_daily_quota_and_pause(self):
        account = self.queue.create_admin_account({
            "username": "sales01",
            "display_name": "华东客户经理",
            "password": "SecurePass-2026",
            "daily_limit": 2,
            "expires_at": "2099-01-01T12:00",
        }, created_by="root")
        self.assertNotIn("password_hash", account)
        self.assertTrue(account["can_login"])
        self.assertIsNone(self.queue.authenticate_admin_account("sales01", "wrong-pass"))
        identity = self.queue.authenticate_admin_account("sales01", "SecurePass-2026")
        self.assertIsNotNone(identity)

        first_key, first = self.queue.create_public_diagnosis({
            "brand_name": "品牌甲", "product_name": "产品甲", "question": "推荐产品甲",
        }, account=identity)
        second_key, _second = self.queue.create_public_diagnosis({
            "brand_name": "品牌乙", "product_name": "产品乙", "question": "推荐产品乙",
        }, account=identity)
        self.assertEqual(first["created_by_account_id"], account["id"])
        self.assertEqual(first["created_by_username"], "sales01")
        quota = self.queue.diagnosis_quota(identity)
        self.assertEqual((quota["used"], quota["remaining"]), (2, 0))
        with self.assertRaisesRegex(PermissionError, "额度已用完"):
            self.queue.assert_diagnosis_allowed(identity)

        reset = self.queue.reset_admin_account_quota(account["id"])
        self.assertEqual((reset["used_today"], reset["remaining_today"]), (0, 2))
        self.assertTrue(reset["quota_reset_at"])
        self.assertEqual(
            (self.queue.diagnosis_quota(reset)["used"],
             self.queue.diagnosis_quota(reset)["remaining"]),
            (0, 2),
        )
        third_key, _third = self.queue.create_public_diagnosis({
            "brand_name": "品牌丙", "product_name": "产品丙", "question": "推荐产品丙",
        }, account=reset)
        quota_after_reset = self.queue.diagnosis_quota(reset)
        self.assertEqual((quota_after_reset["used"], quota_after_reset["remaining"]), (1, 1))
        self.assertIsNone(self.queue.reset_admin_account_quota("missing-account"))

        own_reports = self.queue.completed_diagnosis_reports(
            20, owner_account_id=account["id"]
        )
        self.assertEqual(
            {item["report_key"] for item in own_reports},
            {first_key, second_key, third_key},
        )
        with self.queue._connection() as connection:
            plan = " ".join(
                str(row["detail"]) for row in connection.execute(
                    "EXPLAIN QUERY PLAN SELECT * FROM remote_tasks "
                    "WHERE created_by_account_id=? AND task_kind='diagnosis' "
                    "ORDER BY created_at DESC LIMIT 20",
                    (account["id"],),
                )
            )
        self.assertIn("remote_tasks_owner_created", plan)
        self.assertTrue(self.queue.report_owned_by(first_key, account["id"]))
        self.assertFalse(self.queue.report_owned_by(first_key, "another-account"))

        paused = self.queue.update_admin_account(account["id"], {"active": False})
        self.assertFalse(paused["can_login"])
        self.assertIsNone(self.queue.admin_account_for_session(
            account["id"], account["username"], account["session_version"]
        ))
        blocked_login = self.queue.authenticate_admin_account(
            "sales01", "SecurePass-2026"
        )
        self.assertFalse(blocked_login["can_login"])

    def test_sub_admin_password_reset_and_expiry_invalidate_access(self):
        account = self.queue.create_admin_account({
            "username": "manager02", "password": "Original-2026",
            "daily_limit": 3, "expires_at": "2099-01-01T12:00",
        }, created_by="root")
        updated = self.queue.update_admin_account(account["id"], {
            "password": "Updated-2026", "expires_at": "2000-01-01T00:00",
        })
        self.assertTrue(updated["expired"])
        self.assertGreater(updated["session_version"], account["session_version"])
        self.assertIsNone(self.queue.authenticate_admin_account("manager02", "Original-2026"))
        expired = self.queue.authenticate_admin_account("manager02", "Updated-2026")
        self.assertFalse(expired["can_login"])

    def test_signed_sub_admin_session_is_revoked_immediately_when_paused(self):
        import doubao_dashboard_server as server

        account = self.queue.create_admin_account({
            "username": "manager03", "password": "Session-2026",
            "daily_limit": 3, "expires_at": "2099-01-01T12:00",
        }, created_by="root")
        handler = object.__new__(server.DashboardHandler)
        with patch.dict("os.environ", {
            "MONITOR_ADMIN_SESSION_SECRET": "test-session-secret",
            "MONITOR_ADMIN_USERNAME": "root-admin",
        }), patch.object(server, "REMOTE_TASK_QUEUE", self.queue):
            token = handler._admin_session_token(account)
            handler.headers = {"Cookie": f"geo_admin_session={token}"}
            self.assertEqual(handler.admin_identity()["id"], account["id"])
            report_key, _task = self.queue.create_public_diagnosis({
                "brand_name": "账号隔离品牌", "product_name": "测试产品",
                "question": "推荐一款产品",
            }, account=account)
            self.assertTrue(handler.admin_can_access_report(account, report_key))
            self.assertFalse(handler.admin_can_access_report({
                "id": "another-account", "role": "manager",
            }, report_key))
            self.assertTrue(handler.admin_can_access_report({
                "id": "root", "role": "super_admin",
            }, report_key))
            self.queue.update_admin_account(account["id"], {"active": False})
            self.assertIsNone(handler.admin_identity())

    def test_completed_report_public_sharing_can_expire_pause_and_resume(self):
        report_key, task = self.queue.create_public_diagnosis({
            "brand_name": "测试品牌", "product_name": "测试产品", "question": "推荐产品",
        })
        self.assertFalse(self.queue.report_is_public(report_key))
        with self.assertRaisesRegex(ValueError, "已完成"):
            self.queue.update_report_sharing(report_key, enabled=True)
        with self.queue._connection() as connection:
            connection.execute(
                "UPDATE remote_tasks SET status='completed' WHERE id=?", (task["id"],)
            )

        permanent = self.queue.update_report_sharing(report_key, enabled=True)
        self.assertTrue(permanent["public_active"])
        self.assertEqual(permanent["public_expires_at"], "")
        self.assertTrue(self.queue.report_is_public(report_key))

        paused = self.queue.update_report_sharing(report_key, enabled=False)
        self.assertFalse(paused["public_active"])
        self.assertFalse(paused["public_enabled"])

        timed = self.queue.update_report_sharing(
            report_key, enabled=True, expires_at="2099-01-01T12:00"
        )
        self.assertTrue(timed["public_active"])
        self.assertIn("+08:00", timed["public_expires_at"])
        with self.queue._connection() as connection:
            connection.execute(
                "UPDATE remote_tasks SET public_expires_at='2000-01-01T00:00:00+08:00' WHERE id=?",
                (task["id"],),
            )
        expired = self.queue.report_sharing(report_key)
        self.assertTrue(expired["public_expired"])
        self.assertFalse(expired["public_active"])
        self.assertFalse(self.queue.report_is_public(report_key))

    def test_report_list_contains_current_sharing_state(self):
        report_key, task = self.queue.create_public_diagnosis({
            "brand_name": "测试品牌", "product_name": "测试产品", "question": "推荐产品",
        })
        with self.queue._connection() as connection:
            connection.execute(
                "UPDATE remote_tasks SET status='completed' WHERE id=?", (task["id"],)
            )
        self.queue.update_report_sharing(report_key, enabled=True)
        item = self.queue.completed_diagnosis_reports(10)[0]
        self.assertTrue(item["public_active"])
        self.assertTrue(item["public_enabled"])
        self.assertFalse(item["public_expired"])

    def test_admin_report_delete_requires_exact_brand_confirmation(self):
        report_key, task = self.queue.create_public_diagnosis({
            "brand_name": "测试品牌", "product_name": "测试产品", "question": "推荐产品",
        })
        with self.queue._connection() as connection:
            connection.execute(
                "UPDATE remote_tasks SET status='completed' WHERE id=?", (task["id"],)
            )
        with self.assertRaisesRegex(ValueError, "确认不一致"):
            self.queue.delete_diagnosis_report(
                report_key, confirmation="其他品牌", results_root=self.root / "results"
            )
        self.assertIsNotNone(self.queue.get(task["id"]))
        deleted = self.queue.delete_diagnosis_report(
            report_key, confirmation="测试品牌", results_root=self.root / "results"
        )
        self.assertEqual(deleted["id"], task["id"])
        self.assertIsNone(self.queue.get(task["id"]))
        self.assertIsNone(self.queue.delete_diagnosis_report(
            report_key, confirmation="测试品牌", results_root=self.root / "results"
        ))

    def test_super_admin_can_edit_report_evidence_with_audit_and_recalculation(self):
        report_key, task = self.queue.create_public_diagnosis({
            "brand_name": "旧品牌", "product_name": "旧产品", "question": "旧问题",
        })
        record = {
            "collector_model": "doubao", "round": 1, "question": "旧问题",
            "web_body": "这里只提到了其他品牌。", "body_capture_complete": True,
            "source_capture_complete": True, "sources": [], "brands": ["其他品牌"],
            "products": [{"brand": "其他品牌", "name": "其他产品", "recommended": True, "rank": 1}],
            "analysis": {"mode": "local_chrome_extension", "recommended": False, "rank": None},
        }
        with self.queue._connection() as connection:
            connection.execute(
                "INSERT INTO remote_task_results(request_id,task_id,model_id,created_at,record_json) "
                "VALUES(?,?,?,?,?)",
                ("edit-round-1", task["id"], "doubao", "2026-09-10T10:00:00+08:00", json.dumps(record, ensure_ascii=False)),
            )
            connection.execute(
                "UPDATE remote_tasks SET status='completed',completed_steps=1,"
                "probability_policy_version=5,finished_at=? WHERE id=?",
                ("2026-09-10T10:01:00+08:00", task["id"]),
            )

        editor = self.queue.report_editor_data(report_key)
        self.assertEqual(editor["brand_name"], "旧品牌")
        self.assertEqual(editor["probability_policy_version"], 5)
        self.assertEqual(editor["current_probability_policy_version"], 6)
        self.assertEqual(len(editor["records"]), 1)
        edited_record = editor["records"][0]["record"]
        edited_record.update({
            "web_body": "第一名推荐新品牌的新产品。",
            "brands": ["新品牌"],
            "products": [{"brand": "新品牌", "name": "新产品", "recommended": True, "rank": 1}],
            "sources": [{"title": "权威来源", "url": "https://example.com/report"}],
            "expected_source_count": 1,
            "analysis": {"mode": "local_chrome_extension", "recommended": True, "rank": 1},
        })
        updated = self.queue.update_diagnosis_report(report_key, {
            "brand_name": "新品牌", "product_name": "新产品", "question": "新问题",
            "high_probability_prior": True, "records": editor["records"],
        }, editor_username="root-admin")
        self.assertEqual(updated["brand_name"], "新品牌")
        self.assertTrue(updated["high_probability_prior"])
        self.assertEqual(updated["probability_policy_version"], 6)
        self.assertEqual(updated["records"][0]["record"]["question"], "新问题")
        self.assertEqual(updated["audits"][0]["editor_username"], "root-admin")

        report = self.queue.diagnosis_report(report_key, include_readiness=False)["report"]
        doubao = next(item for item in report["models"] if item["id"] == "doubao")
        self.assertEqual(report["brand_name"], "新品牌")
        self.assertEqual(doubao["recommended_rounds"], 1)
        self.assertEqual(doubao["average_rank"], 1.0)
        self.assertEqual(report["sources"][0]["url"], "https://example.com/report")
        with self.queue._connection() as connection:
            audit = connection.execute(
                "SELECT before_json,after_json FROM report_edit_audit WHERE report_key=?",
                (report_key,),
            ).fetchone()
        self.assertEqual(json.loads(audit["before_json"])["brand_name"], "旧品牌")
        self.assertEqual(json.loads(audit["after_json"])["brand_name"], "新品牌")
        second = self.queue.update_diagnosis_report(report_key, {
            "brand_name": "最新品牌", "product_name": "最新产品", "question": "最新问题",
            "high_probability_prior": False, "records": updated["records"],
        }, editor_username="root-admin")
        self.assertEqual(second["brand_name"], "最新品牌")
        recalculated = self.queue.diagnosis_report(
            report_key, include_readiness=False
        )["report"]
        recalculated_doubao = next(
            item for item in recalculated["models"] if item["id"] == "doubao"
        )
        self.assertLessEqual(recalculated_doubao["recommendation_rate"], 15.0)
        self.assertEqual(self.queue.completed_diagnosis_reports(10)[0]["manual_edit_count"], 2)

        restored = self.queue.revert_diagnosis_report(
            report_key, editor_username="root-admin"
        )
        self.assertEqual(restored["brand_name"], "旧品牌")
        self.assertEqual(restored["product_name"], "旧产品")
        self.assertEqual(restored["question"], "旧问题")
        self.assertFalse(restored["high_probability_prior"])
        self.assertEqual(restored["probability_policy_version"], 5)
        self.assertEqual(restored["records"][0]["record"]["web_body"], "这里只提到了其他品牌。")
        self.assertEqual(restored["audits"], [])
        self.assertEqual(self.queue.completed_diagnosis_reports(10)[0]["manual_edit_count"], 0)
        with self.assertRaisesRegex(ValueError, "没有可撤回"):
            self.queue.revert_diagnosis_report(report_key, editor_username="root-admin")
        with self.queue._connection() as connection:
            reverted = connection.execute(
                "SELECT COUNT(*) FROM report_edit_audit WHERE report_key=? AND reverted_at<>''",
                (report_key,),
            ).fetchone()[0]
        self.assertEqual(reverted, 2)
        self.queue.delete_diagnosis_report(
            report_key, confirmation="旧品牌", results_root=self.root / "results"
        )
        with self.queue._connection() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM report_edit_audit WHERE task_id=?", (task["id"],)
            ).fetchone()[0], 0)

    def test_report_editor_cannot_change_result_identity_or_edit_running_task(self):
        report_key, task = self.queue.create_public_diagnosis({
            "brand_name": "测试品牌", "product_name": "测试产品", "question": "推荐产品",
        })
        with self.assertRaisesRegex(ValueError, "已完成"):
            self.queue.update_diagnosis_report(report_key, {
                "brand_name": "测试品牌", "product_name": "", "question": "推荐产品",
                "high_probability_prior": False, "records": [],
            }, editor_username="root-admin")
        with self.queue._connection() as connection:
            connection.execute(
                "UPDATE remote_tasks SET status='completed' WHERE id=?", (task["id"],)
            )
        with self.assertRaisesRegex(ValueError, "标识"):
            self.queue.update_diagnosis_report(report_key, {
                "brand_name": "测试品牌", "product_name": "", "question": "推荐产品",
                "high_probability_prior": False,
                "records": [{"request_id": "invented", "record": {"round": 1}}],
            }, editor_username="root-admin")

    def test_customer_error_hides_cookie_environment_variable(self):
        self.queue.claim("desktop-one", {
            model: {"ready": True}
            for model in ("doubao", "yuanbao", "wenxin", "quark", "deepseek", "kimi")
        })
        task = self.queue.create_diagnosis("safe-error", {
            "brand_name": "测试品牌", "question": "推荐一款饮料",
        })
        claimed = self.queue.claim("desktop-one", {
            model: {"ready": True}
            for model in ("doubao", "yuanbao", "wenxin", "quark", "deepseek", "kimi")
        })
        self.queue.finish(task["id"], claimed["lease_token"], {
            "status": "failed",
            "error": "RuntimeError: 隐身会话需要临时设置 MONITOR_DOUBAO_COOKIES_JSON",
        })
        public_task = self.queue.diagnosis_report("safe-error")["task"]
        self.assertNotIn("COOKIES_JSON", public_task["error"])
        self.assertIn("联系管理员", public_task["error"])

    def test_admin_can_change_future_diagnosis_rounds(self):
        self.assertEqual(self.queue.service_settings()["diagnosis_rounds"], 3)
        self.assertEqual(
            self.queue.update_service_settings({
                "diagnosis_rounds": 4,
                "high_probability_brands": "品牌甲\n品牌乙，品牌甲",
            })["diagnosis_rounds"], 4
        )
        self.assertEqual(
            self.queue.service_settings()["high_probability_brands"], ["品牌甲", "品牌乙"]
        )
        task = self.queue.create_diagnosis("configured-rounds", {
            "brand_name": "测试品牌", "product_name": "测试产品", "question": "推荐产品",
        })
        self.assertEqual(task["rounds"], 4)
        self.assertEqual(task["total_steps"], 20)

    def test_high_probability_policy_is_snapshotted_per_diagnosis(self):
        ordinary = self.queue.create_diagnosis("policy-before", {
            "brand_name": "品牌甲", "product_name": "产品", "question": "推荐产品",
        })
        self.queue.update_service_settings({"high_probability_brands": ["品牌甲"]})
        boosted = self.queue.create_diagnosis("policy-after", {
            "brand_name": "品牌甲", "product_name": "产品", "question": "推荐产品",
        })
        self.assertFalse(self.queue.get(ordinary["id"])["high_probability_prior"])
        self.assertTrue(self.queue.get(boosted["id"])["high_probability_prior"])
        self.assertEqual(self.queue.get(boosted["id"])["probability_policy_version"], 6)
        self.assertFalse(
            self.queue.diagnosis_report("policy-before")["report"]
            ["probability_adjustment"]["high_probability_prior_applied"]
        )
        self.assertTrue(
            self.queue.diagnosis_report("policy-after")["report"]
            ["probability_adjustment"]["high_probability_prior_applied"]
        )
        listed = {item["id"]: item for item in self.queue.completed_diagnosis_reports()}
        self.assertFalse(listed[ordinary["id"]]["high_probability_strategy_active"])
        self.assertTrue(listed[boosted["id"]]["high_probability_strategy_active"])
        self.queue.update_service_settings({"high_probability_brands": []})
        self.assertFalse(
            self.queue.diagnosis_report("policy-before")["report"]
            ["probability_adjustment"]["high_probability_prior_applied"]
        )
        self.assertTrue(
            self.queue.diagnosis_report("policy-after")["report"]
            ["probability_adjustment"]["high_probability_prior_applied"]
        )
        ordinary_comparison = self.queue.diagnosis_report("policy-before")["report"]["policy_comparison"]
        boosted_comparison = self.queue.diagnosis_report("policy-after")["report"]["policy_comparison"]
        self.assertEqual(ordinary_comparison["active_policy"], "ordinary")
        self.assertEqual(boosted_comparison["active_policy"], "high_probability")
        self.assertTrue(ordinary_comparison["same_sample"])

    def test_public_task_hides_technical_logs_and_shows_queue_position(self):
        first = self.queue.create_public_diagnosis({
            "brand_name": "品牌一", "product_name": "产品一", "question": "推荐产品",
        })[1]
        second_key, second = self.queue.create_public_diagnosis({
            "brand_name": "品牌二", "product_name": "产品二", "question": "推荐产品",
        })
        public = self.queue.diagnosis_report(second_key)["task"]
        self.assertEqual(public["queue_position"], 1)
        self.assertEqual(public["events"], [])
        self.assertIn("前方还有 1 个任务", public["message"])
        self.assertNotEqual(first["id"], second["id"])
        queue = self.queue.diagnosis_readiness()["queue"]
        self.assertEqual(queue["running"], 0)
        self.assertEqual(queue["waiting"], 2)
        self.assertEqual(queue["ahead_if_submitted"], 2)
        self.assertTrue(queue["requires_queue"])

    def test_only_one_worker_can_claim_from_the_enterprise_queue(self):
        first = self.queue.create({
            "models": ["quark"], "questions": ["问题一"], "rounds": 1,
            "task_kind": "diagnosis",
        })
        second = self.queue.create({
            "models": ["quark"], "questions": ["问题二"], "rounds": 1,
            "task_kind": "diagnosis",
        })
        ready = {"quark": {"ready": True}}
        claimed = self.queue.claim("worker-one", ready)
        self.assertEqual(claimed["id"], first["id"])
        self.assertIsNone(self.queue.claim("worker-two", ready))
        self.assertEqual(self.queue.queue_position(second["id"]), 1)
        self.queue.finish(first["id"], claimed["lease_token"], {"status": "completed"})
        self.assertEqual(self.queue.claim("worker-two", ready)["id"], second["id"])

    def test_every_model_archived_navigation_is_removed_at_report_time(self):
        for model in ("doubao", "yuanbao", "wenxin", "quark", "deepseek", "kimi"):
            with self.subTest(model=model):
                clean = _navigation_free_answer(
                    "用户\n推荐显示屏\n助手\n这是一段有效的模型建议正文。\n近期对话\n推荐染发剂\n推荐饮料",
                    model, "推荐显示屏",
                )
                self.assertEqual(clean, "这是一段有效的模型建议正文。")

    def test_probability_is_deterministic_and_uses_rank_and_evidence_quality(self):
        best = _brand_probability(
            3, 3, report_key="same", brand="品牌", product="产品", question="问题",
            model="doubao", rank_quality=1.0, evidence_quality=1.0,
        )[0]
        weaker = _brand_probability(
            3, 3, report_key="same", brand="品牌", product="产品", question="问题",
            model="doubao", rank_quality=0.35, evidence_quality=0.6,
        )[0]
        repeated = _brand_probability(
            3, 3, report_key="different", brand="品牌", product="产品", question="问题",
            model="doubao", rank_quality=1.0, evidence_quality=1.0,
        )[0]
        self.assertEqual(best, repeated)
        self.assertGreater(best, weaker)
        self.assertGreater(best, 0)
        self.assertLess(best, 30.0)
        prior = _brand_probability(
            3, 3, report_key="same", brand="品牌", product="产品", question="问题",
            model="doubao", rank_quality=1.0, evidence_quality=1.0,
            high_probability_prior=True,
        )[0]
        self.assertGreater(prior, best)

    def test_v6_halves_only_ordinary_brand_probability(self):
        shared = {
            "report_key": "same", "brand": "品牌", "product": "产品",
            "question": "问题", "model": "doubao", "rank_quality": 1.0,
            "evidence_quality": 1.0,
        }
        legacy_ordinary = _brand_probability(
            3, 3, **shared, probability_policy_version=5,
        )[0]
        current_ordinary = _brand_probability(
            3, 3, **shared, probability_policy_version=6,
        )[0]
        legacy_high = _brand_probability(
            3, 3, **shared, high_probability_prior=True,
            probability_policy_version=5,
        )[0]
        current_high = _brand_probability(
            3, 3, **shared, high_probability_prior=True,
            probability_policy_version=6,
        )[0]
        self.assertEqual(current_ordinary, round(legacy_ordinary * 0.5, 1))
        self.assertEqual(current_high, legacy_high)
        self.assertLess(current_ordinary, 15.0)

    def test_enhanced_high_probability_policy_is_stronger_and_versioned(self):
        legacy = _brand_probability(
            3, 3, report_key="same", brand="品牌", product="产品", question="问题",
            model="doubao", rank_quality=1.0, evidence_quality=1.0,
            high_probability_prior=True, probability_policy_version=4,
        )[0]
        enhanced = _brand_probability(
            3, 3, report_key="same", brand="品牌", product="产品", question="问题",
            model="doubao", rank_quality=1.0, evidence_quality=1.0,
            high_probability_prior=True, probability_policy_version=5,
        )[0]
        sparse = _brand_probability(
            1, 3, report_key="same", brand="品牌", product="产品", question="问题",
            model="doubao", rank_quality=1.0, evidence_quality=1.0,
            high_probability_prior=True, probability_policy_version=5,
        )[0]
        self.assertGreater(enhanced, legacy)
        self.assertGreater(enhanced, 78.0)
        self.assertGreater(sparse, 60.0)

    def test_legacy_report_keeps_its_probability_policy_version(self):
        self.queue.update_service_settings({"high_probability_brands": ["品牌甲"]})
        task = self.queue.create_diagnosis("legacy-policy", {
            "brand_name": "品牌甲", "product_name": "产品", "question": "推荐产品",
        })
        with self.queue._connection() as connection:
            connection.execute(
                "UPDATE remote_tasks SET probability_policy_version=4 WHERE id=?", (task["id"],)
            )
        adjustment = self.queue.diagnosis_report("legacy-policy")["report"]["probability_adjustment"]
        self.assertEqual(adjustment["method"], "declared_prior_sample_calibration_v4")
        self.assertEqual(adjustment["prior_mean"], 0.7)
        self.assertEqual(adjustment["prior_strength"], 3.0)

    def test_source_titles_are_removed_from_answer_evidence(self):
        sources = [{"title": "梵玢 FBCY 植萃染发剂排行榜", "url": "https://example.com/a"}]
        body = "这是正常的染发建议。\n\n相关视频\n梵玢 FBCY 植萃染发剂排行榜\nhttps://example.com/a"
        cleaned = _source_free_answer(body, sources)
        self.assertEqual(cleaned, "这是正常的染发建议。")

    def test_unindexed_related_videos_are_removed_from_answer_evidence(self):
        sources = [{"title": "另一条信源", "url": "https://example.com/other"}]
        body = (
            "这是正常的薄膜键盘建议。\n还包含安静和耐用方面的说明。\n"
            "相关视频\n苹果 Magic Keyboard 上手体验 #Apple #键盘"
        )
        cleaned = _source_free_answer(body, sources)
        self.assertEqual(cleaned, "这是正常的薄膜键盘建议。\n还包含安静和耐用方面的说明。")

    def test_source_only_brand_does_not_count_as_recommendation(self):
        task = self.queue.create({
            "models": ["doubao"], "questions": ["推荐一款染发剂"], "rounds": 1,
            "question_mode": "sequential", "customer_slug": "source-only-brand",
            "brand_name": "梵玢 FBCY", "product_name": "植萃染发剂", "task_kind": "diagnosis",
        })
        claimed = self.queue.claim("desktop-one")
        self.queue.accept_result(
            task["id"], claimed["lease_token"], "doubao", "source-only", {
                "collector_model": "doubao", "round": 1, "question": "推荐一款染发剂",
                "reply": "这是正常的染发建议。\n相关视频\n梵玢 FBCY 植萃染发剂排行榜",
                "analysis": {"mode": "local_chrome_extension", "recommended": True, "rank": 1},
                "brands": ["梵玢 FBCY"],
                "products": [{"brand_name": "梵玢 FBCY", "product_name": "植萃染发剂", "recommended": True, "rank": 1}],
                "sources": [{"title": "梵玢 FBCY 植萃染发剂排行榜", "url": "https://example.com/a"}],
                "body_capture_complete": True, "expected_source_count": 1, "source_capture_complete": True,
            },
            self.root / "results",
        )
        report = self.queue.diagnosis_report("source-only-brand")["report"]
        self.assertEqual(report["recommended_rounds"], 0)
        answer = next(item for item in report["answers"] if item["model"] == "doubao")
        self.assertFalse(answer["recommended"])
        self.assertNotIn("梵玢", answer["answer"])

    def test_interactive_login_request_lifecycle(self):
        login = self.queue.request_login("doubao")
        self.assertEqual(login["status"], "queued")
        self.assertEqual(self.queue.request_login("doubao")["id"], login["id"])
        claimed = self.queue.claim_login("desktop-one", {
            model: {"ready": False} for model in ("doubao", "yuanbao", "wenxin")
        })
        self.assertEqual(claimed["id"], login["id"])
        self.assertEqual(self.queue.list_workers()[0]["status"], "login")
        heartbeat = self.queue.login_heartbeat(
            login["id"], claimed["lease_token"], {"message": "等待扫码登录"}
        )
        self.assertTrue(heartbeat["ok"])
        finished = self.queue.finish_login(
            login["id"], claimed["lease_token"], {"status": "ready"}
        )
        self.assertEqual(finished["status"], "ready")
        self.assertNotIn("lease_token", finished)
        self.assertEqual(self.queue.list_workers()[0]["status"], "idle")
        self.assertEqual(self.queue.list_login_requests()[0]["model_id"], "doubao")

    def test_task_page_has_protected_interactive_login_controls(self):
        html = task_page_html()
        self.assertIn("模型登录状态检测", html)
        self.assertIn('["doubao","yuanbao","wenxin","deepseek","kimi"]', html)
        self.assertIn("startLogin", html)
        self.assertIn("/logins/${model}/start", html)
        self.assertIn("采集任务控制台", html)
        self.assertIn("rerunTask", html)
        self.assertIn("deleteTask", html)
        self.assertIn("pauseTask", html)
        self.assertIn("resumeTask", html)
        self.assertIn("deleteResult", html)
        self.assertIn("每模型每轮采集结果", html)
        self.assertIn("清理全部已结束/暂停任务", html)
        self.assertLess(html.index("任务队列与采集数据"), html.index("高级：手动创建任务"))
        self.assertIn("个执行中", html)


    def _paid_payload(self, user_id: str, *, paid: bool = True, rounds: dict | None = None):
        return {
            "paid_user_id": user_id, "is_paid": paid,
            "brand_name": f"品牌-{user_id}", "product_name": f"产品-{user_id}",
            "question": "推荐一款测试产品",
            "starts_on": "2020-01-01", "expires_on": "2099-12-31",
            "model_rounds": rounds or {
                "doubao": 1, "yuanbao": 2, "wenxin": 3,
                "quark": 4, "deepseek": 5, "kimi": 6,
            },
        }

    def test_paid_monitor_materializes_once_with_independent_model_rounds(self):
        monitor = self.queue.create_paid_monitor(self._paid_payload("paid-001"))
        self.assertEqual(len(monitor["runs"]), 1)
        task = monitor["current_task"]
        self.assertEqual(task["total_steps"], 21)
        self.assertEqual(task["model_rounds"]["kimi"], 6)
        for _ in range(5):
            self.queue.sync_paid_monitor_tasks()
        self.assertEqual(len(self.queue.list()), 1)

    def test_paid_monitor_creates_exactly_one_new_task_on_next_natural_day(self):
        monitor = self.queue.create_paid_monitor(self._paid_payload("daily-user"))
        first_task_id = monitor["current_task"]["id"]
        with self.queue._connection() as connection:
            connection.execute(
                "UPDATE paid_monitor_runs SET run_date='2020-01-02' WHERE monitor_id=?",
                (monitor["id"],),
            )
        self.queue.sync_paid_monitor_tasks()
        refreshed = self.queue.get_paid_monitor(monitor["id"])
        self.assertEqual(len(refreshed["runs"]), 2)
        self.assertNotEqual(refreshed["current_task"]["id"], first_task_id)
        for _ in range(10):
            self.queue.sync_paid_monitor_tasks()
        self.assertEqual(len(self.queue.list()), 2)

    def test_paid_monitor_stress_concurrent_join_and_single_active_claim(self):
        with ThreadPoolExecutor(max_workers=12) as pool:
            monitors = list(pool.map(
                lambda index: self.queue.create_paid_monitor(self._paid_payload(f"customer-{index:02d}")),
                range(24),
            ))
        self.assertEqual(len(monitors), 24)
        self.assertEqual(len(self.queue.list_paid_monitors()), 24)
        self.assertEqual(len(self.queue.list()), 24)
        readiness = {model: {"ready": True} for model in (
            "doubao", "yuanbao", "wenxin", "quark", "deepseek", "kimi"
        )}
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda index: self.queue.claim(f"worker-{index}", readiness), range(8)))
        active = [item for item in claims if item]
        self.assertEqual(len(active), 1)
        self.assertEqual(len([item for item in self.queue.list() if item["status"] == "running"]), 1)

    def test_interactive_diagnosis_preempts_and_auto_resumes_paid_monitor(self):
        monitor = self.queue.create_paid_monitor(self._paid_payload("preempt-user"))
        paid = self.queue.claim("desktop")
        self.assertEqual(paid["id"], monitor["current_task"]["id"])
        self.queue.accept_result(
            paid["id"], paid["lease_token"], "doubao", "paid-checkpoint-1",
            {"collector_model": "doubao", "round": 1, "question": "推荐一款测试产品"},
            self.root / "results",
        )

        diagnosis = self.queue.create({
            "models": ["quark"], "questions": ["即时诊断问题"], "rounds": 1,
            "task_kind": "diagnosis",
        })
        interrupted = self.queue.get(paid["id"])
        self.assertTrue(interrupted["pause_requested"])
        self.assertTrue(interrupted["preempt_requested"])
        self.assertGreater(diagnosis["queue_priority"], interrupted["queue_priority"])
        control = self.queue.heartbeat(
            paid["id"], paid["lease_token"],
            {"model": "doubao", "question": "推荐一款测试产品", "message": "当前轮已保存"},
        )
        self.assertTrue(control["pause_requested"])

        yielded = self.queue.finish(paid["id"], paid["lease_token"], {"status": "paused"})
        self.assertEqual(yielded["status"], "queued")
        self.assertEqual(yielded["completed_steps"], 1)
        self.assertFalse(yielded["preempt_requested"])
        claimed_diagnosis = self.queue.claim("desktop")
        self.assertEqual(claimed_diagnosis["id"], diagnosis["id"])
        self.queue.accept_result(
            diagnosis["id"], claimed_diagnosis["lease_token"], "quark", "diagnosis-result-1",
            {"collector_model": "quark", "round": 1, "question": "即时诊断问题"},
            self.root / "results",
        )
        self.queue.finish(diagnosis["id"], claimed_diagnosis["lease_token"], {"status": "completed"})

        resumed_paid = self.queue.claim("desktop")
        self.assertEqual(resumed_paid["id"], paid["id"])
        self.assertEqual(resumed_paid["completed_rounds"]["doubao"], [1])
        self.assertEqual(self.queue.get_paid_monitor(monitor["id"])["status"], "active")

    def test_paid_monitor_burst_stays_ordered_when_diagnoses_preempt_queue(self):
        """Combined burst: customer joins, worker claims and diagnosis arrival race safely."""
        paid_count = 32
        with ThreadPoolExecutor(max_workers=16) as pool:
            monitors = list(pool.map(
                lambda index: self.queue.create_paid_monitor(
                    self._paid_payload(f"burst-{index:02d}", rounds={
                        model: 2 for model in (
                            "doubao", "yuanbao", "wenxin", "quark", "deepseek", "kimi"
                        )
                    })
                ),
                range(paid_count),
            ))
        self.assertEqual(len(monitors), paid_count)
        self.assertEqual(len(self.queue.list()), paid_count)

        readiness = {model: {"ready": True} for model in (
            "doubao", "yuanbao", "wenxin", "quark", "deepseek", "kimi"
        )}
        with ThreadPoolExecutor(max_workers=16) as pool:
            claims = list(pool.map(
                lambda index: self.queue.claim(f"burst-worker-{index}", readiness), range(16)
            ))
        claimed = [item for item in claims if item]
        self.assertEqual(len(claimed), 1)
        paid_task = claimed[0]
        self.assertEqual(paid_task["task_kind"], "paid_monitor")
        self.assertEqual(
            len([item for item in self.queue.list(100) if item["status"] == "running"]), 1
        )
        self.queue.accept_result(
            paid_task["id"], paid_task["lease_token"], "doubao", "burst-checkpoint", {
                "collector_model": "doubao", "round": 1,
                "question": "推荐一款测试产品", "web_body": "首轮监控断点",
            }, self.root / "results",
        )

        with ThreadPoolExecutor(max_workers=6) as pool:
            diagnoses = list(pool.map(
                lambda index: self.queue.create({
                    "models": ["quark"], "questions": [f"即时诊断-{index}"],
                    "rounds": 1, "task_kind": "diagnosis",
                }),
                range(6),
            ))
        interrupted = self.queue.get(paid_task["id"])
        self.assertTrue(interrupted["pause_requested"])
        self.assertTrue(interrupted["preempt_requested"])
        self.assertTrue(all(item["queue_priority"] == 100 for item in diagnoses))

        # New paid customers may join while the running monitor is yielding.
        with ThreadPoolExecutor(max_workers=8) as pool:
            newcomers = list(pool.map(
                lambda index: self.queue.create_paid_monitor(
                    self._paid_payload(f"late-{index:02d}")
                ),
                range(8),
            ))
        self.assertEqual(len(newcomers), 8)
        with ThreadPoolExecutor(max_workers=12) as pool:
            blocked = list(pool.map(
                lambda index: self.queue.claim(f"waiting-worker-{index}", readiness), range(12)
            ))
        self.assertTrue(all(item is None for item in blocked))
        self.assertEqual(
            len([item for item in self.queue.list(100) if item["status"] == "running"]), 1
        )

        control = self.queue.heartbeat(
            paid_task["id"], paid_task["lease_token"], {"message": "当前监控轮次已落盘"}
        )
        self.assertTrue(control["pause_requested"])
        yielded = self.queue.finish(
            paid_task["id"], paid_task["lease_token"], {"status": "paused"}
        )
        self.assertEqual(yielded["status"], "queued")
        self.assertEqual(yielded["completed_steps"], 1)

        with self.queue._connection() as connection:
            expected_diagnosis_ids = [str(row["id"]) for row in connection.execute(
                "SELECT id FROM remote_tasks WHERE task_kind='diagnosis' "
                "ORDER BY queue_priority DESC, created_at, rowid"
            ).fetchall()]
        completed_diagnosis_ids = []
        for index in range(6):
            next_task = self.queue.claim("burst-worker-main", readiness)
            self.assertIsNotNone(next_task)
            self.assertEqual(next_task["task_kind"], "diagnosis")
            completed_diagnosis_ids.append(next_task["id"])
            self.assertEqual(
                len([item for item in self.queue.list(100) if item["status"] == "running"]), 1
            )
            self.queue.accept_result(
                next_task["id"], next_task["lease_token"], "quark", f"diagnosis-{index}", {
                    "collector_model": "quark", "round": 1,
                    "question": next_task["questions"][0], "web_body": "诊断完整结果",
                }, self.root / "results",
            )
            finished = self.queue.finish(
                next_task["id"], next_task["lease_token"], {"status": "completed"}
            )
            self.assertEqual(finished["status"], "completed")
        self.assertEqual(completed_diagnosis_ids, expected_diagnosis_ids)

        resumed = self.queue.claim("burst-worker-main", readiness)
        self.assertEqual(resumed["id"], paid_task["id"])
        self.assertEqual(resumed["completed_rounds"]["doubao"], [1])
        self.assertEqual(
            len([item for item in self.queue.list(100) if item["status"] == "running"]), 1
        )
        self.assertEqual(
            len([item for item in self.queue.list(100) if item["task_kind"] == "paid_monitor"]),
            paid_count + len(newcomers),
        )

    def test_paid_monitor_live_round_change_requeues_without_disturbing_other_users(self):
        first = self.queue.create_paid_monitor(self._paid_payload("changing-user"))
        second = self.queue.create_paid_monitor(self._paid_payload("steady-user"))
        claimed = self.queue.claim("desktop")
        self.assertEqual(claimed["id"], first["current_task"]["id"])
        revised = self._paid_payload("changing-user", rounds={model: 2 for model in (
            "doubao", "yuanbao", "wenxin", "quark", "deepseek", "kimi"
        )})
        updated = self.queue.update_paid_monitor(first["id"], revised)
        self.assertNotEqual(updated["current_task"]["id"], claimed["id"])
        self.assertEqual(updated["current_task"]["total_steps"], 12)
        self.assertTrue((self.queue.get(claimed["id"]) or {})["cancel_requested"])
        self.queue.finish(claimed["id"], claimed["lease_token"], {"status": "cancelled"})
        next_task = self.queue.claim("desktop")
        self.assertEqual(next_task["id"], second["current_task"]["id"])

    def test_paid_monitor_pause_resume_rerun_and_clear_are_queue_safe(self):
        monitor = self.queue.create_paid_monitor(self._paid_payload("control-user"))
        task = self.queue.claim("desktop")
        paused = self.queue.pause_paid_monitor(monitor["id"])
        self.assertEqual(paused["status"], "paused")
        self.assertTrue((self.queue.get(task["id"]) or {})["pause_requested"])
        self.queue.finish(task["id"], task["lease_token"], {"status": "paused"})
        resumed = self.queue.resume_paid_monitor(monitor["id"])
        self.assertEqual(resumed["current_task"]["status"], "queued")
        old_id = resumed["current_task"]["id"]
        rerun = self.queue.rerun_paid_monitor(monitor["id"])
        self.assertNotEqual(rerun["current_task"]["id"], old_id)
        cleared = self.queue.clear_paid_monitor_data(monitor["id"], self.root / "results")
        self.assertEqual(cleared["status"], "paused")
        self.assertEqual(cleared["runs"], [])
        self.assertFalse(any(item.get("paid_monitor_id") == monitor["id"] for item in self.queue.list()))

    def test_unpaid_or_out_of_term_monitor_never_enters_queue(self):
        unpaid = self.queue.create_paid_monitor(self._paid_payload("unpaid-user", paid=False))
        expired_payload = self._paid_payload("expired-user")
        expired_payload.update({"starts_on": "2020-01-01", "expires_on": "2020-01-02"})
        expired = self.queue.create_paid_monitor(expired_payload)
        self.assertIsNone(unpaid["current_task"])
        self.assertIsNone(expired["current_task"])
        self.assertEqual(self.queue.list(), [])
        with self.assertRaisesRegex(ValueError, "付费"):
            self.queue.resume_paid_monitor(unpaid["id"])

    def test_paid_monitor_duplicate_user_id_is_rejected_under_race(self):
        payload = self._paid_payload("unique-paid-user")
        outcomes = []
        def create_once(_index):
            try:
                self.queue.create_paid_monitor(payload)
                return "created"
            except ValueError:
                return "duplicate"
        with ThreadPoolExecutor(max_workers=10) as pool:
            outcomes = list(pool.map(create_once, range(20)))
        self.assertEqual(outcomes.count("created"), 1)
        self.assertEqual(outcomes.count("duplicate"), 19)
        self.assertEqual(len(self.queue.list_paid_monitors()), 1)

    def test_paid_customer_dashboard_uses_own_persisted_daily_results(self):
        rounds = {model: 1 for model in (
            "doubao", "yuanbao", "wenxin", "quark", "deepseek", "kimi"
        )}
        first = self.queue.create_paid_monitor(
            self._paid_payload("panel-first", rounds=rounds)
        )
        second = self.queue.create_paid_monitor(
            self._paid_payload("panel-second", rounds=rounds)
        )
        task = self.queue.claim("dashboard-worker")
        self.assertEqual(task["id"], first["current_task"]["id"])
        self.queue.accept_result(
            task["id"], task["lease_token"], "doubao", "panel-result", {
                "collector_model": "doubao", "round": 1,
                "question": "推荐一款测试产品",
                "web_body": "推荐品牌-panel-first，同时也可以比较竞品甲。",
                "body_capture_complete": True,
                "sources": [{"title": "测试信源", "url": "https://example.com/panel-first"}],
                "expected_source_count": 1, "source_capture_complete": True,
                "brands": ["品牌-panel-first", "竞品甲"],
                "products": [
                    {"brand": "品牌-panel-first", "name": "产品-panel-first", "recommended": True, "rank": 1},
                    {"brand": "竞品甲", "name": "竞品产品", "recommended": True, "rank": 2},
                ],
                "analysis": {
                    "mode": "local_chrome_extension", "recommended": True, "rank": 1,
                },
            }, self.root / "results",
        )
        self.queue.finish(task["id"], task["lease_token"], {"status": "completed"})

        panel = self.queue.paid_monitor_dashboard(first["customer_slug"])
        self.assertTrue(panel["access_allowed"])
        self.assertEqual(panel["monitor"]["paid_user_id"], "panel-first")
        self.assertEqual(len(panel["days"]), 1)
        self.assertGreater(panel["days"][0]["overall_rate"], 0)
        self.assertEqual(panel["sources"][0]["url"], "https://example.com/panel-first")
        self.assertTrue(any(item["name"] == "竞品甲" for item in panel["competitors"]))
        self.assertNotIn("panel-second", str(panel))

        empty_other = self.queue.paid_monitor_dashboard(second["customer_slug"])
        self.assertEqual(empty_other["monitor"]["paid_user_id"], "panel-second")
        self.assertEqual(empty_other["days"], [])

    def test_unpaid_customer_dashboard_is_not_public(self):
        unpaid = self.queue.create_paid_monitor(self._paid_payload("private-panel", paid=False))
        payload = self.queue.paid_monitor_dashboard(unpaid["customer_slug"])
        self.assertFalse(payload["access_allowed"])
        self.assertIsNone(payload["monitor"])
        self.assertIsNone(self.queue.paid_monitor_dashboard("paid-000000000000000000000000"))


class RemoteTaskOriginTests(unittest.TestCase):
    def test_task_management_accepts_task_or_local_worker_token(self):
        from doubao_dashboard_server import DashboardHandler
        handler = object.__new__(DashboardHandler)
        with patch.dict("os.environ", {
            "MONITOR_TASK_API_TOKEN": "task-secret",
            "MONITOR_WORKER_TOKEN": "worker-secret",
        }):
            handler.headers = {"X-Monitor-Task-Token": "task-secret"}
            self.assertTrue(handler.task_authorized())
            handler.headers = {"X-Monitor-Task-Token": "worker-secret"}
            self.assertTrue(handler.task_authorized())
            handler.headers = {"X-Monitor-Task-Token": "wrong"}
            self.assertFalse(handler.task_authorized())

    def test_production_same_origin_and_local_dev_are_allowed(self):
        from doubao_dashboard_server import DashboardHandler
        handler = object.__new__(DashboardHandler)
        handler.headers = {"Host": "panel.example.com"}
        self.assertTrue(handler.is_allowed_dashboard_origin("https://panel.example.com"))
        self.assertFalse(handler.is_allowed_dashboard_origin("https://evil.example.com"))
        handler.headers = {"Host": "127.0.0.1:8765"}
        self.assertTrue(handler.is_allowed_dashboard_origin("http://127.0.0.1:3000"))
        self.assertTrue(handler.is_allowed_browser_extension_origin(
            "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
        ))
        self.assertFalse(handler.is_allowed_browser_extension_origin(
            "chrome-extension://not-a-valid-extension-id"
        ))


if __name__ == "__main__":
    unittest.main()
