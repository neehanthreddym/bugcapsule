"""Fixed standalone loopback replay. No captured headers, upstream, or access logs.

Routes match exact methods/paths, sorted decoded query pairs (including repeats),
and typed JSON bodies; absent bodies differ from JSON null. Object order is not
significant, array order and scalar types are. No body inference or coercion.
This file is copied verbatim into capsules, never generated from capture data.
"""

import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit


METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
MAX_BODY_BYTES = 1_048_576
MAX_HAR_BYTES = 16_777_216


class ReplayError(ValueError):
    """Fixed diagnostic without route/body data."""


def _reject(_value):
    raise ReplayError("Invalid JSON.")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReplayError("Ambiguous JSON object.")
        result[key] = value
    return result


def read_json(text):
    return json.loads(text, parse_constant=_reject, object_pairs_hook=_object)


def _typed(value):
    if value is None:
        return ("null",)
    if type(value) in (str, bool, int, float):
        if type(value) is float and not math.isfinite(value):
            raise ReplayError("Unsupported JSON number.")
        return (type(value).__name__, value)
    if type(value) is list:
        return ("array", tuple(_typed(child) for child in value))
    if type(value) is dict:
        return ("object", tuple((key, _typed(child)) for key, child in sorted(value.items())))
    raise ReplayError("Unsupported JSON value.")


def route_key(method, url, has_body=False, body=None):
    parts = urlsplit(url)
    if (method not in METHODS or not parts.path.startswith("/")
            or parts.path.startswith("//") or parts.fragment):
        raise ReplayError("Unsupported route.")
    query = tuple(sorted(parse_qsl(parts.query, keep_blank_values=True, encoding="utf-8", errors="strict")))
    return method, parts.path, query, ("json", _typed(body)) if has_body else ("absent",)


def build_routes(har):
    """Validate packaged entries; ambiguous keys fail before listening/export."""
    routes = {}
    try:
        entries = har["log"]["entries"]
        if type(entries) is not list or not 1 <= len(entries) <= 10:
            raise ReplayError("Invalid replay entries.")
        for entry in entries:
            request, response = entry["request"], entry["response"]
            parts = urlsplit(request["url"])
            if (parts.scheme != "https" or parts.netloc != "replay.example.test"
                    or parts.fragment):
                raise ReplayError("Invalid replay origin.")
            post = request.get("postData")
            if post is not None and (post.get("mimeType") != "application/json"
                                     or "encoding" in post):
                raise ReplayError("Unsupported request body.")
            body = read_json(post["text"]) if post is not None else None
            if post is not None and len(post["text"].encode("utf-8")) > MAX_BODY_BYTES:
                raise ReplayError("Request body exceeds replay limit.")
            key = route_key(request["method"], request["url"], post is not None, body)
            if key in routes:
                raise ReplayError("Ambiguous replay routes.")
            if (type(response["status"]) is not int or not 200 <= response["status"] <= 599
                    or response["status"] in (204, 205, 304)):
                raise ReplayError("Unsupported response status.")
            content = response["content"]
            if content["mimeType"] != "application/json" or "encoding" in content:
                raise ReplayError("Unsupported response body.")
            # Encode locally, preserving JSON types, never replaying captured bytes/headers.
            data = json.dumps(read_json(content["text"]), ensure_ascii=False,
                              allow_nan=False, separators=(",", ":")).encode("utf-8")
            routes[key] = response["status"], data
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError, UnicodeError):
        raise ReplayError("Invalid or ambiguous replay data.") from None
    return routes


def make_server(har, port=8000):
    """Construct a loopback-only server; port=0 lets the OS allocate a test port."""
    routes = build_routes(har)

    class Handler(BaseHTTPRequestHandler):
        server_version = "BugCapsuleReplay"
        sys_version = ""

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, _format, *args):
            pass

        def _send(self, status, data):
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
            self.close_connection = True

        def send_error(self, code, message=None, explain=None):
            self._send(code, b'{"error":"local replay request rejected"}')

        def _replay(self):
            try:
                parts = urlsplit(self.path)
                if parts.scheme or parts.netloc or parts.fragment:
                    raise ReplayError("Unsupported request target.")
                lengths = self.headers.get_all("Content-Length", [])
                if (self.headers.get("Transfer-Encoding") is not None or len(lengths) > 1
                        or self.headers.get("Content-Encoding", "identity").lower() != "identity"):
                    raise ReplayError("Unsupported body framing.")
                length = int(lengths[0]) if lengths else 0
                if not 0 <= length <= MAX_BODY_BYTES:
                    raise ReplayError("Unsupported body size.")
                has_body = length > 0
                body = None
                if has_body:
                    mime = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                    if mime != "application/json" and not (mime.startswith("application/") and mime.endswith("+json")):
                        raise ReplayError("Unsupported body format.")
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise ReplayError("Incomplete body.")
                    body = read_json(raw.decode("utf-8"))
                key = route_key(self.command, self.path, has_body, body)
                response = routes.get(key)
                if response is None:
                    self._send(404, b'{"error":"no matching local replay transaction"}')
                else:
                    self._send(*response)
            except (ValueError, TypeError, UnicodeError, RecursionError, OSError):
                self._send(400, b'{"error":"invalid local replay request"}')

        do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _replay

    return HTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description="Standalone loopback BugCapsule replay")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    try:
        path = Path(__file__).resolve().with_name("transformed.har")
        if path.stat().st_size > MAX_HAR_BYTES:
            raise ReplayError("Replay file exceeds limit.")
        server = make_server(read_json(path.read_text(encoding="utf-8")), args.port)
        print(f"Replay listening on http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, TypeError, RecursionError):
        print("Replay failed: invalid data or unavailable local port.", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
