from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from windows_worker_sdk.contracts import AnalysisResult, CapturedAnswer, build_record
from windows_worker_sdk.runner import LocalSpool, build_schedule, deterministic_request_id


class WindowsWorkerSDKTests(unittest.TestCase):
    def test_schedule_matches_server_modes(self):
        self.assertEqual(build_schedule(["a", "b"], 2, "interleaved"), ["a", "b", "a", "b"])
        self.assertEqual(build_schedule(["a", "b"], 2, "sequential"), ["a", "a", "b", "b"])

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


if __name__ == "__main__":
    unittest.main()

