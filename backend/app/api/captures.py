"""Bounded raw-JSON HAR imports and local capture/selection summaries.

MAX_IMPORT_BYTES is a hard 16 MiB actual-byte limit, independent of the declared
Content-Length. The parser currently takes a path: a private temporary file is
streamed and always removed, including parse, size and capacity failures.
"""

from tempfile import NamedTemporaryFile

from fastapi import APIRouter, Request, Response
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from app.api.schemas import CaptureApiError, CaptureSummary, SelectionRequest, SelectionSummary
from app.engine.har import HarParseError, parse_har
from app.services.capture_store import CaptureStore


MAX_IMPORT_BYTES = 16 * 1024 * 1024
router = APIRouter(prefix="/api/captures", tags=["Local captures"])


def _store(request: Request) -> CaptureStore:
    return request.app.state.capture_store


@router.post("", status_code=201, response_model=CaptureSummary,
             openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
                 "schema": {"type": "object", "description": "Raw UTF-8 HAR JSON, at most 16 MiB."}}}}})
async def import_capture(request: Request) -> CaptureSummary:
    media_types = request.headers.getlist("content-type")
    if (len(media_types) != 1 or media_types[0].split(";", 1)[0].strip().lower() != "application/json"
            or request.headers.get("content-encoding", "identity").lower() != "identity"):
        raise CaptureApiError("unsupported_media_type")
    try:
        with NamedTemporaryFile(mode="w+b", prefix="bugcapsule-", suffix=".har") as temporary:
            received = 0
            async for chunk in request.stream():
                received += len(chunk)
                if received > MAX_IMPORT_BYTES:
                    raise CaptureApiError("payload_too_large")
                temporary.write(chunk)
            temporary.flush()
            parsed = await run_in_threadpool(parse_har, temporary.name)
        return _store(request).add(parsed)
    except HarParseError:
        raise CaptureApiError("invalid_har") from None
    except ClientDisconnect:
        raise CaptureApiError("invalid_request") from None
    except OSError:
        raise CaptureApiError("internal_error") from None


@router.get("/{capture_id}", response_model=CaptureSummary)
def get_capture(capture_id: str, request: Request) -> CaptureSummary:
    return _store(request).summary(capture_id)


@router.put("/{capture_id}/selection", response_model=SelectionSummary)
def select_capture(capture_id: str, selection: SelectionRequest, request: Request) -> SelectionSummary:
    return _store(request).select(capture_id, selection.entry_ids)


@router.delete("/{capture_id}", status_code=204)
def delete_capture(capture_id: str, request: Request) -> Response:
    _store(request).delete(capture_id)
    return Response(status_code=204)
