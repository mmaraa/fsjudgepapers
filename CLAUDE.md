# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Web app that generates judging packets for figure skating competitions. Users upload PDF exports from Figure Skating Manager (FSM); the backend splits, categorizes, and merges them into per-judge/referee/official PDF packets. UI is bilingual (Finnish default, English).

**This repo is backend-only.** The frontend was moved to the `figureskatingtools-site` repo, which serves it at `https://figureskatingtools.com/judgepapers/` and proxies `/judgepapers/api/*` here. The local `frontend/` directory is legacy: it is no longer built, deployed, or referenced by CI, and will be deleted at teardown. `PROXY-CONTRACT.md` is the authoritative description of the router → Function App contract.

## Commands

```bash
# Backend (cd infra/functions)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
func start         # Azure Functions on :7071 (requires local.settings.json, see README)

# Exercise an endpoint the way the site router does (see PROXY-CONTRACT.md)
curl -s http://localhost:7071/api/check_user_permission \
  -H 'x-forwarded-user-email: you@example.com'

# Deployment (manual; CI does this automatically on push to main / dispatch)
./deploy_infra.sh [--proxy-secret <SECRET>]   # Bicep, subscription-scoped
./deploy_backend.sh -g <resource-group>       # Functions ZIP deploy
```

There are no tests and no linter configured. To drive the backend from a UI, run the frontend from the `figureskatingtools-site` repo and point its judgepapers function-app URL at `http://localhost:7071`. `start_locally.sh`, `deploy_frontend.sh` and `create_auth_app.sh` are leftovers of the old per-tool Web App and no longer reflect how this tool is hosted.

## Architecture

Two pieces deployed from here, plus a frontend that lives elsewhere:

1. **Frontend** — *no longer in this repo.* The `figureskatingtools-site` repo hosts a single App Service that serves every tool's UI (`/judgepapers/`, `/scoremodifier/`, …), owns Easy Auth and the `figureskatingtools.com` domain, and proxies `/judgepapers/api/*` to the Function App deployed here. The legacy `frontend/` directory is dead code kept only until teardown; do not edit it, and do not restore it to CI.

2. **Backend** (`infra/functions/`) — Python Azure Functions, all HTTP-triggered, defined in `function_app.py`. The PDF pipeline lives in plain modules called by the `generate_judging_papers` endpoint:
   - `processor.py` — orchestrator: parse CompetitionSchedule → extract segment names → split judge sheets → generate cover pages → merge per-person packets → ZIP. Runs on local temp dirs after downloading blobs.
   - `split_judges_sheets.py` — splits `*_JudgesSheetAll.pdf` into per-judge/referee PDFs by parsing role lines; also detects withdrawn skaters (`is_withdrawn_page` recognises both the wide-column `WD    Name` layout and the single-space `WD Name` one used by the Technical Controller/Specialist sheets, scanning only the page's header block so a `WD Name` row deeper in a table doesn't condemn the page, and ignoring the `WD  Withdrawn` legend; `filter_withdrawn_pages` copies a PDF without those pages). Step 3 of `processor.py` runs that filter over the per-skater `*_TechnicalControllerSheet.pdf` / `*_TechnicalSpecialistSheet{1,2}.pdf` before merging them into the packets, caching the result as `*_nowd.pdf` (written once per segment, reused by every official). The referee sheet is deliberately left whole: it is a per-segment table with all skaters as rows, so a page-level filter would drop the entire table.
   - `create_cover_pages.py` — reportlab cover/segment pages, strikethrough start lists.
   - `combine_judging_papers.py` — date/time/panel extraction and final merge.
   - `categories.py` — table-driven category lookup (see below).
   - `competition_schedule.py` — parses the CompetitionSchedule PDF for start times.

3. **Infra** (`infra/main.bicep` + `modules/`) — subscription-scoped Bicep, backend-only: resource group, storage (`storage.bicep`), Function App + App Insights (`function.bicep`, Flex Consumption, **SystemAssigned identity only**, CORS empty), and storage RBAC (`roleassignment.bicep`). Params are just `resourceGroupName`, `location` and the secure `proxySharedSecret`; per-env values in `infra/parameters/{test,prod}.bicepparam`. `main.bicep` exports `functionPrincipalId` so the site repo can grant this Function App read access to the shared competition-data container. **No Web App, managed identity, DNS or custom-domain modules live here any more** — hosting, Easy Auth, the `figureskatingtools.com` zone and all custom domains belong to the `figureskatingtools-site` repo.

