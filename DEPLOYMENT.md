# Deployment

Operator runbook for an already-running deployment. For the GitHub Actions
deploy workflow setup (Workload Identity Federation), see
[`infra/gcp-setup-cicd.md`](infra/gcp-setup-cicd.md).

## Before you touch anything in production

1. Confirm your active project: `gcloud config get-value project` should
   return the project that owns the Cloud Run service (currently
   `packages-data-extraction`).
2. Confirm the region: everything lives in `us-central1`. Free-tier requires
   it. Don't deploy anywhere else without re-reading the cost section.
3. Sanity-check the budget alert in the Billing console. If it's missing,
   create one at $5/mo before doing anything that incurs cost.

## Deploy a new revision

```powershell
# from the repo root
gcloud builds submit `
    --config=infra/cloudbuild.yaml `
    --substitutions=_SHEET_ID=<your-sheet-id>,_FOLDER_ID=<your-drive-folder-id>
```

Cloud Build will:
1. Build the container image (`infra/Dockerfile`).
2. Push it to Artifact Registry tagged with `$BUILD_ID` and `latest`.
3. Deploy the new revision to Cloud Run, routing 100% of traffic to it.

Build time: ~3–4 minutes on a clean cache, ~90 seconds otherwise.

After it returns, verify:

```powershell
# the URL is printed at the end of the deploy step
gcloud run services describe fustelle-extractor --region=us-central1 `
    --format='value(status.url)'

# /ready should return 200 within ~5 seconds (OCR engine warms on first hit)
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" `
    https://<service-url>/ready
```

If `/ready` returns 503, read the response body — it names the failing check
(`gemini_api_key`, `ocr_engine`, or `sheet`).

## Roll back

Cloud Run keeps prior revisions. To roll traffic to the previous one:

```powershell
gcloud run services update-traffic fustelle-extractor `
    --region=us-central1 `
    --to-revisions=<previous-revision-name>=100
```

List revisions with `gcloud run revisions list --service=fustelle-extractor
--region=us-central1`. The most recent successful revision (i.e. the one
serving traffic before the bad deploy) is the one you want.

If the previous revision is also broken, you can pin to any earlier one — Cloud
Run does not garbage-collect revisions automatically, so they should all still
be there.

## Rotate the Gemini API key

1. Generate a new key in [Google AI Studio](https://aistudio.google.com/).
2. Add it as a new version of the secret:
   ```powershell
   echo -n "<new-key>" | gcloud secrets versions add GEMINI_API_KEY --data-file=-
   ```
3. Cloud Run pulls `GEMINI_API_KEY:latest` on revision start, so the new key
   takes effect on the next deploy. To force a redeploy without changing code:
   ```powershell
   gcloud run services update fustelle-extractor --region=us-central1 `
       --update-secrets=GEMINI_API_KEY=GEMINI_API_KEY:latest
   ```
4. Wait for `/ready` to return 200, then disable the old key version:
   ```powershell
   gcloud secrets versions disable <old-version-number> --secret=GEMINI_API_KEY
   ```
5. After a day or two of clean operation, destroy the old version.

If the new key is wrong, `/ready` returns 503 from the Gemini check. Re-enable
the old version and redeploy.

## Rerun a failed pack

Look at the `run_metadata` tab. Failed runs have `outcome=error` and the error
string in the last column. Find the filename, then either:

**Option A — drop a fresh copy in Drive.** If the original cause was transient
(API blip, OCR flake), the next trigger window picks it up. The idempotency
check inspects `run_metadata` for `outcome=success`, so an `error` row does
*not* block reprocessing.

**Option B — run it locally.** If you want to debug:

```powershell
.venv\Scripts\Activate.ps1
$env:GOOGLE_SHEET_ID = "<sheet-id>"   # or unset to skip Sheet write
python -m src.pipeline path\to\pack.pdf
```

`run_single` is the same function the server calls, so anything you reproduce
locally is faithful to production behaviour.

## Add a new field

The schema is the source of truth. Adding a field is a four-step change:

