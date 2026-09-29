# A2Z Search

A2Z Search is a from-scratch Python search engine for UCI-hosted webpages. It builds a disk-backed inverted index, serves ranked results through a concurrent HTTP API, and includes a responsive browser interface. The project avoids external search frameworks so the indexing, compression, retrieval, and ranking pipeline are implemented directly.

## Background

Originally built for UC Irvine's Information Retrieval course (Fall 2024); later refactored and extended with parallel indexing, a concurrent HTTP API, and benchmarking tooling.

The goal is to build a working search engine from first principles over a real web crawl: roughly 55,000 pages from UC Irvine's computer science and informatics websites (51,480 after removing exact duplicates). Instead of relying on a search library, the project implements the full pipeline and deals with the problems a production search system faces:

- **Messy input.** Crawled pages include malformed HTML and duplicate content under different URLs, so parsing and deduplication have to be robust.
- **Indexes larger than memory.** The index is built in bounded-memory batches that are flushed to disk as partial indexes and then merged, instead of holding everything in RAM.
- **Fast disk-based retrieval.** At query time, only the posting lists for the query terms are read from disk, using a lexicon of byte offsets and compressed postings.
- **Relevance, not just matching.** Words in titles, headings, and bold text count more than body text, and scores are normalized for document length so long pages don't dominate.

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
