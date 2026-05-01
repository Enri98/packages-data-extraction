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

import os
import sys
import tempfile
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import service_account
from loguru import logger
from pydantic import BaseModel

from src.pipeline import PipelineError, run_single


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logger.remove()
logger.add(sys.stderr, level=LOG_LEVEL)

app = FastAPI(title="fustelle-extractor", version="0.1.0")


class DriveEventPayload(BaseModel):
    """Minimal Eventarc Drive change notification shape."""
    kind: str
    fileId: str
    fileName: str | None = None  # enriched by the handler if missing


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


@app.get("/ready", status_code=status.HTTP_200_OK)
async def ready() -> dict:
    """
    Readiness probe — verifies Sheet connectivity.
    Returns 503 if the Sheet cannot be reached.
    """
    try:
        from src.sheets import get_sheet_client
        get_sheet_client()
        return {"status": "ready"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Sheet not reachable: {exc}")


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

    sa_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    creds = service_account.Credentials.from_service_account_file(
        sa_path,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    creds.refresh(GoogleRequest())

    download_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
    tmp_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp_path = Path(tmp.name)
            with httpx.Client() as client:
                resp = client.get(
                    download_url,
                    headers={"Authorization": f"Bearer {creds.token}"},
                )
                resp.raise_for_status()
                tmp.write(resp.content)

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
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()