1. Add the field to `PackData` in `src/schemas/pack.py`. Pick the right type
   (`ExtractedField` for strings, `PresenceField` for booleans, plain `str`
   for deterministic constants). Add it to `_SHEET_FIELDS` at the right index.
2. Add a column to the Google Sheet header row in the same position.
3. Update the prompt at `prompts/extraction_v1.txt` (or fork to `_v2.txt`) to
   describe the new field.
4. If the field needs a parser-side rule, edit `src/parsing.py`. If validator
   logic depends on it, edit `src/validator.py`.

Run the eval harness to see what changed:

```powershell
uv run pytest tests/test_deterministic_eval.py
uv run python scripts/eval_deterministic.py    # full report
```

Then redeploy. The schema verifier on startup checks column count, so a Sheet
header that doesn't match `len(_SHEET_FIELDS)` will surface immediately as a
`/ready` 503.

## Tail the logs

```powershell
gcloud run services logs tail fustelle-extractor --region=us-central1
```

Logs are JSON in production (one line per record, `severity` field maps to
Cloud Logging levels). For richer querying, the Logs Explorer in the Cloud
console understands the JSON structure natively — filter on
`jsonPayload.request_id` to follow one request through the pipeline.

## Force-trigger the Apps Script poll

Open the Apps Script project (script.google.com → docsprocess-trigger) and
click *Run* on the `pollDriveFolder` function. This polls the watched folder
once, immediately, ignoring the trigger window. Useful for verifying a new
deploy without waiting up to 5 minutes.

## Disable the trigger

If you need to stop new PDFs from being processed (e.g. while migrating the
sheet, rotating credentials, or debugging):

1. script.google.com → docsprocess-trigger → *Triggers* (clock icon).
2. Delete the time-driven trigger row.

Re-enable by clicking *Add trigger*: function `pollDriveFolder`, event source
*Time-driven*, type *Minutes timer*, *Every 5 minutes*.

PDFs uploaded while the trigger is disabled will be picked up on the next poll
once it's re-enabled, because the script re-reads the folder contents from
scratch each time and only filters by the `lastSeenIds` set in script
properties — which persists across trigger runs but not across `clear()`s.

## Cost expectations

Steady state for ~50 PDFs/month:

| Line item | Cost |
|---|---|
| Gemini 2.5 Pro (50 calls × ~$0.03–$0.07) | ~$1.50/mo |
| Cloud Run (CPU/memory under always-free limits) | $0 |
| Cloud Build (120 free build-minutes/day) | $0 |
| Artifact Registry (image ~530 MB, slightly over the 0.5 GB free tier; cleanup policy keeps max 2 images, deletes >3 days old) | ~$0.05 – $0.15/mo |
| Secret Manager (10K free access ops/mo) | $0 |
| Cloud Logging (50 GiB free ingest/mo) | $0 |
| **Total** | **~$1.55 – $1.65/mo** |

Watch out for:
- Leaving `GEMINI_LIVE=1` set in a CI loop (every test pass would call Gemini).
- Cloud Run retry storms on a permanently-failing PDF — the 202 response
  should prevent this, but verify in the logs if a budget alert fires.
- A jump in build minutes if Docker layer caching breaks — usually fine, but
  worth checking after Dockerfile edits.

## Things to check if something is wrong

| Symptom | First thing to look at |
|---|---|
| `/ready` returns 503 | The response body names the failing check. Most often the Gemini key was rotated or the runtime SA lost a role. |
| Rows are missing from `pack_data` | `run_metadata` for the same filename. If `outcome=error`, the error string is in the row. If no row at all, Apps Script never POSTed — check the Apps Script execution log. |
| Duplicate rows | Trigger interval too short relative to pipeline latency. Confirm it's set to 5 minutes, not 1. |
| `/process` returns 500 with "Service account config error" | Rotate or re-add the `GOOGLE_SERVICE_ACCOUNT_JSON` secret. |
| Cold-start ~30s | Expected after idle. The OCR engine and the Cloud Run container both warm up on first hit. |
| `simbolo_*` fields wrong | VLM judgement. See the accuracy notes in `ARCHITECTURE.md` — known weak spots. |
