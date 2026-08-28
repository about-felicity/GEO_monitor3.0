import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from monitor_core.remote_tasks import RemoteTaskQueue, secure_token_matches, task_page_html


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

    def test_same_worker_can_reclaim_running_task_after_extension_refresh(self):
        task = self.queue.create({
            "models": ["doubao"], "questions": ["问题"], "rounds": 2,
        })
        first = self.queue.claim("chrome-office")
        self.queue.accept_result(
            task["id"], first["lease_token"], "doubao", "resume-round-one", {
                "collector_model": "doubao", "round": 1, "question": "问题",
                "web_body": "第一轮正文",
            }, self.root / "results",
        )
        self.assertIsNone(self.queue.claim("different-worker"))
        resumed = self.queue.claim("chrome-office")
        self.assertEqual(resumed["id"], task["id"])
        self.assertNotEqual(resumed["lease_token"], first["lease_token"])
        self.assertEqual(resumed["completed_rounds"]["doubao"], [1])

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

    def test_customer_diagnosis_uses_three_models_eight_rounds_and_builds_report(self):
        self.queue.claim("desktop-one", {
            model: {"ready": True, "message": "ok"}
            for model in ("doubao", "yuanbao", "wenxin")
        })
        task = self.queue.create_diagnosis("dianjiezhi", {
            "brand_name": "外星人",
            "product_name": "外星人电解质水",
            "question": "推荐一款大量出汗后适合喝的电解质饮料",
        })
        self.assertEqual(task["models"], ["doubao", "yuanbao", "wenxin"])
        self.assertEqual(task["rounds"], 8)
        self.assertEqual(task["total_steps"], 24)
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
                "reply": "推荐外星人电解质水，适合运动后补充电解质。",
                "analysis": {
                    "mode": "local_chrome_extension", "version": 1,
                    "recommended": True, "rank": 2,
                },
                "products": [{"brand_name": "外星人", "product_name": "电解质水", "rank": 2}],
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
        self.assertEqual(report["report"]["overall_rate"], 100.0)
        self.assertEqual(report["report"]["models"][0]["average_rank"], 2.0)
        self.assertEqual(report["task"]["model_progress"]["doubao"]["completed"], 1)
        self.assertEqual(len(report["report"]["sources"]), 1)
        self.assertEqual(report["report"]["quality"]["body_complete_rounds"], 1)
        self.assertEqual(report["report"]["quality"]["source_complete_rounds"], 1)
        self.assertEqual(report["report"]["quality"]["analysis_complete_rounds"], 1)
        self.assertGreater(report["report"]["quality"]["total_body_chars"], 0)
        self.assertEqual(report["report"]["answers"][0]["expected_source_count"], 1)
        self.assertTrue(report["report"]["answers"][0]["body_capture_complete"])
        self.assertTrue(report["report"]["answers"][0]["source_capture_complete"])
        self.assertGreaterEqual(len(report["task"]["events"]), 3)
        self.assertFalse((self.root / "results" / "doubao_results.jsonl").exists())

    def test_diagnosis_can_queue_until_browser_sessions_are_ready(self):
        self.queue.claim("desktop-one", {
            "doubao": {"ready": False},
            "yuanbao": {"ready": True},
            "wenxin": {"ready": True},
        })
        readiness = self.queue.diagnosis_report("new-customer")["readiness"]
        self.assertFalse(readiness["ready"])
        self.assertFalse(readiness["models"]["doubao"]["ready"])
        task = self.queue.create_diagnosis("new-customer", {
            "brand_name": "测试品牌", "question": "推荐一款饮料",
        })
        self.assertEqual(task["status"], "queued")

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

    def test_customer_error_hides_cookie_environment_variable(self):
        self.queue.claim("desktop-one", {
            model: {"ready": True} for model in ("doubao", "yuanbao", "wenxin")
        })
        task = self.queue.create_diagnosis("safe-error", {
            "brand_name": "测试品牌", "question": "推荐一款饮料",
        })
        claimed = self.queue.claim("desktop-one", {
            model: {"ready": True} for model in ("doubao", "yuanbao", "wenxin")
        })
        self.queue.finish(task["id"], claimed["lease_token"], {
            "status": "failed",
            "error": "RuntimeError: 隐身会话需要临时设置 MONITOR_DOUBAO_COOKIES_JSON",
        })
        public_task = self.queue.diagnosis_report("safe-error")["task"]
        self.assertNotIn("COOKIES_JSON", public_task["error"])
        self.assertIn("隐身登录未就绪", public_task["error"])

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
        self.assertIn("三模型隐身登录检测", html)
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
