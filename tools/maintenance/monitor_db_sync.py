"""Continuously mirror model result files into PostgreSQL."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import signal
import time

from monitor_core.database import ensure_schema, sync_incremental
from monitor_core.plugins import discover_plugins


STOP = False


def versions(plugins):
    result = {}
    for model_id, plugin in plugins.items():
        paths = getattr(plugin, "results_dependencies", None)
        if not paths:
            paths = (getattr(plugin, "results", None),)
        stamps = []
        for path in paths:
            try:
                stat = Path(path).stat()
                stamps.append((str(path), stat.st_mtime_ns, stat.st_size))
            except (OSError, TypeError):
                stamps.append((str(path), 0, 0))
        result[model_id] = tuple(stamps)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--log", default="runtime/unified_control/database_sync.log")
    args = parser.parse_args()
    Path(args.log).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(args.log, encoding="utf-8"), logging.StreamHandler()])
    log = logging.getLogger("monitor-db-sync")
    ensure_schema()
    plugins = discover_plugins()
    previous = None

    def stop(*_):
        global STOP
        STOP = True
    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"): signal.signal(signal.SIGTERM, stop)

    while not STOP:
        current = versions(plugins)
        if current != previous:
            try:
                changed_models = list(plugins) if previous is None else [
                    model_id for model_id in plugins
                    if current.get(model_id) != previous.get(model_id)
                ]
                result = sync_incremental({
                    model_id: plugins[model_id].analytics_runs()
                    for model_id in changed_models
                })
                log.info("database sync (%s): %s", ",".join(changed_models),
                         json.dumps(result, ensure_ascii=False))
                previous = current
            except Exception:
                log.exception("database sync failed; files remain authoritative and will retry")
        time.sleep(max(0.5, args.interval))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
