"""Repeatable load test for cached dashboard API endpoints."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import statistics
import time
import urllib.error
import urllib.request


def percentile(values: list[float], value: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * value))))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8765/api/analytics?view=sources",
    )
    parser.add_argument("--requests", type=int, default=1000)
    parser.add_argument("--concurrency", type=int, default=100)
    parser.add_argument("--conditional", action="store_true")
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    warm_request = urllib.request.Request(args.url, headers={"Accept-Encoding": "gzip"})
    with opener.open(warm_request, timeout=args.timeout) as response:
        response.read()
        etag = response.headers.get("ETag", "")

    headers = {"Accept-Encoding": "gzip"}
    if args.conditional and etag:
        headers["If-None-Match"] = etag

    def fetch(_index: int) -> tuple[int, float, int, str]:
        started = time.perf_counter()
        request = urllib.request.Request(args.url, headers=headers)
        try:
            with opener.open(request, timeout=args.timeout) as response:
                body = response.read()
                return (
                    response.status,
                    (time.perf_counter() - started) * 1000,
                    len(body),
                    response.headers.get("X-Monitor-Cache", ""),
                )
        except urllib.error.HTTPError as exc:
            body = exc.read()
            return (
                exc.code,
                (time.perf_counter() - started) * 1000,
                len(body),
                exc.headers.get("X-Monitor-Cache", ""),
            )
        except Exception as exc:
            return (0, (time.perf_counter() - started) * 1000, 0, type(exc).__name__)

    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as executor:
        rows = list(executor.map(fetch, range(max(1, args.requests))))
    elapsed = time.perf_counter() - started

    good_statuses = {200, 304}
    errors = sum(status not in good_statuses for status, *_ in rows)
    latencies = [latency for _, latency, _, _ in rows]
    statuses: dict[str, int] = {}
    caches: dict[str, int] = {}
    for status, _latency, _size, cache in rows:
        statuses[str(status)] = statuses.get(str(status), 0) + 1
        caches[cache or "NONE"] = caches.get(cache or "NONE", 0) + 1
    result = {
        "requests": len(rows),
        "concurrency": args.concurrency,
        "conditional": args.conditional,
        "elapsed_seconds": round(elapsed, 3),
        "requests_per_second": round(len(rows) / elapsed, 1),
        "latency_ms": {
            "average": round(statistics.fmean(latencies), 2),
            "p50": round(percentile(latencies, 0.50), 2),
            "p95": round(percentile(latencies, 0.95), 2),
            "p99": round(percentile(latencies, 0.99), 2),
            "maximum": round(max(latencies), 2),
        },
        "downloaded_bytes": sum(size for _, _, size, _ in rows),
        "statuses": statuses,
        "cache": caches,
        "errors": errors,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
