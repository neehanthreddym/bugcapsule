"""Private in-memory captures, at most five for 30 minutes from import.

TTL is absolute, measured with a monotonic clock; access does not extend it.
Expired captures are purged on store operations. Active captures are never
evicted to make room. Locks make capacity and selection changes atomic across
request threads. Each application receives a new store; clear() resets it.
No raw data is logged, written, or returned in summary DTOs.
"""

from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass
import secrets
from threading import RLock
import time
from urllib.parse import urlsplit

from app.api.schemas import (
    CaptureApiError, CaptureSummary, OriginGroup, SelectionSummary,
    SkippedEntrySummary, TransactionSummary,
)
from app.engine.har import HarSelectionError, select_transactions
from app.schemas import ParsedHar


MAX_CAPTURES = 5
CAPTURE_TTL_SECONDS = 30 * 60
_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"})


@dataclass(repr=False)
class _Capture:
    parsed: ParsedHar
    expires_at: float
    entry_ids: tuple[int, ...] = ()


class CaptureStore:
    """Own private parsed data; methods return independently constructed DTOs."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._captures: dict[str, _Capture] = {}
        self._lock = RLock()

    def clear(self) -> None:
        with self._lock:
            self._captures.clear()

    def _purge(self) -> None:
        now = self._clock()
        expired = [capture_id for capture_id, capture in self._captures.items() if capture.expires_at <= now]
        for capture_id in expired:
            del self._captures[capture_id]

    def _get(self, capture_id: str) -> _Capture:
        self._purge()
        if capture_id not in self._captures:
            raise CaptureApiError("capture_not_found")
        return self._captures[capture_id]

    @staticmethod
    def _selection(capture: _Capture) -> SelectionSummary:
        selected = select_transactions(capture.parsed, capture.entry_ids) if capture.entry_ids else []
        return SelectionSummary(entry_ids=list(capture.entry_ids), count=len(selected),
                                origin=selected[0].origin if selected else None)

    def _summary(self, capture_id: str, capture: _Capture) -> CaptureSummary:
        parsed = capture.parsed
        return CaptureSummary(
            capture_id=capture_id,
            entry_count=len(parsed.transactions) + len(parsed.skipped_entries),
            supported_count=len(parsed.transactions), skipped_count=len(parsed.skipped_entries),
            transactions=[TransactionSummary(
                entry_id=tx.entry_id, method=tx.method if tx.method in _METHODS else "OTHER",
                status=tx.status, origin=tx.origin, display_path=urlsplit(tx.url).path or "/",
            ) for tx in parsed.transactions],
            origin_groups=[OriginGroup(origin=origin, entry_ids=list(ids)) for origin, ids in parsed.by_origin.items()],
            skipped_entries=[SkippedEntrySummary(entry_id=entry.entry_id, reason=entry.reason)
                             for entry in parsed.skipped_entries],
            selection=self._selection(capture),
        )

    def add(self, parsed: ParsedHar) -> CaptureSummary:
        with self._lock:
            self._purge()
            if len(self._captures) >= MAX_CAPTURES:
                raise CaptureApiError("capture_capacity")
            capture_id = secrets.token_urlsafe(24)
            while capture_id in self._captures:
                capture_id = secrets.token_urlsafe(24)
            capture = _Capture(deepcopy(parsed), self._clock() + CAPTURE_TTL_SECONDS)
            summary = self._summary(capture_id, capture)
            self._captures[capture_id] = capture
            return summary

    def summary(self, capture_id: str) -> CaptureSummary:
        with self._lock:
            return self._summary(capture_id, self._get(capture_id))

    def select(self, capture_id: str, entry_ids: Sequence[int]) -> SelectionSummary:
        with self._lock:
            capture = self._get(capture_id)
            try:
                selected = select_transactions(capture.parsed, entry_ids)
            except HarSelectionError:
                raise CaptureApiError("invalid_selection") from None
            # Commit only after the real parser's selection validation succeeds.
            capture.entry_ids = tuple(tx.entry_id for tx in selected)
            return self._selection(capture)

    def delete(self, capture_id: str) -> None:
        with self._lock:
            self._get(capture_id)
            del self._captures[capture_id]
