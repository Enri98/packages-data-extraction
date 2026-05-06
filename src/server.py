"""
FastAPI HTTP server for Cloud Run deployment.

Responsibilities:
- POST /process — accepts an Eventarc Drive notification (CloudEvent),
  downloads the PDF from Drive, and runs the pipeline.
- GET  /health  — liveness probe (Cloud Run requires a fast 200 response).
- GET  /ready   — readiness probe (checks Sheet connectivity).
- Validate incoming Eventarc payloads via Pydantic request models.
- Add a correlation ID (X-Request-ID) to every log record for Cloud Logging.
- Return 202 Accepted for /process so Cloud Run does not time-out while
  the pipeline runs (actual processing happens synchronously within the
  request for v1; async job queue is a roadmap item).

Eventarc Drive notification payload (abridged):
  {
    "kind": "drive#change",
    "fileId": "<Drive file ID>",
    "file": { "name": "<filename>.pdf", "mimeType": "application/pdf" }
  }

Environment variables consumed:
  GOOGLE_DRIVE_FOLDER_ID  — only process files from this folder
  GOOGLE_SHEET_ID         — passed through to pipeline
  GOOGLE_SERVICE_ACCOUNT_JSON — path to service account key for Drive download
  LOG_LEVEL               — loguru level (default: INFO)
"""

import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

# Load .env before any module reads os.environ. No-op in production where
# env vars are injected by Cloud Run / Secret Manager. Imports below MUST
# stay below this call — they pull in modules that read os.environ at import.
load_dotenv()
from fastapi import FastAPI, HTTPException, Request, status  # noqa: E402
from google.auth.transport.requests import Request as GoogleRequest  # noqa: E402
from google.oauth2 import service_account  # noqa: E402
from loguru import logger  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from src.pipeline import PipelineError, run_single  # noqa: E402
from src.secrets import get_secret, load_service_account_info  # noqa: E402

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Map loguru level names to Cloud Logging severity strings.
_SEVERITY_MAP: dict[str, str] = {
    "TRACE": "DEBUG",
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "SUCCESS": "INFO",
    "WARNING": "WARNING",
    "ERROR": "ERROR",
    "CRITICAL": "CRITICAL",
}


def _make_json_sink(stream: Any) -> Any:
    """Return a loguru sink callable that writes single-line JSON to *stream*.

    Using a sink function (rather than a ``format`` callable) avoids loguru's
    internal ``str.format_map`` pass, which would attempt to substitute
    ``{severity}`` etc. as format keys and crash on every log call.

    The ``severity`` field is what Cloud Logging uses for log-level filtering.
    Any extra context bound with ``logger.contextualize(...)`` (e.g.
    ``request_id`` set by the correlation-ID middleware) is included at the
    top level of the JSON object.
    """

    def _sink(message: Any) -> None:
        record = message.record
        severity = _SEVERITY_MAP.get(record["level"].name, "DEFAULT")
        payload: dict = {
            "severity": severity,
            "message": record["message"],
            "timestamp": record["time"].isoformat(),
            "logger": record["name"],
            "function": record["function"],
            "line": record["line"],
        }
        # Spread any contextualize() extras (e.g. request_id) into the top level.
        payload.update(record.get("extra", {}))
        stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
        stream.flush()

    return _sink


logger.remove()
if os.getenv("K_SERVICE"):
    # Cloud Run: emit structured JSON to stderr for Cloud Logging ingestion.
    logger.add(_make_json_sink(sys.stderr), level=LOG_LEVEL, colorize=False)
else:
    # Local dev: human-readable colorized output.
    logger.add(sys.stderr, level=LOG_LEVEL)

app = FastAPI(title="fustelle-extractor", version="0.1.0")


class DriveEventPayload(BaseModel):
    """Minimal Eventarc Drive change notification shape."""

    kind: str
    fileId: str
    fileName: str | None = None  # enriched by the handler if missing


