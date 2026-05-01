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
  LOG_LEVEL               — loguru level (default: INFO)
"""

from fastapi import FastAPI, Request, Response, status
from loguru import logger
from pydantic import BaseModel

app = FastAPI(title="fustelle-extractor", version="0.1.0")


class DriveEventPayload(BaseModel):
    """Minimal Eventarc Drive change notification shape."""
    kind: str
    fileId: str
    fileName: str | None = None  # enriched by the handler if missing


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
    raise NotImplementedError("Session 6 deliverable")


@app.post("/process", status_code=status.HTTP_202_ACCEPTED)
async def process(request: Request) -> dict:
    """
    Accept an Eventarc Drive notification, download the PDF, run the pipeline.
    Returns 202 immediately; pipeline errors are logged and written to
    run_metadata (they do not surface as 5xx so Eventarc does not retry
    infinitely on bad PDFs).
    """
    raise NotImplementedError("Session 6 deliverable")
