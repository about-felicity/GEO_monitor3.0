"""Copy this module to a Windows-only package and implement the interfaces."""

from __future__ import annotations

from typing import Any

from .contracts import AnalysisResult, CapturedAnswer, ProgressCallback


class CollectorTemplate:
    def __init__(self, model_id: str):
        self.model_id = model_id

    def check_ready(self) -> dict[str, Any]:
        return {"ready": False, "message": "collector not implemented"}

    def collect_new_conversation(
        self, question: str, round_number: int, progress: ProgressCallback
    ) -> CapturedAnswer:
        raise NotImplementedError(
            "Implement a brand-new conversation, complete body capture, and source capture"
        )

    def close(self) -> None:
        return None


class AnalyzerTemplate:
    def analyze(
        self,
        model_id: str,
        question: str,
        brand_name: str,
        product_name: str,
        captured: CapturedAnswer,
    ) -> AnalysisResult:
        raise NotImplementedError("Implement local product recommendation analysis on Windows")


def create_collector(model_id: str) -> CollectorTemplate:
    return CollectorTemplate(model_id)


def create_analyzer() -> AnalyzerTemplate:
    return AnalyzerTemplate()

