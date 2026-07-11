"""HTTP concurrency load test for the search service."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import cycle, islice
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def request(url: str, query: str) -> tuple[float, bool]:
    endpoint = f"{url.rstrip('/')}/api/search?{urlencode({'q': query, 'limit': 5})}"
    started = time.perf_counter_ns()
    try:
        with urlopen(endpoint, timeout=10) as response:
            success = response.status == 200 and bool(json.loads(response.read()).get("results"))
    except Exception:
        success = False
    return (time.perf_counter_ns() - started) / 1_000_000, success


def main() -> None:
    parser = argparse.ArgumentParser(description="Load-test the concurrent HTTP search service")
    parser.add_argument("url", help="service root, such as http://127.0.0.1:8000")
    parser.add_argument("queries", help="one query per line")
    parser.add_argument("--requests", type=int, default=100, help="requests at each concurrency level")
    parser.add_argument("--concurrency", default="1,2,4,8,16", help="comma-separated client counts")
    parser.add_argument("--output", help="optional JSON report path")
    args = parser.parse_args()
    if args.requests < 1:
        parser.error("--requests must be positive")
    try:
        levels = [int(value) for value in args.concurrency.split(",")]
    except ValueError:
        parser.error("--concurrency must contain integers")
    if not levels or any(level < 1 for level in levels):
        parser.error("concurrency levels must be positive")

    queries = [
        line.strip()
        for line in Path(args.queries).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not queries:
        parser.error("query file contains no queries")

    workloads = list(islice(cycle(queries), args.requests))
    results = []
    for concurrency in levels:
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            samples = list(executor.map(lambda query: request(args.url, query), workloads))
        wall_seconds = time.perf_counter() - started
        latencies = [sample[0] for sample in samples]
        successes = sum(sample[1] for sample in samples)
        results.append(
            {
                "concurrency": concurrency,
                "requests": args.requests,
                "successful": successes,
                "errors": args.requests - successes,
                "throughput_qps": round(args.requests / wall_seconds, 2),
                "median_ms": round(statistics.median(latencies), 2),
                "p95_ms": round(percentile(latencies, 0.95), 2),
                "p99_ms": round(percentile(latencies, 0.99), 2),
                "max_ms": round(max(latencies), 2),
            }
        )

    report = {"url": args.url, "results": results}
    rendered = json.dumps(report, indent=2) + "\n"
    print(rendered, end="")
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
