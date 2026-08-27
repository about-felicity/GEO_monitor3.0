from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parent.parent
PLUGINS_ROOT = ROOT / "model_plugins"


class ModelPlugin:
    id = ""
    name = ""
    short_name = ""
    tone = ""
    supports_control = True
    execution = "local"
    ingest_only = False

    @property
    def stats_endpoint(self) -> str:
        return f"/api/models/{self.id}/stats"

    def metadata(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "short_name": self.short_name,
                "tone": self.tone, "stats_endpoint": self.stats_endpoint,
                "supports_control": self.supports_control,
                "execution": self.execution,
                "ingest_only": self.ingest_only}

    def ready(self) -> bool:
        raise NotImplementedError

    def command(self, options: dict[str, Any]) -> tuple[list[str], Path]:
        raise NotImplementedError

    def prepare(self, options: dict[str, Any], progress: Callable[[str], None] | None = None) -> None:
        if progress:
            progress("正在准备运行环境")

    def load_questions(self) -> list[str]:
        raise NotImplementedError

    def save_questions(self, questions: list[str]) -> None:
        raise NotImplementedError

    def load_question_mode(self) -> str:
        from monitor_core.scheduling import normalize_question_mode
        path = ROOT / "runtime" / "unified_control" / f"{self.id}_question_mode.txt"
        try:
            return normalize_question_mode(path.read_text(encoding="utf-8"))
        except OSError:
            return "interleaved"

    def save_question_mode(self, mode: str) -> None:
        from monitor_core.scheduling import normalize_question_mode
        path = ROOT / "runtime" / "unified_control" / f"{self.id}_question_mode.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(normalize_question_mode(mode) + "\n", encoding="utf-8")

    def account_check(self) -> dict[str, Any]:
        return {"ok": False, "status": "unsupported", "message": "该模型尚未实现统一账号校验"}

    def stats(self) -> dict[str, Any]:
        return {"generated_at": "", "runs": [], "daily": [], "total_runs": 0,
                "successful_runs": 0, "total_sources": 0, "questions": [], "devices": []}

    def analytics_runs(self) -> list[dict[str, Any]]:
        from monitor_core.analytics import load_generic_runs
        return load_generic_runs(self.id, self.stats())

    def activity(self, limit: int = 40) -> dict[str, Any]:
        del limit
        return {"ok": True, "model": self.id,
                "queue": {"queued": 0, "processed": 0, "errors": 0}, "events": []}

def _load_plugin(path: Path) -> ModelPlugin:
    module_name = "monitor_model_" + re.sub(r"[^a-z0-9_]", "_", path.parent.name.lower())
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载模型插件：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plugin = module.Plugin()
    if not isinstance(plugin, ModelPlugin) or not re.fullmatch(r"[a-z0-9_-]+", plugin.id):
        raise RuntimeError(f"模型插件契约无效：{path}")
    return plugin


def discover_plugins() -> dict[str, ModelPlugin]:
    plugins: dict[str, ModelPlugin] = {}
    if not PLUGINS_ROOT.exists():
        return plugins
    for path in sorted(PLUGINS_ROOT.glob("*/plugin.py")):
        plugin = _load_plugin(path)
        if plugin.id in plugins:
            raise RuntimeError(f"重复模型 ID：{plugin.id}")
        plugins[plugin.id] = plugin
    return plugins
