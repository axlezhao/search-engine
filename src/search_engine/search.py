"""Disk-seeking retrieval with BM25-style tf-idf ranking."""

from __future__ import annotations

import argparse
import json
import math
import os
import time
import zlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote
from urllib.parse import urlsplit

from .lexicon import DiskLexicon
from .text import tokenize


@dataclass(frozen=True)
class SearchResult:
    rank: int
    score: float
    url: str
    matched_terms: int
    query_terms: int
    title: str
    snippet: str
    display_url: str


class SearchEngine:
    """Load metadata in memory and seek requested posting lists from disk."""

    IMPORTANT_BOOST = 2.0
    K1 = 1.2
    B = 0.75
    URL_BOOST = 0.75
    RERANK_CANDIDATES = 256

    def __init__(self, index_directory: str | Path):
        self.directory = Path(index_directory)
        self.metadata = json.loads((self.directory / "metadata.json").read_text(encoding="utf-8"))
        if self.metadata.get("format_version") != 2:
            raise ValueError("unsupported index format; run upgrade-index for a version 1 index")
        self.lexicon = DiskLexicon(
            self.directory / "lexicon.idx",
            self.directory / "lexicon.sparse.json",
        )
        self.urls: list[str] = []
        self.document_lengths: list[int] = []
        self.titles: list[str] = []
        self.snippets: list[str] = []
        with (self.directory / "documents.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                record = json.loads(line)
                _, url = record[:2]
                document_length = record[2] if len(record) > 2 else 1
                self.urls.append(url)
                self.document_lengths.append(max(1, document_length))
                self.titles.append(record[3] if len(record) > 3 and record[3] else self._fallback_title(url))
                self.snippets.append(record[4] if len(record) > 4 else "")
        self.average_document_length = (
            sum(self.document_lengths) / len(self.document_lengths) if self.document_lengths else 1.0
        )
        self._postings_fd = os.open(self.directory / "postings.idx", os.O_RDONLY)

    @staticmethod
    def _fallback_title(url: str) -> str:
        parsed = urlsplit(url)
        tail = unquote(parsed.path.rstrip("/").rsplit("/", 1)[-1])
        if not tail:
            return parsed.netloc
        title = tail.replace("-", " ").replace("_", " ")
        return " ".join(word.capitalize() for word in title.split())

    def close(self) -> None:
        os.close(self._postings_fd)
        self.lexicon.close()

    def __enter__(self) -> "SearchEngine":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _read_postings(self, term: str) -> list[list[int]]:
        entry = self.lexicon.get(term)
        if entry is None:
            return []
        offset, length, _ = entry
        # Positional reads keep the shared SearchEngine safe for concurrent
        # request threads; no mutable seek cursor exists.
        payload = os.pread(self._postings_fd, length, offset)
        encoded = json.loads(zlib.decompress(payload))
        postings: list[list[int]] = []
        doc_id = 0
        for gap, tf, important_tf in encoded:
            doc_id += gap
            postings.append([doc_id, tf, important_tf])
        return postings

    def search(self, query: str, limit: int = 5, mode: str = "auto") -> list[SearchResult]:
        """Search in strict AND, coverage-first OR, or AND-first fallback mode."""
        if mode not in {"all", "any", "auto"}:
            raise ValueError("mode must be 'all', 'any', or 'auto'")
        if limit < 1:
            return []

        query_counts = Counter(tokenize(query))
        if not query_counts:
            return []
        entries = {term: self.lexicon.get(term) for term in query_counts}
        known_terms = [term for term, entry in entries.items() if entry is not None]
        if not known_terms or (mode == "all" and len(known_terms) != len(query_counts)):
            return []

        # Rare terms first makes strict conjunctions cheap and predictable.
        def document_frequency(term: str) -> int:
            entry = entries[term]
            assert entry is not None
            return entry[2]

        terms = sorted(known_terms, key=document_frequency)
        document_count = int(self.metadata["indexed_documents"])
        scores: dict[int, float] = {}
        match_counts: Counter[int] = Counter()
        idfs: dict[str, float] = {}

        for term in terms:
            df = document_frequency(term)
            idf = math.log(1.0 + (document_count - df + 0.5) / (df + 0.5))
            idfs[term] = idf
            query_weight = 1.0 + math.log(query_counts[term])
            for doc_id, tf, important_tf in self._read_postings(term):
                weighted_tf = tf + self.IMPORTANT_BOOST * important_tf
                length_ratio = self.document_lengths[doc_id] / self.average_document_length
                saturation = (weighted_tf * (self.K1 + 1.0)) / (
                    weighted_tf + self.K1 * (1.0 - self.B + self.B * length_ratio)
                )
                scores[doc_id] = scores.get(doc_id, 0.0) + query_weight * idf * saturation
                match_counts[doc_id] += 1

        required = len(query_counts)
        if mode == "all":
            eligible = [doc_id for doc_id in scores if match_counts[doc_id] == required]
            eligible.sort(key=lambda doc_id: (-scores[doc_id], doc_id))
        elif mode == "any":
            eligible = list(scores)
            eligible.sort(key=lambda doc_id: (-match_counts[doc_id], -scores[doc_id], doc_id))
        else:
            # Full conjunctions lead. Partial matches fill the page only when needed.
            eligible = list(scores)
            eligible.sort(
                key=lambda doc_id: (
                    -(match_counts[doc_id] == required),
                    -match_counts[doc_id],
                    -scores[doc_id],
                    doc_id,
                )
            )

        # URL terms are strong navigational/entity signals. Rerank only a bounded
        # head so common terms cannot turn this refinement into a full-corpus pass.
        head = eligible[: max(self.RERANK_CANDIDATES, limit * 20)]
        reranked_scores: dict[int, float] = {}
        for doc_id in head:
            url_terms = set(tokenize(unquote(self.urls[doc_id])))
            bonus = sum(idfs[term] for term in terms if term in url_terms) * self.URL_BOOST
            reranked_scores[doc_id] = scores[doc_id] + bonus

        if mode == "all":
            head.sort(key=lambda doc_id: (-reranked_scores[doc_id], doc_id))
        elif mode == "any":
            head.sort(key=lambda doc_id: (-match_counts[doc_id], -reranked_scores[doc_id], doc_id))
        else:
            head.sort(
                key=lambda doc_id: (
                    -(match_counts[doc_id] == required),
                    -match_counts[doc_id],
                    -reranked_scores[doc_id],
                    doc_id,
                )
            )
        eligible[: len(head)] = head

        return [
            SearchResult(
                rank,
                reranked_scores.get(doc_id, scores[doc_id]),
                self.urls[doc_id],
                match_counts[doc_id],
                required,
                self.titles[doc_id],
                self.snippets[doc_id],
                urlsplit(self.urls[doc_id]).netloc + urlsplit(self.urls[doc_id]).path,
            )
            for rank, doc_id in enumerate(eligible[:limit], 1)
        ]


def print_results(engine: SearchEngine, query: str, limit: int, mode: str) -> None:
    started = time.perf_counter()
    results = engine.search(query, limit, mode)
    elapsed_ms = (time.perf_counter() - started) * 1_000
    if results:
        for result in results:
            coverage = f"{result.matched_terms}/{result.query_terms}"
            print(f"{result.rank:>2}. {result.score:9.4f}  [{coverage}]  {result.url}")
    else:
        print("No matching documents.")
    print(f"{elapsed_ms:.2f} ms")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Search a disk-backed index")
    parser.add_argument("index", help="index directory created by build-index")
    parser.add_argument("--query", help="run one query instead of the interactive prompt")
    parser.add_argument("--limit", type=int, default=5, help="maximum results (default: 5)")
    parser.add_argument(
        "--mode",
        choices=("all", "any", "auto"),
        default="auto",
        help="all=AND, any=OR, auto=AND-first with OR fallback (default: auto)",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    with SearchEngine(args.index) as engine:
        if args.query is not None:
            print_results(engine, args.query, args.limit, args.mode)
            return
        print("UCI ICS Search Engine. Press Enter on an empty line to quit.")
        while True:
            try:
                query = input("search> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not query:
                break
            print_results(engine, query, args.limit, args.mode)


if __name__ == "__main__":
    main()
