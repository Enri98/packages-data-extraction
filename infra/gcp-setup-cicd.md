# GCP setup for the GitHub Actions deploy workflow

This runbook wires `.github/workflows/deploy.yml` to Google Cloud using
**Workload Identity Federation** (WIF). No service-account JSON key is created
or stored in GitHub. GitHub Actions exchanges its short-lived OIDC token for a
GCP access token at runtime, scoped to one specific repository and one
specific service account.

You should only need to run this **once**, when you first wire CI/CD.

## Prerequisites

- The base GCP setup is done: project exists, runtime service account
  (`docsprocess-runtime`) and deployer service account (`docsprocess-deployer`)
  are created, secrets are populated, the first manual deploy via
  `gcloud builds submit` succeeded.
- The repository at `Enri98/packages-data-extraction` is public on GitHub.
- You have `gcloud` authenticated as an owner/editor on the GCP project.
- You're running PowerShell on Windows. Bash equivalents differ only in
  `\`-vs-`` ` `` line continuations.

## Variables

Set these once at the top of your shell. Replace the project number with
yours if it differs (the original was `317332267279`).

```powershell
$env:PROJECT_ID = "packages-data-extraction"
$env:PROJECT_NUMBER = (gcloud projects describe $env:PROJECT_ID --format='value(projectNumber)')
$env:GITHUB_REPO = "Enri98/packages-data-extraction"
$env:POOL_ID = "github-actions-pool"
$env:PROVIDER_ID = "github-actions-provider"
$env:DEPLOYER_SA = "docsprocess-deployer@$($env:PROJECT_ID).iam.gserviceaccount.com"
$env:RUNTIME_SA  = "docsprocess-runtime@$($env:PROJECT_ID).iam.gserviceaccount.com"
```

## 1. Enable the required APIs

These should already be on from the base setup, but `iamcredentials` in
particular is the one that's easy to miss — WIF token exchange requires it.

```powershell
gcloud services enable `
    iamcredentials.googleapis.com `
    iam.googleapis.com `
    sts.googleapis.com `
    --project=$env:PROJECT_ID
```

## 2. Create the workload identity pool

A "pool" is a named container for one or more external identity providers.
We'll use one provider (GitHub) inside it.

```powershell
gcloud iam workload-identity-pools create $env:POOL_ID `
    --project=$env:PROJECT_ID `
    --location=global `
    --display-name="GitHub Actions Pool"
```

## 3. Create the GitHub OIDC provider inside the pool

The `attribute-condition` is the security boundary. **Do not skip it.**
Without a condition, *any* GitHub repository on the planet could request a
token from this provider. With it, only your repo can.

```powershell
gcloud iam workload-identity-pools providers create-oidc $env:PROVIDER_ID `
    --project=$env:PROJECT_ID `
    --location=global `
    --workload-identity-pool=$env:POOL_ID `
    --display-name="GitHub Actions Provider" `
    --issuer-uri="https://token.actions.githubusercontent.com" `
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.actor=assertion.actor,attribute.ref=assertion.ref" `
    --attribute-condition="assertion.repository == '$env:GITHUB_REPO'"
```

## 4. Bind the deployer service account to the WIF principal

This grants `roles/iam.workloadIdentityUser` to the GitHub principal — i.e.
"a workflow running in this exact repo can impersonate this service account."

```powershell
gcloud iam service-accounts add-iam-policy-binding $env:DEPLOYER_SA `
    --project=$env:PROJECT_ID `
    --role="roles/iam.workloadIdentityUser" `
    --member="principalSet://iam.googleapis.com/projects/$env:PROJECT_NUMBER/locations/global/workloadIdentityPools/$env:POOL_ID/attribute.repository/$env:GITHUB_REPO"
```

## 5. Grant the deployer service account the deploy roles

These are the GCP-side permissions the workflow needs to actually do its job.

```powershell
# Cloud Run admin (deploy revisions, route traffic)
gcloud projects add-iam-policy-binding $env:PROJECT_ID `
    --member="serviceAccount:$env:DEPLOYER_SA" `
    --role="roles/run.admin"

# Cloud Build editor (submit builds)
gcloud projects add-iam-policy-binding $env:PROJECT_ID `
    --member="serviceAccount:$env:DEPLOYER_SA" `
    --role="roles/cloudbuild.builds.editor"

# Artifact Registry writer (push container images)
gcloud projects add-iam-policy-binding $env:PROJECT_ID `
    --member="serviceAccount:$env:DEPLOYER_SA" `
    --role="roles/artifactregistry.writer"

# Logs viewer (so the workflow can stream Cloud Build logs)
gcloud projects add-iam-policy-binding $env:PROJECT_ID `
    --member="serviceAccount:$env:DEPLOYER_SA" `
    --role="roles/logging.viewer"
```

