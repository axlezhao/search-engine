"""Concurrent local HTTP API and A2Z Search browser interface."""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import signal
import socket
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .search import SearchEngine


INDEX_PATH = Path(__file__).with_name("web") / "index.html"


class SearchHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        engine: SearchEngine,
        *,
        reuse_port: bool = False,
        verbose: bool = False,
    ):
        self.engine = engine
        self.reuse_port = reuse_port
        self.verbose = verbose
        self.index_html = INDEX_PATH.read_bytes()
        super().__init__(address, SearchRequestHandler)

    def server_bind(self) -> None:
        if self.reuse_port:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        super().server_bind()

    def server_close(self) -> None:
        super().server_close()
        self.engine.close()


class SearchRequestHandler(BaseHTTPRequestHandler):
    server: SearchHTTPServer

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: HTTPStatus, value: object) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlsplit(self.path)
        if parsed.path in {"/", "/search"}:
            self._send(HTTPStatus.OK, self.server.index_html, "text/html; charset=utf-8")
            return
        if parsed.path == "/health":
            self._json(HTTPStatus.OK, {"status": "ok", "product": "A2Z Search"})
            return
        if parsed.path != "/api/search":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return

        parameters = parse_qs(parsed.query)
        query = parameters.get("q", [""])[0].strip()
        mode = parameters.get("mode", ["auto"])[0]
        try:
            limit = min(50, max(1, int(parameters.get("limit", ["10"])[0])))
            if not query:
                raise ValueError("Enter something to search for.")
            if len(query) > 512:
                raise ValueError("Searches must be at most 512 characters.")
            started = time.perf_counter()
            results = self.server.engine.search(query, limit=limit, mode=mode)
            elapsed_ms = (time.perf_counter() - started) * 1_000
        except (TypeError, ValueError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        self._json(
            HTTPStatus.OK,
            {
                "query": query,
                "mode": mode,
                "count": len(results),
                "elapsed_ms": round(elapsed_ms, 3),
                "results": [result.__dict__ for result in results],
            },
        )

    def log_message(self, format: str, *args: object) -> None:
        if self.server.verbose:
            print(f"{self.client_address[0]} - {format % args}")


def create_server(
    index: str | Path,
    host: str = "127.0.0.1",
    port: int = 8000,
    *,
    reuse_port: bool = False,
    verbose: bool = False,
) -> SearchHTTPServer:
    return SearchHTTPServer(
        (host, port),
        SearchEngine(index),
        reuse_port=reuse_port,
        verbose=verbose,
    )


def _serve_worker(index: str, host: str, port: int, verbose: bool) -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    server = create_server(index, host, port, reuse_port=True, verbose=verbose)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the concurrent A2Z Search API and UI")
    parser.add_argument("index", help="index directory created by build-index")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--workers", type=int, default=1, help="independent server processes (default: 1)")
    parser.add_argument("--verbose", action="store_true", help="log every HTTP request")
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be positive")
    if args.workers > 1 and args.port == 0:
        parser.error("--port 0 cannot be used with multiple workers")

    if args.workers > 1:
        print(f"A2Z Search: http://{args.host}:{args.port} ({args.workers} workers)")
        context = mp.get_context("spawn")
        processes = [
            context.Process(target=_serve_worker, args=(args.index, args.host, args.port, args.verbose))
            for _ in range(args.workers)
        ]
        try:
            for process in processes:
                process.start()
            for process in processes:
                process.join()
        except KeyboardInterrupt:
            print()
            for process in processes:
                process.terminate()
        finally:
            for process in processes:
                process.join()
        return


    server = create_server(args.index, args.host, args.port, verbose=args.verbose)
    print(f"A2Z Search: http://{args.host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
