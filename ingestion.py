"""
ingestion.py — Ingestion pipeline: fetch audit rows, process files, write sales and inventory.
"""
from fastapi import APIRouter, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List, Optional
import openpyxl
import io
import re
import uuid
import asyncio
import urllib.request
import json
from datetime import datetime, date, timedelta
import time

from shared import (
    _sessions, _jobs, fire_webhook,
    normalize_to_saturday,
    N8N_POSTGRES_WEBHOOK,
    N8N_FETCH_FILE_WEBHOOK,
    register_ingestion_timeout_handler,
)

router = APIRouter()

# ─────────────────────────────────────────────
# DATE HELPERS
# ─────────────────────────────────────────────

MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12
}



def parse_date_value(val, date_config: dict) -> Optional[date]:
    """
    Parse a date column header value to a week-ending Saturday date.
    Handles: datetime objects, date_range_string (12/21-12/27), fiscal_week_label (Feb Wk 1)
    """
    year = date_config.get("year_value", date.today().year)
    fmt  = date_config.get("date_format") or date_config.get("format", "")

    if isinstance(val, datetime):
        return normalize_to_saturday(val.date())

    if isinstance(val, date):
        return normalize_to_saturday(val)

    if not isinstance(val, str):
        return None

    s = val.strip()

    # datetime string: "2026-01-03 00:00:00"
    if re.match(r'^\d{4}-\d{2}-\d{2}', s):
        try:
            d = datetime.strptime(s[:10], "%Y-%m-%d").date()
            return normalize_to_saturday(d)
        except:
            return None

    # date_range_string: "12/21-12/27" or "1/4-1/10"
    m = re.match(r'^(\d{1,2})/(\d{1,2})[-–](\d{1,2})/(\d{1,2})$', s)
    if m:
        end_month = int(m.group(3))
        end_day   = int(m.group(4))
        start_month = int(m.group(1))
        # Year boundary — if start month is Dec and end is Jan, end is in next year
        end_year = year
        if start_month == 12 and end_month == 1:
            end_year = year + 1
        elif start_month == 1 and end_month == 12:
            end_year = year - 1
        try:
            return date(end_year, end_month, end_day)
        except:
            return None

    # fiscal_week_label: "Feb Wk 1", "Mar Wk 2", "Sept Wk 1"
    m = re.match(r'^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+wk\s*(\d+)$', s, re.IGNORECASE)
    if m:
        month_num = MONTH_MAP[m.group(1).lower()[:3]]   # slice to 3 chars: "sept" -> "sep"
        week_num  = int(m.group(2))
        # Year boundary: Jan weeks on a Dec->Jan spanning file belong to year+1
        year_boundary = date_config.get("year_boundary_detected", False)
        effective_year = (year + 1) if (year_boundary and month_num == 1) else year
        try:
            first_of_month = date(effective_year, month_num, 1)
            approx = first_of_month + timedelta(days=(week_num - 1) * 7)
            return normalize_to_saturday(approx)
        except:
            return None

    return None


# ─────────────────────────────────────────────
# SQL BUILDERS
# ─────────────────────────────────────────────

def build_fetch_audit_row_sql(file_audit_id: str) -> str:
    return f"""
SELECT id, filename, file_hash, minio_path, retailer, status, discovery_result
FROM file_audit
WHERE id = '{file_audit_id}'
LIMIT 1
""".strip()


def build_fetch_retailer_config_sql(retailer: str) -> str:
    retailer_safe = retailer.replace("'", "''")
    return f"""
SELECT id, retailer, file_set_size, qualified_sheets, column_mapping, date_config, flags
FROM retailer_configs
WHERE UPPER(retailer) = UPPER('{retailer_safe}')
  AND status = 'active'
LIMIT 1
""".strip()


