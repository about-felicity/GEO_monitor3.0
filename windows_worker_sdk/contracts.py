"""Collector and analyzer contracts independent of any browser framework."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Protocol
from urllib.parse import urlparse


ProgressCallback = Callable[[str], None]


@dataclass(slots=True)
class CapturedAnswer:
    body: str
    sources: list[dict[str, Any]] = field(default_factory=list)
    page_url: str = ""
    expected_source_count: int = 0
    body_capture_complete: bool = False
    source_capture_complete: bool = False
    body_capture_origin: str = ""
    source_capture_origins: dict[str, int] = field(default_factory=dict)
    capture_mode: str = "windows_browser"
    started_at: str = ""
    finished_at: str = ""

    def validate(self) -> None:
        self.body = str(self.body or "").strip()
        if not self.body:
            raise ValueError("collector returned an empty answer body")
        if not self.body_capture_complete:
            raise ValueError("collector did not prove that the answer body is complete")
        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in self.sources:
            if not isinstance(raw, dict):
                continue
            value = dict(raw)
            url = str(value.get("url") or value.get("href") or "").strip()
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                continue
            if url in seen:
                continue
            seen.add(url)
            value["url"] = url
            value.setdefault("title", url)
            unique.append(value)
        self.sources = unique
        self.expected_source_count = max(int(self.expected_source_count or 0), len(unique))
        if self.source_capture_complete and len(unique) < self.expected_source_count:
            raise ValueError("source_capture_complete conflicts with the captured source count")
        if not self.started_at:
            self.started_at = datetime.now().astimezone().isoformat(timespec="seconds")
        if not self.finished_at:
            self.finished_at = datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(slots=True)
class AnalysisResult:
    recommended: bool
    rank: int | None = None
    matched_terms: list[str] = field(default_factory=list)
    products: list[dict[str, Any]] = field(default_factory=list)
    brands: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    analyzed_at: str = ""

    def __post_init__(self) -> None:
        if self.rank is not None and int(self.rank) < 1:
            raise ValueError("rank must be a positive integer or None")
        if not self.analyzed_at:
            self.analyzed_at = datetime.now().astimezone().isoformat(timespec="seconds")


class Collector(Protocol):
    """One model adapter. Every call must start a brand-new conversation."""

    def check_ready(self) -> dict[str, Any]: ...

    def collect_new_conversation(
        self,
        question: str,
        round_number: int,
        progress: ProgressCallback,
    ) -> CapturedAnswer: ...

    def close(self) -> None: ...


class Analyzer(Protocol):
    def analyze(
        self,
        model_id: str,
        question: str,
        brand_name: str,
        product_name: str,
        captured: CapturedAnswer,
    ) -> AnalysisResult: ...


def build_record(
    *,
    task: dict[str, Any],
    model_id: str,
    question: str,
    round_number: int,
    captured: CapturedAnswer,
    analysis: AnalysisResult,
) -> dict[str, Any]:
    captured.validate()
    return {
        "collector_model": model_id,
        "model_id": model_id,
        "serial": f"{model_id}-windows",
        "task_id": 1,
        "round": round_number,
        "question": question,
        "prompt": question,
        "reply": captured.body,
        "web_body": captured.body,
        "sources": captured.sources,
        "status": "success",
        "started_at": captured.started_at,
        "finished_at": captured.finished_at,
        "capture_mode": captured.capture_mode,
        "capture_label": "Windows 本地浏览器网页直采",
        "body_capture_complete": captured.body_capture_complete,
        "body_capture_origin": captured.body_capture_origin,
        "expected_source_count": captured.expected_source_count,
        "source_capture_complete": captured.source_capture_complete,
        "source_capture_origins": captured.source_capture_origins,
        "page_url": captured.page_url,
        "remote_task_id": str(task.get("id") or ""),
        "customer_slug": str(task.get("customer_slug") or ""),
        "brand_name": str(task.get("brand_name") or ""),
        "product_name": str(task.get("product_name") or ""),
        "task_kind": str(task.get("task_kind") or ""),
        "products": analysis.products,
        "brands": analysis.brands,
        "recommended": analysis.recommended,
        "rank": analysis.rank,
        "analysis": {
            # Compatibility value required by the unchanged report server.
            "mode": "local_chrome_extension",
            "engine": "windows_worker",
            "recommended": analysis.recommended,
            "rank": analysis.rank,
            "matched_terms": analysis.matched_terms,
            "details": analysis.details,
            "analyzed_at": analysis.analyzed_at,
        },
    }

