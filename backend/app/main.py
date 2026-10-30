"""Loopback-only local FastAPI entry point; not a production upload service.

Browser origins are explicitly limited to HTTP local Vite (5173) and backend
docs (8000), using localhost, 127.0.0.1 or [::1]. CLI requests without Origin
are allowed. No wildcard CORS, credentials, forwarded-host trust or redirects.
"""

from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware

from app.api.captures import router
from app.api.schemas import CaptureApiError, error_response
from app.services.capture_store import CaptureStore


LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
LOCAL_BROWSER_ORIGINS = frozenset(
    f"http://{host}:{port}"
    for host in ("localhost", "127.0.0.1", "[::1]") for port in (5173, 8000)
)
_API_METHODS = {"GET", "POST", "PUT", "DELETE"}


def _loopback_host(values: list[str]) -> bool:
    if len(values) != 1:
        return False
    value = values[0]
    if not value or value.endswith(":") or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        return False
    try:
        parts = urlsplit("//" + value)
        parts.port  # Validate a supplied numeric port and its range.
        return (parts.netloc == value and parts.hostname in LOOPBACK_HOSTS and parts.username is None and parts.password is None
                and not parts.path and not parts.query and not parts.fragment and "\\" not in value)
    except ValueError:
        return False


def create_app(*, capture_store: CaptureStore | None = None) -> FastAPI:
    """Create isolated application state; tests may inject a store/monotonic clock."""
    app = FastAPI(title="BugCapsule", description="Share the bug. Keep the production data.", version="0.1.0")
    app.state.capture_store = capture_store if capture_store is not None else CaptureStore()
    app.include_router(router)
    app.add_middleware(CORSMiddleware, allow_origins=sorted(LOCAL_BROWSER_ORIGINS),
                       allow_methods=sorted(_API_METHODS), allow_headers=["Content-Type"],
                       allow_credentials=False)

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        if not _loopback_host(request.headers.getlist("host")):
            return error_response("invalid_host")
        if request.method not in {"GET", "HEAD"}:
            origins = request.headers.getlist("origin")
            if origins and (len(origins) != 1 or origins[0] not in LOCAL_BROWSER_ORIGINS):
                return error_response("invalid_origin")
            if request.method == "OPTIONS" and origins and "access-control-request-method" in request.headers:
                headers = request.headers.get("access-control-request-headers", "")
                if (request.headers["access-control-request-method"] not in _API_METHODS
                        or any(header.strip().lower() not in {"", "content-type", "accept", "accept-language", "content-language"}
                               for header in headers.split(","))):
                    return error_response("invalid_request")
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(CaptureApiError)
    async def capture_error(_request: Request, error: CaptureApiError):
        return error_response(error.code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, _error: RequestValidationError):
        code = "invalid_selection" if request.method == "PUT" and request.url.path.endswith("/selection") else "invalid_request"
        return error_response(code)

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, error: HTTPException):
        return error_response({404: "not_found", 405: "method_not_allowed"}.get(error.status_code, "invalid_request"))

    @app.get("/health", tags=["System"])
    def health() -> dict[str, str]:
        """Confirm that the backend is running."""
        return {"status": "ok"}

    return app


app = create_app()
