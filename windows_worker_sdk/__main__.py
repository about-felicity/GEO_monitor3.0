from __future__ import annotations

import argparse
import os
from pathlib import Path

from .protocol import ServerClient
from .runner import LocalSpool, WorkerRunner, load_factory


def main() -> int:
    parser = argparse.ArgumentParser(description="Replacement Windows GEO worker protocol runner")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).parent / "data")
    args = parser.parse_args()
    server = os.environ.get("GEO_SERVER_URL", "https://www.ifbcy.com/geo")
    token = os.environ.get("GEO_WORKER_TOKEN", "")
    worker_id = os.environ.get("GEO_WORKER_ID", "windows-office-01")
    collector_factory = load_factory(os.environ.get("GEO_COLLECTOR_FACTORY", ""))
    analyzer_factory = load_factory(os.environ.get("GEO_ANALYZER_FACTORY", ""))
    runner = WorkerRunner(
        ServerClient(server, token),
        worker_id,
        collector_factory,
        analyzer_factory(),
        LocalSpool(args.data_dir),
    )
    try:
        runner.poll(once=args.once, poll_seconds=args.poll_seconds)
    finally:
        runner.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
