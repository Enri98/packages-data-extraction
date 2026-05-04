/**
 * docsprocess-trigger — polls a Drive folder every minute and POSTs new PDFs
 * to the Cloud Run /process endpoint, authenticated with an OIDC ID token
 * minted via a GCP service account private key.
 *
 * Required Script Properties (Project Settings → Script Properties):
 *   FOLDER_ID       — Google Drive folder ID to watch
 *   CLOUD_RUN_URL   — Cloud Run service base URL (no trailing slash)
 *   SA_JSON         — Full JSON content of the appsscript-invoker SA key file
 *   LAST_SEEN_IDS_JSON — (managed automatically) JSON array of processed file IDs
 *
 * No external libraries required — the OIDC flow uses GAS built-ins only:
 *   self-signed JWT with target_audience claim, signed via
 *   Utilities.computeRsaSha256Signature, exchanged at the Google token
 *   endpoint for an id_token.
 *   Reference: https://cloud.google.com/run/docs/authenticating/service-to-service
 */

// ─── Constants ────────────────────────────────────────────────────────────────

/** Maximum number of file IDs kept in LAST_SEEN_IDS_JSON before FIFO pruning.
 *  Script Properties are limited to 9 KB per key. A Drive file ID is 33 chars;
 *  500 IDs ≈ 16.5 KB as a JSON array, which exceeds the limit. Use 200 instead:
 *  200 × 33 + JSON overhead ≈ 7 KB — safely under the 9 KB ceiling. In
 *  practice this pipeline processes a few PDFs per day, so 200 IDs ≈ 3 months
 *  of history, which is sufficient for idempotency.
 */
var MAX_SEEN_IDS = 200;

/** Minimum age of a Drive file (in milliseconds) before we process it.
 *  Drive sets dateCreated at upload start, but the file content may still be
 *  streaming. 10 seconds is conservative but safe for typical PDF sizes.
 */
var MIN_FILE_AGE_MS = 10000;

// ─── Main entry point ─────────────────────────────────────────────────────────

/**
 * pollFolder — called by the time-driven trigger every 1 minute.
 * Lists PDFs in the watched folder, skips already-seen files, and POSTs
 * new ones to Cloud Run. File IDs are persisted immediately on 2xx so that
 * a script crash mid-batch does not re-deliver completed files next minute.
 */
function pollFolder() {
  var props = PropertiesService.getScriptProperties();

  var folderId = props.getProperty('FOLDER_ID');
  var cloudRunUrl = props.getProperty('CLOUD_RUN_URL');
  var saJson = props.getProperty('SA_JSON');

  if (!folderId || !cloudRunUrl || !saJson) {
    throw new Error(
      'Missing required Script Properties. Ensure FOLDER_ID, CLOUD_RUN_URL, ' +
      'and SA_JSON are set in Project Settings → Script Properties.'
    );
  }

  // Deserialize the seen-IDs set. Use a plain Object as a hash set for O(1) lookup.
  var seenIdsJson = props.getProperty('LAST_SEEN_IDS_JSON') || '[]';
  var seenIdsArray = JSON.parse(seenIdsJson);
  var seenIdsSet = {};
  for (var i = 0; i < seenIdsArray.length; i++) {
    seenIdsSet[seenIdsArray[i]] = true;
  }

  var folder = DriveApp.getFolderById(folderId);
  var files = folder.getFilesByType(MimeType.PDF);
  var now = Date.now();
  var processed = 0;

  while (files.hasNext()) {
    var file = files.next();
    var fileId = file.getId();
    var fileName = file.getName();

    // Skip files already handled.
    if (seenIdsSet[fileId]) {
      continue;
    }

    // Skip files that were uploaded too recently (partial-upload guard).
    var ageMs = now - file.getDateCreated().getTime();
    if (ageMs < MIN_FILE_AGE_MS) {
      Logger.log('Skipping recently created file (age %dms < %dms): %s', ageMs, MIN_FILE_AGE_MS, fileName);
      continue;
    }

    Logger.log('Processing new PDF: %s (%s)', fileName, fileId);

    var idToken = getIdToken_(cloudRunUrl);
    var body = JSON.stringify({ fileId: fileId, fileName: fileName, folderId: folderId });

    var response = UrlFetchApp.fetch(cloudRunUrl + '/process', {
      method: 'post',
      contentType: 'application/json',
      headers: { 'Authorization': 'Bearer ' + idToken },
      payload: body,
      muteHttpExceptions: true
    });

    var statusCode = response.getResponseCode();
    if (statusCode >= 200 && statusCode < 300) {
      Logger.log('Accepted by Cloud Run (HTTP %d): %s', statusCode, fileName);
      // Persist immediately so a crash later in the loop does not re-deliver.
      seenIdsArray.push(fileId);
      seenIdsSet[fileId] = true;
      seenIdsArray = pruneSeenIds_(seenIdsArray, MAX_SEEN_IDS);
      props.setProperty('LAST_SEEN_IDS_JSON', JSON.stringify(seenIdsArray));
      processed++;
    } else {
      // Log and leave the file out of seenIdsSet — next run will retry naturally.
      Logger.log(
        'Cloud Run returned HTTP %d for %s — will retry next poll. Body: %s',
        statusCode, fileName, response.getContentText().substring(0, 200)
      );
    }
  }

  Logger.log('pollFolder complete. New files sent: %d', processed);
}

