"""
Secret loading with local-dev back-compat.

In production (Cloud Run), secrets are injected as environment variables via
``--update-secrets`` in cloudbuild.yaml.  ``get_secret`` is therefore a thin
wrapper around ``os.getenv`` — no Secret Manager client is needed (Approach A
from the design doc).

The Cloud-Run-vs-local distinction exists ONLY in ``load_service_account_info``,
where the ``GOOGLE_SERVICE_ACCOUNT_JSON`` value may be:

- A file path (local dev: .env points to a key file on disk).
- A JSON string (Cloud Run: ``--update-secrets`` injects the JSON content
  directly as the env-var value).

Both cases are handled transparently; callers always get a ``dict``.
"""

import json
import os
from pathlib import Path
from typing import Any

from loguru import logger

# Module-level cache: each secret is resolved at most once per process.
_CACHE: dict[str, str] = {}


def get_secret(name: str) -> str:
    """Return the value of *name* from the environment, caching the result.

    In Cloud Run, ``--update-secrets`` has already injected the secret value
    as an env var before this function runs, so ``os.getenv`` is sufficient.
    Locally, values come from a ``.env`` file loaded by ``python-dotenv`` at
    startup (see ``server.py`` / ``__main__.py``).

    Returns an empty string when the variable is unset — callers that require
    a non-empty value (e.g. the Gemini API key) must check and raise explicitly.
    """
    if name in _CACHE:
        return _CACHE[name]
    value = os.getenv(name, "")
    # Only cache non-empty values. Caching "" would poison the cache when a
    # missing secret is later mounted (e.g. a delayed Cloud Run secret binding,
    # or a `/ready` probe firing before --update-secrets has propagated).
    if value:
        _CACHE[name] = value
    logger.debug("Secret loaded | name={} | present={}", name, bool(value))
    return value


def load_service_account_info(env_var: str = "GOOGLE_SERVICE_ACCOUNT_JSON") -> dict[str, Any]:
    """Parse service-account credentials from *env_var* and return a dict.

    Handles two formats transparently:

    1. **File path** (local dev): the env-var value is a path to a JSON key
       file on disk.  Read the file and parse it.
    2. **JSON string** (Cloud Run): ``--update-secrets`` injects the raw JSON
       content.  Parse it directly.

    Raises:
        ValueError: if the env var is unset, the referenced file doesn't exist,
            or the value cannot be parsed as JSON.
    """
    # Read directly from os.getenv (not the cache) so that test monkeypatching
    # and runtime credential rotation are reflected immediately.
    raw = os.getenv(env_var, "")
    if not raw:
        raise ValueError(
            f"{env_var} is not set. "
            "Set it to a service account JSON key file path (local) "
            "or the JSON content string (Cloud Run via --update-secrets)."
        )

    # Back-compat: if the value looks like a file path that exists, read it.
    candidate = Path(raw)
    if candidate.exists() and candidate.is_file():
        logger.debug("Loading service account from file | path={}", raw)
        try:
            return json.loads(candidate.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Service account file at {raw!r} is not valid JSON: {exc}") from exc

    # Otherwise treat as inline JSON content (Cloud Run injection path).
    logger.debug("Parsing service account from inline JSON content")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{env_var} is neither a valid file path nor parseable JSON: {exc}"
        ) from exc
