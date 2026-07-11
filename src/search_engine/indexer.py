"""SPIMI-style index construction and k-way external merge."""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import os
import shutil
import sys
import tempfile
import time
import zlib
from collections import defaultdict, deque
from concurrent.futures import Future, ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .corpus import CorpusReader
from .text import extract_document_fields


Posting = list[int]  # [doc_id, term_frequency, important_frequency]
Analysis = tuple[dict[str, int], dict[str, int], str, str]


def _analyze_content(content: str) -> Analysis:
    """Process-pool entry point; must remain module-level and picklable."""
    total, important, title, snippet = extract_document_fields(content)
    return dict(total), dict(important), title, snippet


def _default_workers() -> int:
    return max(1, min(4, os.cpu_count() or 1))


@dataclass
class BuildStats:
    indexed_documents: int = 0
    skipped_documents: int = 0
    duplicate_documents: int = 0
    unique_tokens: int = 0
    partial_indexes: int = 0
    elapsed_seconds: float = 0.0
    index_bytes: int = 0
    postings_bytes: int = 0
    workers: int = 1


class IndexBuilder:
    """Construct an index while bounding memory with repeated disk flushes."""

    def __init__(
        self,
        corpus: str | Path,
        output: str | Path,
        *,
        flush_documents: int = 5_000,
        workers: int = 1,
        keep_partials: bool = False,
        overwrite: bool = False,
    ):
        if flush_documents < 1:
            raise ValueError("flush_documents must be positive")
        if workers < 1:
            raise ValueError("workers must be positive")
        self.reader = CorpusReader(corpus)
        self.output = Path(output)
        self.requested_flush_documents = flush_documents
        self.workers = workers
        self.keep_partials = keep_partials
        self.overwrite = overwrite
        self.stats = BuildStats()
        self.stats.workers = workers

    def build(self) -> BuildStats:
        started = time.perf_counter()
        if self.output.exists() and not self.overwrite:
            raise FileExistsError(f"Output already exists: {self.output}; pass --overwrite to replace it")

        parent = self.output.parent.resolve()
        parent.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix=f".{self.output.name}.building-", dir=parent))
        partial_dir = work / "partials"
        partial_dir.mkdir()

        # Cap oversized batches relative to raw corpus size. The ten-way target
        # leaves ample headroom for duplicate removal while still guaranteeing
        # repeated offloads on the supplied developer corpus.
        total_hint = self.reader.document_count_hint()
        max_corpus_relative_batch = max(1, total_hint // 10)
        flush_documents = min(self.requested_flush_documents, max_corpus_relative_batch)

        in_memory: dict[str, list[Posting]] = defaultdict(list)
        partial_paths: list[Path] = []
        documents_path = work / "documents.jsonl"
        seen_urls: set[str] = set()
        seen_content_hashes: set[bytes] = set()

        try:
            with documents_path.open("w", encoding="utf-8") as documents_file:
                since_flush = 0

                def commit(source: str, url: str, analysis: Analysis | Future[Analysis]) -> None:
                    nonlocal since_flush
                    try:
                        term_counts, important_counts, title, snippet = (
                            analysis.result() if isinstance(analysis, Future) else analysis
                        )
                    except Exception as exc:  # malformed pages must not end the build
                        self.stats.skipped_documents += 1
                        print(f"warning: skipped {source}: {exc}", file=sys.stderr)
                        return

                    doc_id = self.stats.indexed_documents
                    document_length = sum(term_counts.values())
                    documents_file.write(
                        json.dumps(
                            [doc_id, url, document_length, title, snippet],
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    for term, tf in term_counts.items():
                        in_memory[term].append([doc_id, tf, important_counts.get(term, 0)])

                    self.stats.indexed_documents += 1
                    since_flush += 1
                    if since_flush >= flush_documents:
                        partial_paths.append(self._flush(in_memory, partial_dir, len(partial_paths)))
                        in_memory.clear()
                        since_flush = 0
                        print(
                            f"flushed partial index {len(partial_paths)} "
                            f"after {self.stats.indexed_documents} documents"
                        )

                def unique_documents():
                    for document in self.reader:
                        content_hash = hashlib.blake2b(
                            document.content.encode("utf-8", errors="replace"), digest_size=16
                        ).digest()
                        if document.url in seen_urls or content_hash in seen_content_hashes:
                            self.stats.duplicate_documents += 1
                            continue
                        seen_urls.add(document.url)
                        seen_content_hashes.add(content_hash)
                        yield document

                if self.workers == 1:
                    for document in unique_documents():
                        try:
                            analysis = _analyze_content(document.content)
                        except Exception as exc:
                            self.stats.skipped_documents += 1
                            print(f"warning: skipped {document.source}: {exc}", file=sys.stderr)
                            continue
                        commit(document.source, document.url, analysis)
                else:
                    # A small bounded queue prevents parsed documents and their
                    # term dictionaries from accumulating faster than the writer.
                    pending: deque[tuple[str, str, Future[Analysis]]] = deque()
                    max_pending = self.workers * 2
                    with ProcessPoolExecutor(max_workers=self.workers) as executor:
                        for document in unique_documents():
                            pending.append(
                                (
                                    document.source,
                                    document.url,
                                    executor.submit(_analyze_content, document.content),
                                )
                            )
                            if len(pending) >= max_pending:
                                commit(*pending.popleft())
                        while pending:
                            commit(*pending.popleft())

                if in_memory or not partial_paths:
                    partial_paths.append(self._flush(in_memory, partial_dir, len(partial_paths)))
                    in_memory.clear()

            self.stats.partial_indexes = len(partial_paths)
            self.stats.unique_tokens = self._merge(
                partial_paths,
                work / "postings.idx",
                work / "lexicon.idx",
                work / "lexicon.sparse.json",
            )
            if not self.keep_partials:
                shutil.rmtree(partial_dir)

            self.stats.postings_bytes = (work / "postings.idx").stat().st_size
            self.stats.index_bytes = sum(path.stat().st_size for path in work.iterdir() if path.is_file())
            self.stats.elapsed_seconds = time.perf_counter() - started
            metadata = {
                "format_version": 2,
                "indexed_documents": self.stats.indexed_documents,
                "skipped_documents": self.stats.skipped_documents,
                "duplicate_documents": self.stats.duplicate_documents,
                "unique_tokens": self.stats.unique_tokens,
                "partial_indexes": self.stats.partial_indexes,
                "index_bytes": self.stats.index_bytes,
                "index_kb": round(self.stats.index_bytes / 1024, 2),
                "postings_bytes": self.stats.postings_bytes,
                "elapsed_seconds": round(self.stats.elapsed_seconds, 3),
                "flush_documents": flush_documents,
                "workers": self.workers,
                "posting_codec": "zlib-compressed JSON with delta-encoded document IDs",
                "scoring": "BM25-style tf-idf with important-field boost; all/any/auto matching",
            }
            (work / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

            if self.output.exists():
                shutil.rmtree(self.output)
            os.replace(work, self.output)
            return self.stats
        except Exception:
            shutil.rmtree(work, ignore_errors=True)
            raise

    @staticmethod
    def _flush(index: dict[str, list[Posting]], partial_dir: Path, number: int) -> Path:
        path = partial_dir / f"partial-{number:04d}.idx"
        with path.open("w", encoding="utf-8", newline="\n") as stream:
            for term in sorted(index):
                stream.write(term)
                stream.write("\t")
                stream.write(json.dumps(index[term], separators=(",", ":")))
                stream.write("\n")
        return path

    @staticmethod
    def _read_record(stream: BinaryIO) -> tuple[str, list[Posting]] | None:
        line = stream.readline()
        if not line:
            return None
        term_bytes, payload = line.rstrip(b"\n").split(b"\t", 1)
        return term_bytes.decode("utf-8"), json.loads(payload)

    def _merge(
        self,
        partial_paths: list[Path],
        postings_path: Path,
        lexicon_path: Path,
        sparse_path: Path,
    ) -> int:
        streams = [path.open("rb") for path in partial_paths]
        heap: list[tuple[str, int, list[Posting]]] = []
        sparse: list[list[str | int]] = []
        term_count = 0
        try:
            for number, stream in enumerate(streams):
                record = self._read_record(stream)
                if record is not None:
                    term, postings = record
                    heapq.heappush(heap, (term, number, postings))

            with postings_path.open("wb") as output, lexicon_path.open("wb") as lexicon:
                while heap:
                    term, source, postings = heapq.heappop(heap)
                    combined = postings
                    next_record = self._read_record(streams[source])
                    if next_record is not None:
                        heapq.heappush(heap, (next_record[0], source, next_record[1]))

                    while heap and heap[0][0] == term:
                        _, source, same_term = heapq.heappop(heap)
                        combined.extend(same_term)
                        next_record = self._read_record(streams[source])
                        if next_record is not None:
                            heapq.heappush(heap, (next_record[0], source, next_record[1]))

                    # Partial files cover disjoint, increasing doc-id ranges.
                    previous_doc_id = 0
                    delta_encoded: list[Posting] = []
                    for doc_id, tf, important_tf in combined:
                        delta_encoded.append([doc_id - previous_doc_id, tf, important_tf])
                        previous_doc_id = doc_id
                    payload = zlib.compress(
                        json.dumps(delta_encoded, separators=(",", ":")).encode("utf-8"),
                        level=6,
                    )
                    offset = output.tell()
                    output.write(payload)
                    if term_count % 256 == 0:
                        sparse.append([term, lexicon.tell()])
                    lexicon.write(term.encode("utf-8"))
                    lexicon.write(b"\t")
                    lexicon.write(
                        json.dumps([offset, len(payload), len(combined)], separators=(",", ":")).encode("ascii")
                    )
                    lexicon.write(b"\n")
                    term_count += 1
        finally:
            for stream in streams:
                stream.close()

        sparse_path.write_text(json.dumps(sparse, separators=(",", ":")), encoding="utf-8")
        return term_count


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a disk-backed inverted index")
    parser.add_argument("corpus", help="developer.zip, analyst.zip, or an extracted corpus directory")
    parser.add_argument("output", help="directory to create for the finished index")
    parser.add_argument("--flush-documents", type=int, default=5_000, help="documents per partial index (default: 5000)")
    parser.add_argument(
        "--workers",
        type=int,
        default=_default_workers(),
        help="HTML parsing worker processes (default: up to 4)",
    )
    parser.add_argument("--keep-partials", action="store_true", help="retain intermediate partial index files")
    parser.add_argument("--overwrite", action="store_true", help="replace an existing output directory")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    stats = IndexBuilder(
        args.corpus,
        args.output,
        flush_documents=args.flush_documents,
        workers=args.workers,
        keep_partials=args.keep_partials,
        overwrite=args.overwrite,
    ).build()
    print(json.dumps(stats.__dict__, indent=2))


if __name__ == "__main__":
    main()
