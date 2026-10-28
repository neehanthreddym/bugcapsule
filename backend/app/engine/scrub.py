"""Deterministic local credential scrubbing, never guaranteed anonymization.

SENSITIVE_NAMES is the single exact-name policy for headers, query parameters,
JSON keys, and form fields. Matching case-folds and removes hyphens, underscores,
dots, and whitespace; camelCase therefore matches too. No substring/value search
is performed: page_token, cursor, customer_id, and business data stay private.
Unknown credential names, credentials embedded in ordinary values, URL paths or
fragments, URL-valued headers, comments, and custom HAR metadata are not inspected.

Supported request bodies are UTF-8 JSON and application/x-www-form-urlencoded,
with plain text or the HAR-style base64 text convention. Multipart, compressed,
opaque, missing-but-declared, and conflicting body representations fail closed.
Nonempty params alongside JSON text are ambiguous and rejected. Rewritten bodies
are uncompressed; stale wire encoding/size metadata is removed or updated.
Outputs remain local HarTransaction objects, not outbound/model-safe payloads.
Counts describe removals per representation, not distinct secret values.
"""

import base64
from collections import Counter
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
import json
import re
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit

from pydantic import JsonValue

from app.schemas import HarTransaction


SENSITIVE_NAMES = frozenset({
    "authorization", "proxy-authorization", "cookie", "cookies", "set-cookie",
    "api-key", "x-api-key", "api-token", "api-secret", "x-auth-token",
    "password", "passwords", "passwd", "pwd", "client-secret", "secret", "token",
    "access-token", "refresh-token", "id-token", "auth-token", "x-access-token",
    "session", "session-id", "session-token", "session-key", "session-secret",
    "session-cookie", "x-session-id", "x-session-token",
    "csrf-token", "xsrf-token", "x-csrf-token", "x-xsrf-token",
})


def _normalize(name: str) -> str:
    return re.sub(r"[-_.\s]", "", name).casefold()


_SENSITIVE = frozenset(_normalize(name) for name in SENSITIVE_NAMES)

RemovalCategory = Literal[
    "request_headers", "response_headers", "request_cookies", "response_cookies",
    "url_userinfo", "url_query", "query_string", "response_json", "request_json",
    "request_params", "request_form",
]


@dataclass(frozen=True)
class RemovalCount:
    """Only an original entry ID, fixed category, and occurrence count."""

    entry_id: int
    category: RemovalCategory
    count: int


@dataclass(frozen=True)
class ScrubResult:
    """Independent local transactions and a value-free removal summary."""

    transactions: list[HarTransaction]
    removals: list[RemovalCount]


class ScrubError(ValueError):
    """Scrubbing failed; messages identify entries without captured values."""


def _sensitive(name: str) -> bool:
    return _normalize(name) in _SENSITIVE


def _named_items(items: list) -> list:
    if not isinstance(items, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("name"), str)
        or not isinstance(item.get("value"), str) for item in items
    ):
        raise ScrubError("invalid name/value representation")
    return items


def _filter_named(items: list, counts: Counter, category: RemovalCategory) -> list:
    retained = []
    for item in _named_items(items):
        if _sensitive(item["name"]):
            counts[category] += 1
        else:
            retained.append(item)
    return retained


def _json_scrub(value: JsonValue, counts: Counter, category: RemovalCategory) -> JsonValue:
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if _sensitive(key):
                counts[category] += 1
            else:
                result[key] = _json_scrub(child, counts, category)
        return result
    if isinstance(value, list):
        return [_json_scrub(child, counts, category) for child in value]
    return value


def _reject_constant(_value: str) -> None:
    raise ScrubError("invalid JSON body")


def _read_text(body: dict) -> str:
    text = body.get("text")
    if not isinstance(text, str):
        raise ScrubError("missing or invalid request/response body text")
    if "encoding" not in body:
        return text
    if body["encoding"] != "base64":
        raise ScrubError("unsupported body text encoding")
    try:
        return base64.b64decode(text, validate=True).decode("utf-8")
    except (ValueError, UnicodeError):
        raise ScrubError("invalid base64 or UTF-8 body") from None


def _json_text(value: JsonValue) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _set_text(body: dict, text: str) -> int:
    data = text.encode("utf-8")
    body["text"] = (
        base64.b64encode(data).decode("ascii") if body.get("encoding") == "base64" else text
    )
    return len(data)


def _update_sizes(part: dict, size: int) -> None:
    part["bodySize"] = size
    part["headersSize"] = -1
    # HAR text is decoded data; output bodies have no wire compression/chunking.
    part["headers"] = [
        header for header in part["headers"]
        if header["name"].lower() not in {"content-encoding", "transfer-encoding"}
    ]
    for header in part["headers"]:
        if header["name"].lower() == "content-length":
            header["value"] = str(size)


def _query_pairs(text: str) -> list[tuple[str, str]]:
    return parse_qsl(text, keep_blank_values=True, encoding="utf-8", errors="strict")


def _filter_pairs(pairs: list, counts: Counter, category: RemovalCategory) -> list:
    result = []
    for name, value in pairs:
        if _sensitive(name):
            counts[category] += 1
        else:
            result.append((name, value))
    return result


def _scrub_url(request: dict, counts: Counter) -> str:
    parts = urlsplit(request["url"])
    netloc = parts.netloc
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
        counts["url_userinfo"] += 1
    pairs = _filter_pairs(_query_pairs(parts.query), counts, "url_query")
    if "queryString" in request:
        params = _filter_named(request["queryString"], counts, "query_string")
        if [(item["name"], item["value"]) for item in params] != pairs:
            raise ScrubError("inconsistent URL and queryString representations")
    else:
        params = [{"name": name, "value": value} for name, value in pairs]
    request["queryString"] = params
    url = parts._replace(netloc=netloc, query=urlencode(pairs)).geturl()
    request["url"] = url
    return url


