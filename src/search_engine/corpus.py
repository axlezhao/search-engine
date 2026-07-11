"""Streaming readers for the provided ZIP corpus and extracted corpus folders."""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class CorpusDocument:
    source: str
    url: str
    content: str


class CorpusReader:
    """Read JSON documents one at a time without extracting the whole corpus."""

    def __init__(self, source: str | Path):
        self.source = Path(source)
        if not self.source.exists():
            raise FileNotFoundError(f"Corpus does not exist: {self.source}")
        if not (self.source.is_dir() or zipfile.is_zipfile(self.source)):
            raise ValueError("Corpus must be a ZIP archive or an extracted directory")

    def document_count_hint(self) -> int:
        if self.source.is_dir():
            return sum(1 for path in self.source.rglob("*.json") if path.is_file())
        with zipfile.ZipFile(self.source) as archive:
            return sum(1 for info in archive.infolist() if not info.is_dir() and info.filename.endswith(".json"))

    def __iter__(self) -> Iterator[CorpusDocument]:
        if self.source.is_dir():
            yield from self._from_directory()
        else:
            yield from self._from_zip()

    @staticmethod
    def _decode(raw: bytes) -> dict | None:
        try:
            value = json.loads(raw.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _to_document(source: str, value: dict | None) -> CorpusDocument | None:
        if value is None:
            return None
        url = value.get("url")
        content = value.get("content")
        if not isinstance(url, str) or not isinstance(content, str):
            return None
        # Fragments identify locations within a page, not separate documents.
        return CorpusDocument(source=source, url=url.split("#", 1)[0], content=content)

    def _from_directory(self) -> Iterator[CorpusDocument]:
        for path in sorted(self.source.rglob("*.json")):
            try:
                value = self._decode(path.read_bytes())
            except OSError:
                continue
            document = self._to_document(str(path), value)
            if document is not None:
                yield document

    def _from_zip(self) -> Iterator[CorpusDocument]:
        with zipfile.ZipFile(self.source) as archive:
            names = sorted(
                info.filename
                for info in archive.infolist()
                if not info.is_dir() and info.filename.endswith(".json")
            )
            for name in names:
                try:
                    raw = archive.read(name)
                except (KeyError, OSError, zipfile.BadZipFile):
                    continue
                document = self._to_document(name, self._decode(raw))
                if document is not None:
                    yield document