### Auth chain (important when touching any endpoint)

Full details in **`PROXY-CONTRACT.md`** (keep it in sync when touching this). Summary:

All function routes are `AuthLevel.ANONYMOUS`; real auth is Entra ID Easy Auth on the site router. Identity flows: Easy Auth injects `X-MS-CLIENT-PRINCIPAL*` headers on the router → the router extracts the email and forwards it as `X-Forwarded-User-Email` (plus `X-Proxy-Secret`) to the Function App → `get_user_email_from_header()` in `function_app.py` checks the shared secret first, then tries (in order) direct Easy Auth header, forwarded header, base64 SWA principal, then Bearer JWT claims. Every endpoint must call it and return 401 on `None`. `is_user_allowed()` currently allows all authenticated users and is the hook for a future allowlist.

**The Function App itself must be `AllowAnonymous`** (`function.bicep` `globalValidation`), *not* `requireAuthentication: true` — the router forwards only the email header, no bearer token, so Easy Auth enforcement would 401 every proxied request (`WWW-Authenticate: Bearer`, empty body) before the app's header check runs. No identity provider is registered on the Function App at all.

Because the function endpoint is public, a **shared secret** stops anyone from calling it directly with a spoofed email header: the router holds the secret and sends it as `X-Proxy-Secret` on every proxied call, and `_proxy_secret_ok()` (called first in `get_user_email_from_header`) rejects requests whose header doesn't match → 401. **Enforced only when the env var is set** (local/dev and brief pre-rollout windows fail open). The secret is a per-environment GitHub Environment secret here, injected into the Function App via the `proxySharedSecret` Bicep param (in the authoritative `appSettings` array); the same value lives in the site repo as `PROXY_SHARED_SECRET_JUDGEPAPERS`. Two rejected alternatives: inbound **IP restrictions** `403` the GitHub runner during the Flex deploy's sync-triggers/health-check and hang the pipeline; **Network Security Perimeter** can't hold `Microsoft.Web` apps (not an onboarded NSP resource type).

### Storage layout & dual credential pattern

Every competition has an immutable 8-char hex **id** (`uuid4().hex[:8]`) — the identifier in all API calls and table keys. Competition **names are not unique** (trial-and-error re-creation is allowed); the blob folder is `{sanitized-name}-{id}/`, stored on the entity as `FolderPath` (endpoints resolve id → FolderPath via `get_competition_entity`).

Blob container `fs-judgepapers`:
- `{name}-{id}/metadata.json` — id/name/createdBy/createdDate/language; its existence defines the competition "folder"
- `{name}-{id}/{PREFIX}_{Suffix}.pdf` — uploaded FSM exports
- `{name}-{id}/judgePapers/...` — generated output (ZIPs + merged PDFs get 5-day SAS links)

Tables: `competitions` is the **authoritative, permanent history** of every competition ever created (PartitionKey `GLOBAL`, RowKey = id; rows are never deleted — there is no blob-scan fallback anymore). Columns: `Name`, `FolderPath`, `Visible` (controls UI listing), `CreatedBy/CreatedDate`, `DeletedDate/DeletedBy`, the optional `PlatformId` (see below), and usage counters for statistics (`UploadedFileCount`, `GenerateRunCount`, `LastGeneratedDate`, maintained best-effort by `_bump_competition_counters`). Deleting a competition removes its blobs and `generatedpapers` rows but only soft-deletes the `competitions` row (`Visible=false` + delete audit). Legacy name-keyed rows are lazily migrated by `list_competitions` (`migrate_legacy_row`: new id-keyed row with `FolderPath` = old name folder, generatedpapers re-keyed). `generatedpapers` holds SAS download links (PartitionKey = competition id), `categories` is the category registry.

