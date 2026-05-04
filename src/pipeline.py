"""
End-to-end orchestration of the extraction pipeline.

Responsibilities:
- Accept a PDF file path as input (Drive download handled by caller in
  server mode).
- Coordinate: parsing → VLM extraction → validation → Sheet write.
- Pass typed Pydantic objects between stages; never use raw dicts between
  modules.
- Enforce idempotency at the pipeline level (delegates the EAN check to
  sheets.py before incurring any VLM cost).
- Support both single-file (sync) and batch (async + asyncio.Semaphore)
  execution modes.
- Emit structured loguru events at every stage boundary:
    {stage, filename, ean, latency_ms, outcome}
- Propagate typed exceptions upward; never swallow errors silently.
- Record run metadata to Sheet on both success and failure.

Not responsible for:
- HTTP request handling (delegated to server.py).
- The logic of any individual stage (each stage owns its own module).

Environment variables consumed:
  GOOGLE_SHEET_ID      — target Sheet ID
  MAX_CONCURRENT_PDFS  — semaphore limit for batch mode (default: 3)
"""

import asyncio
import os
import time
from pathlib import Path

from loguru import logger

from src.ocr import extract_ocr_text
from src.parsing import extract_text_fields, parse_filename
from src.schemas.pack import PackData
from src.sheets import has_ean_been_processed, write_pack, write_run_metadata
from src.validator import validate
from src.vlm import extract_visual_fields


MAX_CONCURRENT_PDFS = int(os.getenv("MAX_CONCURRENT_PDFS", "3"))


def run_single(pdf_path: Path) -> PackData:
    """
    Process one PDF end-to-end (blocking). Returns the final PackData.
    Raises PipelineError on unrecoverable failure.
    """
    t0 = time.perf_counter()
    sheet_id = os.getenv("GOOGLE_SHEET_ID", "")

    try:
        ean, _, _ = parse_filename(pdf_path.name)
    except ValueError as exc:
        raise PipelineError(pdf_path.name, "filename_parse", exc) from exc

    # Idempotency: skip the heavy work (OCR + Gemini, ~$0.05) when a previous
    # run already wrote a successful row for this EAN. Critical when overlapping
    # Apps Script triggers POST the same file before the first request finishes.
    # Only enforced when sheet_id is set (skipped in CLI/CI dry-runs).
    if sheet_id and has_ean_been_processed(sheet_id, ean):
        logger.info(
            "Skipping pipeline | ean={} | reason=already in pack_data via run_metadata",
            ean,
        )
        return PackData(codice_ean=ean)

    logger.info("Pipeline start | ean={} | file={}", ean, pdf_path.name)

    try:
        try:
            parser_output = extract_text_fields(pdf_path)
        except Exception as exc:
            raise PipelineError(pdf_path.name, "parsing", exc) from exc
        logger.info("Parsing done | ean={}", ean)

        try:
            # OCR is path-cached in src.ocr; if parsing already triggered it
            # this is a free lookup, otherwise it pays the OCR cost once.
            ocr_text = extract_ocr_text(pdf_path)
            vlm_output = extract_visual_fields(
                pdf_path, parser_output, ocr_text=ocr_text
            )
        except Exception as exc:
            raise PipelineError(pdf_path.name, "vlm", exc) from exc
        logger.info("VLM done | ean={}", ean)

        result = validate(parser_output, vlm_output)
        logger.info(
            "Validation done | ean={} | overall_confidence={:.2f} | needs_review={}",
            ean,
            result.overall_confidence,
            result.needs_review,
        )

        if sheet_id:
            write_pack(result, sheet_id)
            latency_ms = int((time.perf_counter() - t0) * 1000)
            write_run_metadata(pdf_path.name, "success", latency_ms, None, sheet_id)
            logger.info("Sheet write done | ean={}", ean)

        latency_ms = int((time.perf_counter() - t0) * 1000)
        logger.info("Pipeline complete | ean={} | latency_ms={}", ean, latency_ms)
        return result.pack

    except PipelineError as exc:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        if sheet_id:
            write_run_metadata(pdf_path.name, "error", latency_ms, str(exc), sheet_id)
        raise


async def run_batch(pdf_paths: list[Path]) -> list[PackData | Exception]:
    """
    Process a list of PDFs concurrently, bounded by MAX_CONCURRENT_PDFS.
    Returns results in input order; failed items are returned as exceptions,
    not raised, so the batch continues on partial failure.
    """
    logger.info(
        "Batch start | count={} | max_concurrent={}", len(pdf_paths), MAX_CONCURRENT_PDFS
    )

    sem = asyncio.Semaphore(MAX_CONCURRENT_PDFS)

    async def _run_one(path: Path) -> PackData | Exception:
        async with sem:
            loop = asyncio.get_running_loop()
            try:
                return await loop.run_in_executor(None, run_single, path)
            except Exception as exc:
                logger.error("Batch item failed | file={} | error={}", path.name, exc)
                return exc

    tasks = [_run_one(p) for p in pdf_paths]
    return await asyncio.gather(*tasks)


class PipelineError(Exception):
    """Raised when a pipeline run fails unrecoverably after retries."""

    def __init__(self, filename: str, stage: str, cause: Exception) -> None:
        super().__init__(f"{stage} failed for {filename}: {cause}")
        self.filename = filename
        self.stage = stage
        self.cause = cause