## 6. Allow the deployer to impersonate the runtime SA

Cloud Run revisions run *as* the runtime SA. Deploying a revision means
"set this SA on the new revision", which requires
`roles/iam.serviceAccountUser` on the runtime SA.

```powershell
gcloud iam service-accounts add-iam-policy-binding $env:RUNTIME_SA `
    --project=$env:PROJECT_ID `
    --role="roles/iam.serviceAccountUser" `
    --member="serviceAccount:$env:DEPLOYER_SA"
```

## 7. Capture the WIF provider resource name

The deploy workflow needs this string. Print it so you can paste it into a
GitHub variable in the next step:

```powershell
$WIF_PROVIDER = "projects/$env:PROJECT_NUMBER/locations/global/workloadIdentityPools/$env:POOL_ID/providers/$env:PROVIDER_ID"
Write-Output $WIF_PROVIDER
```

## 8. Set the GitHub Actions variables

These are **Variables** (not Secrets) — they're environment-specific config,
not credentials. The actual credential is the OIDC token GitHub mints at
runtime, which is already a secret by virtue of being short-lived and bound
to the workflow run.

In the GitHub UI: **Settings → Secrets and variables → Actions → Variables
tab → New repository variable**. Add four:

| Name | Value |
|---|---|
| `WIF_PROVIDER` | the string from step 7, e.g. `projects/317332267279/locations/global/workloadIdentityPools/github-actions-pool/providers/github-actions-provider` |
| `WIF_SERVICE_ACCOUNT` | `docsprocess-deployer@packages-data-extraction.iam.gserviceaccount.com` |
| `SHEET_ID` | the target Google Sheet ID |
| `FOLDER_ID` | the watched Drive folder ID |

## 9. First deploy

Don't wait for a real merge to test. Trigger the deploy manually:

1. Push the new workflow files to `main` (or merge a PR that includes them).
2. GitHub UI → **Actions** tab → **Deploy** workflow → **Run workflow** button
   → choose `main` branch → **Run workflow**.
3. Watch the run. The first authentication step is where WIF problems
   surface — if step 4's binding or step 3's attribute condition is wrong,
   `google-github-actions/auth@v2` fails with a clear error message.
4. If auth passes, `gcloud builds submit` takes 3–4 minutes. Cloud Build
   itself runs the existing `infra/cloudbuild.yaml`, which builds the image,
   pushes it, and deploys to Cloud Run.
5. Verify the new revision is live: `gcloud run services describe
   fustelle-extractor --region=us-central1`. Or hit `/ready` and confirm 200.

## Branch protection (recommended)

Once you've seen CI go green a few times, lock down `main` so the workflows
actually gate merges:

1. GitHub UI → **Settings → Branches → Add branch protection rule**.
2. Branch name pattern: `main`.
3. Tick **Require a pull request before merging**.
4. Tick **Require status checks to pass before merging** → search for `test`
   (the CI job name) → add it.
5. Optionally tick **Require linear history** for a cleaner log.

## Cost note

WIF itself: free. Token exchange: free. GitHub Actions on a public repo:
free, unlimited. Cloud Build: 120 free build-minutes/day, a deploy is ~3–4
minutes — comfortably inside the free tier even with multiple deploys per
day.

The only paid line item is the Gemini API calls inside the deployed service,
which is unchanged by this CI/CD wiring.

## Rollback / undoing the WIF setup

If anything misbehaves and you need to revoke GitHub's access immediately:

```powershell
# Remove the workload-identity binding from the deployer SA — fastest kill.
gcloud iam service-accounts remove-iam-policy-binding $env:DEPLOYER_SA `
    --project=$env:PROJECT_ID `
    --role="roles/iam.workloadIdentityUser" `
    --member="principalSet://iam.googleapis.com/projects/$env:PROJECT_NUMBER/locations/global/workloadIdentityPools/$env:POOL_ID/attribute.repository/$env:GITHUB_REPO"
```

The deploy workflow will start failing at the auth step on the next run.
Existing Cloud Run revisions are untouched — production keeps serving.

To fully tear down the pool and provider (rare):

```powershell
gcloud iam workload-identity-pools providers delete $env:PROVIDER_ID `
    --project=$env:PROJECT_ID --location=global `
    --workload-identity-pool=$env:POOL_ID

gcloud iam workload-identity-pools delete $env:POOL_ID `
    --project=$env:PROJECT_ID --location=global
```

Pools enter a soft-deleted state for 30 days before final removal, so a
typo'd delete is recoverable via `undelete`.