**Platform binding.** The site owns the competition registry and its nav selector's "active competition" (a GUID). `POST /resolve_competition` (`{platformId, name}` → `{id, name, created}`) maps that GUID onto this tool's own record so the site's UI needs no per-tool competition list: look it up by `PlatformId` (server-side filter, `Visible is False` filtered in Python because legacy rows lack the property and soft-deleted rows keep their `PlatformId` while their blobs are gone; newest `CreatedDate` wins) → else *adopt* a visible, unbound row whose normalized name (casefold, alphanumerics only) matches, stamping `PlatformId` on it → else create one via `create_competition_record`, the creation block shared with `create_competition`. Legacy name-keyed rows are never adopted (`migrate_legacy_row` re-keys them, which would drop the binding) and `migrate_legacy_row` never writes a `PlatformId`. The legacy `list/create/delete/extend` routes are unchanged for the standalone app.

**Pool refresh.** Blobs copied in from the pool carry `poolSource` / `poolUploadedUtc` / `importedBy` blob metadata (`_copy_pool_blob`, shared by `import_platform_file` and the refresh). Both read paths — `get_competition_details` and `generate_judging_papers` — first run `_refresh_from_pool`, which re-copies any file the tool *already holds* whose pool copy (`uploads/` or `fsm/`, newest of the two) is newer than the stamped `poolUploadedUtc`, so an FSM re-export under the same stable filename replaces the tool's copy automatically instead of generating from a stale one. It never adds or deletes files — new pool files still need an explicit import — and it is best-effort: every failure is logged and swallowed. `get_competition_details` reports what it replaced as `refreshedFromPool` and returns per-file `uploadedUtc` (the pool upload / FSM push time — what the UI shows), `lastModified` (when this copy was written) and `poolSource`.

Every storage client helper supports two credential modes and new storage code must too: managed identity when `AzureWebJobsStorage__accountName` is set (production; SAS via user-delegation key), connection string via `AzureWebJobsStorage` otherwise (local dev; SAS via account key).

### Category/filename parsing

FSM filenames are `{CATEGORY_ABBREVIATION}{SEGMENT_MARKER}_{Suffix}.pdf` (e.g. segment markers `QUAL`/`FNL`, split suffixes `#N`). Abbreviations are not hardcoded — they live in the `categories` Azure Table (RowKey = abbreviation, with `DisplayName`, `DisplayNameFi`, `JudgingMethod` = ISU|MUPI) and are matched longest-prefix-first. `categories.py` caches the table in memory for 5 minutes. This parsing logic is duplicated in spirit on the frontend (`validate.ts` consumes the parsed structure from `get_competition_details`).

Human-readable segment names aren't in filenames; they're extracted from line 2 of each `JudgesSheetAll.pdf` (stripping the category display name prefix, with punctuation-tolerant fuzzy matching). This enrichment happens both in `function_app.py` (`get_competition_details`) and `processor.py`.

## Branch / Deploy Strategy

`test` → `main` promote via PRs, merged with **squash** (one commit per release on `main`). `.github/workflows/deploy.yml` has two deploy jobs — infra (Bicep) then backend (Functions ZIP) — targeting the matching GitHub environment: **push to `main` auto-deploys prod**; **`test` is manual-only** via `workflow_dispatch` (run the workflow from the branch whose code you want, pick the environment) — there is no `test`-branch push trigger. This mirrors the figureskatingtools-site repo. There is **no frontend job and no Entra/Graph step** any more; the environments need only `AZURE_CLIENT_ID` + `PROXY_SHARED_SECRET` secrets and the `AZURE_TENANT_ID` / `AZURE_SUBSCRIPTION_ID` / `LOCATION` / `RESOURCE_GROUP_NAME` vars (`AUTH_CLIENT_ID`, `AUTH_APP_OBJECT_ID` and `CUSTOM_DOMAIN` are obsolete and can be deleted).

**After every squash-merge to `main`, reset `test` to `main`** — squash creates a new commit on `main`, so without this every future PR re-lists all old commits:

```bash
git checkout test && git fetch && git reset --hard origin/main && git push --force origin test
```

When asked to create a `test` → `main` PR: create it, then ask whether it has been merged (it's usually checked and squash-merged almost immediately) and run the reset above once it is. Verify before resetting that `git diff origin/main origin/test` is empty.
