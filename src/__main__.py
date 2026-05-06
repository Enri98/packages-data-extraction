"""
CLI dev runner for the fustelle-extractor pipeline.

Usage:
    python -m src <pdf_path> [--write] [--no-cache]
    python -m src --verify-sheet

By default the runner is a dry-run (no Sheet write) and uses a cached VLM
response when one is available under eval_vlm_cache/<stem>__vlm-image+ocr.json.

Flags:
    --write         Actually write the result to Google Sheets.
                    Requires GOOGLE_SHEET_ID and GOOGLE_SERVICE_ACCOUNT_JSON to
                    be set in the environment / .env file.
    --use-cache     (default: True) Use cached VLM response when available.
    --no-cache      Force a live Gemini call (requires GEMINI_LIVE=1 and
                    GEMINI_API_KEY; will incur API cost).
    --verify-sheet  Check that the pack_data Sheet header matches the Pydantic
                    schema. Exits 0 on match (or when GOOGLE_SHEET_ID is unset),
                    exits 1 on mismatch.

Exit codes:
    0  success
    1  pipeline error / schema mismatch
    2  bad arguments
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

from dotenv import load_dotenv
from loguru import logger

# Load .env before anything reads os.environ — mirrors server.py:36-40.
load_dotenv()


def _usage() -> None:
    print(
        "Usage: python -m src <pdf_path> [--write] [--no-cache]",
        file=sys.stderr,
    )


def _load_cached_vlm(pdf_path: Path) -> "PackData | None":  # noqa: F821
    """
    Return a PackData parsed from the cached VLM response file, or None if no
    cache exists for this PDF.

    Cache files are stored as:
        eval_vlm_cache/<stem>__vlm-image+ocr.json
    """
    from src.schemas.pack import PackData

    cache_dir = Path("eval_vlm_cache")
    cache_file = cache_dir / f"{pdf_path.stem}__vlm-image+ocr.json"
    if not cache_file.exists():
        logger.debug("No VLM cache found at {}", cache_file)
        return None

    logger.info("Loading cached VLM response from {}", cache_file)
    with cache_file.open(encoding="utf-8") as fh:
        cached = json.load(fh)

    return PackData.model_validate(cached["vlm_pack"])


def _run_verify_sheet() -> None:
    """Run the Sheet schema verification and exit 0/1."""
    sheet_id = os.environ.get("GOOGLE_SHEET_ID", "")
    if not sheet_id:
        logger.info("GOOGLE_SHEET_ID not set — skipping Sheet schema verification (exit 0).")
        sys.exit(0)
    from src.sheets import SheetConfigError, verify_sheet_schema

    try:
        verify_sheet_schema(sheet_id)
        sys.exit(0)
    except SheetConfigError as exc:
        logger.error("Sheet schema verification failed: {}", exc)
        sys.exit(1)
    except Exception as exc:
        logger.error("Unexpected error during Sheet schema verification: {}", exc)
        sys.exit(1)


def main() -> None:
    args = sys.argv[1:]

    if args and args[0] == "--verify-sheet":
        _run_verify_sheet()
        return  # _run_verify_sheet calls sys.exit; this is unreachable

    if not args or args[0].startswith("-"):
        _usage()
        sys.exit(2)

    pdf_path = Path(args[0])
    remaining = args[1:]

    if "--write" in remaining and "--no-cache" not in remaining:
        use_cache = True
    elif "--no-cache" in remaining:
        use_cache = False
    else:
        use_cache = True  # default

    do_write = "--write" in remaining

    unknown = [a for a in remaining if a not in {"--write", "--use-cache", "--no-cache"}]
    if unknown:
        print(f"Unknown flags: {unknown}", file=sys.stderr)
        _usage()
        sys.exit(2)

    if not pdf_path.exists():
        print(f"File not found: {pdf_path}", file=sys.stderr)
        sys.exit(2)

    # Dry-run: hide GOOGLE_SHEET_ID so pipeline.run_single skips Sheet write.
    # pipeline.py line 87: `if sheet_id:` — empty string → skip.
    original_sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    if not do_write:
        os.environ.pop("GOOGLE_SHEET_ID", None)

    try:
        if not use_cache:
            logger.warning(
                "--no-cache requested: a live Gemini call will be made. "
                "This requires GEMINI_LIVE=1 and GEMINI_API_KEY and WILL incur API cost."
            )
            _run_pipeline(pdf_path)
        else:
            cached_pack = _load_cached_vlm(pdf_path)
            if cached_pack is None:
                logger.warning(
                    "No VLM cache found for {}; falling back to live Gemini call. "
                    "Set GEMINI_LIVE=1 and GEMINI_API_KEY or add a cache file.",
                    pdf_path.name,
                )
                _run_pipeline(pdf_path)
            else:
                _run_pipeline_with_cache(pdf_path, cached_pack)
    except SystemExit:
        raise
    finally:
        # Restore the original env var so we don't mutate the caller's env.
        if not do_write:
            if original_sheet_id is not None:
                os.environ["GOOGLE_SHEET_ID"] = original_sheet_id
            else:
                os.environ.pop("GOOGLE_SHEET_ID", None)


def _run_pipeline(pdf_path: Path) -> None:
    """Run the pipeline without any patching (live VLM or gate check will apply)."""
    from src.pipeline import PipelineError, run_single
    from src.schemas.pack import PackData

    try:
        pack: PackData = run_single(pdf_path)
    except PipelineError as exc:
        logger.error("Pipeline failed: {}", exc)
        sys.exit(1)

    _print_result(pack)


def _run_pipeline_with_cache(pdf_path: Path, cached_pack: "PackData") -> None:  # noqa: F821
    """Run the pipeline with extract_visual_fields monkey-patched to the cache."""
    from src.pipeline import PipelineError, run_single

    with patch("src.pipeline.extract_visual_fields", return_value=cached_pack):
        try:
            pack = run_single(pdf_path)
        except PipelineError as exc:
            logger.error("Pipeline failed: {}", exc)
            sys.exit(1)

    _print_result(pack)


def _print_result(pack: "PackData") -> None:  # noqa: F821
    """Serialize PackData to pretty JSON and write to stdout (UTF-8 safe)."""
    json_str = pack.model_dump_json(indent=2)
    # Write directly to the stdout buffer to avoid Windows cp1252 codec issues
    # when the JSON contains non-BMP characters (e.g. ə in marketing copy).
    sys.stdout.buffer.write(json_str.encode("utf-8"))
    sys.stdout.buffer.write(b"\n")


if __name__ == "__main__":
    main()
