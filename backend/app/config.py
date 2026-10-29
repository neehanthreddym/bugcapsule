"""Load backend-only configuration and environment settings."""

import os


META_API_HOST = "api.meta.ai"
META_RESPONSES_PATH = "/v1/responses"
META_MODEL = "muse-spark-1.3"
META_TIMEOUT_SECONDS = 30
META_MAX_RESPONSE_BYTES = 65_536
META_MAX_OUTPUT_TOKENS = 4096
META_REASONING_EFFORT = "low"


class ConfigError(ValueError):
    """Backend configuration is missing or malformed; never contains secrets."""


def get_meta_api_key() -> str:
    """Read META_API_KEY from the environment without loading .env files."""
    key = os.environ.get("META_API_KEY", "").strip()
    if not key:
        raise ConfigError("missing_META_API_KEY")
    if not key.isascii() or any(character.isspace() for character in key):
        raise ConfigError("invalid_key_format")
    return key
