from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from windows_worker_sdk.contracts import AnalysisResult, CapturedAnswer, build_record
from windows_worker_sdk.runner import (
    LocalSpool,
    WorkerRunner,
    build_schedule,
    deterministic_request_id,
    round_attempts,
    text_fingerprint,
)


class WindowsWorkerSDKTests(unittest.TestCase):
    def test_identical_answer_from_two_proven_fresh_documents_is_valid(self):
        class Client:
            def __init__(self):
                self.records = []

            def heartbeat(self, *args, **kwargs):
                return {"cancel_requested": False, "pause_requested": False}

            def submit_result(self, task_id, lease, model_id, request_id, record):
                self.records.append(record)

        class Collector:
            batch_collection_enabled = False

            def check_ready(self):
                return {"ready": True}

            def collect_new_conversation(self, question, round_number, progress):
                return CapturedAnswer(
                    body="百度对相同搜索词稳定返回的同一份完整回答",
                    captured_question=question,
                    capture_identity=f"fresh-document-{round_number}",
                    body_capture_complete=True,
                    source_capture_complete=True,
                )

        class Analyzer:
            def analyze(self, *args):
                return AnalysisResult(recommended=False)

        client = Client()
        task = {
            "id": "fresh-identical-answer-task", "lease_token": "lease",
            "task_kind": "diagnosis", "questions": ["推荐孕妇喝的酸奶"],
            "rounds": 2, "question_mode": "interleaved", "brand_name": "品牌",
            "product_name": "酸奶", "completed_rounds": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            WorkerRunner(
                client, "worker", lambda _: Collector(), Analyzer(), LocalSpool(Path(directory))
            )._run_model(task, "wenxin", threading.Event())
        self.assertEqual(len(client.records), 2)
        self.assertNotEqual(
            client.records[0]["capture_identity"], client.records[1]["capture_identity"]
        )

    def test_exact_previous_round_answer_is_rejected_and_current_round_retried(self):
        class Client:
            def __init__(self):
                self.records = []

            def heartbeat(self, *args, **kwargs):
                return {"cancel_requested": False, "pause_requested": False}

            def submit_result(self, task_id, lease, model_id, request_id, record):
                self.records.append(record)

        class Collector:
            batch_collection_enabled = False

            def __init__(self):
                self.calls = []

            def check_ready(self):
                return {"ready": True}

            def collect_new_conversation(self, question, round_number, progress):
                self.calls.append(round_number)
                body = "第一份完整回答" if len(self.calls) < 3 else "第二份不同的完整回答"
                return CapturedAnswer(
                    body=body,
                    captured_question=question,
                    body_capture_complete=True,
                    source_capture_complete=True,
                )

        class Analyzer:
            def analyze(self, *args):
                return AnalysisResult(recommended=False)

        client = Client()
        collector = Collector()
        task = {
            "id": "duplicate-answer-task", "lease_token": "lease",
            "task_kind": "diagnosis", "questions": ["推荐孕妇喝的酸奶"],
            "rounds": 2, "question_mode": "interleaved", "brand_name": "品牌",
            "product_name": "酸奶", "completed_rounds": {},
        }
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.dict("os.environ", {"GEO_ROUND_ATTEMPTS": "2"}), \
             mock.patch("windows_worker_sdk.runner.time.sleep", return_value=None):
            runner = WorkerRunner(
                client, "worker", lambda _: collector, Analyzer(), LocalSpool(Path(directory))
            )
            runner._run_model(task, "doubao", threading.Event())
        self.assertEqual(collector.calls, [1, 2, 2])
        self.assertEqual([record["web_body"] for record in client.records], [
            "第一份完整回答", "第二份不同的完整回答",
        ])
        self.assertNotEqual(
            text_fingerprint(client.records[0]["web_body"]),
            text_fingerprint(client.records[1]["web_body"]),
        )

    def test_schedule_matches_server_modes(self):
        self.assertEqual(build_schedule(["a", "b"], 2, "interleaved"), ["a", "b", "a", "b"])
        self.assertEqual(build_schedule(["a", "b"], 2, "sequential"), ["a", "a", "b", "b"])

    def test_paid_monitor_forces_strict_single_round_collection(self):
        class Client:
            def __init__(self):
                self.records = []

            def heartbeat(self, *args, **kwargs):
                return {"cancel_requested": False, "pause_requested": False}

            def submit_result(self, task_id, lease, model_id, request_id, record):
                self.records.append(record)

        class Collector:
            batch_collection_enabled = True

            def __init__(self):
                self.prepared = []
                self.single_rounds = []

            def prepare_diagnosis(self, rounds):
                self.prepared.append(rounds)

            def check_ready(self):
                return {"ready": True}

            def collect_batch(self, rounds, progress):
                raise AssertionError("付费监控不得进入批量提问")

            def collect_new_conversation(self, question, round_number, progress):
                self.single_rounds.append(round_number)
                return CapturedAnswer(
                    body=f"第 {round_number} 轮完整回答",
                    body_capture_complete=True,
                    source_capture_complete=True,
                )

        class Analyzer:
            def analyze(self, *args):
                return AnalysisResult(recommended=False)

        client = Client()
        collector = Collector()
        task = {
            "id": "paid-single-round-task", "lease_token": "lease",
            "task_kind": "paid_monitor", "questions": ["question"],
            "rounds": 3, "model_rounds": {"doubao": 3},
            "question_mode": "interleaved", "brand_name": "brand",
            "product_name": "product", "completed_rounds": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            runner = WorkerRunner(
                client, "worker", lambda _: collector, Analyzer(), LocalSpool(Path(directory))
            )
            runner._run_model(task, "doubao", threading.Event())
        self.assertEqual(collector.prepared, [1])
        self.assertEqual(collector.single_rounds, [1, 2, 3])
        self.assertEqual([record["round"] for record in client.records], [1, 2, 3])

    def test_capture_deduplicates_and_rejects_false_completeness(self):
        captured = CapturedAnswer(
            body="complete answer",
            sources=[
                {"title": "A", "url": "https://example.com/a"},
                {"title": "duplicate", "href": "https://example.com/a"},
                {"title": "bad", "url": "javascript:alert(1)"},
            ],
            body_capture_complete=True,
            source_capture_complete=True,
            expected_source_count=1,
        )
        captured.validate()
        self.assertEqual(len(captured.sources), 1)
        self.assertEqual(captured.sources[0]["url"], "https://example.com/a")

    def test_record_preserves_server_compatibility_marker(self):
        captured = CapturedAnswer(
            body="外星人电解质水",
            body_capture_complete=True,
            source_capture_complete=True,
        )
        analysis = AnalysisResult(recommended=True, rank=1, matched_terms=["外星人"])
        record = build_record(
            task={"id": "a" * 32, "brand_name": "外星人", "product_name": "电解质水"},
            model_id="doubao",
            question="推荐饮料",
            round_number=1,
            captured=captured,
            analysis=analysis,
        )
        self.assertEqual(record["analysis"]["mode"], "local_chrome_extension")
        self.assertEqual(record["analysis"]["engine"], "windows_worker")
        self.assertTrue(record["recommended"])

    def test_spool_can_replay_before_reasking(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = LocalSpool(Path(directory))
            request_id = deterministic_request_id("task", "doubao", 1, "q")
            spool.append("task", {"request_id": request_id, "record": {"web_body": "saved"}})
            self.assertEqual(spool.find_record("task", request_id), {"web_body": "saved"})

    def test_spool_does_not_replay_a_server_acknowledged_result(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = LocalSpool(Path(directory))
            request_id = deterministic_request_id("task", "doubao", 1, "q")
            spool.append("task", {"request_id": request_id, "record": {"web_body": "saved"}})
            spool.mark_submitted("task", request_id)
            self.assertIsNone(spool.find_record("task", request_id))

    def test_transient_round_failure_is_retried_before_task_failure(self):
        class Client:
            def __init__(self):
                self.records = []

            def heartbeat(self, *args, **kwargs):
                return {}

            def submit_result(self, *args):
                self.records.append(args)

        class FlakyCollector:
            def __init__(self):
                self.calls = 0

            def check_ready(self):
                return {"ready": True}

            def collect_new_conversation(self, question, round_number, progress):
                self.calls += 1
                if self.calls == 1:
                    raise TimeoutError("temporary page timeout")
                return CapturedAnswer(
                    body="complete answer after retry",
                    body_capture_complete=True,
                    source_capture_complete=True,
                )

            def close(self):
                return None

        class Analyzer:
            def analyze(self, *args):
                return AnalysisResult(recommended=False)

        client = Client()
        collector = FlakyCollector()
        task = {
            "id": "retry-task",
            "lease_token": "lease",
            "questions": ["question"],
            "rounds": 1,
            "question_mode": "interleaved",
            "brand_name": "brand",
            "product_name": "product",
            "completed_rounds": {},
        }
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.dict("os.environ", {"GEO_ROUND_ATTEMPTS": "2"}), \
             mock.patch("windows_worker_sdk.runner.time.sleep", return_value=None):
            runner = WorkerRunner(
                client, "worker", lambda _: collector, Analyzer(), LocalSpool(Path(directory))
            )
            runner._run_model(task, "doubao", threading.Event())
        self.assertEqual(collector.calls, 2)
        self.assertEqual(len(client.records), 1)

    def test_quark_has_one_outer_attempt_because_extension_owns_page_recovery(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(round_attempts("quark"), 1)
            self.assertEqual(round_attempts("doubao"), 3)
        with mock.patch.dict("os.environ", {
            "GEO_ROUND_ATTEMPTS": "4",
            "GEO_QUARK_ROUND_ATTEMPTS": "2",
        }, clear=True):
            self.assertEqual(round_attempts("quark"), 2)
            self.assertEqual(round_attempts("doubao"), 4)

    def test_progress_heartbeats_are_throttled_and_recent_success_masks_one_network_blip(self):
        class Client:
            def __init__(self):
                self.calls = 0

            def heartbeat(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 2:
                    raise ConnectionError("temporary socket exhaustion")
                return {"cancel_requested": False, "pause_requested": False}

        with tempfile.TemporaryDirectory() as directory:
            client = Client()
            runner = WorkerRunner(
                client, "worker", lambda _: None, mock.Mock(), LocalSpool(Path(directory))
            )
            runner._check_control("task", "lease", "doubao", "q", "first", force=True)
            runner._check_control("task", "lease", "doubao", "q", "too soon")
            self.assertEqual(client.calls, 1)
            runner._check_control("task", "lease", "doubao", "q", "forced blip", force=True)
            self.assertEqual(client.calls, 2)

    def test_batch_collector_submits_once_and_results_keep_round_order(self):
        class Client:
            def __init__(self):
                self.records = []

            def heartbeat(self, *args, **kwargs):
                return {"cancel_requested": False, "pause_requested": False}

            def submit_result(self, task_id, lease, model_id, request_id, record):
                self.records.append(record)

        class BatchCollector:
            def __init__(self):
                self.batch_calls = []
                self.single_calls = 0

            def check_ready(self):
                return {"ready": True}

            def collect_batch(self, rounds, progress):
                self.batch_calls.append(list(rounds))
                return {
                    number: CapturedAnswer(
                        body=f"batch answer {number}",
                        body_capture_complete=True,
                        source_capture_complete=True,
                    )
                    for number, _ in rounds
                }

            def collect_new_conversation(self, question, round_number, progress):
                self.single_calls += 1
                raise AssertionError("batch success must not fall back to single collection")

        class Analyzer:
            def analyze(self, *args):
                return AnalysisResult(recommended=False)

        client = Client()
        collector = BatchCollector()
        task = {
            "id": "batch-task", "lease_token": "lease", "task_kind": "diagnosis",
            "questions": ["question"], "rounds": 2, "question_mode": "interleaved",
            "brand_name": "brand", "product_name": "product", "completed_rounds": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            runner = WorkerRunner(
                client, "worker", lambda _: collector, Analyzer(), LocalSpool(Path(directory))
            )
            runner._run_model(task, "kimi", threading.Event())
        self.assertEqual(collector.batch_calls, [[(1, "question"), (2, "question")]])
        self.assertEqual(collector.single_calls, 0)
        self.assertEqual([row["round"] for row in client.records], [1, 2])

    def test_progressive_collector_persists_each_round_without_atomic_batch(self):
        class Client:
            def __init__(self):
                self.records = []

            def heartbeat(self, *args, **kwargs):
                return {"cancel_requested": False, "pause_requested": False}

            def submit_result(self, task_id, lease, model_id, request_id, record):
                self.records.append(record)

        class ProgressiveCollector:
            batch_collection_enabled = False

            def __init__(self):
                self.batch_calls = 0
                self.single_calls = []

            def check_ready(self):
                return {"ready": True}

            def collect_batch(self, rounds, progress):
                self.batch_calls += 1
                raise AssertionError("progressive collector must not use atomic batch")

            def collect_new_conversation(self, question, round_number, progress):
                self.single_calls.append(round_number)
                return CapturedAnswer(
                    body=f"progressive answer {round_number}",
                    body_capture_complete=True,
                    source_capture_complete=True,
                )

        class Analyzer:
            def analyze(self, *args):
                return AnalysisResult(recommended=False)

        client = Client()
        collector = ProgressiveCollector()
        task = {
            "id": "progressive-task", "lease_token": "lease", "task_kind": "diagnosis",
            "questions": ["question"], "rounds": 3, "question_mode": "interleaved",
            "brand_name": "brand", "product_name": "product", "completed_rounds": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            runner = WorkerRunner(
                client, "worker", lambda _: collector, Analyzer(), LocalSpool(Path(directory))
            )
            runner._run_model(task, "quark", threading.Event())
        self.assertEqual(collector.batch_calls, 0)
        self.assertEqual(collector.single_calls, [1, 2, 3])
        self.assertEqual([row["round"] for row in client.records], [1, 2, 3])

    def test_progressive_batch_publishes_each_round_before_batch_finishes(self):
        timeline = []

        class Client:
            def __init__(self):
                self.records = []

            def heartbeat(self, *args, **kwargs):
                return {"cancel_requested": False, "pause_requested": False}

            def submit_result(self, task_id, lease, model_id, request_id, record):
                self.records.append(record)
                timeline.append(f"submitted-{record['round']}")

        class ProgressiveBatchCollector:
            batch_collection_enabled = True

            def check_ready(self):
                return {"ready": True}

            def collect_batch(self, rounds, progress):
                raise AssertionError("progressive batch API must take precedence")

            def collect_batch_progressive(self, rounds, progress, on_result):
                output = {}
                for number, _question in rounds:
                    captured = CapturedAnswer(
                        body=f"parallel answer {number}",
                        body_capture_complete=True,
                        source_capture_complete=True,
                    )
                    output[number] = captured
                    on_result(number, captured)
                    timeline.append(f"collector-after-{number}")
                return output

            def collect_new_conversation(self, question, round_number, progress):
                raise AssertionError("successful progressive batch must not fall back")

        class Analyzer:
            def analyze(self, *args):
                return AnalysisResult(recommended=False)

        client = Client()
        task = {
            "id": "progressive-batch-task", "lease_token": "lease",
            "task_kind": "diagnosis", "questions": ["question"], "rounds": 3,
            "question_mode": "interleaved", "brand_name": "brand",
            "product_name": "product", "completed_rounds": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            runner = WorkerRunner(
                client, "worker", lambda _: ProgressiveBatchCollector(),
                Analyzer(), LocalSpool(Path(directory)),
            )
            runner._run_model(task, "quark", threading.Event())
        self.assertEqual([row["round"] for row in client.records], [1, 2, 3])
        self.assertEqual(timeline[:2], ["submitted-1", "collector-after-1"])
        self.assertEqual(len(client.records), 3)

    def test_partial_progressive_batch_resets_to_one_round_before_fallback(self):
        class Client:
            def __init__(self):
                self.records = []

            def heartbeat(self, *args, **kwargs):
                return {"cancel_requested": False, "pause_requested": False}

            def submit_result(self, task_id, lease, model_id, request_id, record):
                self.records.append(record)

        class PartialBatchCollector:
            batch_collection_enabled = True

            def __init__(self):
                self.batch_size = 1
                self.prepared = []
                self.single_calls = []

            def prepare_diagnosis(self, rounds):
                self.batch_size = rounds
                self.prepared.append(rounds)

            def check_ready(self):
                return {"ready": True}

            def collect_batch(self, rounds, progress):
                raise AssertionError("progressive batch API must take precedence")

            def collect_batch_progressive(self, rounds, progress, on_result):
                for number, _question in rounds[:2]:
                    on_result(number, CapturedAnswer(
                        body=f"saved answer {number}",
                        body_capture_complete=True,
                        source_capture_complete=True,
                    ))
                raise RuntimeError("third burst conversation failed")

            def collect_new_conversation(self, question, round_number, progress):
                self.single_calls.append((round_number, self.batch_size))
                return CapturedAnswer(
                    body="recovered final answer",
                    body_capture_complete=True,
                    source_capture_complete=True,
                )

        class Analyzer:
            def analyze(self, *args):
                return AnalysisResult(recommended=False)

        collector = PartialBatchCollector()
        task = {
            "id": "partial-progressive-batch-task", "lease_token": "lease",
            "task_kind": "diagnosis", "questions": ["question"], "rounds": 3,
            "question_mode": "interleaved", "brand_name": "brand",
            "product_name": "product", "completed_rounds": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            runner = WorkerRunner(
                Client(), "worker", lambda _: collector,
                Analyzer(), LocalSpool(Path(directory)),
            )
            runner._run_model(task, "doubao", threading.Event())
        self.assertEqual(collector.prepared, [3, 1])
        self.assertEqual(collector.single_calls, [(3, 1)])

    def test_one_provider_failure_requeues_task_and_does_not_stop_other_models(self):
        class Client:
            def __init__(self):
                self.finished = []
                self.records = []

            def heartbeat(self, *args, **kwargs):
                return {"cancel_requested": False, "pause_requested": False}

            def submit_result(self, *args):
                self.records.append(args)

            def finish(self, *args):
                self.finished.append(args)

        class Collector:
            def __init__(self, model):
                self.model = model

            def check_ready(self):
                return {"ready": True}

            def collect_new_conversation(self, question, round_number, progress):
                if self.model == "quark":
                    raise RuntimeError("temporary selector mismatch")
                return CapturedAnswer(
                    body=f"{self.model} complete answer",
                    body_capture_complete=True,
                    source_capture_complete=True,
                )

            def close(self):
                return None

        class Analyzer:
            def analyze(self, *args):
                return AnalysisResult(recommended=False)

        client = Client()
        task = {
            "id": "isolated-provider-task", "lease_token": "lease",
            "models": ["doubao", "quark"], "questions": ["question"],
            "rounds": 1, "question_mode": "interleaved",
            "brand_name": "brand", "product_name": "product",
            "completed_rounds": {},
        }
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.dict("os.environ", {"GEO_ROUND_ATTEMPTS": "1", "GEO_MODEL_CONCURRENCY": "2"}):
            runner = WorkerRunner(
                client, "worker", lambda model: Collector(model), Analyzer(),
                LocalSpool(Path(directory)),
            )
            runner.run_task(task)
        self.assertEqual(len(client.records), 1)
        self.assertEqual(client.records[0][2], "doubao")
        self.assertEqual(client.finished[0][2], "retrying")


if __name__ == "__main__":
    unittest.main()
