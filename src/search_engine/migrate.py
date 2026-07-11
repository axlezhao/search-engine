"""In-place format migrations for generated indexes."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def upgrade_v1(directory: Path) -> None:
    metadata_path = directory / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("format_version") != 1:
        raise ValueError("only format version 1 can be upgraded")

    old_path = directory / "lexicon.json"
    lexicon = json.loads(old_path.read_text(encoding="utf-8"))
    new_path = directory / ".lexicon.idx.upgrading"
    sparse_path = directory / ".lexicon.sparse.json.upgrading"
    sparse: list[list[str | int]] = []
    with new_path.open("wb") as output:
        for number, (term, entry) in enumerate(lexicon.items()):
            if number % 256 == 0:
                sparse.append([term, output.tell()])
            output.write(term.encode("utf-8"))
            output.write(b"\t")
            output.write(json.dumps(entry, separators=(",", ":")).encode("ascii"))
            output.write(b"\n")
    sparse_path.write_text(json.dumps(sparse, separators=(",", ":")), encoding="utf-8")

    os.replace(new_path, directory / "lexicon.idx")
    os.replace(sparse_path, directory / "lexicon.sparse.json")
    old_path.unlink()
    metadata["format_version"] = 2
    metadata["postings_bytes"] = (directory / "postings.idx").stat().st_size
    metadata["index_bytes"] = sum(
        path.stat().st_size for path in directory.iterdir() if path.is_file() and path.name != "metadata.json"
    )
    metadata["index_kb"] = round(metadata["index_bytes"] / 1024, 2)
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Upgrade a generated search index")
    parser.add_argument("index", help="index directory to upgrade in place")
    args = parser.parse_args()
    upgrade_v1(Path(args.index))


if __name__ == "__main__":
    main()
