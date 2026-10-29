"""Explicit, isolated Meta access check; never load local data or the app pipeline.

Request/response format: https://dev.meta.ai/docs/quickstart
Uses META_API_KEY instead of the documentation's MODEL_API_KEY environment name;
both supply the same Bearer authentication. No .env loading, retries, or redirects.
"""

import http.client
import json
import os
import sys


MODEL = "muse-spark-1.3"
PROMPT = "Synthetic connectivity test. Reply only with: BugCapsule connection OK."
TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 65_536


def _error(category: str, status: int | None = None) -> int:
    suffix = f" (HTTP {status})" if status is not None else ""
    print(f"Error: {category}{suffix}", file=sys.stderr)
    return 1


def _acknowledgment(payload: dict) -> str:
    texts = []
    output = payload.get("output", [])
    if not isinstance(output, list):
        return ""
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        if item.get("role") != "assistant":
            continue
        content = item.get("content", [])
        if not isinstance(content, list):
            continue
        for part in content:
            if (
                isinstance(part, dict)
                and part.get("type") == "output_text"
                and isinstance(part.get("text"), str)
            ):
                texts.append(part["text"])
    return " ".join(" ".join(texts).split())


def main() -> int:
    key = os.environ.get("META_API_KEY", "").strip()
    if not key:
        return _error("missing_META_API_KEY")
    if not key.isascii() or any(character.isspace() for character in key):
        return _error("invalid_key_format")

    connection = None
    try:
        connection = http.client.HTTPSConnection("api.meta.ai", timeout=TIMEOUT_SECONDS)
        connection.request(
            "POST",
            "/v1/responses",
            body=json.dumps({"model": MODEL, "input": PROMPT}).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        response = connection.getresponse()
        if not 200 <= response.status < 300:
            return _error("http_error", response.status)
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            return _error("response_too_large")
        payload = json.loads(body)
        if not isinstance(payload, dict) or payload.get("status") != "completed":
            return _error("unexpected_response")
        model = payload.get("model")
        acknowledgment = _acknowledgment(payload)
        if not isinstance(model, str) or not model or len(model) > 128 or not acknowledgment:
            return _error("unexpected_response")
        # Print only the requested fields, bounded and terminal-escaped. Even an
        # unexpected echoed key is redacted before any response text is printed.
        print(json.dumps({
            "status": "success",
            "model": model.replace(key, "[redacted]"),
            "acknowledgment": acknowledgment.replace(key, "[redacted]")[:160],
        }))
        return 0
    except TimeoutError:
        return _error("timeout")
    except (OSError, http.client.HTTPException):
        return _error("network_or_TLS_error")
    except (ValueError, TypeError, RecursionError):
        return _error("invalid_request_or_response")
    except KeyboardInterrupt:
        return _error("interrupted")
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
