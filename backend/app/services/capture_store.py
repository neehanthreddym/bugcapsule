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
    CaptureApiError, CaptureSummary, ModelPreview, OriginGroup, PlanningDiagnostics, PreviewMetadata, ProposalReview,
    SchemaLabelChoices, SelectionSummary, SkippedEntrySummary, TransactionSummary,
)
from app.engine.har import HarSelectionError, select_transactions
from app.schemas import ParsedHar
from app.privacy import (
    FailurePattern, PreparedPayload, PrivacyError, available_schema_labels, prepare_model_payload,
)
from app.agent.client import Transport
from app.agent.execution import ExecutionSession
from app.agent.workflow import ValidatedProposal, approve_preview
from app.config import ConfigError, get_meta_api_key
from app.services.planning import planning_failure, proposal_review


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
class _Planning:
    preview: _Preview
    sha256: str
    session: ExecutionSession
    proposal: ValidatedProposal | None = None
    proposal_id: str | None = None


@dataclass(repr=False)
class _Capture:
    parsed: ParsedHar
    expires_at: float
    entry_ids: tuple[int, ...] = ()
    labels: tuple[str, ...] | None = None
    preview: _Preview | None = None
    planning: _Planning | None = None
    # One initial submission per capture, even after failed requests or preview
    # regeneration. Future revision must use the retained session's budget.
    planning_consumed: bool = False


class CaptureStore:
    """Own private parsed data; methods return independently constructed DTOs."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 planning_transport: Transport | None = None, key_provider: Callable[[], str] | None = None):
        self._clock = clock
        self._planning_transport = planning_transport
        self._key_provider = key_provider if key_provider is not None else get_meta_api_key
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
                capture.planning = None
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
            capture.planning = None
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

    def request_plan(self, capture_id: str, preview_id: str, sha256: str, approved: bool) -> ProposalReview:
        """Reserve once atomically, call the existing session outside the store lock.

        This synchronous method is called by a FastAPI worker thread, not the
        async event loop. Configuration is checked only after approval/context
        validation, before reservation or request accounting. Failed attempted
        requests consume the capture's submission; changing/preparing a preview
        cannot create another initial request. Stale completions are discarded.
        """
        with self._lock:
            capture = self._get(capture_id)
            if approved is not True:
                raise CaptureApiError("approval_required")
            preview = capture.preview
            if (preview is None or preview.preview_id != preview_id or preview.entry_ids != capture.entry_ids
                    or hashlib.sha256(preview.prepared.preview_json.encode("utf-8")).hexdigest() != sha256):
                raise CaptureApiError("stale_preview")
            if capture.planning_consumed:
                session = capture.planning.session if capture.planning is not None else None
                attempts = session.attempts if session is not None else 1
                raise CaptureApiError("planning_consumed", diagnostics=PlanningDiagnostics(
                    attempts=attempts, request_attempted=attempts > 0))
            try:
                key = self._key_provider()
            except ConfigError:
                raise CaptureApiError("missing_meta_configuration", diagnostics=PlanningDiagnostics()) from None
            try:
                session = ExecutionSession.from_prepared(
                    self._selected(capture), preview.prepared, preview.failure_pattern, approved_labels=preview.approved_labels,
                )
            except (ValueError, TypeError, RecursionError):
                raise CaptureApiError("stale_preview") from None
            self._get(capture_id)  # Local validation must not revive an expired capture.
            reservation = _Planning(preview, sha256, session)
            capture.planning = reservation
            capture.planning_consumed = True

        started = time.perf_counter()
        failure_code, review, proposal = None, None, None
        try:
            proposal = session.request_proposal(approve_preview(preview.prepared), api_key=key,
                                                transport=self._planning_transport)
            diagnostics = PlanningDiagnostics(attempts=session.attempts, request_attempted=session.attempts > 0,
                                               latency_ms=max(0, int((time.perf_counter() - started) * 1000)))
            review = proposal_review(proposal, preview.prepared, proposal_id=secrets.token_urlsafe(24),
                                     preview_id=preview_id, sha256=sha256, diagnostics=diagnostics)
        except Exception as error:
            # The API boundary never propagates unexpected transport/provider
            # prose or exception dumps into responses or server tracebacks.
            diagnostics = PlanningDiagnostics(attempts=session.attempts, request_attempted=session.attempts > 0,
                                               latency_ms=max(0, int((time.perf_counter() - started) * 1000)))
            failure_code = planning_failure(error, diagnostics)

        with self._lock:
            current = self._get(capture_id)
            if (current is not capture or current.preview is not preview or current.planning is not reservation
                    or current.entry_ids != preview.entry_ids
                    or hashlib.sha256(preview.prepared.preview_json.encode("utf-8")).hexdigest() != sha256):
                session.cancel()
                raise CaptureApiError("stale_preview", diagnostics=diagnostics)
            if failure_code is not None:
                session.cancel()
                raise CaptureApiError(failure_code, diagnostics=diagnostics)
            reservation.proposal = proposal
            reservation.proposal_id = review.proposal_id
            return review

    def delete(self, capture_id: str) -> None:
        with self._lock:
            self._get(capture_id)
            del self._captures[capture_id]
