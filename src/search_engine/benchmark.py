"""Repeatable latency and result-quality smoke benchmark."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

from .search import SearchEngine


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark disk-backed query latency")
    parser.add_argument("index", help="index directory created by build-index")
    parser.add_argument("queries", help="UTF-8 file containing one query per line")
    parser.add_argument("--runs", type=int, default=5, help="timed runs per query (default: 5)")
    parser.add_argument("--mode", choices=("all", "any", "auto"), default="auto")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--output", help="optional JSON report path")
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")

    queries = [
        line.strip()
        for line in Path(args.queries).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not queries:
        parser.error("query file contains no queries")

    timings: list[float] = []
    details: list[dict] = []
    with SearchEngine(args.index) as engine:
        for query in queries:
            engine.search(query, args.limit, args.mode)  # warm OS and process caches
            samples: list[float] = []
            results = []
            for _ in range(args.runs):
                started = time.perf_counter_ns()
                results = engine.search(query, args.limit, args.mode)
                samples.append((time.perf_counter_ns() - started) / 1_000_000)
            timings.extend(samples)
            details.append(
                {
                    "query": query,
                    "median_ms": round(statistics.median(samples), 3),
                    "max_ms": round(max(samples), 3),
                    "result_count": len(results),
                    "top_url": results[0].url if results else None,
                }
            )

    report = {
        "query_count": len(queries),
        "runs_per_query": args.runs,
        "mode": args.mode,
        "median_ms": round(statistics.median(timings), 3),
        "p95_ms": round(percentile(timings, 0.95), 3),
        "p99_ms": round(percentile(timings, 0.99), 3),
        "max_ms": round(max(timings), 3),
        "queries": details,
    }
    rendered = json.dumps(report, indent=2) + "\n"
    print(rendered, end="")
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
