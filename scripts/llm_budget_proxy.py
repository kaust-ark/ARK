#!/usr/bin/env python3
"""Local forwarding proxy for bounded model experiments.

Sits between an agent runtime and an OpenAI-compatible or Anthropic-style
endpoint. It counts requests and the tokens the provider reports, refuses
everything past a request or token cap (so an experiment's budget holds
whatever the harness does), and can answer chosen requests with an error
instead of forwarding them, to exercise retry and failure handling against a
real model.

  python scripts/llm_budget_proxy.py --upstream https://gateway/v1 --port 18900 \
      --max-requests 60 --max-tokens 4000000 --inject 3:500,4:500,5:500,6:500 \
      --log proxy.jsonl
  export LLM_BASE_URL=http://127.0.0.1:18900/v1

Injected and refused requests never reach the upstream, so they cost nothing.
Usage is read from the response (`usage` in a JSON body or in a streamed
chunk). The key is passed through in the request headers and never logged.
"""
from __future__ import annotations

import argparse
import http.client
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

_ERRORS = {
    429: {"error": {"message": "Rate limit reached (injected by budget proxy)", "type": "rate_limit_error",
                    "code": "rate_limit_exceeded"}},
    500: {"error": {"message": "Internal server error (injected by budget proxy)", "type": "server_error"}},
    402: {"error": {"message": "Insufficient Balance (injected by budget proxy)",
                    "type": "insufficient_balance_error"}},
}
_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "input_tokens", "output_tokens")


class State:
    def __init__(self, a):
        self.a, self.lock, self.n, self.tokens = a, threading.Lock(), 0, 0
        self.inject = {int(i): int(c) for i, c in (x.split(":") for x in a.inject.split(",") if x)}

    def log(self, **rec):
        if self.a.log:
            with self.lock, open(self.a.log, "a") as fh:
                fh.write(json.dumps({"t": round(time.time(), 2), **rec}) + "\n")


def _tokens(chunk: str) -> int:
    """Tokens in the last usage object seen in a body or stream chunk."""
    best = 0
    for m in re.finditer(r'"usage"\s*:\s*(\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\})', chunk):
        try:
            u = json.loads(m.group(1))
        except ValueError:
            continue
        best = max(best, sum(int(u.get(k) or 0) for k in _USAGE_KEYS))
    return best


def make_handler(state: State):
    up = urlsplit(state.a.upstream)

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _reply(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _forward(self, method, body=None):
            path = up.path.rstrip("/") + self.path[len(state.a.prefix):] if self.path.startswith(
                state.a.prefix) else self.path
            conn = (http.client.HTTPSConnection if up.scheme == "https" else http.client.HTTPConnection)(
                up.netloc, timeout=state.a.timeout)
            headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in ("host", "content-length", "connection", "accept-encoding")}
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in ("transfer-encoding", "connection", "content-length", "content-encoding"):
                    self.send_header(k, v)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            seen, tail = 0, ""
            while True:
                chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
                if not chunk:
                    break
                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                self.wfile.flush()
                text = tail + chunk.decode("utf-8", "replace")
                seen = max(seen, _tokens(text))
                tail = text[-4096:]
            self.wfile.write(b"0\r\n\r\n")
            conn.close()
            return resp.status, seen

        def do_GET(self):
            try:
                self._forward("GET")
            except Exception as e:
                self._reply(502, {"error": {"message": f"budget proxy: upstream failed: {type(e).__name__}"}})

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            with state.lock:
                state.n += 1
                n, used = state.n, state.tokens
            if n in state.inject:
                code = state.inject[n]
                state.log(n=n, injected=code)
                return self._reply(code, _ERRORS.get(code, {"error": {"message": f"injected {code}"}}))
            if n > state.a.max_requests or used >= state.a.max_tokens:
                state.log(n=n, refused="budget", tokens=used)
                return self._reply(402, {"error": {"message": "budget proxy: experiment cap reached",
                                                   "type": "insufficient_balance_error"}})
            t0 = time.time()
            try:
                status, tokens = self._forward("POST", body)
            except Exception as e:
                state.log(n=n, upstream_error=type(e).__name__)
                return self._reply(502, {"error": {"message": f"budget proxy: upstream failed: {type(e).__name__}"}})
            with state.lock:
                state.tokens += tokens
                total = state.tokens
            state.log(n=n, status=status, tokens=tokens, total_tokens=total, s=round(time.time() - t0, 2))

        def log_message(self, *a):
            pass

    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream", required=True, help="endpoint base URL, e.g. https://host/v1")
    ap.add_argument("--port", type=int, default=18900)
    ap.add_argument("--prefix", default="/v1", help="local path prefix mapped onto the upstream path")
    ap.add_argument("--max-requests", type=int, default=100)
    ap.add_argument("--max-tokens", type=int, default=2_000_000)
    ap.add_argument("--inject", default="", help="comma list of request:status, e.g. 3:500,7:402")
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--log", default="")
    a = ap.parse_args()
    state = State(a)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(state))
    print(f"budget proxy on 127.0.0.1:{a.port} -> {urlsplit(a.upstream).scheme}://<upstream>", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
