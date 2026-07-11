"""Sparse, disk-backed term lexicon."""

from __future__ import annotations

import json
import os
from bisect import bisect_right
from functools import lru_cache
from pathlib import Path


class DiskLexicon:
    """Resolve terms by scanning one small block of a sorted on-disk lexicon."""

    def __init__(self, lexicon_path: str | Path, sparse_path: str | Path):
        self.path = Path(lexicon_path)
        sparse = json.loads(Path(sparse_path).read_text(encoding="utf-8"))
        self.terms = [record[0] for record in sparse]
        self.offsets = [record[1] for record in sparse]
        self.size = self.path.stat().st_size
        self.file_descriptor = os.open(self.path, os.O_RDONLY)

    @lru_cache(maxsize=4_096)
    def get(self, term: str) -> tuple[int, int, int] | None:
        block = bisect_right(self.terms, term) - 1
        if block < 0:
            return None
        start = self.offsets[block]
        end = self.offsets[block + 1] if block + 1 < len(self.offsets) else self.size
        # pread does not mutate a shared file position, so concurrent query
        # threads can safely share one DiskLexicon instance.
        for line in os.pread(self.file_descriptor, end - start, start).splitlines():
            encoded_term, payload = line.rstrip(b"\n").split(b"\t", 1)
            candidate = encoded_term.decode("utf-8")
            if candidate == term:
                offset, length, df = json.loads(payload)
                return offset, length, df
            if candidate > term:
                break
        return None

    def close(self) -> None:
        os.close(self.file_descriptor)
