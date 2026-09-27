import io
import unittest
import zipfile

from monitor_core.paid_monitor_export import build_paid_monitor_xlsx


class PaidMonitorExportTests(unittest.TestCase):
    def test_daily_export_contains_answer_and_source_audit_sheets(self):
        content = build_paid_monitor_xlsx(
            {"brand_name": "头皮去屑专题"},
            "2026-09-27",
            [{
                "date": "2026-09-27", "model": "doubao", "round": 1,
                "question": "为什么用了去屑洗发水还有头屑",
                "answer": "应先区分干性头屑、脂溢性皮炎与真菌相关头屑。",
                "recommended": False, "mentioned": True, "evidence_state": "mentioned",
                "rank": None, "body_length": 25, "body_capture_complete": True,
                "expected_source_count": 1, "source_capture_complete": True,
                "sources": [{"title": "医学参考", "url": "https://example.com/scalp"}],
                "analysis": {"summary": "需结合成因判断", "sentiment": "neutral", "brands": []},
            }],
        )
        self.assertTrue(content.startswith(b"PK"))
        with zipfile.ZipFile(io.BytesIO(content)) as workbook:
            names = set(workbook.namelist())
            self.assertIn("xl/worksheets/sheet1.xml", names)
            self.assertIn("xl/worksheets/sheet2.xml", names)
            answers = workbook.read("xl/worksheets/sheet1.xml").decode("utf-8")
            sources = workbook.read("xl/worksheets/sheet2.xml").decode("utf-8")
        self.assertIn("为什么用了去屑洗发水还有头屑", answers)
        self.assertIn("应先区分干性头屑", answers)
        self.assertIn("https://example.com/scalp", sources)


if __name__ == "__main__":
    unittest.main()
