import azure.functions as func
import logging
import os
import tempfile
import shutil
import base64
import json
import re
import unicodedata
import io
from pypdf import PdfReader
from datetime import datetime
from azure.storage.blob import BlobServiceClient, generate_blob_sas, BlobSasPermissions
from azure.identity import DefaultAzureCredential
from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableClient, UpdateMode
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from processor import process_judging_papers
from categories import load_categories, match_category, parse_filename_generic
from competition_schedule import parse_competition_schedule, get_schedule_start_time

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# Maximum file upload size: 25 MB
MAX_UPLOAD_SIZE = 25 * 1024 * 1024

# Platform competition GUIDs (the PlatformId binding) always look like this.
# Pinned before being spliced into a pool blob path — the binding originates
# from a client body (resolve_competition).
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")

# Automatic competition deletion: lifetime after creation, extension per
# "extend" click, and the fixed migration date for rows created before this
# feature existed (rows missing DeletionDate).
DELETION_RETENTION_DAYS = 60
DELETION_EXTENSION_DAYS = 7
LEGACY_DELETION_DATE = "2026-06-12T00:00:00Z"
AUTO_CLEANUP_ACTOR = "auto-cleanup"

def is_user_allowed(email: str) -> bool:
    """
    Check if the user is allowed to perform sensitive operations.
    Policy for v1.0.0: All authenticated Entra ID users are allowed.
    To restrict access, implement an allowlist here (e.g. from Table Storage or env var).
    """
    return True

def _proxy_secret_ok(req: func.HttpRequest) -> bool:
    """
    Verify the request came from the Web App proxy by checking the shared
    secret header. The function endpoint is public, so this prevents anyone
    from spoofing the X-Forwarded-User-Email header directly.

    Enforced only when PROXY_SHARED_SECRET is set (so local dev and any
    brief pre-rollout window fail open rather than locking everyone out).
    """
    expected = os.environ.get("PROXY_SHARED_SECRET")
    if not expected:
        return True
    provided = req.headers.get("X-Proxy-Secret") or req.headers.get("x-proxy-secret")
    return provided == expected

def _decode_jwt_payload(token: str) -> dict | None:
    """Decode the payload of a JWT token without verification (base64 only)."""
    try:
        parts = token.split('.')
        if len(parts) != 3:
            return None
        # JWT base64url decode (add padding)
        payload_b64 = parts[1]
        payload_b64 += '=' * (4 - len(payload_b64) % 4)
        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        return json.loads(payload_bytes)
    except Exception as e:
        logging.error(f"Error decoding JWT payload: {e}")
        return None