def build_fetch_sku_map_sql(retailer: str, retailer_skus: list) -> str:
    retailer_safe = retailer.replace("'", "''")
    quoted = ", ".join("'" + str(s).replace("'", "''") + "'" for s in retailer_skus if s)
    if not quoted:
        quoted = "''"
    return f"""
SELECT retailer_sku, supplier_sku, base_model, base_variant, description
FROM retailer_sku_map
WHERE UPPER(retailer) = UPPER('{retailer_safe}')
  AND UPPER(retailer_sku) = ANY(ARRAY[{quoted}]::text[])
  AND active = true
""".strip()



def build_lookup_supplier_skus_sql(retailer: str, retailer_skus: list) -> str:
    """Look up supplier SKUs from retailer_sku_map for a list of retailer SKUs."""
    retailer_safe = retailer.replace("'", "''")
    quoted = ", ".join("'" + str(s).replace("'", "''") + "'" for s in retailer_skus if s)
    if not quoted:
        quoted = "''"
    return f"""
SELECT retailer_sku, supplier_sku, base_model, base_variant
FROM retailer_sku_map
WHERE UPPER(retailer) = UPPER('{retailer_safe}')
  AND UPPER(retailer_sku) = ANY(ARRAY[{quoted}]::text[])
  AND active = true
  AND supplier_sku IS NOT NULL
""".strip()

def build_create_sales_table_sql(retailer: str) -> str:
    table = retailer.lower().replace(" ", "_") + "_weekly_sales"
    return f"""
CREATE TABLE IF NOT EXISTS {table} (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    retailer_sku    TEXT NOT NULL,
    supplier_sku    TEXT,
    week_ending     DATE NOT NULL,
    units_sold      INTEGER NOT NULL,
    file_audit_id   UUID,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked          BOOLEAN NOT NULL DEFAULT true,
    UNIQUE (retailer_sku, week_ending)
)
""".strip()


def build_upsert_sales_sql(retailer: str, rows: list, file_audit_id: str) -> str:
    """Build a multi-row upsert for sales data."""
    table = retailer.lower().replace(" ", "_") + "_weekly_sales"
    values = []
    for row in rows:
        retailer_sku  = str(row["retailer_sku"]).replace("'", "''")
        supplier_sku  = f"'{str(row['supplier_sku']).replace(chr(39), chr(39)*2)}'" if row.get("supplier_sku") else "NULL"
        week_ending   = str(row["week_ending"])
        units_sold    = int(row["units_sold"])
        values.append(f"('{retailer_sku}', {supplier_sku}, '{week_ending}', {units_sold}, '{file_audit_id}')")

    values_str = ",\n".join(values)
    return f"""
INSERT INTO {table} (retailer_sku, supplier_sku, week_ending, units_sold, file_audit_id)
VALUES {values_str}
ON CONFLICT (retailer_sku, week_ending)
DO UPDATE SET
    units_sold    = EXCLUDED.units_sold,
    file_audit_id = EXCLUDED.file_audit_id
""".strip()


def build_upsert_inventory_sql(retailer: str, rows: list, file_audit_id: str, as_of_date: str) -> str:
    """Build a multi-row upsert for inventory snapshot."""
    values = []
    for row in rows:
        retailer_sku   = str(row["retailer_sku"]).replace("'", "''")
        on_hand        = int(row.get("on_hand_qty") or 0)
        open_order     = int(row.get("open_order_qty") or 0)
        values.append(f"('{retailer}', '{retailer_sku}', {on_hand}, {open_order}, '{as_of_date}', '{file_audit_id}')")

    values_str = ",\n".join(values)
    return f"""
INSERT INTO retailer_inventory (retailer, retailer_sku, on_hand_qty, open_order_qty, as_of_date, file_audit_id)
VALUES {values_str}
ON CONFLICT (retailer, retailer_sku)
DO UPDATE SET
    on_hand_qty    = EXCLUDED.on_hand_qty,
    open_order_qty = EXCLUDED.open_order_qty,
    as_of_date     = EXCLUDED.as_of_date,
    file_audit_id  = EXCLUDED.file_audit_id,
    updated_at     = now()
WHERE retailer_inventory.as_of_date <= EXCLUDED.as_of_date
""".strip()


