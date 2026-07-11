# A2Z Search

A2Z Search is a from-scratch Python search engine for UCI-hosted webpages. It builds a disk-backed inverted index, serves ranked results through a concurrent HTTP API, and includes a responsive browser interface. The project avoids external search frameworks so the indexing, compression, retrieval, and ranking pipeline are implemented directly.

## Highlights

- Processes malformed HTML with BeautifulSoup, Porter stemming, parallel document analysis, and exact URL/content deduplication.
- Builds a disk-backed SPIMI-style inverted index with k-way external merging, sparse lexicon lookups, and delta-encoded zlib-compressed posting lists.
- Ranks results with BM25-style scoring, document-length normalization, HTML-field boosts, URL signals, and flexible all/any/auto query matching.
- Provides a concurrent HTTP search API, web UI, benchmark runner, load-test tool, and unit tests.

## Measured Results

Measured locally on the supplied UCI developer corpus:

| Metric | Result |
|---|---:|
| Indexed pages | 51,480 |
| Exact duplicates removed | 3,913 |
| Unique stemmed terms | 1,082,500 |
| Partial indexes merged | 11 |
| Total index footprint | 91 MB |
| Compressed postings | 42 MB |
| Four-worker full build time | 357.3 s |
| Median query latency | 33.8 ms |
| p95 / p99 query latency | 50.0 / 79.8 ms |

Concurrent HTTP load testing on the same machine reached about 26 queries per second. At four simultaneous clients, p95 latency was 235.9 ms and p99 latency was 294.4 ms.

## Project Structure

```text
src/search_engine/
  corpus.py      Corpus readers and duplicate filtering
  indexer.py     SPIMI partial indexing and external merge
  lexicon.py     Disk-backed lexicon and posting-list storage
  search.py      Query execution and BM25-style ranking
  server.py      HTTP API and static web serving
  benchmark.py   Query latency benchmark
  loadtest.py    Concurrent HTTP load testing
  text.py        HTML parsing, tokenization, and stemming
```

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

## Build An Index

The indexer reads the corpus ZIP directly.

```bash
build-index /path/to/developer.zip index_data/developer
```

Use `--workers 1` for a serial build or choose another bounded worker count:

```bash
build-index /path/to/developer.zip index_data/developer --workers 4
```

Existing output directories are protected by default. Rebuild intentionally with `--overwrite`.

## Search

Run the interactive console:

```bash
search-index index_data/developer
```

Run the HTTP API and web interface:

```bash
serve-search index_data/developer --workers 4
```

Then open `http://127.0.0.1:8000`.

Example CLI query:

```bash
search-index index_data/developer --query "machine learning" --limit 5
```

Query modes:

- `auto`: prefer full-term matches, then fall back to broader matches.
- `all`: require every query term.
- `any`: return documents matching at least one query term.

## Benchmarking

Measure warm in-process retrieval latency:

```bash
benchmark-search index_data/developer benchmarks/queries.txt \
  --runs 5 --output benchmarks/developer-results.json
```

Measure HTTP throughput and tail latency while the service is running:

```bash
benchmark-concurrency http://127.0.0.1:8000 benchmarks/queries.txt \
  --requests 100 --concurrency 1,2,4,8,16 \
  --output benchmarks/concurrency-results.json
```

## Tests

```bash
python -m unittest discover -s tests -v
```

## Notes

This project is intended to demonstrate the core mechanics behind a search system: bounded-memory indexing, on-disk retrieval, posting-list compression, ranking, serving, benchmarking, and reliability checks.