def get_user_email_from_header(req: func.HttpRequest) -> str | None:
    """
    Extracts the user email from the X-MS-CLIENT-PRINCIPAL header 
    injected by Azure Static Web Apps or App Service Auth,
    or from the Authorization Bearer JWT token.
    """
    # 0. Reject requests that didn't come through the Web App proxy
    if not _proxy_secret_ok(req):
        logging.warning("Proxy shared secret missing or mismatched; rejecting request")
        return None

    # 1. Try Direct Header (Standard Easy Auth)
    val = req.headers.get("X-MS-CLIENT-PRINCIPAL-NAME") or req.headers.get("x-ms-client-principal-name")
    if val:
        logging.info(f"Authenticated as (Header Name): {val}")
        return val

    # 2. Try forwarded header from Web App proxy (server.js)
    forwarded = req.headers.get("X-Forwarded-User-Email") or req.headers.get("x-forwarded-user-email")
    if forwarded:
        logging.info(f"Authenticated as (Forwarded): {forwarded}")
        return forwarded

    # 3. Try Base64 Header (SWA / Advanced)
    header = req.headers.get("x-ms-client-principal") or req.headers.get("X-MS-CLIENT-PRINCIPAL")
    if header:
        try:
            decoded = base64.b64decode(header).decode("utf-8")
            principal = json.loads(decoded)
            email = principal.get("userDetails")
            logging.info(f"Authenticated as (Base64): {email}")
            return email
        except Exception as e:
            logging.error(f"Error parsing auth header: {e}")

    # 3. Try Authorization Bearer token (JWT id_token or access_token from App Service Easy Auth)
    auth_header = req.headers.get("Authorization") or req.headers.get("authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header[7:]
        claims = _decode_jwt_payload(token)
        if claims:
            logging.info(f"JWT claims present: {list(claims.keys())}")
            # Try standard email claims (works for work accounts)
            email = claims.get("preferred_username") or claims.get("email") or claims.get("upn") or claims.get("unique_name")
            # Personal accounts (live.com/outlook.com) may use 'emails' array
            if not email:
                emails = claims.get("emails")
                if isinstance(emails, list) and emails:
                    email = emails[0]
            # Last resort: use 'name' or 'oid' as identity
            if not email:
                email = claims.get("name") or claims.get("oid")
            if email:
                logging.info(f"Authenticated as (Bearer JWT): {email}")
                return email
            else:
                logging.warning(f"JWT decoded but no usable identity claim found. Claims: {list(claims.keys())}")

    # DEBUG: Log if no identity found
    safe_headers = {k: v for k, v in req.headers.items() if 'auth' not in k.lower() and 'cookie' not in k.lower()}
    logging.warning(f"No Identity found. Safe Headers: {safe_headers}")
    return None

@app.route(route="check_user_permission", auth_level=func.AuthLevel.ANONYMOUS)
def check_user_permission(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Checking user permission...')
    
    email = get_user_email_from_header(req)
    
    if not email:
        return func.HttpResponse(json.dumps({"allowed": False, "email": None}), mimetype="application/json", status_code=401)

    return func.HttpResponse(json.dumps({"allowed": True, "email": email}), mimetype="application/json")


def get_blob_service_client():
    """Helper to connect to Blob Storage"""
    try:
        # Managed Identity
        account_name = os.environ.get("AzureWebJobsStorage__accountName")
        if account_name:
            credential = DefaultAzureCredential()
            account_url = f"https://{account_name}.blob.core.windows.net"
            return BlobServiceClient(account_url=account_url, credential=credential)
        
        # Connection String
        connection_string = os.environ.get("AzureWebJobsStorage")
        if connection_string:
            return BlobServiceClient.from_connection_string(connection_string)
            
        return None
    except Exception as e:
        logging.error(f"Failed to create blob client: {e}")
        return None

def get_platform_container_client():
    """
    Container client for the platform's shared competition file pool
    (competition-data/<platform-guid>/uploads/...), hosted in the site repo's
    storage account. Returns None when PLATFORM_STORAGE_ACCOUNT is unset, which
    cleanly turns the pool-import feature off.

    No connection string for that account exists, deliberately: access rests
    on the Storage Blob Data *Reader* grant to this Function App's system
    identity (site repo's shared-data-access.bicep), so the app can never
    write there. DefaultAzureCredential resolves to that managed identity in
    production; its dev-credential fallbacks only matter locally, where the
    developer's own RBAC decides.
    """
    account_name = os.environ.get("PLATFORM_STORAGE_ACCOUNT")
    if not account_name:
        return None
    # Creation failures propagate: the import route maps them to 502
    # "platform_unavailable", which is distinct from the deliberate
    # feature-off None (503 "platform_not_configured").
    container_name = os.environ.get("PLATFORM_DATA_CONTAINER") or "competition-data"
    client = BlobServiceClient(
        account_url=f"https://{account_name}.blob.core.windows.net",
        credential=DefaultAzureCredential(),
    )
    return client.get_container_client(container_name)


class _PoolFileTooLarge(ValueError):
    """Pool blob exceeds MAX_UPLOAD_SIZE — the import route maps it to 413."""


class _PoolReadError(Exception):
    """Reading a pool blob failed — the import route maps it to 502."""


def _copy_pool_blob(pool_container, pool_path, container, blob_path, metadata):
    """
    Copy one blob out of the platform file pool into this tool's container,
    stamping provenance metadata (poolSource / poolUploadedUtc / importedBy)
    on the destination so a later refresh can tell what it already holds.

    Read-side failures are raised as _PoolFileTooLarge (413),
    ResourceNotFoundError (404) or _PoolReadError (502); upload failures
    propagate unchanged (500) — the import route maps all four.
    """
    try:
        data = pool_container.get_blob_client(pool_path).download_blob().readall()
    except ResourceNotFoundError:
        raise
    except Exception as e:
        raise _PoolReadError(f"could not read pool blob {pool_path}: {e}") from e

    if len(data) > MAX_UPLOAD_SIZE:
        raise _PoolFileTooLarge(pool_path)

    container.upload_blob(blob_path, data, overwrite=True, metadata=metadata)


def _refresh_from_pool(entity, container, folder_path):
    """
    Re-copy every file this competition already holds that is newer in the
    platform file pool, and return the (sorted) names that were replaced.

    The HOVTP listener overwrites pool blobs under the same stable filename on
    every FS Manager re-export, and the UI hides pool files the tool already
    holds — so without this the tool would keep generating from a stale copy.
    Only names already held are refreshed: nothing is ever added or deleted.

    Best effort by design: this runs on the read path (details + generate) and
    must never break it, so every failure is logged and swallowed.
    """
    refreshed = []
    try:
        platform_id = (entity or {}).get("PlatformId")
        if not platform_id or not _UUID_RE.fullmatch(str(platform_id)):
            return []

        try:
            pool_container = get_platform_container_client()
        except Exception as e:
            logging.warning(f"Pool refresh: could not open the competition file pool: {e}")
            return []
        if pool_container is None:
            return []

        # What we hold today, and how fresh that copy is: the stamped pool
        # timestamp when we imported it, else the blob's own last_modified.
        own = {}
        prefix = f"{folder_path}/"
        for blob in container.list_blobs(name_starts_with=prefix, include=["metadata"]):
            name = blob.name[len(prefix):]
            if not name or '/' in name:
                continue
            if name in ("metadata.json", "init.md"):
                continue
            if not name.lower().endswith(".pdf"):
                continue
            recorded = _parse_iso_utc((blob.metadata or {}).get("poolUploadedUtc"))
            if recorded is None:
                recorded = _as_utc(blob.last_modified)
            own[name] = recorded
        if not own:
            return []

        # Newest pool candidate per name, across both pool folders.
        candidates = {}
        for source, folder in (("upload", "uploads"), ("fsm", "fsm")):
            pool_prefix = f"{platform_id}/{folder}/"
            try:
                for blob in pool_container.list_blobs(name_starts_with=pool_prefix):
                    name = blob.name[len(pool_prefix):]
                    if not name or '/' in name or name not in own:
                        continue
                    modified = _as_utc(blob.last_modified)
                    if modified is None:
                        continue
                    current = candidates.get(name)
                    if current is None or modified > current[0]:
                        candidates[name] = (modified, source, blob.name)
            except Exception as e:
                logging.warning(f"Pool refresh: could not list {pool_prefix}: {e}")

        for name in sorted(candidates):
            modified, source, pool_path = candidates[name]
            recorded = own.get(name)
            if recorded is not None and modified <= recorded:
                continue
            try:
                _copy_pool_blob(pool_container, pool_path, container, f"{folder_path}/{name}", {
                    "poolSource": source,
                    "poolUploadedUtc": _iso_utc(modified),
                    "importedBy": "pool-sync",
                })
                refreshed.append(name)
            except _PoolFileTooLarge:
                logging.warning(
                    f"Pool refresh: {pool_path} exceeds the {MAX_UPLOAD_SIZE // (1024*1024)} MB limit, skipping")
            except Exception as e:
                logging.warning(f"Pool refresh: could not refresh {name} from {pool_path}: {e}")

        if refreshed:
            logging.info(
                f"Pool refresh: replaced {len(refreshed)} file(s) in {folder_path} with newer pool copies")
    except Exception as e:
        logging.warning(f"Pool refresh failed for {folder_path}: {e}")

    return sorted(refreshed)


def get_table_client(table_name="generatedpapers"):
    """Helper to connect to Table Storage"""
    try:
        # Managed Identity
        account_name = os.environ.get("AzureWebJobsStorage__accountName")
        if account_name:
            credential = DefaultAzureCredential()
            endpoint = f"https://{account_name}.table.core.windows.net"
            return TableClient(endpoint=endpoint, table_name=table_name, credential=credential)
        
        # Connection String
        connection_string = os.environ.get("AzureWebJobsStorage")
        if connection_string:
            return TableClient.from_connection_string(conn_str=connection_string, table_name=table_name)
            
        return None
    except Exception as e:
        logging.error(f"Failed to create table client: {e}")
        return None

def sanitize_name(name: str) -> str:
    """Sanitize a competition display name: keep alphanumerics, spaces, hyphens, underscores."""
    return "".join([c for c in name if c.isalnum() or c in (' ', '-', '_')]).strip()


def normalize_competition_name(name) -> str:
    """
    Normalize a competition name for platform matching: NFKD diacritic folding,
    alphanumerics only, casefold. Mirrors the site's shared-ui
    `normalizeCompetitionCode` semantics so "Kevät Cup 2026" and
    "kevatcup2026"-style variants collapse to the same key.
    """
    if not name:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(name))
    return "".join(c for c in decomposed
                   if c.isalnum() and not unicodedata.combining(c)).casefold()


def generate_competition_id(comp_table) -> str:
    """Generate a unique 8-char hex competition id (checked against the competitions table)."""
    for _ in range(5):
        cid = uuid4().hex[:8]
        try:
            comp_table.get_entity(partition_key="GLOBAL", row_key=cid)
        except ResourceNotFoundError:
            return cid
    raise RuntimeError("Could not generate a unique competition id")


def get_competition_entity(comp_id: str):
    """Look up a competition entity by its id (RowKey). Returns None if not found."""
    try:
        comp_table = get_table_client("competitions")
        if not comp_table:
            return None
        return comp_table.get_entity(partition_key="GLOBAL", row_key=comp_id)
    except ResourceNotFoundError:
        return None
    except Exception as e:
        logging.error(f"Error fetching competition entity {comp_id}: {e}")
        return None


def _parse_iso_utc(value):
    """
    Parse a stored ISO timestamp (naive UTC with a trailing 'Z', as written by
    `datetime.utcnow().isoformat() + "Z"`) into an aware UTC datetime.
    Returns None on any failure — callers must treat None as "unknown", never
    as "expired".
    """
    if not value or not isinstance(value, str):
        return None
    try:
        s = value[:-1] if value.endswith("Z") else value
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception as e:
        logging.warning(f"Could not parse ISO timestamp '{value}': {e}")
        return None


def _as_utc(dt):
    """Normalize a datetime to an aware UTC one (None for anything else)."""
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso_utc(dt):
    """
    Format a datetime as the ISO-8601 UTC string this codebase stores
    everywhere (naive UTC + 'Z', round-trips through `_parse_iso_utc`).
    Returns "" for None/garbage — blob metadata values must be strings.
    """
    dt = _as_utc(dt)
    if dt is None:
        return ""
    return dt.replace(tzinfo=None).isoformat() + "Z"


def _ensure_deletion_date(comp_table, entity):
    """
    Lazy migration: rows created before the auto-deletion feature have no
    DeletionDate — backfill the fixed migration date. Mutates the in-memory
    entity and MERGE-writes the row (best-effort, mirrors migrate_legacy_row).
    """
    if entity.get("DeletionDate"):
        return entity
    entity["DeletionDate"] = LEGACY_DELETION_DATE
    try:
        comp_table.update_entity({
            "PartitionKey": "GLOBAL",
            "RowKey": entity["RowKey"],
            "DeletionDate": LEGACY_DELETION_DATE,
        }, mode=UpdateMode.MERGE)
    except Exception as e:
        logging.warning(f"Failed to backfill DeletionDate for {entity['RowKey']}: {e}")
    return entity


def _bump_competition_counters(comp_id, uploaded_delta=0, generate_delta=0, set_last_generated=False):
    """
    Best-effort update of usage counters on the competitions entity.
    Never raises — counter failures must not fail the user's operation.
    """
    try:
        comp_table = get_table_client("competitions")
        if not comp_table:
            return
        entity = comp_table.get_entity(partition_key="GLOBAL", row_key=comp_id)
        update = {"PartitionKey": "GLOBAL", "RowKey": comp_id}
        if uploaded_delta:
            update["UploadedFileCount"] = max(0, int(entity.get("UploadedFileCount", 0)) + uploaded_delta)
        if generate_delta:
            update["GenerateRunCount"] = int(entity.get("GenerateRunCount", 0)) + generate_delta
        if set_last_generated:
            update["LastGeneratedDate"] = f"{datetime.utcnow().isoformat()}Z"
        comp_table.update_entity(update, mode=UpdateMode.MERGE)
    except Exception as e:
        logging.warning(f"Failed to update counters for competition {comp_id}: {e}")


def _store_competition_statistics(comp_id, stats):
    """
    Best-effort write of the usage-statistics snapshot (returned by the
    processor) onto the competitions entity. Overwrites the previous snapshot;
    only TotalPagesGenerated accumulates across generate runs.
    Never raises — statistics failures must not fail the user's operation.
    """
    if not stats:
        return
    try:
        comp_table = get_table_client("competitions")
        if not comp_table:
            return
        entity = comp_table.get_entity(partition_key="GLOBAL", row_key=comp_id)

        pages = int(stats.get("pages_generated", 0))
        update = {
            "PartitionKey": "GLOBAL",
            "RowKey": comp_id,
            "PagesGenerated": pages,
            "TotalPagesGenerated": int(entity.get("TotalPagesGenerated", 0)) + pages,
            "LastStatisticsDate": f"{datetime.utcnow().isoformat()}Z",
        }

        # snake_case stats key -> (PascalCase column, JSON-encode?)
        column_map = {
            "competition_type": ("CompetitionType", False),
            "categories": ("CategoriesJson", True),
            "category_count": ("CategoryCount", False),
            "competitor_count": ("CompetitorCount", False),
            "segment_count": ("SegmentCount", False),
            "segment_types": ("SegmentTypesJson", True),
            "judge_assignment_count": ("JudgeAssignmentCount", False),
            "unique_judge_count": ("UniqueJudgeCount", False),
            "unique_official_count": ("UniqueOfficialCount", False),
            "officials_by_role": ("OfficialsByRoleJson", True),
            "withdrawn_count": ("WithdrawnCount", False),
            "day_count": ("CompetitionDayCount", False),
            "first_date": ("FirstCompetitionDate", False),
            "last_date": ("LastCompetitionDate", False),
            "judging_method": ("JudgingMethod", False),
            "language": ("PacketLanguage", False),
        }
        for key, (column, as_json) in column_map.items():
            value = stats.get(key)
            if value is None:
                continue  # skip unknowns (e.g. CompetitorCount without a schedule)
            update[column] = json.dumps(value, ensure_ascii=False) if as_json else value

        comp_table.update_entity(update, mode=UpdateMode.MERGE)
    except Exception as e:
        logging.warning(f"Failed to store statistics for competition {comp_id}: {e}")


def migrate_legacy_row(comp_table, entity):
    """
    Migrate a legacy competitions row (RowKey = competition name, name-based blob
    folder) to the id-keyed schema. Azure Tables cannot rename a RowKey, so a new
    entity is created and the old one deleted. The blob folder is left untouched;
    FolderPath points at it. generatedpapers rows are re-keyed name -> id.
    Returns the new entity.
    """
    old_name = entity["RowKey"]
    new_id = generate_competition_id(comp_table)
    logging.info(f"Migrating legacy competition '{old_name}' to id {new_id}")

    new_entity = {
        "PartitionKey": "GLOBAL",
        "RowKey": new_id,
        "Name": entity.get("Name", old_name),
        "FolderPath": old_name,
        "Visible": True,
        "CreatedBy": entity.get("CreatedBy", "-"),
        "CreatedDate": entity.get("CreatedDate", "-"),
        "UploadedFileCount": 0,
        "GenerateRunCount": 0
    }

    # Re-key generatedpapers rows (PartitionKey: old name -> new id)
    try:
        papers_table = get_table_client()
        if papers_table:
            safe_pk = old_name.replace("'", "''")
            for paper in list(papers_table.query_entities(f"PartitionKey eq '{safe_pk}'")):
                new_paper = dict(paper)
                new_paper["PartitionKey"] = new_id
                papers_table.upsert_entity(new_paper)
                papers_table.delete_entity(partition_key=old_name, row_key=paper["RowKey"])
    except Exception as e:
        logging.warning(f"Failed to re-key generatedpapers rows for '{old_name}': {e}")

    comp_table.create_entity(new_entity)
    comp_table.delete_entity(partition_key="GLOBAL", row_key=old_name)
    return new_entity


def create_and_store_sas_link(blob_service_client, container_name, blob_name, competition, filename, file_size=0):
    try:
        table_client = get_table_client()
        if not table_client:
            logging.warning("No table client available, skipping SAS creation")
            return

        # Ensure table exists (idempotent usually, checking first saves errors)
        try:
             table_client.create_table()
        except:
             pass

        # Generate SAS
        start_time = datetime.utcnow()
        expiry = start_time + timedelta(days=5)
        sas_token = ""
        
        account_name = blob_service_client.account_name
        blob_url_base = f"https://{account_name}.blob.core.windows.net/{container_name}/{blob_name}"
        
        # Managed Identity Logic
        if os.environ.get("AzureWebJobsStorage__accountName"):
             ud_key = blob_service_client.get_user_delegation_key(start_time, expiry)
             sas_token = generate_blob_sas(
                 account_name=account_name,
                 container_name=container_name,
                 blob_name=blob_name,
                 user_delegation_key=ud_key,
                 permission=BlobSasPermissions(read=True),
                 expiry=expiry,
                 start=start_time
             )
        else:
             # Connection String Logic (Dev)
             conn_str = os.environ.get("AzureWebJobsStorage")
             if conn_str:
                 items = dict(item.split('=', 1) for item in conn_str.split(';') if '=' in item)
                 key = items.get('AccountKey')
                 if key:
                     sas_token = generate_blob_sas(
                         account_name=items.get('AccountName'),
                         container_name=container_name,
                         blob_name=blob_name,
                         account_key=key,
                         permission=BlobSasPermissions(read=True),
                         expiry=expiry,
                         start=start_time
                     )

        if sas_token:
            full_url = f"{blob_url_base}?{sas_token}"
            
            # Determine Description
            desc = "Individual judge papers (ZIP)" if filename.lower().endswith('.zip') else "All judge papers (PDF)"
            
            # Form Entity
            entity = {
                "PartitionKey": competition,
                "RowKey": filename.replace('/', '_').replace('\\', '_'),
                "Url": full_url,
                "ExpirationDate": expiry.isoformat(),
                "Description": desc,
                "FileName": filename,
                "FileSize": int(file_size)
            }
            
            table_client.upsert_entity(entity)
            logging.info(f"Stored SAS link for {filename}")
            
    except Exception as e:
        logging.error(f"Error creating SAS: {e}")


@app.route(route="list_competitions", auth_level=func.AuthLevel.ANONYMOUS)
def list_competitions(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Listing competitions...')
    
    email = get_user_email_from_header(req)
    if not email:
        return func.HttpResponse("Unauthorized", status_code=401)

    try:
        # The competitions table is the source of truth (permanent history).
        # Rows with Visible=False (soft-deleted) are kept for statistics but hidden here.
        table_client = get_table_client("competitions")
        if not table_client:
            return func.HttpResponse("Storage configuration invalid", status_code=500)

        # Ensure table exists
        try: table_client.create_table()
        except: pass

        competitions = []
        for entity in list(table_client.query_entities("PartitionKey eq 'GLOBAL'")):
            # Lazily migrate legacy rows (RowKey = name) to the id-keyed schema
            if "FolderPath" not in entity:
                try:
                    entity = migrate_legacy_row(table_client, entity)
                except Exception as e:
                    logging.error(f"Failed to migrate legacy competition '{entity['RowKey']}': {e}")

            # Hide soft-deleted competitions
            if entity.get("Visible") is False:
                continue

            # Lazily backfill DeletionDate on pre-feature rows
            _ensure_deletion_date(table_client, entity)

            competitions.append({
                "id": entity["RowKey"],
                "name": entity.get("Name", entity["RowKey"]),
                "createdBy": entity.get("CreatedBy", "-"),
                "createdDate": entity.get("CreatedDate", "-"),
                "deletionDate": entity.get("DeletionDate", "-")
            })

        return func.HttpResponse(json.dumps(competitions), mimetype="application/json")
    except Exception as e:
        logging.error(f"Error listing competitions: {e}")
        return func.HttpResponse(json.dumps({"error": "Internal server error"}), status_code=500, mimetype="application/json")


def create_competition_record(comp_table, blob_service_client, safe_name, email, platform_id=None):
    """
    Create a competition: the blob "folder" (its metadata.json) plus the
    permanent competitions history row. Shared by `create_competition` and
    `resolve_competition`; `platform_id` binds the record to the site's
    platform competition (omitted for the legacy standalone create route).
    Returns the created entity dict.
    """
    new_id = generate_competition_id(comp_table)
    folder_path = f"{safe_name}-{new_id}"

    # Create metadata.json file to establish the "folder"
    now = datetime.utcnow()
    metadata = {
        "id": new_id,
        "name": safe_name,
        "createdBy": email,
        "createdDate": f"{now.isoformat()}Z"
    }
    if platform_id:
        metadata["platformId"] = platform_id

    container = blob_service_client.get_container_client("fs-judgepapers")
    container.upload_blob(f"{folder_path}/metadata.json", json.dumps(metadata, indent=4), overwrite=True)

    # Permanent history row — never deleted, only hidden via Visible=False
    entity = {
        "PartitionKey": "GLOBAL",
        "RowKey": new_id,
        "Name": safe_name,
        "FolderPath": folder_path,
        "Visible": True,
        "CreatedBy": metadata["createdBy"],
        "CreatedDate": metadata["createdDate"],
        "DeletionDate": f"{(now + timedelta(days=DELETION_RETENTION_DAYS)).isoformat()}Z",
        "UploadedFileCount": 0,
        "GenerateRunCount": 0
    }
    if platform_id:
        entity["PlatformId"] = platform_id
    comp_table.create_entity(entity)
    return entity


@app.route(route="create_competition", auth_level=func.AuthLevel.ANONYMOUS)
def create_competition(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Creating competition...')

    email = get_user_email_from_header(req)
    if not email:
        return func.HttpResponse("Unauthorized", status_code=401)

    name = req.params.get('name')
    if not name:
        return func.HttpResponse("Missing name parameter", status_code=400)

    # Sanitize name (names are NOT unique — the id is the identifier)
    safe_name = sanitize_name(name)
    if not safe_name:
         return func.HttpResponse("Invalid name", status_code=400)

    try:
        blob_service_client = get_blob_service_client()
        if not blob_service_client:
             return func.HttpResponse("Storage configuration invalid", status_code=500)

        comp_table = get_table_client("competitions")
        if not comp_table:
            return func.HttpResponse("Storage configuration invalid", status_code=500)

        # Ensure table exists
        try: comp_table.create_table()
        except: pass

        entity = create_competition_record(comp_table, blob_service_client, safe_name, email)

        return func.HttpResponse(json.dumps({"id": entity["RowKey"], "name": safe_name}), mimetype="application/json")
    except Exception as e:
        logging.error(f"Error creating competition: {e}")
        return func.HttpResponse("Internal server error", status_code=500)


def _newest_competition(entities):
    """Pick the entity with the newest CreatedDate (unparseable dates sort oldest)."""
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    return max(entities, key=lambda e: _parse_iso_utc(e.get("CreatedDate")) or oldest)


def find_competition_by_platform_id(comp_table, platform_id):
    """
    Find the live competition bound to a platform competition GUID.

    The PlatformId filter runs server-side; `Visible is False` is filtered in
    Python because legacy rows lack the property altogether (a `Visible eq true`
    filter would drop them). Soft-deleted rows keep their PlatformId but their
    blobs are gone, so they must never be resurrected. Newest CreatedDate wins
    if several visible rows share the id.
    """
    safe_pid = str(platform_id).replace("'", "''")
    rows = list(comp_table.query_entities(
        f"PartitionKey eq 'GLOBAL' and PlatformId eq '{safe_pid}'"
    ))
    visible = [e for e in rows if e.get("Visible") is not False]
    if not visible:
        return None
    return _newest_competition(visible)


def adopt_competition_by_name(comp_table, platform_id, name):
    """
    Link a pre-existing, unbound competition to a platform competition: a
    visible row with no PlatformId whose normalized Name matches the platform
    name gets PlatformId stamped on it (MERGE write) and is returned. Legacy
    name-keyed rows (no FolderPath) are skipped — `list_competitions` re-keys
    them, which would drop the binding. Returns None when nothing matches.
    """
    target = normalize_competition_name(name)
    if not target:
        return None

    candidates = []
    for entity in list(comp_table.query_entities("PartitionKey eq 'GLOBAL'")):
        if entity.get("Visible") is False:
            continue
        if entity.get("PlatformId"):
            continue
        if "FolderPath" not in entity:
            continue
        if normalize_competition_name(entity.get("Name") or entity.get("RowKey")) != target:
            continue
        candidates.append(entity)

    if not candidates:
        return None

    entity = _newest_competition(candidates)
    comp_table.update_entity({
        "PartitionKey": "GLOBAL",
        "RowKey": entity["RowKey"],
        "PlatformId": platform_id
    }, mode=UpdateMode.MERGE)
    entity["PlatformId"] = platform_id
    logging.info(f"Adopted competition {entity['RowKey']} for platform id {platform_id}")
    return entity


def resolve_competition_record(comp_table, blob_service_client, platform_id, name, email):
    """
    Core of POST /resolve_competition: platform-id lookup -> name adoption ->
    create. Pure with respect to HTTP (clients are injected) so it can be driven
    by fakes. Returns the response payload dict {id, name, created}.
    """
    entity = find_competition_by_platform_id(comp_table, platform_id)
    if entity is not None:
        return {"id": entity["RowKey"], "name": entity.get("Name", entity["RowKey"]), "created": False}

    entity = adopt_competition_by_name(comp_table, platform_id, name)
    if entity is not None:
        return {"id": entity["RowKey"], "name": entity.get("Name", entity["RowKey"]), "created": False}

    safe_name = sanitize_name(name)
    entity = create_competition_record(
        comp_table, blob_service_client, safe_name, email, platform_id=platform_id
    )
    return {"id": entity["RowKey"], "name": safe_name, "created": True}


@app.route(route="resolve_competition", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST"])
def resolve_competition(req: func.HttpRequest) -> func.HttpResponse:
    """
    Bind the site's active platform competition to this tool's competition
    record: look it up by PlatformId, else adopt a matching unbound record,
    else create one. Body {platformId, name} -> {id, name, created}.
    """
    logging.info('Resolving platform competition...')

    email = get_user_email_from_header(req)
    if not email:
        return func.HttpResponse("Unauthorized", status_code=401)

    try:
        req_body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON body", status_code=400)

    platform_id = (req_body or {}).get('platformId')
    name = (req_body or {}).get('name')
    if not platform_id or not isinstance(platform_id, str):
        return func.HttpResponse("Missing platformId parameter", status_code=400)
    if not name or not sanitize_name(str(name)):
        return func.HttpResponse("Missing or invalid name parameter", status_code=400)

    try:
        blob_service_client = get_blob_service_client()
        if not blob_service_client:
            return func.HttpResponse("Storage configuration invalid", status_code=500)

        comp_table = get_table_client("competitions")
        if not comp_table:
            return func.HttpResponse("Storage configuration invalid", status_code=500)

        # Ensure table exists
        try: comp_table.create_table()
        except: pass

        result = resolve_competition_record(
            comp_table, blob_service_client, platform_id, str(name), email
        )
        logging.info(f"Resolved platform {platform_id} -> competition {result['id']} (created={result['created']})")
        return func.HttpResponse(json.dumps(result), mimetype="application/json")
    except Exception as e:
        logging.error(f"Error resolving competition: {e}")
        return func.HttpResponse("Internal server error", status_code=500)


def _delete_competition_data(entity, deleted_by):
    """
    Shared deletion flow used by the HTTP endpoint and the auto-deletion timer:
    delete the competition's blobs and generatedpapers rows, then soft-delete
    the competitions row (Visible=False + deletion audit). Returns the number
    of blobs deleted. Raises on hard failures (caller decides how to handle).
    """
    comp_id = entity["RowKey"]
    folder_path = entity.get("FolderPath", comp_id)

    blob_service_client = get_blob_service_client()
    if not blob_service_client:
        raise RuntimeError("Storage configuration invalid")

    container = blob_service_client.get_container_client("fs-judgepapers")

    # List all blobs with this prefix and delete them
    blobs = container.list_blobs(name_starts_with=f"{folder_path}/")
    count = 0
    for blob in blobs:
        container.delete_blob(blob.name)
        count += 1

    # Delete generated-papers table entities for this competition
    try:
        table_client = get_table_client()
        if table_client:
            safe_pk = comp_id.replace("'", "''")
            entities = table_client.query_entities(f"PartitionKey eq '{safe_pk}'")
            table_count = 0
            for paper in entities:
                table_client.delete_entity(partition_key=paper['PartitionKey'], row_key=paper['RowKey'])
                table_count += 1
            logging.info(f"Deleted {table_count} table rows for competition {comp_id}")
    except Exception as table_err:
        logging.warning(f"Error deleting table entities: {table_err}")

    # Keep the competitions row as permanent history: hide it and record the deletion
    comp_table = get_table_client("competitions")
    comp_table.update_entity({
        "PartitionKey": "GLOBAL",
        "RowKey": comp_id,
        "Visible": False,
        "DeletedDate": f"{datetime.utcnow().isoformat()}Z",
        "DeletedBy": deleted_by
    }, mode=UpdateMode.MERGE)

    return count


@app.route(route="delete_competition", auth_level=func.AuthLevel.ANONYMOUS)
def delete_competition(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Deleting competition...')

    email = get_user_email_from_header(req)
    if not email:
        return func.HttpResponse("Unauthorized", status_code=401)

    comp_id = req.params.get('id')
    if not comp_id:
        return func.HttpResponse("Missing id parameter", status_code=400)

    try:
        entity = get_competition_entity(comp_id)
        if not entity:
            return func.HttpResponse("Competition not found", status_code=404)

        count = _delete_competition_data(entity, email)

        return func.HttpResponse(json.dumps({"deleted": count}), mimetype="application/json")
    except Exception as e:
        logging.error(f"Error deleting competition: {e}")
        return func.HttpResponse("Internal server error", status_code=500)


@app.route(route="extend_competition_deletion", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST", "GET"])
def extend_competition_deletion(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Extending competition deletion date...')

    email = get_user_email_from_header(req)
    if not email:
        return func.HttpResponse("Unauthorized", status_code=401)

    comp_id = req.params.get('id')
    if not comp_id:
        return func.HttpResponse("Missing id parameter", status_code=400)

    try:
        comp_table = get_table_client("competitions")
        if not comp_table:
            return func.HttpResponse("Storage configuration invalid", status_code=500)

        entity = get_competition_entity(comp_id)
        if not entity or entity.get("Visible") is False:
            return func.HttpResponse("Competition not found", status_code=404)

        _ensure_deletion_date(comp_table, entity)

        # Extend from the current deletion date, but never land in the past:
        # if the date already passed (timer hasn't swept yet), extend from now.
        now = datetime.now(timezone.utc)
        current = _parse_iso_utc(entity.get("DeletionDate")) or now
        new_deletion = max(current, now) + timedelta(days=DELETION_EXTENSION_DAYS)
        new_str = f"{new_deletion.replace(tzinfo=None).isoformat()}Z"

        comp_table.update_entity({
            "PartitionKey": "GLOBAL",
            "RowKey": comp_id,
            "DeletionDate": new_str
        }, mode=UpdateMode.MERGE)

        logging.info(f"Extended deletion date for {comp_id} to {new_str} (by {email})")
        return func.HttpResponse(json.dumps({"deletionDate": new_str}), mimetype="application/json")
    except Exception as e:
        logging.error(f"Error extending deletion date for competition: {e}")
        return func.HttpResponse("Internal server error", status_code=500)


@app.timer_trigger(schedule="0 0 3 * * *", arg_name="timer", run_on_startup=False)
def auto_delete_expired_competitions(timer: func.TimerRequest) -> None:
    """
    Daily sweep (03:00 UTC): delete competitions whose DeletionDate has passed,
    using the same flow as manual deletion. Rows without a DeletionDate are
    backfilled with the migration date instead of being treated as expired.
    """
    logging.info("Running auto-deletion sweep for expired competitions...")
    try:
        comp_table = get_table_client("competitions")
        if not comp_table:
            logging.error("Auto-deletion: storage configuration invalid")
            return

        now = datetime.now(timezone.utc)
        deleted = 0
        for entity in list(comp_table.query_entities("PartitionKey eq 'GLOBAL'")):
            if entity.get("Visible") is False:
                continue
            # Legacy name-keyed rows are migrated lazily by list_competitions;
            # skip them here rather than running the heavier migration.
            if "FolderPath" not in entity:
                continue

            _ensure_deletion_date(comp_table, entity)
            deletion = _parse_iso_utc(entity.get("DeletionDate"))
            # Never delete on an unparseable date
            if deletion is None or deletion > now:
                continue

            try:
                _delete_competition_data(entity, AUTO_CLEANUP_ACTOR)
                deleted += 1
                logging.info(f"Auto-deleted expired competition {entity['RowKey']} ({entity.get('Name', '?')})")
            except Exception as e:
                logging.error(f"Auto-deletion failed for {entity['RowKey']}: {e}")

        logging.info(f"Auto-deletion sweep complete. Deleted {deleted} competition(s).")
    except Exception as e:
        logging.error(f"Auto-deletion sweep error: {e}")


def _get_categories():
    """Load categories from the Azure Table, with caching."""
    try:
        table_client = get_table_client("categories")
        if table_client:
            return load_categories(table_client)
    except Exception as e:
        logging.warning(f"Failed to load categories table: {e}")
    return []


def parse_competition_file(filename: str, categories=None):
    """
    Parses the filename to extract Type, Category, Segment, JudgingMethod, and Suffix.
    Uses the 'categories' Azure Table for abbreviation matching (longest-prefix-match).
    Segment detection (QUAL/FNL) and split/group numbers are parsed generically.
    """
    try:
        if categories is None:
            categories = _get_categories()

        result = parse_filename_generic(filename, categories)
        return result
    except Exception:
        return None

@app.route(route="get_categories", auth_level=func.AuthLevel.ANONYMOUS)
def get_categories(req: func.HttpRequest) -> func.HttpResponse:
    """Returns all competition categories from the categories table."""
    logging.info('Getting categories...')
    
    email = get_user_email_from_header(req)
    if not email:
        return func.HttpResponse("Unauthorized", status_code=401)

    try:
        categories = _get_categories()
        return func.HttpResponse(json.dumps(categories), mimetype="application/json")
    except Exception as e:
        logging.error(f"Error getting categories: {e}")
        return func.HttpResponse(json.dumps({"error": "Internal server error"}), status_code=500, mimetype="application/json")


@app.route(route="get_competition_details", auth_level=func.AuthLevel.ANONYMOUS)
def get_competition_details(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Getting competition details...')
    
    email = get_user_email_from_header(req)
    if not email:
        return func.HttpResponse("Unauthorized", status_code=401)
        
    comp_id = req.params.get('id')
    if not comp_id:
        return func.HttpResponse("Missing id parameter", status_code=400)

    try:
        entity = get_competition_entity(comp_id)
        if not entity:
            return func.HttpResponse("Competition not found", status_code=404)

        folder_path = entity.get("FolderPath", entity["RowKey"])
        display_name = entity.get("Name", folder_path)

        blob_service_client = get_blob_service_client()
        if not blob_service_client: return func.HttpResponse("Config Error", status_code=500)

        container = blob_service_client.get_container_client("fs-judgepapers")

        # Pre-load categories once for all files
        categories = _get_categories()

        # Load competition settings from metadata.json
        competition_language = 'fi'  # Default to Finnish
        try:
            metadata_blob = container.get_blob_client(f"{folder_path}/metadata.json")
            if metadata_blob.exists():
                meta_stream = metadata_blob.download_blob().readall()
                meta = json.loads(meta_stream)
                competition_language = meta.get('language', 'fi')
        except Exception:
            pass

        # FS Manager re-exports overwrite the pool copy under the same name;
        # pull any newer pool version in before listing what we hold.
        refreshed = _refresh_from_pool(entity, container, folder_path)

        blobs = container.list_blobs(name_starts_with=f"{folder_path}/", include=["metadata"])
        
        files_data = []
        structure = {} # { category: { segment: [files] } }
        
        detected_types = set()
        detected_names = set()
        detected_dates = set()
        detected_category_codes = set()
        
        # For enriching segment names:
        # Maps (categoryCode, raw_segment_marker) -> actual segment name
        prefix_segment_names = {}  # prefix -> segment name from JudgesSheetAll line 2
        schedule_blob_name = None  # Track CompetitionSchedule blob for later parsing

        for blob in blobs:
            # Filter out files in subfolders (only process root of competition folder)
            # blob.name is "{folder_path}/{filename}"
            # We want to skip "{folder_path}/{subfolder}/{filename}"
            if '/' in blob.name[len(folder_path)+1:]:
                continue

            # Skip the init.md or metadata.json file
            if blob.name.endswith('init.md') or blob.name.endswith('metadata.json'):
                continue
                
            parsed = parse_competition_file(blob.name, categories)
            if parsed:
                # files_data, structure and competitionFiles share this dict.
                parsed['lastModified'] = _iso_utc(blob.last_modified)
                parsed['poolSource'] = (blob.metadata or {}).get('poolSource')
                files_data.append(parsed)
                cat = parsed['category']
                seg = parsed['segment']
                
                if parsed.get('categoryCode'):
                    detected_category_codes.add(parsed['categoryCode'])
                
                # Track CompetitionSchedule blob for later parsing
                if parsed['suffix'] == 'CompetitionSchedule.pdf':
                    schedule_blob_name = blob.name
                
                # Analyze Type (now from categories table, skip 'Competition' pseudo-type)
                if parsed.get('type') and parsed['type'] != 'Competition':
                    detected_types.add(parsed['type'])
                
                # Analyze Name (from JudgesSheetAll)
                if "JudgesDetailsAll" in parsed['suffix'] or "JudgesSheetAll" in parsed['suffix']:
                    try:
                        blob_client = container.get_blob_client(blob.name)
                        stream = io.BytesIO()
                        blob_client.download_blob().readinto(stream)
                        stream.seek(0)
                        
                        reader = PdfReader(stream)
                        if len(reader.pages) > 0:
                            # Use layout mode to respect visual order (Top-down)
                            try:
                                text = reader.pages[0].extract_text(extraction_mode="layout")
                            except Exception:
                                text = reader.pages[0].extract_text()
                                
                            if text:
                                lines = text.splitlines()
                                non_empty = [l.strip() for l in lines if l.strip()]
                                
                                # Line 1 = competition name
                                if non_empty:
                                    detected_names.add(non_empty[0])
                                    parsed['competition_name'] = non_empty[0]
                                
                                # Line 2 = full segment description (category + segment name)
                                # Extract just the segment part by stripping the category display name
                                if len(non_empty) >= 2:
                                    full_seg_line = non_empty[1]
                                    seg_name = full_seg_line
                                    
                                    # Get category display name for this file
                                    cat_display = parsed.get('category', '')
                                    # Strip "#N" split suffix if present
                                    cat_base = re.sub(r'\s*#\d+$', '', cat_display)
                                    
                                    if cat_base:
                                        upper_line = full_seg_line.upper()
                                        upper_cat = cat_base.upper()
                                        if upper_line.startswith(upper_cat):
                                            seg_name = full_seg_line[len(cat_base):].strip()
                                        else:
                                            # Fuzzy: normalize punctuation and try again
                                            norm_line = re.sub(r'[,./\-]', ' ', upper_line)
                                            norm_line = re.sub(r'\s+', ' ', norm_line).strip()
                                            norm_cat = re.sub(r'[,./\-]', ' ', upper_cat)
                                            norm_cat = re.sub(r'\s+', ' ', norm_cat).strip()
                                            if norm_line.startswith(norm_cat):
                                                # Walk original string to find split position
                                                ci = 0
                                                for i, ch in enumerate(full_seg_line):
                                                    if ci >= len(norm_cat):
                                                        seg_name = full_seg_line[i:].strip()
                                                        break
                                                    uch = ch.upper()
                                                    if uch in ',.-/' or (uch == ' ' and ci > 0 and norm_cat[ci-1] == ' '):
                                                        continue
                                                    if ci < len(norm_cat) and uch == norm_cat[ci]:
                                                        ci += 1
                                    
                                    if seg_name:
                                        file_prefix = parsed.get('prefix', '')
                                        if file_prefix:
                                            prefix_segment_names[file_prefix] = seg_name
                                            parsed['segment_display_name'] = seg_name
                    except Exception as ex:
                        logging.warning(f"Error reading PDF {blob.name}: {ex}")

                # Analyze Dates (from StartListwithTimes)
                if "StartListwithTimes" in parsed['suffix']:
                    try:
                        blob_client = container.get_blob_client(blob.name)
                        stream = io.BytesIO()
                        blob_client.download_blob().readinto(stream)
                        stream.seek(0)
                        
                        reader = PdfReader(stream)
                        if len(reader.pages) > 0:
                            try:
                                text = reader.pages[0].extract_text(extraction_mode="layout")
                            except:
                                text = reader.pages[0].extract_text()

                            if text:
                                # Look for Event Date pattern: dd MONTH yyyy (e.g. 25 OCTOBER 2025)
                                # Usually appears in header like: SATURDAY, 25 OCTOBER 2025
                                # We ignore the d.m.yyyy format to avoid capturing "Printed at" footer timestamps
                                months = r"(?:JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)"
                                pattern = r"\b(\d{1,2})\s+(" + months + r")\s+(\d{4})\b"
                                
                                matches = re.findall(pattern, text, re.IGNORECASE)
                                for match in matches:
                                    day, month_name, year = match
                                    try:
                                        # Parse date (e.g. "25 October 2025")
                                        dt_str = f"{day} {month_name.title()} {year}"
                                        dt = datetime.strptime(dt_str, "%d %B %Y")
                                        
                                        # Store as YYYY-MM-DD for sorting
                                        detected_dates.add(dt.strftime("%Y-%m-%d"))
                                    except Exception as e:
                                        logging.warning(f"Date parse error: {e}")
                    except Exception as ex:
                        logging.warning(f"Error reading PDF {blob.name} for dates: {ex}")

                # Competition-wide files (e.g. CompetitionSchedule) go to a
                # separate list, not into the per-category structure.
                if parsed.get('type') == 'Competition':
                    pass  # handled below via competitionFiles
                else:
                    if cat not in structure:
                        structure[cat] = {}
                    if seg not in structure[cat]:
                        structure[cat][seg] = []
                    
                    structure[cat][seg].append(parsed)
            else:
                # Unparsed file
                if "Uncategorized" not in structure:
                    structure["Uncategorized"] = {}
                if "Files" not in structure["Uncategorized"]:
                    structure["Uncategorized"]["Files"] = []
                structure["Uncategorized"]["Files"].append({
                    "filename": blob.name.split('/')[-1],
                    "suffix": blob.name.split('/')[-1],
                    "lastModified": _iso_utc(blob.last_modified)
                })

        # ---------------------------------------------------------
        # Enrich segment names using JudgesSheetAll line 2
        # ---------------------------------------------------------
        # The raw segment keys are "QUAL", "FNL", "Category General", etc.
        # Enrich them with actual names from JudgesSheetAll (e.g., "PDK1 (Starlight Waltz)")
        # by looking up the prefix→segment_name mapping we built above.
        enriched_structure = {}
        for cat, segments in structure.items():
            enriched_structure[cat] = {}
            for seg_key, files in segments.items():
                # Find the best display name for this segment
                display_seg = seg_key
                if seg_key not in ("Category General", "General"):
                    # Look through files in this segment to find a prefix with a known name
                    for f in files:
                        file_prefix = f.get('prefix', '')
                        if file_prefix and file_prefix in prefix_segment_names:
                            display_seg = prefix_segment_names[file_prefix]
                            break
                    # If still a raw marker, apply fallback display names
                    if display_seg == seg_key:
                        if seg_key == "QUAL":
                            display_seg = "Short Program (QUAL)"
                        elif seg_key == "FNL":
                            display_seg = "Free Skating (FNL)"
                
                # Update the segment field in each file's parsed data too
                for f in files:
                    f['segment'] = display_seg
                
                if display_seg not in enriched_structure[cat]:
                    enriched_structure[cat][display_seg] = []
                enriched_structure[cat][display_seg].extend(files)
        
        structure = enriched_structure

        # Parse CompetitionSchedule if present (for schedule info in response)
        schedule_data = []
        if schedule_blob_name:
            try:
                blob_client = container.get_blob_client(schedule_blob_name)
                stream = io.BytesIO()
                blob_client.download_blob().readinto(stream)
                stream.seek(0)
                schedule_data = parse_competition_schedule(stream, categories)
            except Exception as ex:
                logging.warning(f"Error parsing CompetitionSchedule: {ex}")

        # Process metadata
        comp_type_display = list(detected_types)[0] if len(detected_types) == 1 else "Unknown" if len(detected_types) == 0 else "Mixed"
        comp_full_name = list(detected_names)[0] if len(detected_names) == 1 else "-"         
        
        # Process dates
        comp_date_display = "-"
        if detected_dates:
            sorted_dates = sorted(list(detected_dates))
            start_date = datetime.strptime(sorted_dates[0], "%Y-%m-%d")
            end_date = datetime.strptime(sorted_dates[-1], "%Y-%m-%d")
            
            # Format: d.M.yyyy
            start_str = f"{start_date.day}.{start_date.month}.{start_date.year}"
            end_str = f"{end_date.day}.{end_date.month}.{end_date.year}"
            
            if start_str == end_str:
                comp_date_display = start_str
            else:
                comp_date_display = f"{start_str} - {end_str}"

        alerts = []
        if len(detected_names) > 1:
            alerts.append(f"Multiple competition names found: {', '.join(detected_names)}")

        # Fetch generated links (keyed by competition id)
        generated_links = []
        try:
            table_client = get_table_client()
            if table_client:
                 safe_pk = comp_id.replace("'", "''")
                 try:
                     entities = table_client.query_entities(f"PartitionKey eq '{safe_pk}'")
                     for entity in entities:
                         generated_links.append({
                             "fileName": entity.get("FileName"),
                             "url": entity.get("Url"),
                             "description": entity.get("Description"),
                             "expiration": entity.get("ExpirationDate"),
                             "size": entity.get("FileSize")
                         })
                 except ResourceNotFoundError:
                     pass 
        except Exception as e:
            logging.warning(f"Could not fetch generated links: {e}")

        # Collect competition-wide files (e.g. CompetitionSchedule)
        competition_files = [f for f in files_data if f.get('type') == 'Competition']

        return func.HttpResponse(json.dumps({
            "id": comp_id,
            "name": display_name,
            "fullName": comp_full_name,
            "type": comp_type_display,
            "date": comp_date_display,
            "language": competition_language,
            "files": files_data,
            "structure": structure,
            "competitionFiles": competition_files,
            "alerts": alerts,
            "categories": list(detected_category_codes),
            "generatedFiles": generated_links,
            # Names whose copy was just replaced by a newer one from the pool.
            "refreshedFromPool": refreshed,
            # The site UI has no competition list anymore; retention (auto-delete
            # date + extend) is surfaced in the detail view instead.
            "deletionDate": entity.get("DeletionDate")
        }), mimetype="application/json")
    except Exception as e:
        logging.error(f"Error getting details: {e}")
        return func.HttpResponse("Internal server error", status_code=500)


@app.route(route="save_competition_settings", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST"])
def save_competition_settings(req: func.HttpRequest) -> func.HttpResponse:
    """Save competition settings (e.g. language) to metadata.json."""
    logging.info('Saving competition settings...')

    email = get_user_email_from_header(req)
    if not email:
        return func.HttpResponse("Unauthorized", status_code=401)

    try:
        req_body = req.get_json()
        comp_id = req_body.get('id')
        settings = req_body.get('settings', {})
    except ValueError:
        return func.HttpResponse("Invalid JSON body", status_code=400)

    if not comp_id:
        return func.HttpResponse("Missing id parameter", status_code=400)

    try:
        entity = get_competition_entity(comp_id)
        if not entity:
            return func.HttpResponse("Competition not found", status_code=404)

        folder_path = entity.get("FolderPath", entity["RowKey"])

        blob_service_client = get_blob_service_client()
        if not blob_service_client:
            return func.HttpResponse("Storage configuration error", status_code=500)

        container = blob_service_client.get_container_client("fs-judgepapers")
        metadata_blob = container.get_blob_client(f"{folder_path}/metadata.json")

        # Read existing metadata
        existing_meta = {}
        try:
            if metadata_blob.exists():
                stream = metadata_blob.download_blob().readall()
                existing_meta = json.loads(stream)
        except Exception:
            pass

        # Merge new settings into existing metadata
        for key, value in settings.items():
            existing_meta[key] = value

        # Write back
        container.upload_blob(
            f"{folder_path}/metadata.json",
            json.dumps(existing_meta, indent=4),
            overwrite=True
        )

        return func.HttpResponse("Settings saved", status_code=200)
    except Exception as e:
        logging.error(f"Error saving settings: {e}")
        return func.HttpResponse("Internal server error", status_code=500)


@app.route(route="generate_judging_papers", auth_level=func.AuthLevel.ANONYMOUS)
def generate_judging_papers(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Python HTTP trigger function processed a request.')

    # 1. Security Check
    email = get_user_email_from_header(req)
    
    if not email:
        return func.HttpResponse("Unauthorized", status_code=401)
    if not is_user_allowed(email):
        return func.HttpResponse("Forbidden: You are not on the allow list.", status_code=403)

    try:
        req_body = req.get_json()
        # workingFolder carries the competition id; the blob folder is resolved from the table
        comp_id = req_body.get('workingFolder')
        options = req_body.get('options', {})
    except ValueError:
        return func.HttpResponse("Invalid JSON body", status_code=400)

    if not comp_id:
        return func.HttpResponse("Please pass a workingFolder in the request body", status_code=400)

    entity = get_competition_entity(comp_id)
    if not entity:
        return func.HttpResponse("Competition not found", status_code=404)

    working_folder = entity.get("FolderPath", entity["RowKey"])

    # Connect to Blob Storage
    try:
        blob_service_client = get_blob_service_client()
        if not blob_service_client:
             return func.HttpResponse("Storage configuration not found (AzureWebJobsStorage or AzureWebJobsStorage__accountName)", status_code=500)
            
        logging.info("Connected to Blob Storage")
    
        container_name = "fs-judgepapers"
        container_client = blob_service_client.get_container_client(container_name)

        # Create temp directories
        temp_dir = tempfile.mkdtemp()
        source_dir = os.path.join(temp_dir, "source")
        output_dir = os.path.join(temp_dir, "output")
        os.makedirs(source_dir)
        os.makedirs(output_dir)

        # Generate from the current FSM export, not a stale copy.
        _refresh_from_pool(entity, container_client, working_folder)

        # Download files
        logging.info(f"Downloading files from {working_folder}...")
        blobs = container_client.list_blobs(name_starts_with=working_folder)
        download_count = 0
        for blob in blobs:
            # Calculate relative path to maintain structure inside the working folder
            clean_working_folder = working_folder.strip("/")
            if blob.name.startswith(clean_working_folder + "/"):
                relative_path = blob.name[len(clean_working_folder)+1:]
            elif blob.name == clean_working_folder:
                continue 
            else:
                if not blob.name.startswith(clean_working_folder + "/"):
                    continue
                relative_path = blob.name[len(clean_working_folder)+1:]

            local_path = os.path.join(source_dir, relative_path)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            
            with open(local_path, "wb") as download_file:
                download_file.write(container_client.download_blob(blob.name).readall())
            download_count += 1
        
        logging.info(f"Downloaded {download_count} files.")

        if download_count == 0:
             return func.HttpResponse(f"No files found in folder '{working_folder}'", status_code=404)

        # Run the processor
        logging.info("Running processor...")
        stats = process_judging_papers(source_dir, output_dir, options=options)

        # Upload results
        logging.info("Uploading results...")
        upload_count = 0
        for root, dirs, files in os.walk(output_dir):
            for file in files:
                local_file_path = os.path.join(root, file)
                relative_path = os.path.relpath(local_file_path, output_dir)
                
                # Upload to workingFolder/judgePapers/relative_path
                blob_name = f"{clean_working_folder}/judgePapers/{relative_path}".replace("\\", "/")
                
                # Get file size
                file_size = os.path.getsize(local_file_path)

                with open(local_file_path, "rb") as data:
                    container_client.upload_blob(name=blob_name, data=data, overwrite=True)
                upload_count += 1
                
                # Check for generate files (PDF summaries or ZIPs)
                if file.lower().endswith('.zip') or (file.lower().startswith('judgingpapers_') and file.lower().endswith('.pdf')):
                     create_and_store_sas_link(blob_service_client, "fs-judgepapers", blob_name, comp_id, file, file_size)

        logging.info(f"Uploaded {upload_count} files.")

        _bump_competition_counters(comp_id, generate_delta=1, set_last_generated=True)
        _store_competition_statistics(comp_id, stats or {})

        return func.HttpResponse(f"Successfully processed {download_count} files and generated {upload_count} output files.", status_code=200)

    except Exception as e:
        logging.error(f"Error: {e}", exc_info=True)
        return func.HttpResponse("Error processing request. Check server logs for details.", status_code=500)
    finally:
        # Cleanup
        if 'temp_dir' in locals() and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
@app.route(route="upload_file", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST"])
def upload_file(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Uploading file...')
    
    email = get_user_email_from_header(req)
    if not email:
        return func.HttpResponse("Unauthorized", status_code=401)
        
    competition = req.params.get('competition')
    filename = req.params.get('filename')
    
    if not competition or not filename:
        return func.HttpResponse("Missing competition or filename", status_code=400)
    
    # Sanitize filename: strip path traversal characters
    filename = os.path.basename(filename)
    
    if not filename.lower().endswith('.pdf'):
        return func.HttpResponse("Only PDF files are allowed", status_code=400)

    # Check file size limit
    content_length = req.headers.get('Content-Length')
    if content_length and int(content_length) > MAX_UPLOAD_SIZE:
        return func.HttpResponse(f"File too large. Maximum size is {MAX_UPLOAD_SIZE // (1024*1024)} MB.", status_code=413)

    try:
        file_content = req.get_body()

        if len(file_content) > MAX_UPLOAD_SIZE:
            return func.HttpResponse(f"File too large. Maximum size is {MAX_UPLOAD_SIZE // (1024*1024)} MB.", status_code=413)

        # 'competition' carries the competition id; resolve the blob folder from the table
        entity = get_competition_entity(competition)
        if not entity:
            return func.HttpResponse("Competition not found", status_code=404)

        folder_path = entity.get("FolderPath", entity["RowKey"])

        blob_service_client = get_blob_service_client()
        if not blob_service_client: return func.HttpResponse("Config Error", status_code=500)

        container = blob_service_client.get_container_client("fs-judgepapers")

        blob_path = f"{folder_path}/{filename}"

        container.upload_blob(blob_path, file_content, overwrite=True)

        _bump_competition_counters(competition, uploaded_delta=1)

        return func.HttpResponse(json.dumps({"filename": filename, "status": "uploaded"}), mimetype="application/json")
    except Exception as e:
        logging.error(f"Error uploading file: {e}")
        return func.HttpResponse("Internal server error", status_code=500)

@app.route(route="import_platform_file", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST"])
def import_platform_file(req: func.HttpRequest) -> func.HttpResponse:
    """
    Copy a file from the platform's shared competition file pool into this
    tool's competition folder. Query: competition (this tool's competition id),
    name (pool blob file name) and the optional source — "upload" (the default,
    files people uploaded) or "fsm" (files the HOVTP listener pushed). The pool
    path is composed server-side from the competition's bound PlatformId and
    the source's fixed folder name, so a client can never point this at another
    competition's files.
    """
    logging.info('Importing file from the platform file pool...')

    email = get_user_email_from_header(req)
    if not email:
        return func.HttpResponse("Unauthorized", status_code=401)

    competition = req.params.get('competition')
    name = req.params.get('name')

    if not competition or not name:
        return func.HttpResponse("Missing competition or name", status_code=400)

    # The pool has two folders; the client picks one by name, never by path.
    source = req.params.get('source') or 'upload'
    if source not in ('upload', 'fsm'):
        return func.HttpResponse(
            "invalid_source: source must be 'upload' or 'fsm'", status_code=400)

    # Sanitize: only the basename is ever used, both for the pool lookup and
    # for the destination blob path.
    filename = os.path.basename(name)

    if not filename.lower().endswith('.pdf'):
        return func.HttpResponse("Only PDF files are allowed", status_code=400)

    entity = get_competition_entity(competition)
    if not entity:
        return func.HttpResponse("Competition not found", status_code=404)

    platform_id = entity.get("PlatformId")
    if not platform_id:
        return func.HttpResponse(
            "not_bound: this competition is not linked to a platform competition",
            status_code=409)
    # The binding originates from a client body (resolve_competition), so pin it
    # to a UUID before splicing it into the pool blob path.
    if not _UUID_RE.fullmatch(platform_id):
        logging.warning(f"Rejecting non-UUID PlatformId {platform_id!r} on competition {competition}")
        return func.HttpResponse(
            "not_bound: this competition's platform link is invalid",
            status_code=409)

    try:
        pool_container = get_platform_container_client()
    except Exception as e:
        logging.error(f"Platform pool client creation failed: {e}")
        return func.HttpResponse(
            "platform_unavailable: could not read the competition file pool",
            status_code=502)
    if pool_container is None:
        return func.HttpResponse(
            "platform_not_configured: the platform file pool is not configured",
            status_code=503)

    pool_path = f"{platform_id}/{'fsm' if source == 'fsm' else 'uploads'}/{filename}"

    try:
        properties = pool_container.get_blob_client(pool_path).get_blob_properties()
        if properties.size and properties.size > MAX_UPLOAD_SIZE:
            return func.HttpResponse(f"File too large. Maximum size is {MAX_UPLOAD_SIZE // (1024*1024)} MB.", status_code=413)
    except ResourceNotFoundError:
        return func.HttpResponse("File not found in the competition file pool", status_code=404)
    except Exception as e:
        logging.error(f"Error reading platform pool blob {pool_path}: {e}")
        return func.HttpResponse(
            "platform_unavailable: could not read the competition file pool",
            status_code=502)

    try:
        folder_path = entity.get("FolderPath", entity["RowKey"])

        blob_service_client = get_blob_service_client()
        if not blob_service_client: return func.HttpResponse("Config Error", status_code=500)

        container = blob_service_client.get_container_client("fs-judgepapers")

        blob_path = f"{folder_path}/{filename}"
    except Exception as e:
        logging.error(f"Error importing file: {e}")
        return func.HttpResponse("Internal server error", status_code=500)

    # Provenance: which pool folder the copy came from, how fresh the pool blob
    # was, and who pulled it — `_refresh_from_pool` compares against this.
    metadata = {
        "poolSource": source,
        "poolUploadedUtc": _iso_utc(getattr(properties, "last_modified", None)),
        "importedBy": email,
    }

    try:
        _copy_pool_blob(pool_container, pool_path, container, blob_path, metadata)
    except _PoolFileTooLarge:
        return func.HttpResponse(f"File too large. Maximum size is {MAX_UPLOAD_SIZE // (1024*1024)} MB.", status_code=413)
    except ResourceNotFoundError:
        return func.HttpResponse("File not found in the competition file pool", status_code=404)
    except _PoolReadError as e:
        logging.error(f"Error reading platform pool blob {pool_path}: {e}")
        return func.HttpResponse(
            "platform_unavailable: could not read the competition file pool",
            status_code=502)
    except Exception as e:
        logging.error(f"Error importing file: {e}")
        return func.HttpResponse("Internal server error", status_code=500)

    try:
        _bump_competition_counters(competition, uploaded_delta=1)

        return func.HttpResponse(json.dumps({"filename": filename, "status": "uploaded"}), mimetype="application/json")
    except Exception as e:
        logging.error(f"Error importing file: {e}")
        return func.HttpResponse("Internal server error", status_code=500)

@app.route(route="delete_file", auth_level=func.AuthLevel.ANONYMOUS, methods=["DELETE", "POST"])
def delete_file(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Deleting file...')
    
    email = get_user_email_from_header(req)
    if not email:
        return func.HttpResponse("Unauthorized", status_code=401)
        
    competition = req.params.get('competition')
    filename = req.params.get('filename')
    
    if not competition or not filename:
        return func.HttpResponse("Missing competition or filename", status_code=400)

    try:
        # 'competition' carries the competition id; resolve the blob folder from the table
        entity = get_competition_entity(competition)
        if not entity:
            return func.HttpResponse("Competition not found", status_code=404)

        folder_path = entity.get("FolderPath", entity["RowKey"])

        blob_service_client = get_blob_service_client()
        if not blob_service_client: return func.HttpResponse("Config Error", status_code=500)

        container = blob_service_client.get_container_client("fs-judgepapers")

        blob_path = f"{folder_path}/{filename}"

        if container.get_blob_client(blob_path).exists():
            container.delete_blob(blob_path)

            # Try to delete associated table entity (if distinct generated file)
            try:
                table_client = get_table_client()
                if table_client:
                    simple_filename = os.path.basename(filename)
                    row_key = simple_filename.replace('/', '_').replace('\\', '_')
                    # We ignore errors if the entity does not exist
                    table_client.delete_entity(partition_key=competition, row_key=row_key)
                    logging.info(f"Deleted table entity for {simple_filename}")
            except ResourceNotFoundError:
                pass
            except Exception as table_err:
                logging.warning(f"Error deleting table entity: {table_err}")

            # Only source uploads count toward UploadedFileCount (not generated outputs)
            if not filename.startswith('judgePapers/'):
                _bump_competition_counters(competition, uploaded_delta=-1)

            return func.HttpResponse(json.dumps({"status": "deleted"}), mimetype="application/json")
        else:
            return func.HttpResponse("File not found", status_code=404)
            
    except Exception as e:
        logging.error(f"Error deleting file: {e}")
        return func.HttpResponse("Internal server error", status_code=500)