@app.on_event("startup")
async def _startup_verify_sheet() -> None:
    """Verify the Sheet header at startup.

    Skipped when GOOGLE_SHEET_ID is unset (local CLI dry-run / CI).

    Failures are logged at CRITICAL but do NOT re-raise — a transient Sheets
    API blip at boot must not permanently kill the Cloud Run instance. The
    `/ready` probe will continue to fail (it also calls into the Sheet client),
    so Cloud Run will not route traffic until the API recovers, but the
    process itself stays alive and re-tries on the next probe cycle.
    """
    sheet_id = os.getenv("GOOGLE_SHEET_ID", "")
    if not sheet_id:
        logger.info("GOOGLE_SHEET_ID not set — skipping Sheet schema verification.")
        return
    try:
        from src.sheets import verify_sheet_schema

        verify_sheet_schema(sheet_id)
    except Exception as exc:
        logger.critical(
            "Sheet schema verification failed at startup: {} — server will boot "
            "but /ready will return 503 until the Sheets API is reachable.",
            exc,
        )


@app.middleware("http")
async def add_correlation_id(request: Request, call_next):
    correlation_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    with logger.contextualize(request_id=correlation_id):
        response = await call_next(request)
    response.headers["X-Request-ID"] = correlation_id
    return response


@app.get("/health", status_code=status.HTTP_200_OK)
async def health() -> dict:
    """Liveness probe — always returns 200 if the process is alive."""
    return {"status": "ok"}


# Module-level flag to avoid re-paying the OCR engine cold-start on each probe.
# Read-then-write is non-atomic but `_get_engine()` is itself an idempotent
# singleton, so concurrent /ready calls before warm-up only race to set the
# flag — no double-init. If a non-idempotent warm-up is added later, wrap
# this in a threading.Lock.
_ocr_engine_warmed: bool = False


@app.get("/ready", status_code=status.HTTP_200_OK)
async def ready() -> dict:
    """
    Readiness probe.

    Checks (in order):
    1. GEMINI_API_KEY is non-empty (does NOT make a Gemini API call).
    2. OCR engine can be loaded (warm it once; subsequent calls are no-ops).
    3. Sheet connectivity (authenticates via service account).

    Returns 503 with the name of the failing check if any check fails.
    """
    global _ocr_engine_warmed

    # 1. Gemini API key presence.
    if not get_secret("GEMINI_API_KEY"):
        raise HTTPException(status_code=503, detail="gemini_api_key: not set")

    # 2. OCR engine warm-up (paid once; ~1.6 s cold-start).
    if not _ocr_engine_warmed:
        try:
            from src.ocr import _get_engine

            _get_engine()
            _ocr_engine_warmed = True
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"ocr_engine: {exc}")

    # 3. Sheet connectivity.
    try:
        from src.sheets import get_sheet_client

        get_sheet_client()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"sheet: {exc}")

    return {"status": "ready"}


@app.post("/process", status_code=status.HTTP_202_ACCEPTED)
async def process(request: Request) -> dict:
    """
    Accept an Eventarc Drive notification, download the PDF, run the pipeline.
    Returns 202 immediately; pipeline errors are logged and written to
    run_metadata (they do not surface as 5xx so Eventarc does not retry
    infinitely on bad PDFs).
    """
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {exc}")

    file_id = payload.get("fileId")
    file_name = payload.get("file", {}).get("name") or payload.get("fileName", "")

    if not file_id:
        return {"status": "ignored", "reason": "missing fileId"}

    folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
    if folder_id and payload.get("folderId") != folder_id:
        return {"status": "ignored", "reason": "file not in watched folder"}

    if not file_name.endswith(".pdf"):
        return {"status": "ignored", "reason": "not a PDF"}

    try:
        sa_info = load_service_account_info("GOOGLE_SERVICE_ACCOUNT_JSON")
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=f"Service account config error: {exc}")
    creds = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    creds.refresh(GoogleRequest())

    download_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
    # Preserve the original filename: the parser extracts EAN/dimensions/product
    # name from the basename, so a tempfile-style "tmpXXXX.pdf" name would crash
    # filename_parse. Write into a fresh temp directory under the real name.
    tmp_dir = Path(tempfile.mkdtemp(prefix="docsproc-"))
    tmp_path = tmp_dir / file_name

    try:
        with httpx.Client() as client:
            resp = client.get(
                download_url,
                headers={"Authorization": f"Bearer {creds.token}"},
            )
            resp.raise_for_status()
            tmp_path.write_bytes(resp.content)

        try:
            pack = run_single(tmp_path)
            return {"status": "accepted", "ean": pack.codice_ean}
        except PipelineError as exc:
            logger.error(
                "Pipeline failed | file={} | stage={} | error={}",
                file_name,
                exc.stage,
                exc,
            )
            return {"status": "accepted", "ean": None}

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