def _mime(body: dict) -> str:
    mime = body.get("mimeType")
    if not isinstance(mime, str):
        raise ScrubError("missing body MIME type")
    parts = mime.lower().split(";")
    for parameter in parts[1:]:
        name, separator, value = parameter.partition("=")
        if name.strip() == "charset" and (
            not separator or value.strip().strip('"') not in {"utf-8", "utf8"}
        ):
            raise ScrubError("unsupported body charset")
    return parts[0].strip()


def _is_json(mime: str) -> bool:
    return mime == "application/json" or bool(re.fullmatch(
        r"application/[!#$%&'*+.^_`|~0-9a-z-]+\+json", mime
    ))


def _scrub_request_body(request: dict, counts: Counter) -> None:
    headers = request["headers"]
    for header in headers:
        name, value = header["name"].lower(), header["value"]
        if name == "content-encoding" and value.strip().lower() != "identity":
            raise ScrubError("unsupported request-body content encoding")
    if "postData" not in request:
        if request.get("bodySize", 0) != 0 or any(
            header["name"].lower() == "content-length" and int(header["value"]) != 0
            for header in headers
        ):
            raise ScrubError("request body is declared but unavailable for scrubbing")
        return
    post = request["postData"]
    if not isinstance(post, dict):
        raise ScrubError("invalid request-body representation")
    mime = _mime(post)
    if not _is_json(mime) and mime != "application/x-www-form-urlencoded":
        raise ScrubError("unsupported request-body format")
    for header in headers:
        if header["name"].lower() == "content-type" and _mime({"mimeType": header["value"]}) != mime:
            raise ScrubError("conflicting request-body MIME types")
    if "encoding" in post and post["encoding"] != "base64":
        raise ScrubError("unsupported request-body text encoding")
    if _is_json(mime):
        if "params" in post and post["params"] != []:
            raise ScrubError("ambiguous JSON request-body params")
        data = json.loads(_read_text(post), parse_constant=_reject_constant)
        text = _json_text(_json_scrub(data, counts, "request_json"))
    else:
        if "params" not in post and "text" not in post:
            raise ScrubError("missing form request body")
        if any("fileName" in item for item in _named_items(post.get("params", []))):
            raise ScrubError("unsupported file request-body parameters")
        params = _filter_named(post.get("params", []), counts, "request_params")
        pairs = [(item["name"], item["value"]) for item in params]
        if "text" in post:
            text_pairs = _filter_pairs(_query_pairs(_read_text(post)), counts, "request_form")
            if "params" in post and pairs != text_pairs:
                raise ScrubError("conflicting form request-body representations")
            pairs = text_pairs
        elif "encoding" in post:
            raise ScrubError("encoded request body is unavailable for scrubbing")
        post["params"] = (
            params if "params" in post else [{"name": name, "value": value} for name, value in pairs]
        )
        text = urlencode(pairs)
    _update_sizes(request, _set_text(post, text))


def _scrub_one(transaction: HarTransaction, counts: Counter) -> HarTransaction:
    entry = deepcopy(transaction.original_entry)
    request, response = entry["request"], entry["response"]
    if request["url"] != transaction.url:
        raise ScrubError("conflicting transaction URL representations")
    for part, prefix in ((request, "request"), (response, "response")):
        part["headers"] = _filter_named(part.get("headers", []), counts, f"{prefix}_headers")
        cookies = part.get("cookies", [])
        if not isinstance(cookies, list):
            raise ScrubError("invalid cookie array")
        counts[f"{prefix}_cookies"] += len(cookies)
        part["cookies"] = []
        part["headersSize"] = -1
    url = _scrub_url(request, counts)
    _scrub_request_body(request, counts)
    content = response["content"]
    if not _is_json(_mime(content)):
        raise ScrubError("unsupported response-body format")
    data = json.loads(_read_text(content), parse_constant=_reject_constant)
    if json.dumps(data, sort_keys=True, allow_nan=False) != json.dumps(
        transaction.response_json, sort_keys=True, allow_nan=False
    ):
        raise ScrubError("conflicting parsed response and HAR body")
    cleaned = _json_scrub(data, counts, "response_json")
    content["size"] = _set_text(content, _json_text(cleaned))
    content.pop("compression", None)
    _update_sizes(response, content["size"])
    return HarTransaction(
        entry_id=transaction.entry_id, method=transaction.method, url=url,
        origin=transaction.origin, status=transaction.status,
        response_json=cleaned, original_entry=entry,
    )


def scrub_transactions(transactions: Sequence[HarTransaction]) -> ScrubResult:
    """Scrub already-selected transactions without mutating input objects or files.

    Raises ScrubError atomically: no partial result is returned on failure.
    Removal summaries omit zero counts and have deterministic category ordering.
    """
    scrubbed, removals = [], []
    for transaction in transactions:
        counts: Counter[RemovalCategory] = Counter()
        try:
            cleaned = _scrub_one(transaction, counts)
        except ScrubError as error:
            raise ScrubError(f"Entry {transaction.entry_id}: {error}.") from None
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            raise ScrubError(
                f"Entry {transaction.entry_id}: invalid retained HAR data or body."
            ) from None
        scrubbed.append(cleaned)
        removals.extend(
            RemovalCount(transaction.entry_id, category, count)
            for category, count in sorted(counts.items()) if count
        )
    return ScrubResult(transactions=scrubbed, removals=removals)
