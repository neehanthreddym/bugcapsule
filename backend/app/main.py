"""FastAPI entry point for BugCapsule."""

from fastapi import FastAPI

app = FastAPI(
    title="BugCapsule",
    description="Share the bug. Keep the production data.",
    version="0.1.0",
)


@app.get("/health", tags=["System"])
def health() -> dict[str, str]:
    """Confirm that the backend is running."""
    return {"status": "ok"}