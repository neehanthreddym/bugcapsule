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
import hashlib
from threading import RLock
import time
from urllib.parse import urlsplit

from app.api.schemas import (
    CaptureApiError, CaptureSummary, ModelPreview, OriginGroup, PreviewMetadata,
    SchemaLabelChoices, SelectionSummary, SkippedEntrySummary, TransactionSummary,
)
from app.engine.har import HarSelectionError, select_transactions
from app.schemas import ParsedHar
from app.privacy import (
    FailurePattern, PreparedPayload, PrivacyError, available_schema_labels, prepare_model_payload,
)


MAX_CAPTURES = 5
CAPTURE_TTL_SECONDS = 30 * 60
_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"})


@dataclass(frozen=True, repr=False)
class _Preview:
    """Local snapshot and its preparation context; this grants no approval."""

    preview_id: str
    entry_ids: tuple[int, ...]
    failure_pattern: FailurePattern
    approved_labels: frozenset[str]
    prepared: PreparedPayload


@dataclass(repr=False)
class _Capture:
    parsed: ParsedHar
    expires_at: float
    entry_ids: tuple[int, ...] = ()
    labels: tuple[str, ...] | None = None
    preview: _Preview | None = None


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
            next_ids = tuple(tx.entry_id for tx in selected)
            if next_ids != capture.entry_ids:
                capture.labels = None
                capture.preview = None
            capture.entry_ids = next_ids
            return self._selection(capture)

    @staticmethod
    def _selected(capture: _Capture):
        if not capture.entry_ids:
            raise CaptureApiError("selection_required")
        return select_transactions(capture.parsed, capture.entry_ids)

    def _labels(self, capture: _Capture) -> tuple[str, ...]:
        if capture.labels is None:
            try:
                capture.labels = available_schema_labels(self._selected(capture))
            except PrivacyError:
                raise CaptureApiError("label_choices_unavailable") from None
        return capture.labels

    def schema_labels(self, capture_id: str) -> SchemaLabelChoices:
        with self._lock:
            capture = self._get(capture_id)
            labels = self._labels(capture)
            self._get(capture_id)
            return SchemaLabelChoices(labels=list(labels))

    def prepare_preview(self, capture_id: str, failure_pattern: FailurePattern,
                        approved_labels: Sequence[str]) -> ModelPreview:
        with self._lock:
            capture = self._get(capture_id)
            selected = self._selected(capture)
            capture.preview = None
            choices = self._labels(capture)
            if (len(set(approved_labels)) != len(approved_labels)
                    or not set(approved_labels) <= set(choices)):
                raise CaptureApiError("invalid_preview_settings")
            approved = frozenset(approved_labels)
            try:
                prepared = prepare_model_payload(selected, failure_pattern, approved_labels=approved)
            except PrivacyError:
                raise CaptureApiError("preview_unavailable") from None
            # A large preparation must not revive a capture that expired during it.
            self._get(capture_id)
            snapshot = prepared.preview_json
            encoded = snapshot.encode("utf-8")
            metadata = prepared.payload["metadata"]
            preview_id = secrets.token_urlsafe(24)
            result = ModelPreview(
                preview_id=preview_id, preview_json=snapshot, byte_count=len(encoded),
                sha256=hashlib.sha256(encoded).hexdigest(),
                metadata=PreviewMetadata(
                    examples_truncated=metadata["examples_truncated"], examples_omitted=metadata["examples_omitted"],
                    max_examples_per_node=metadata["max_examples_per_node"], max_examples_total=metadata["max_examples_total"],
                ),
            )
            capture.preview = _Preview(preview_id, capture.entry_ids, failure_pattern, approved, prepared)
            return result

    def delete(self, capture_id: str) -> None:
        with self._lock:
            self._get(capture_id)
            del self._captures[capture_id]