// ─── OIDC token helper ────────────────────────────────────────────────────────

/**
 * getIdToken_ — mints a Google OIDC ID token for the given audience using the
 * appsscript-invoker service account private key stored in SA_JSON.
 *
 * Flow (per https://cloud.google.com/run/docs/authenticating/service-to-service):
 *   1. Build a self-signed JWT with claims: iss, sub, aud, target_audience, iat, exp.
 *   2. Sign the JWT with the SA's RSA private key via Utilities.computeRsaSha256Signature.
 *   3. POST the signed JWT to https://oauth2.googleapis.com/token.
 *   4. Extract the id_token field from the response.
 *
 * The token is valid for 1 hour; the script runs every minute so no caching
 * is implemented (the token endpoint call is fast, ~200 ms).
 *
 * @param {string} audience - The Cloud Run service URL (used as both aud for
 *   Google's token endpoint and as target_audience for the issued ID token).
 * @return {string} A signed OIDC ID token.
 */
function getIdToken_(audience) {
  var props = PropertiesService.getScriptProperties();
  var sa = JSON.parse(props.getProperty('SA_JSON'));

  var now = Math.floor(Date.now() / 1000);
  var TOKEN_ENDPOINT = 'https://oauth2.googleapis.com/token';

  // Build JWT header.
  var header = { alg: 'RS256', typ: 'JWT' };

  // Build JWT claims.
  var claims = {
    iss: sa.client_email,
    sub: sa.client_email,
    aud: TOKEN_ENDPOINT,         // audience for Google's token endpoint
    target_audience: audience,   // the ID token's intended audience (Cloud Run URL)
    iat: now,
    exp: now + 3600
  };

  // Encode header and claims as base64url.
  var headerB64 = base64UrlEncode_(JSON.stringify(header));
  var claimsB64 = base64UrlEncode_(JSON.stringify(claims));
  var signingInput = headerB64 + '.' + claimsB64;

  // Sign with the SA private key.
  // computeRsaSha256Signature expects the private key in PKCS8 PEM format,
  // which is exactly what GCP service account JSON keys contain.
  var privateKey = sa.private_key;
  var signatureBytes = Utilities.computeRsaSha256Signature(signingInput, privateKey);
  var signatureB64 = base64UrlEncode_(signatureBytes);

  var jwt = signingInput + '.' + signatureB64;

  // Exchange the self-signed JWT for a Google-issued OIDC ID token.
  var response = UrlFetchApp.fetch(TOKEN_ENDPOINT, {
    method: 'post',
    contentType: 'application/x-www-form-urlencoded',
    payload: 'grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer&assertion=' + jwt,
    muteHttpExceptions: true
  });

  if (response.getResponseCode() !== 200) {
    throw new Error(
      'Failed to mint OIDC ID token. HTTP ' + response.getResponseCode() +
      ': ' + response.getContentText().substring(0, 400)
    );
  }

  var tokenResponse = JSON.parse(response.getContentText());
  if (!tokenResponse.id_token) {
    throw new Error(
      'Token endpoint did not return id_token. Response: ' +
      response.getContentText().substring(0, 400)
    );
  }

  return tokenResponse.id_token;
}

// ─── Utility helpers ──────────────────────────────────────────────────────────

/**
 * base64UrlEncode_ — encodes a string or byte array to base64url (RFC 4648 §5).
 * GAS Utilities.base64Encode returns standard base64 with + and /; we replace
 * those and strip trailing = padding as required by the JWT spec.
 *
 * @param {string|byte[]} input - String (UTF-8 encoded) or byte array.
 * @return {string} Base64url-encoded string.
 */
function base64UrlEncode_(input) {
  var encoded;
  if (typeof input === 'string') {
    encoded = Utilities.base64Encode(Utilities.newBlob(input).getBytes());
  } else {
    // byte array (from computeRsaSha256Signature)
    encoded = Utilities.base64Encode(input);
  }
  return encoded.replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

/**
 * pruneSeenIds_ — keeps only the most recent maxLen IDs (FIFO).
 * Called before persisting LAST_SEEN_IDS_JSON to stay within the 9 KB
 * Script Properties key-size limit.
 *
 * @param {string[]} arr - Current ordered array of file IDs.
 * @param {number} maxLen - Maximum number of IDs to retain.
 * @return {string[]} Pruned array.
 */
function pruneSeenIds_(arr, maxLen) {
  if (arr.length <= maxLen) return arr;
  return arr.slice(arr.length - maxLen);
}
