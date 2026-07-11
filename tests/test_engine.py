from __future__ import annotations

import json
import tempfile
import threading
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import urlopen

from search_engine.indexer import IndexBuilder
from search_engine.search import SearchEngine
from search_engine.server import create_server
from search_engine.text import extract_term_counts, tokenize


class SearchEngineTests(unittest.TestCase):
    def test_tokenization_and_important_text(self) -> None:
        total, important = extract_term_counts("<title>Machine Learning</title><p>machines learn</p>")
        self.assertEqual(tokenize("ACM's systems_2026"), ["acm", "s", "system", "2026"])
        self.assertEqual(total["machin"], 2)
        self.assertEqual(important["machin"], 1)

    def test_partial_build_and_and_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus = root / "corpus.zip"
            pages = [
                ("https://example.test/one#section", "<h1>Machine Learning</h1> algorithms"),
                ("https://example.test/two", "machine learning machine"),
                ("https://example.test/three", "machine vision"),
                ("https://example.test/four", "learning theory"),
                ("https://example.test/five", "ACM software engineering"),
                ("https://example.test/six", "master of software engineering"),
                ("https://example.test/six#duplicate", "duplicate copy should be ignored"),
            ]
            with zipfile.ZipFile(corpus, "w") as archive:
                for number, (url, content) in enumerate(pages):
                    archive.writestr(f"DEV/site/{number}.json", json.dumps({"url": url, "content": content, "encoding": "utf-8"}))

            output = root / "index"
            stats = IndexBuilder(corpus, output, flush_documents=2, workers=2).build()
            self.assertEqual(stats.indexed_documents, 6)
            self.assertEqual(stats.duplicate_documents, 1)
            self.assertGreaterEqual(stats.partial_indexes, 3)
            self.assertGreater(stats.unique_tokens, 5)
            self.assertFalse((output / "partials").exists())

            with SearchEngine(output) as engine:
                results = engine.search("machine learning", mode="all")
                self.assertEqual([result.url for result in results], [
                    "https://example.test/one",
                    "https://example.test/two",
                ])
                self.assertEqual(results[0].title, "Machine Learning")
                self.assertIn("algorithms", results[0].snippet)
                self.assertEqual(engine.search("missing term"), [])
                fallback = engine.search("machine missing", mode="auto")
                self.assertGreaterEqual(len(fallback), 1)
                self.assertEqual(fallback[0].matched_terms, 1)

            serial_output = root / "serial-index"
            IndexBuilder(corpus, serial_output, flush_documents=2, workers=1).build()
            for filename in ("documents.jsonl", "postings.idx", "lexicon.idx", "lexicon.sparse.json"):
                self.assertEqual((output / filename).read_bytes(), (serial_output / filename).read_bytes())

            server = create_server(output, port=0)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            endpoint = (
                f"http://127.0.0.1:{server.server_port}/api/search"
                "?q=machine%20learning&mode=all"
            )
            try:
                home = urlopen(f"http://127.0.0.1:{server.server_port}/").read().decode()
                self.assertIn("A2Z Search", home)
                self.assertIn("results-view", home)
                with ThreadPoolExecutor(max_workers=8) as executor:
                    payloads = list(executor.map(lambda _: urlopen(endpoint).read(), range(24)))
                for payload in payloads:
                    response = json.loads(payload)
                    self.assertEqual(response["count"], 2)
                    self.assertEqual(response["results"][0]["url"], "https://example.test/one")
            finally:
                server.shutdown()
                server_thread.join(timeout=5)
                server.server_close()


if __name__ == "__main__":
    unittest.main()
