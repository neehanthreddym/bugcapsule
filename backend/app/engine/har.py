"""Parse and select private HAR transactions locally, without logging capture data."""

import base64
import binascii
import json
import re
from collections.abc import Sequence
from copy import deepcopy
from os import PathLike
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import JsonValue, ValidationError

from app.schemas import HarTransaction, ParsedHar, SkippedHarEntry


class HarParseError(ValueError):
    """A capture could not be parsed; messages contain no captured values."""


class HarSelectionError(ValueError):
    """A selection violates the bounded, single-origin selection contract."""


def _reject_constant(_value: str) -> None:
    raise ValueError("Non-finite numbers are not JSON.")


def _origin(url: str, entry_id: int) -> str | None:
    # urlsplit otherwise silently strips some whitespace/control characters.
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url):
        raise HarParseError(f"Entry {entry_id}: invalid request.url.")
    if "\\" in url:
        raise HarParseError(f"Entry {entry_id}: invalid request.url.")
    try:
        parts = urlsplit(url)
        if not parts.scheme:
            raise ValueError
        if parts.scheme not in {"http", "https"}:
            return None
        hostname = parts.hostname
        port = parts.port
        if not hostname or parts.netloc.endswith(":"):
            raise ValueError
        host = f"[{hostname}]" if ":" in hostname else hostname
        default_port = 80 if parts.scheme == "http" else 443
        suffix = f":{port}" if port is not None and port != default_port else ""
        return f"{parts.scheme}://{host}{suffix}"
    except ValueError:
        raise HarParseError(f"Entry {entry_id}: invalid request.url.") from None


def _json_body(
    content: dict[str, JsonValue], entry_id: int
) -> tuple[JsonValue, str | None]:
    text = content.get("text")
    if not isinstance(text, str):
        raise HarParseError(f"Entry {entry_id}: content.text must be a string for JSON.")
    encoding = content.get("encoding")
    if "encoding" in content and not isinstance(encoding, str):
        raise HarParseError(f"Entry {entry_id}: content.encoding must be a string.")
    if encoding is not None and encoding != "base64":
        return None, "unsupported content encoding"
    if encoding == "base64":
        try:
            text = base64.b64decode(text, validate=True).decode("utf-8")
        except (ValueError, binascii.Error, UnicodeError):
            raise HarParseError(
                f"Entry {entry_id}: invalid base64 or UTF-8 JSON body."
            ) from None
    try:
        return json.loads(text, parse_constant=_reject_constant), None
    except (ValueError, RecursionError):
        raise HarParseError(f"Entry {entry_id}: invalid JSON response body.") from None


def parse_har(path: str | PathLike[str]) -> ParsedHar:
    """Read a local UTF-8 HAR; fail on malformed data, record unsupported entries.

    MIME matching ignores case and parameters. Only application/json and
    application subtypes ending in +json are supported. No capture-size limit is
    imposed here; the ten-transaction cap applies only during selection.
    """
    try:
        capture = json.loads(
            Path(path).read_text(encoding="utf-8"), parse_constant=_reject_constant
        )
    except OSError:
        raise HarParseError("Could not read the local HAR file.") from None
    except (ValueError, UnicodeError, RecursionError):
        raise HarParseError("HAR file must contain valid UTF-8 JSON.") from None
    if not isinstance(capture, dict) or not isinstance(capture.get("log"), dict):
        raise HarParseError("HAR log must be an object.")
    entries = capture["log"].get("entries")
    if not isinstance(entries, list):
        raise HarParseError("HAR log.entries must be a list.")

    transactions = []
    by_origin: dict[str, list[int]] = {}
    skipped_entries = []
    for entry_id, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise HarParseError(f"Entry {entry_id}: entry must be an object.")
        request, response = entry.get("request"), entry.get("response")
        if not isinstance(request, dict) or not isinstance(response, dict):
            raise HarParseError(f"Entry {entry_id}: request and response must be objects.")
        method, url = request.get("method"), request.get("url")
        if not isinstance(method, str) or not re.fullmatch(
            r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", method
        ):
            raise HarParseError(f"Entry {entry_id}: invalid request.method.")
        if not isinstance(url, str) or not url:
            raise HarParseError(f"Entry {entry_id}: invalid request.url.")
        origin = _origin(url, entry_id)
        status = response.get("status")
        if type(status) is not int or not 100 <= status <= 599:
            raise HarParseError(f"Entry {entry_id}: invalid response.status.")
        content = response.get("content")
        if not isinstance(content, dict):
            raise HarParseError(f"Entry {entry_id}: response.content must be an object.")
        mime = content.get("mimeType")
        if not isinstance(mime, str) or not mime.strip():
            raise HarParseError(
                f"Entry {entry_id}: content.mimeType must be a nonempty string."
            )
        media_type = mime.split(";", 1)[0].strip().lower()
        if media_type != "application/json" and not re.fullmatch(
            r"application/[!#$%&'*+.^_`|~0-9a-z-]+\+json", media_type
        ):
            skipped_entries.append(
                SkippedHarEntry(entry_id=entry_id, reason="unsupported MIME type")
            )
            continue
        response_json, reason = _json_body(content, entry_id)
        if reason is None and origin is None:
            reason = "unsupported URL scheme"
        if reason is not None:
            skipped_entries.append(SkippedHarEntry(entry_id=entry_id, reason=reason))
            continue
        try:
            transaction = HarTransaction(
                entry_id=entry_id,
                method=method,
                url=url,
                origin=origin,
                status=status,
                response_json=response_json,
                original_entry=deepcopy(entry),
            )
        except (ValidationError, RecursionError):
            raise HarParseError(f"Entry {entry_id}: invalid transaction data.") from None
        transactions.append(transaction)
        by_origin.setdefault(origin, []).append(entry_id)
    return ParsedHar(
        transactions=transactions, by_origin=by_origin, skipped_entries=skipped_entries
    )


def select_transactions(
    parsed_capture: ParsedHar, entry_ids: Sequence[int]
) -> list[HarTransaction]:
    """Select 1–10 supported entries from one origin, in the requested order."""
    if isinstance(entry_ids, (str, bytes, bytearray)) or not isinstance(
        entry_ids, Sequence
    ):
        raise HarSelectionError("Selection must be a sequence of integer entry IDs.")
    if not 1 <= len(entry_ids) <= 10:
        raise HarSelectionError("Selection must contain 1–10 entry IDs.")
    if any(type(entry_id) is not int for entry_id in entry_ids):
        raise HarSelectionError("Selection entry IDs must be integers (not booleans).")
    if any(entry_id < 0 for entry_id in entry_ids):
        raise HarSelectionError("Selection entry IDs must be nonnegative.")
    if len(set(entry_ids)) != len(entry_ids):
        raise HarSelectionError("Selection entry IDs must be unique.")
    supported = {
        transaction.entry_id: transaction for transaction in parsed_capture.transactions
    }
    skipped_ids = {entry.entry_id for entry in parsed_capture.skipped_entries}
    selected = []
    for entry_id in entry_ids:
        if entry_id not in supported:
            category = "unsupported" if entry_id in skipped_ids else "unknown"
            raise HarSelectionError(f"Entry {entry_id}: {category} selection ID.")
        selected.append(supported[entry_id])
    if len({transaction.origin for transaction in selected}) != 1:
        raise HarSelectionError("Selection must reference exactly one API origin.")
    return selected
