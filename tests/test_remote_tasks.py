import tempfile
import unittest
from pathlib import Path

from monitor_core.remote_tasks import RemoteTaskQueue, secure_token_matches


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
        lease = claimed["lease_token"]
        heartbeat = self.queue.heartbeat(
            task["id"], lease,
            {"model": "yuanbao", "question": "推荐一款洗发水", "message": "采集中"},
        )
        self.assertFalse(heartbeat["cancel_requested"])

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

    def test_cancel_queued_task(self):
        task = self.queue.create({
            "models": ["doubao"], "questions": ["问题"], "rounds": 1,
        })
        cancelled = self.queue.cancel(task["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIsNone(self.queue.claim("worker"))

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
        self.assertEqual(task["rounds"], 3)
        self.assertTrue(secure_token_matches("same", "same"))
        self.assertFalse(secure_token_matches("same", "different"))
        self.assertFalse(secure_token_matches("", ""))


class RemoteTaskOriginTests(unittest.TestCase):
    def test_production_same_origin_and_local_dev_are_allowed(self):
        from doubao_dashboard_server import DashboardHandler
        handler = object.__new__(DashboardHandler)
        handler.headers = {"Host": "panel.example.com"}
        self.assertTrue(handler.is_allowed_dashboard_origin("https://panel.example.com"))
        self.assertFalse(handler.is_allowed_dashboard_origin("https://evil.example.com"))
        handler.headers = {"Host": "127.0.0.1:8765"}
        self.assertTrue(handler.is_allowed_dashboard_origin("http://127.0.0.1:3000"))


if __name__ == "__main__":
    unittest.main()