def build_update_audit_status_sql(file_audit_id: str, status: str,
                                   unresolved_skus: list = None) -> str:
    unresolved_json = json.dumps(unresolved_skus or []).replace("'", "''")
    return f"""
UPDATE file_audit
SET status          = '{status}',
    unresolved_skus = '{unresolved_json}'::jsonb,
    updated_at      = now()
WHERE id = '{file_audit_id}'
""".strip()


# ─────────────────────────────────────────────
# CORE INGESTION LOGIC
# ─────────────────────────────────────────────

def download_from_url(url: str) -> Optional[bytes]:
    """Download file from a URL."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception as e:
        return None


def extract_sales_and_inventory(
    wb: openpyxl.Workbook,
    discovery_result: dict,
    retailer: str,
    file_audit_id: str
) -> dict:
    """
    Extract sales rows and inventory rows from workbook using discovery result.
    Returns {sales: [...], inventory: [...], unresolved_skus: [...]}
    """
    column_mapping = discovery_result.get("column_mapping", {})
    date_config    = discovery_result.get("date_config", {})
    qualified_sheets = discovery_result.get("qualified_sheets", [])

    # Accumulate sales by (retailer_sku, week_ending) for rollup across sheets
    sales_map      = {}  # (retailer_sku, week_ending) -> units_sold
    sku_supplier   = {}  # retailer_sku -> supplier_sku
    inventory_map  = {}  # retailer_sku -> {on_hand_qty, open_order_qty}
    unresolved     = set()

    for sheet_name in qualified_sheets:
        if sheet_name not in wb.sheetnames:
            continue

        ws      = wb[sheet_name]
        rows    = list(ws.iter_rows(values_only=True))
        mapping = column_mapping.get(sheet_name, [])
        dc      = date_config.get(sheet_name, {})

        if not mapping or not rows:
            continue

        # Find column roles
        retailer_sku_col  = None
        supplier_sku_col  = None
        description_col   = None
        inventory_col     = None
        open_order_col    = None
        date_cols         = []

        inventory_candidates = []  # collect all inventory cols to pick best one

        for col_info in mapping:
            classification = col_info.get("classification")
            col_idx        = col_info.get("col")
            if classification == "retailer_sku":
                retailer_sku_col = col_idx
            elif classification == "supplier_sku":
                supplier_sku_col = col_idx
            elif classification == "description":
                description_col = col_idx
            elif classification == "inventory":
                inventory_candidates.append(col_info)
            elif classification == "open_order":
                open_order_col = col_idx

        # Pick the best inventory column:
        # prefer one whose header contains "total", otherwise take the last one
        if inventory_candidates:
            total_inv = next(
                (c for c in inventory_candidates
                 if "total" in c.get("reason", "").lower()),
                None
            )
            inventory_col = (total_inv or inventory_candidates[-1])["col"]

        # Find date axis from grid
        grid       = discovery_result.get("grid", {}).get(sheet_name, {})
        date_axis  = grid.get("date_axis", {})
        data_start = grid.get("data_start_row", 1)

        if not date_axis or retailer_sku_col is None:
            continue

        date_axis_row = date_axis.get("row", 0)
        date_col_idxs = date_axis.get("cols", [])

        # Parse date headers
        header_row = rows[date_axis_row] if date_axis_row < len(rows) else []
        date_map   = {}  # col_idx -> date
        for col_idx in date_col_idxs:
            if col_idx < len(header_row):
                parsed = parse_date_value(header_row[col_idx], dc)
                if parsed:
                    date_map[col_idx] = parsed

        if not date_map:
            continue

        # Process data rows
        for row in rows[data_start:]:
            if retailer_sku_col >= len(row) or row[retailer_sku_col] is None:
                continue

            rsku = str(row[retailer_sku_col]).strip()
            if not rsku or rsku.lower() in ("total", "grand total", "subtotal", ""):
                continue

            # Skip subtotal/category rows
            if retailer_sku_col > 0 and (row[0] is None or str(row[0]).strip().upper().startswith("TOTAL")):
                continue

            # Supplier SKU
            ssku = None
            if supplier_sku_col is not None and supplier_sku_col < len(row):
                ssku = str(row[supplier_sku_col]).strip() if row[supplier_sku_col] else None
            if ssku:
                sku_supplier[rsku] = ssku

            # Inventory
            if inventory_col is not None and inventory_col < len(row):
                v = row[inventory_col]
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    inv = inventory_map.get(rsku, {"on_hand_qty": 0, "open_order_qty": 0})
                    inv["on_hand_qty"] = int(v)
                    inventory_map[rsku] = inv

            # Open orders
            if open_order_col is not None and open_order_col < len(row):
                v = row[open_order_col]
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    inv = inventory_map.get(rsku, {"on_hand_qty": 0, "open_order_qty": 0})
                    inv["open_order_qty"] = int(v)
                    inventory_map[rsku] = inv

            # Sales — each date column
            for col_idx, week_end in date_map.items():
                if col_idx >= len(row):
                    continue
                val = row[col_idx]
                if val is None or isinstance(val, bool):
                    continue
                if not isinstance(val, (int, float)):
                    continue
                units = int(val)
                if units == 0:
                    continue

                key = (rsku, str(week_end))
                sales_map[key] = sales_map.get(key, 0) + units

    # Build output rows
    sales_rows = []
    for (rsku, week_end), units in sales_map.items():
        sales_rows.append({
            "retailer_sku": rsku,
            "supplier_sku": sku_supplier.get(rsku),
            "week_ending":  week_end,
            "units_sold":   units,
        })
        if not sku_supplier.get(rsku):
            unresolved.add(rsku)

    inventory_rows = []
    for rsku, inv in inventory_map.items():
        inventory_rows.append({
            "retailer_sku":  rsku,
            "on_hand_qty":   inv.get("on_hand_qty", 0),
            "open_order_qty": inv.get("open_order_qty", 0),
        })

    return {
        "sales":           sales_rows,
        "inventory":       inventory_rows,
        "unresolved_skus": sorted(unresolved),
    }


# ─────────────────────────────────────────────
# PIPELINE STAGES
# ─────────────────────────────────────────────


async def ingestion_timeout_handler(sid: str, stage: str):
    """Handle timed-out ingestion jobs."""
    if stage in ("fetch_audit", "fetch_file_binary"):
        asyncio.create_task(stage_process_files(sid))
    elif stage == "sku_map_lookup":
        asyncio.create_task(stage_create_table(sid))
    elif stage == "create_table":
        asyncio.create_task(stage_write_sales(sid))
    elif stage == "write_sales_batch":
        asyncio.create_task(stage_write_inventory(sid))
    elif stage == "write_inventory_batch":
        asyncio.create_task(stage_write_inventory(sid))
    elif stage == "finalize_audit":
        asyncio.create_task(stage_complete(sid))


register_ingestion_timeout_handler(ingestion_timeout_handler)


class IngestRequest(BaseModel):
    file_audit_ids: List[str]


@router.post("/ingest")
async def ingest(request: IngestRequest, background_tasks: BackgroundTasks):
    if not request.file_audit_ids:
        raise HTTPException(status_code=400, detail="At least one file_audit_id required")

    session_id = str(uuid.uuid4())
    _sessions[session_id] = {
        "stage":           "accepted",
        "status":          "running",
        "file_audit_ids":  request.file_audit_ids,
        "retailer":        None,
        "sales_rows":      [],
        "inventory_rows":  [],
        "unresolved_skus": [],
        "errors":          [],
        "result":          None,
        "_pending_jobs":   set(),
        "_audit_rows":     {},   # id -> row data
        "created_at":      time.time(),
    }

    background_tasks.add_task(stage_fetch_audit_rows, session_id)

    return JSONResponse(content={
        "status":     "accepted",
        "session_id": session_id,
        "poll_url":   f"/ingest/status/{session_id}",
    })


async def stage_fetch_audit_rows(session_id: str):
    """Fetch all file_audit rows and fresh signed URLs for the given IDs."""
    session = _sessions[session_id]
    session["stage"]  = "fetching"
    session["status"] = "running"

    for audit_id in session["file_audit_ids"]:
        # Fetch audit row from Postgres
        sql = build_fetch_audit_row_sql(audit_id)
        job_id = str(uuid.uuid4())
        _jobs[job_id] = {
            "session_id": session_id,
            "stage":      "fetch_audit",
            "audit_id":   audit_id,
            "created_at": time.time(),
            "pipeline":   "ingestion",
        }
        err = fire_webhook(N8N_POSTGRES_WEBHOOK, job_id, {"sql": sql, "params": []})
        if err:
            session["errors"].append(f"Failed to fetch audit row {audit_id}: {err}")
        else:
            session["status"] = "awaiting_postgres"
            session["_pending_jobs"].add(job_id)

        # Fetch file binary via S3 webhook — posts back to /file/{job_id}
        file_job_id = str(uuid.uuid4())
        _jobs[file_job_id] = {
            "session_id": session_id,
            "stage":      "fetch_file_binary",
            "audit_id":   audit_id,
            "created_at": time.time(),
            "pipeline":   "ingestion",
        }
        err = fire_webhook(N8N_FETCH_FILE_WEBHOOK, file_job_id, {"file_audit_id": audit_id})
        if err:
            session["errors"].append(f"Failed to fetch file binary for {audit_id}: {err}")
        else:
            session["status"] = "awaiting_postgres"
            session["_pending_jobs"].add(file_job_id)

    if not session["_pending_jobs"]:
        session["stage"]  = "failed"
        session["status"] = "failed"
        session["result"] = {"error": "Failed to fetch any audit rows", "errors": session["errors"]}


async def stage_process_files(session_id: str):
    """Download files from MinIO and extract sales/inventory data."""
    session = _sessions[session_id]
    session["stage"]  = "processing"
    session["status"] = "running"

    audit_rows = session["_audit_rows"]
    if not audit_rows:
        session["stage"]  = "failed"
        session["status"] = "failed"
        session["result"] = {"error": "No audit rows found"}
        return

    # All rows should have same retailer
    retailers = set(r.get("retailer") for r in audit_rows.values() if r.get("retailer"))
    if not retailers:
        session["stage"]  = "failed"
        session["status"] = "failed"
        session["result"] = {"error": "No retailer identified in audit rows"}
        return

    retailer = retailers.pop()
    session["retailer"] = retailer

    all_sales     = {}  # (retailer_sku, week_ending) -> units
    all_inventory = {}  # retailer_sku -> {on_hand_qty, open_order_qty}
    all_unresolved = set()
    as_of_dates   = []

    file_bytes = session.get("_file_bytes", {})

    for audit_id, audit_row in audit_rows.items():
        discovery_result = audit_row.get("discovery_result")
        data             = file_bytes.get(audit_id)

        if not discovery_result:
            session["errors"].append(f"Missing discovery_result for {audit_id}")
            continue

        if not data:
            session["errors"].append(f"No file data available for {audit_id}")
            continue

        try:
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception as e:
            session["errors"].append(f"Failed to open workbook for {audit_id}: {e}")
            continue

        extracted = extract_sales_and_inventory(wb, discovery_result, retailer, audit_id)

        # Rollup sales across files
        for row in extracted["sales"]:
            key = (row["retailer_sku"], row["week_ending"])
            all_sales[key] = all_sales.get(key, 0) + row["units_sold"]
            if row.get("supplier_sku"):
                session.setdefault("_sku_supplier", {})[row["retailer_sku"]] = row["supplier_sku"]

        # Inventory — latest file wins per SKU
        for row in extracted["inventory"]:
            all_inventory[row["retailer_sku"]] = row

        all_unresolved.update(extracted["unresolved_skus"])

        # Track as_of_date from date_config
        dc = discovery_result.get("date_config", {})
        for sheet_dc in dc.values():
            year = sheet_dc.get("year_value")
            if year:
                as_of_dates.append(str(year))

    # Rebuild sales rows with supplier SKU
    sku_supplier = session.get("_sku_supplier", {})
    session["sales_rows"] = [
        {
            "retailer_sku": rsku,
            "supplier_sku": sku_supplier.get(rsku),
            "week_ending":  week_end,
            "units_sold":   units,
        }
        for (rsku, week_end), units in all_sales.items()
    ]
    session["inventory_rows"]  = list(all_inventory.values())
    session["unresolved_skus"] = sorted(all_unresolved)
    session["as_of_date"]      = max(as_of_dates) if as_of_dates else str(date.today())

    await stage_lookup_supplier_skus(session_id)



async def stage_lookup_supplier_skus(session_id: str):
    """Look up supplier SKUs from retailer_sku_map for all retailer SKUs."""
    session  = _sessions[session_id]
    retailer = session["retailer"]
    session["stage"]  = "looking_up_skus"
    session["status"] = "running"

    sales_rows = session.get("sales_rows", [])
    if not sales_rows:
        await stage_create_table(session_id)
        return

    # Collect unique retailer SKUs that don't have supplier SKU yet
    retailer_skus = list({
        row["retailer_sku"] for row in sales_rows
        if not row.get("supplier_sku")
    })

    if not retailer_skus:
        await stage_create_table(session_id)
        return

    sql = build_lookup_supplier_skus_sql(retailer, retailer_skus)
    job_id = str(uuid.uuid4())
    _jobs[job_id] = {
        "session_id": session_id,
        "stage":      "sku_map_lookup",
        "created_at": time.time(),
        "pipeline":   "ingestion",
    }
    err = fire_webhook(N8N_POSTGRES_WEBHOOK, job_id, {"sql": sql, "params": []})
    if err:
        session["errors"].append(f"Failed to look up supplier SKUs: {err}")
        await stage_create_table(session_id)
    else:
        session["status"] = "awaiting_postgres"
        session["_pending_jobs"].add(job_id)

async def stage_create_table(session_id: str):
    """Ensure retailer sales table exists."""
    session  = _sessions[session_id]
    retailer = session["retailer"]
    session["stage"]  = "creating_table"
    session["status"] = "running"

    sql = build_create_sales_table_sql(retailer)
    job_id = str(uuid.uuid4())
    _jobs[job_id] = {
        "session_id": session_id,
        "stage":      "create_table",
        "created_at": time.time(),
            "pipeline":  "ingestion",
        }
    err = fire_webhook(N8N_POSTGRES_WEBHOOK, job_id, {"sql": sql, "params": []})
    if err:
        session["errors"].append(f"Failed to create sales table: {err}")
        await stage_write_sales(session_id)
    else:
        session["status"] = "awaiting_postgres"
        session["_pending_jobs"].add(job_id)


async def stage_write_sales(session_id: str):
    """Write sales rows to retailer sales table."""
    session   = _sessions[session_id]
    retailer  = session["retailer"]
    session["stage"]  = "writing_sales"
    session["status"] = "running"

    sales_rows = session.get("sales_rows", [])
    if not sales_rows:
        await stage_write_inventory(session_id)
        return

    # Write in batches of 500
    BATCH_SIZE = 500
    batches = [sales_rows[i:i+BATCH_SIZE] for i in range(0, len(sales_rows), BATCH_SIZE)]

    for i, batch in enumerate(batches):
        # Use first file_audit_id for the batch
        audit_id = session["file_audit_ids"][0]
        sql = build_upsert_sales_sql(retailer, batch, audit_id)
        job_id = str(uuid.uuid4())
        _jobs[job_id] = {
            "session_id": session_id,
            "stage":      "write_sales_batch",
            "batch":      i,
            "created_at": time.time(),
            "pipeline":  "ingestion",
        }
        err = fire_webhook(N8N_POSTGRES_WEBHOOK, job_id, {"sql": sql, "params": []})
        if err:
            session["errors"].append(f"Failed to write sales batch {i}: {err}")
        else:
            session["status"] = "awaiting_postgres"
            session["_pending_jobs"].add(job_id)

    if not session["_pending_jobs"]:
        await stage_write_inventory(session_id)


async def stage_write_inventory(session_id: str):
    """Write inventory snapshot."""
    session   = _sessions[session_id]
    retailer  = session["retailer"]
    session["stage"]  = "writing_inventory"
    session["status"] = "running"

    inventory_rows = session.get("inventory_rows", [])
    if not inventory_rows:
        await stage_finalize(session_id)
        return

    audit_id   = session["file_audit_ids"][0]
    as_of_date = session.get("as_of_date", str(date.today()))

    BATCH_SIZE = 500
    batches = [inventory_rows[i:i+BATCH_SIZE] for i in range(0, len(inventory_rows), BATCH_SIZE)]

    for i, batch in enumerate(batches):
        sql = build_upsert_inventory_sql(retailer, batch, audit_id, as_of_date)
        job_id = str(uuid.uuid4())
        _jobs[job_id] = {
            "session_id": session_id,
            "stage":      "write_inventory_batch",
            "batch":      i,
            "created_at": time.time(),
            "pipeline":  "ingestion",
        }
        err = fire_webhook(N8N_POSTGRES_WEBHOOK, job_id, {"sql": sql, "params": []})
        if err:
            session["errors"].append(f"Failed to write inventory batch {i}: {err}")
        else:
            session["status"] = "awaiting_postgres"
            session["_pending_jobs"].add(job_id)

    if not session["_pending_jobs"]:
        await stage_finalize(session_id)


async def stage_finalize(session_id: str):
    """Update file_audit status and assemble result."""
    session        = _sessions[session_id]
    unresolved     = session.get("unresolved_skus", [])
    audit_status   = "ingested_partial" if unresolved else "ingested"

    for audit_id in session["file_audit_ids"]:
        sql = build_update_audit_status_sql(audit_id, audit_status, unresolved)
        job_id = str(uuid.uuid4())
        _jobs[job_id] = {
            "session_id": session_id,
            "stage":      "finalize_audit",
            "created_at": time.time(),
            "pipeline":  "ingestion",
        }
        err = fire_webhook(N8N_POSTGRES_WEBHOOK, job_id, {"sql": sql, "params": []})
        if err:
            session["errors"].append(f"Failed to update audit status: {err}")
        else:
            session["status"] = "awaiting_postgres"
            session["_pending_jobs"].add(job_id)

    if not session["_pending_jobs"]:
        await stage_complete(session_id)


async def stage_complete(session_id: str):
    session = _sessions[session_id]
    session["stage"]  = "complete"
    session["status"] = "complete"
    session["result"] = {
        "retailer":        session.get("retailer"),
        "file_audit_ids":  session["file_audit_ids"],
        "sales_rows":      len(session.get("sales_rows", [])),
        "inventory_rows":  len(session.get("inventory_rows", [])),
        "unresolved_skus": session.get("unresolved_skus", []),
        "errors":          session.get("errors", []),
    }


# ─────────────────────────────────────────────
# RESPONSE ENDPOINT
# ─────────────────────────────────────────────

async def ingest_response(job_id: str, job: dict, request_body: dict, background_tasks: BackgroundTasks):
    """Called by unified /response/{job_id} endpoint in discovery.py when pipeline == ingestion."""
    session_id = job["session_id"]
    stage      = job["stage"]

    if session_id not in _sessions:
        return JSONResponse(content={"status": "ok", "note": "session already closed"})

    session = _sessions[session_id]
    session["_pending_jobs"].discard(job_id)

    if stage == "fetch_audit":
        rows = request_body.get("matches", [])
        if rows:
            audit_id = job["audit_id"]
            row      = rows[0]
            # Parse discovery_result if it's a string
            dr = row.get("discovery_result")
            if isinstance(dr, str):
                try:
                    dr = json.loads(dr)
                except:
                    dr = {}
            row["discovery_result"] = dr
            session["_audit_rows"][audit_id] = row

        # Only proceed when no pending jobs AND both audit rows and signed URLs are ready
        expected = len(session["file_audit_ids"])
        if (not session["_pending_jobs"]
                and len(session.get("_audit_rows", {})) >= expected
                and len(session.get("_file_bytes", {})) >= expected):
            background_tasks.add_task(stage_process_files, session_id)

    # fetch_file_binary is handled by /file/{job_id} endpoint directly

    elif stage == "sku_map_lookup":
        # Got supplier SKU mappings — apply to sales rows
        rows = request_body.get("matches", [])
        sku_map = {r["retailer_sku"]: r for r in rows if r.get("supplier_sku")}
        sales_rows = session.get("sales_rows", [])
        for row in sales_rows:
            if not row.get("supplier_sku") and row["retailer_sku"] in sku_map:
                mapped = sku_map[row["retailer_sku"]]
                row["supplier_sku"] = mapped.get("supplier_sku")
        session["sales_rows"] = sales_rows

        # Rebuild unresolved list
        session["unresolved_skus"] = sorted({
            row["retailer_sku"] for row in sales_rows
            if not row.get("supplier_sku")
        })

        if not session["_pending_jobs"]:
            background_tasks.add_task(stage_create_table, session_id)

    elif stage == "create_table":
        if not session["_pending_jobs"]:
            background_tasks.add_task(stage_write_sales, session_id)

    elif stage == "write_sales_batch":
        if not session["_pending_jobs"]:
            background_tasks.add_task(stage_write_inventory, session_id)

    elif stage == "write_inventory_batch":
        if not session["_pending_jobs"]:
            background_tasks.add_task(stage_write_inventory, session_id)

    elif stage == "finalize_audit":
        if not session["_pending_jobs"]:
            background_tasks.add_task(stage_complete, session_id)

    return JSONResponse(content={"status": "ok", "job_id": job_id})


# ─────────────────────────────────────────────
# STATUS ENDPOINT
# ─────────────────────────────────────────────



@router.post("/file/{job_id}")
async def file_upload_response(job_id: str, file: UploadFile = File(...), background_tasks: BackgroundTasks = BackgroundTasks()):
    """Receive binary file from n8n S3 webhook and store in session."""
    if job_id not in _jobs:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    job        = _jobs.pop(job_id)
    session_id = job["session_id"]
    audit_id   = job["audit_id"]

    if session_id not in _sessions:
        return JSONResponse(content={"status": "ok", "note": "session already closed"})

    session = _sessions[session_id]
    session["_pending_jobs"].discard(job_id)

    # Store file bytes in session
    data = await file.read()
    session.setdefault("_file_bytes", {})[audit_id] = data
    session.setdefault("_filenames", {})[audit_id] = file.filename

    # Check if ready to process
    expected = len(session["file_audit_ids"])
    if (not session["_pending_jobs"]
            and len(session.get("_audit_rows", {})) >= expected
            and len(session.get("_file_bytes", {})) >= expected):
        background_tasks.add_task(stage_process_files, session_id)

    return JSONResponse(content={"status": "ok", "job_id": job_id})

@router.get("/ingest/status/{session_id}")
async def ingest_status(session_id: str):
    if session_id not in _sessions:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    session = _sessions[session_id]
    return JSONResponse(content={
        "session_id": session_id,
        "stage":      session.get("stage"),
        "status":     session.get("status"),
        "result":     session.get("result"),
    })


@router.get("/ingest/health")
def health():
    return {"status": "ok", "version": "1.0.0"}
