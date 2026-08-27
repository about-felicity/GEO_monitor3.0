from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

from monitor_core.jsonl_dashboard import build_jsonl_dashboard
from monitor_core.plugins import ModelPlugin, ROOT
from monitor_core.scheduling import normalize_question_mode


class WebModelPlugin(ModelPlugin):
    """Common plugin contract for local browser-only model collectors."""

    supports_control = True
    execution = "local"
    ingest_only = False
    questions: Path

    @property
    def results(self) -> Path:
        return ROOT / "runtime" / "web_results" / f"{self.id}_results.jsonl"

    @property
    def dashboard(self) -> Path:
        return ROOT / "runtime" / "web_results" / f"{self.id}_dashboard.json"

    @property
    def state(self) -> Path:
        return ROOT / "runtime" / "web_state" / f"{self.id}.json"

    def ready(self) -> bool:
        return self.questions.exists() and (ROOT / "web_collectors" / "loop.py").exists()

    def command(self, options: dict[str, Any]) -> tuple[list[str], Path]:
        rounds = max(1, min(int(options.get("rounds") or 10), 10000))
        mode = normalize_question_mode(options.get("question_mode"))
        command = [
            sys.executable, "-m", "web_collectors.loop",
            "--model", self.id,
            "--questions-file", str(self.questions),
            "--rounds-per-question", str(rounds),
            "--question-mode", mode,
            "--results", str(self.results),
            "--state", str(self.state),
            "--resume",
        ]
        return command, ROOT

    def prepare(self, options: dict[str, Any], progress: Callable[[str], None] | None = None) -> None:
        if progress:
            progress(f"正在检查{self.name}网页会话")
        account = self.account_check()
        if not account.get("ok"):
            raise ValueError(account.get("message") or f"{self.name}网页会话不可用")
        if progress:
            progress(f"{self.name}网页已就绪，正在启动无头采集")

    def load_questions(self) -> list[str]:
        try:
            return [line.strip() for line in self.questions.read_text(encoding="utf-8-sig").splitlines() if line.strip() and not line.lstrip().startswith("#")]
        except OSError:
            return []

    def save_questions(self, questions: list[str]) -> None:
        values = [str(item).strip() for item in questions if str(item).strip()]
        self.questions.parent.mkdir(parents=True, exist_ok=True)
        self.questions.write_text("\n".join(dict.fromkeys(values)) + "\n", encoding="utf-8")

    def account_check(self) -> dict[str, Any]:
        from web_collectors.collector import create_collector
        collector = create_collector(self.id, headless=True)
        try:
            return collector.check_ready()
        finally:
            collector.close()

    def stats(self) -> dict[str, Any]:
        if not self.results.exists():
            return super().stats()
        return build_jsonl_dashboard(self.id, self.results, self.dashboard)

    def analytics_runs(self) -> list[dict[str, Any]]:
        from monitor_core.analytics import load_generic_runs
        runs = load_generic_runs(self.id, self.stats())
        if self.id == "doubao":
            from monitor_core.analytics import load_doubao_runs
            runs = load_doubao_runs(
                ROOT / "doubao_refs_result.csv",
                ROOT / "doubao_answers_result.csv",
                ROOT / "doubao_products_result.csv",
            ) + runs
        return runs

    def metadata(self) -> dict[str, Any]:
        value = super().metadata()
        from web_collectors.config import site_config
        value.update({"capture_mode": "headless_web", "home_url": site_config(self.id).home_url})
        return value
