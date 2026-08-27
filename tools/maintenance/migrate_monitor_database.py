"""Build or update the PostgreSQL mirror from all model result files."""

from __future__ import annotations

import argparse
import json
import time

from monitor_core.database import ensure_schema, replace_all, stats, sync_incremental
from monitor_core.plugins import discover_plugins


def source_runs():
    return {model_id: plugin.analytics_runs() for model_id, plugin in discover_plugins().items()}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    ensure_schema()
    started = time.perf_counter()
    result = replace_all(source_runs()) if args.replace else sync_incremental(source_runs())
    print(json.dumps({"ok": True, "sync": result, "database": stats(),
                      "elapsed_seconds": round(time.perf_counter() - started, 3)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
