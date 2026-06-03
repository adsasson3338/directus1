from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse
from typing import List
import openpyxl
import hashlib
import io
import re
from datetime import datetime

app = FastAPI(title="Sheet Discovery Service", version="1.0.0")

# ─────────────────────────────────────────────
# PASS 1 — VOCABULARY
# ─────────────────────────────────────────────

DISQUALIFY_KEYWORDS = [
    "$$$", "$$", "projected orders", "order projection", "total dc order",
    "collaborative fc", "cfp", "oos %", "instock %", "out of stock",
    "forecasts", "forecast", "analysis", "projected",
    "ly sales", "actual sales", "ly promotions", "air sales", "mtd aws",
    "% of sales by color", "ytd sales to ly", "6 month total", "year total",
]

FILENAME_DISQUALIFY = ["forecast", "cfp", "projection"]
SHEETNAME_DISQUALIFY = ["cfp", "forecast", "fcst", "projection", "vendor", "analysis", "oos %", "instock %"]

QUALIFY_KEYWORDS = [
    "sales u", "sku sales", "weekly sku", "units sold", "pos units",
    "sales units", "unit sales", "wk sales",
]

NEUTRAL_SALES_KEYWORDS = [
    "sales", "units", "sku", "pos", "sold",
]

def pass1_vocabulary(ws, sheet_name: str = "", filename: str = "") -> dict:
    rows = list(ws.iter_rows(values_only=True))[:30]
    vocabulary = [
        str(val).strip()
        for row in rows
        for val in row
        if isinstance(val, str) and val.strip()
    ]
    vocab_lower = [v.lower() for v in vocabulary]

    # Cell vocabulary checks
    disqualify_hits = [kw for kw in DISQUALIFY_KEYWORDS if any(kw in v for v in vocab_lower)]
    qualify_hits    = [kw for kw in QUALIFY_KEYWORDS    if any(kw in v for v in vocab_lower)]
    neutral_hits    = [kw for kw in NEUTRAL_SALES_KEYWORDS if any(kw in v for v in vocab_lower)]

    # Sheet name check
    sn_lower = sheet_name.lower()
    sheet_name_hits = [kw for kw in SHEETNAME_DISQUALIFY if kw in sn_lower]

    # Filename check
    fn_lower = filename.lower()
    filename_hits = [kw for kw in FILENAME_DISQUALIFY if kw in fn_lower]

    all_disqualify = disqualify_hits + sheet_name_hits + filename_hits

    if all_disqualify:
        verdict = "disqualified"
    elif qualify_hits:
        verdict = "proceed"
    elif neutral_hits:
        verdict = "proceed"
    else:
        verdict = "uncertain"

    return {
        "verdict": verdict,
        "disqualify_keywords_found": disqualify_hits,
        "sheet_name_disqualify": sheet_name_hits,
        "filename_disqualify": filename_hits,
        "qualify_keywords_found": qualify_hits + neutral_hits,
        "vocabulary_sample": vocabulary[:30],
    }


# ─────────────────────────────────────────────
# PASS 2 — STRUCTURAL ANALYSIS
# ─────────────────────────────────────────────

DATE_RANGE_RE = re.compile(r"^\d{1,2}/\d{1,2}[-–]\d{1,2}/\d{1,2}$")
FISCAL_WEEK_RE = re.compile(r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+wk\s*\d+$", re.IGNORECASE)
WK_NUMBER_RE   = re.compile(r"^wk\s*\d+$", re.IGNORECASE)
YEAR_RE        = re.compile(r"\b(20\d{2})\b")

def classify_cell(val) -> str:
    if val is None:
        return "empty"
    if isinstance(val, datetime):
        return "datetime"
    if isinstance(val, bool):
        return "bool"
    if isinstance(val, int):
        return "integer"
    if isinstance(val, float):
        return "float"
    if isinstance(val, str):
        s = val.strip()
        if DATE_RANGE_RE.match(s):
            return "date_range_string"
        if FISCAL_WEEK_RE.match(s):
            return "fiscal_week_label"
        if WK_NUMBER_RE.match(s):
            return "week_number_label"
        return "string"
    return "unknown"

def is_date_like(val) -> bool:
    return classify_cell(val) in ("datetime", "date_range_string", "fiscal_week_label", "week_number_label")

def find_date_axis(rows) -> dict | None:
    best_row = None
    best_count = 0
    best_cols = []
    best_samples = []
    best_format = None

    for row_idx, row in enumerate(rows[:15]):
        date_cols = [(col_idx, val) for col_idx, val in enumerate(row) if is_date_like(val)]
        if len(date_cols) > best_count:
            best_count = len(date_cols)
            best_row = row_idx
            best_cols = [c for c, _ in date_cols]
            best_samples = [str(v) for _, v in date_cols[:6]]
            formats = set(classify_cell(v) for _, v in date_cols)
            best_format = list(formats)[0] if len(formats) == 1 else "mixed"

    if best_count < 2:
        return None

    # Check if year is present in date samples
    year_present = any(YEAR_RE.search(s) for s in best_samples) or best_format == "datetime"

    # Detect interleaved empty columns
    if len(best_cols) >= 3:
        gaps = [best_cols[i+1] - best_cols[i] for i in range(len(best_cols)-1)]
        interleaved = len(set(gaps)) == 1 and gaps[0] == 2
    else:
        interleaved = False

    return {
        "row": best_row,
        "col_count": best_count,
        "cols": best_cols,
        "sample_values": best_samples,
        "format": best_format,
        "year_present": year_present,
        "interleaved_empty_cols": interleaved,
    }

def find_sku_axis(rows, date_axis_row: int, date_cols: list) -> list:
    if not date_cols:
        return []

    left_boundary = min(date_cols)

    # First pass — look for explicit SKU label in label rows
    SKU_LABELS = ["sku number", "sku", "wic#", "wic #", "dpci", "item", "item #", "item#", "upc"]
    preferred_col = None
    for row_idx in range(max(0, date_axis_row - 2), date_axis_row + 2):
        if row_idx >= len(rows):
            continue
        for col_idx, val in enumerate(rows[row_idx]):
            if col_idx >= left_boundary:
                break
            if isinstance(val, str) and any(sl == val.strip().lower() for sl in SKU_LABELS):
                preferred_col = col_idx
                break
        if preferred_col is not None:
            break

    candidates = []
    for col_idx in range(left_boundary):
        col_values = [rows[r][col_idx] for r in range(len(rows)) if col_idx < len(rows[r])]
        non_empty = [v for v in col_values if v is not None]
        if not non_empty:
            continue

        types = [classify_cell(v) for v in non_empty]
        type_counts = {}
        for t in types:
            type_counts[t] = type_counts.get(t, 0) + 1
        dominant = max(type_counts, key=type_counts.get)

        label = None
        if date_axis_row < len(rows) and col_idx < len(rows[date_axis_row]):
            lv = rows[date_axis_row][col_idx]
            if isinstance(lv, str):
                label = lv.strip()
        # Also check row above date axis for label
        if label is None and date_axis_row > 0 and col_idx < len(rows[date_axis_row - 1]):
            lv = rows[date_axis_row - 1][col_idx]
            if isinstance(lv, str):
                label = lv.strip()

        sample = [v for v in non_empty[1:6] if not isinstance(v, str) or len(v) < 60]

        candidates.append({
            "col": col_idx,
            "label": label,
            "dominant_type": dominant,
            "sample_values": [str(v) for v in sample[:5]],
            "preferred": col_idx == preferred_col,
        })

    # Sort so preferred col comes first
    candidates.sort(key=lambda c: (0 if c["preferred"] else 1, c["col"]))
    return candidates

def sample_crosshair(rows, date_cols: list, sku_candidates: list) -> dict:
    if not date_cols or not sku_candidates:
        return {"values": [], "verdict": "insufficient_data"}

    # Use preferred col first, then fall back to first integer/string candidate
    sku_col = None
    for c in sku_candidates:
        if c.get("preferred"):
            sku_col = c["col"]
            break
    if sku_col is None:
        for c in sku_candidates:
            if c["dominant_type"] in ("integer", "string"):
                sku_col = c["col"]
                break
    if sku_col is None:
        sku_col = sku_candidates[0]["col"]

    values = []
    # Scan up to 100 rows to handle YTD files with many early zero weeks
    for row in rows[1:100]:
        if sku_col >= len(row) or row[sku_col] is None:
            continue
        row_vals = []
        for dc in date_cols[:8]:
            if dc >= len(row):
                continue
            val = row[dc]
            if val is not None:
                row_vals.append(val)
        # Skip rows where all date values are zero — likely early unstarted weeks
        if row_vals and not all(v == 0 for v in row_vals):
            values.extend(row_vals)
        if len(values) >= 30:
            break

    if not values:
        return {"values": [], "verdict": "no_data"}

    numeric = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not numeric:
        return {"values": [], "verdict": "no_numeric_data"}

    # Need meaningful sample size for reliable verdict
    if len(numeric) < 5:
        return {"values": numeric, "verdict": "insufficient_sample"}

    all_int      = all(isinstance(v, int) or (isinstance(v, float) and v == int(v)) for v in numeric)
    # Percentages must be floats with decimal precision between 0-1, not just integers 0 or 1
    all_pct      = (len(numeric) >= 5 and
                    all(isinstance(v, float) and v != int(v) and 0 <= v <= 1 for v in numeric))
    has_prices   = any(v > 1 and isinstance(v, float) and v != int(v) for v in numeric)
    has_units    = any(isinstance(v, int) or (isinstance(v, float) and v == int(v) and v > 1) for v in numeric)

    if all_pct:
        verdict = "percentages"
    elif all_int and not has_prices:
        verdict = "units_sold"
    elif has_prices and has_units:
        verdict = "mixed_prices_and_units"
    elif has_prices:
        verdict = "dollar_values"
    else:
        verdict = "uncertain"

    return {
        "values": [v for v in numeric[:15]],
        "verdict": verdict,
    }

def find_label_row(rows, date_axis_row: int) -> dict:
    # Label row is date_axis_row itself or one below
    for row_idx in [date_axis_row, date_axis_row - 1]:
        if row_idx < 0:
            continue
        row = rows[row_idx]
        labels = {str(col_idx): str(val).strip() for col_idx, val in enumerate(row)
                  if isinstance(val, str) and val.strip()}
        if labels:
            return {"row": row_idx, "values": labels}
    return {"row": date_axis_row, "values": {}}

def find_garbage_patterns(rows, date_cols: list, sku_col: int | None) -> dict:
    section_headers = []
    total_rows = []
    footnote_rows = []

    TOTAL_WORDS = {"total", "grand total", "subtotal", "sum"}
    FOOTNOTE_MARKERS = {"*", "x =", "* =", ">", "<"}

    for row_idx, row in enumerate(rows):
        non_empty = [(ci, v) for ci, v in enumerate(row) if v is not None]
        if not non_empty:
            continue

        str_vals = [str(v).strip() for _, v in non_empty if isinstance(v, str)]
        num_vals = [v for _, v in non_empty if isinstance(v, (int, float)) and not isinstance(v, bool)]

        # Total rows — contain total keyword, have numeric data at date cols
        if any(any(tw in sv.lower() for tw in TOTAL_WORDS) for sv in str_vals):
            total_rows.append({"row": row_idx, "value": str_vals[0] if str_vals else ""})
            continue

        # Section headers — only col 0 has string, no numerics at date cols
        date_vals = [row[dc] for dc in date_cols if dc < len(row) and row[dc] is not None]
        if (len(non_empty) <= 3 and str_vals and not date_vals
                and not any(sv.lower().startswith(m) for sv in str_vals for m in FOOTNOTE_MARKERS)):
            if sku_col is None or row[sku_col] is None:
                section_headers.append({"row": row_idx, "value": str_vals[0]})

        # Footnotes — start with *, x =, >, <
        if str_vals and any(str_vals[0].startswith(m) for m in FOOTNOTE_MARKERS):
            footnote_rows.append({"row": row_idx, "value": str_vals[0]})

    return {
        "section_header_rows": section_headers[:10],
        "total_rows": total_rows[:10],
        "footnote_rows": footnote_rows[:10],
    }

def find_year_anchors(filename: str, rows: list) -> list:
    anchors = []

    # Filename
    for m in YEAR_RE.finditer(filename):
        anchors.append({"source": "filename", "value": m.group(1)})

    # First 5 rows
    for row_idx, row in enumerate(rows[:5]):
        for col_idx, val in enumerate(row):
            if isinstance(val, datetime):
                anchors.append({"source": "cell", "row": row_idx, "col": col_idx, "value": str(val.date())})
            elif isinstance(val, str):
                for m in YEAR_RE.finditer(val):
                    anchors.append({"source": "cell", "row": row_idx, "col": col_idx, "value": m.group(1)})

    return anchors[:5]

def find_supplier_sku(rows, label_row: dict, sku_candidates: list) -> dict:
    SUPPLIER_LABELS = ["vendor part", "vendor style", "style", "part #", "part#",
                       "supplier sku", "mfr", "model", "item #", "vendor part #"]

    for col_str, label in label_row.get("values", {}).items():
        if any(sl in label.lower() for sl in SUPPLIER_LABELS):
            col_idx = int(col_str)
            sample = [str(rows[r][col_idx]) for r in range(1, 6)
                      if col_idx < len(rows[r]) and rows[r][col_idx] is not None]
            return {
                "dedicated_column_found": True,
                "col": col_idx,
                "label": label,
                "sample_values": sample,
            }

    # Check if embedded in description — look for alphanumeric SKU-like patterns
    EMBEDDED_RE = re.compile(r"\b[A-Z0-9]{2,}-[A-Z0-9\-]{2,}\b")
    for c in sku_candidates:
        if c["dominant_type"] == "string":
            hits = sum(1 for v in c["sample_values"] if EMBEDDED_RE.search(v))
            if hits >= 2:
                return {
                    "dedicated_column_found": False,
                    "possibly_embedded_in": {
                        "col": c["col"],
                        "label": c["label"],
                        "evidence": f"{hits} values contain alphanumeric SKU-like patterns",
                        "sample": c["sample_values"][:3],
                    }
                }

    return {"dedicated_column_found": False, "possibly_embedded_in": None}

def count_data_rows(rows, date_cols: list, sku_candidates: list) -> dict:
    if not sku_candidates:
        return {"count": 0, "start_row": None}

    sku_col = next((c["col"] for c in sku_candidates
                    if c["dominant_type"] in ("integer", "string")), sku_candidates[0]["col"])

    data_rows = []
    for row_idx, row in enumerate(rows):
        if sku_col >= len(row) or row[sku_col] is None:
            continue
        if isinstance(row[sku_col], (int, float)) and not isinstance(row[sku_col], bool):
            data_rows.append(row_idx)
        elif isinstance(row[sku_col], str) and row[sku_col].strip():
            # Check if it has numeric values at date cols
            date_vals = [row[dc] for dc in date_cols if dc < len(row) and isinstance(row[dc], (int, float))]
            if date_vals:
                data_rows.append(row_idx)

    return {
        "count": len(data_rows),
        "start_row": data_rows[0] if data_rows else None,
    }

def pass2_structural(ws, filename: str) -> dict:
    rows = list(ws.iter_rows(values_only=True))

    date_axis = find_date_axis(rows)
    if not date_axis:
        return {
            "date_axis": None,
            "sku_axis": [],
            "crosshair": {"values": [], "verdict": "no_date_axis_found"},
            "label_row": {},
            "metadata_cols": [],
            "garbage_patterns": {},
            "year_anchors": find_year_anchors(filename, rows),
            "supplier_sku": {},
            "data_rows": {"count": 0, "start_row": None},
        }

    date_cols = date_axis["cols"]
    sku_candidates = find_sku_axis(rows, date_axis["row"], date_cols)
    crosshair = sample_crosshair(rows, date_cols, sku_candidates)
    label_row = find_label_row(rows, date_axis["row"])

    sku_col = next((c["col"] for c in sku_candidates
                    if c["dominant_type"] in ("integer", "string")), None)

    garbage = find_garbage_patterns(rows, date_cols, sku_col)
    year_anchors = find_year_anchors(filename, rows)
    supplier_sku = find_supplier_sku(rows, label_row, sku_candidates)
    data_rows = count_data_rows(rows, date_cols, sku_candidates)

    # Metadata cols — between sku and first date col
    left_boundary = min(date_cols) if date_cols else 0
    metadata_cols = []
    for c in sku_candidates:
        if c["col"] < left_boundary and c["dominant_type"] not in ("integer",):
            metadata_cols.append(c)

    return {
        "date_axis": date_axis,
        "sku_axis": sku_candidates,
        "crosshair": crosshair,
        "label_row": label_row,
        "metadata_cols": metadata_cols,
        "garbage_patterns": garbage,
        "year_anchors": year_anchors,
        "supplier_sku": supplier_sku,
        "data_rows": data_rows,
    }


# ─────────────────────────────────────────────
# CROSS-SHEET ANALYSIS
# ─────────────────────────────────────────────

def cross_sheet_analysis(sheets_data: list) -> list | None:
    qualified = [s for s in sheets_data if s["pass1"]["verdict"] != "disqualified"
                 and s["pass2"]["crosshair"]["verdict"] == "units_sold"]

    if len(qualified) < 2:
        return None

    results = []
    for i in range(len(qualified)):
        for j in range(i + 1, len(qualified)):
            a = qualified[i]
            b = qualified[j]

            # Build (sku, date_col_idx) key sets from raw rows for comparison
            # Simplified: compare date col sets and note overlap
            a_dates = set(a["pass2"]["date_axis"]["sample_values"])
            b_dates = set(b["pass2"]["date_axis"]["sample_values"])
            date_overlap = bool(a_dates & b_dates)

            results.append({
                "sheets": [a["name"], b["name"]],
                "date_overlap": date_overlap,
                "recommendation": (
                    "same date range — check for SKU overlap and value relationship"
                    if date_overlap else
                    "different date ranges — likely safe to treat independently"
                )
            })

    return results if results else None


# ─────────────────────────────────────────────
# ENDPOINT
# ─────────────────────────────────────────────

def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

@app.post("/discover")
async def discover(files: List[UploadFile] = File(...)):
    if not files:
        raise HTTPException(status_code=400, detail="At least one file required")

    results = []

    for upload in files:
        if not upload.filename.lower().endswith((".xlsx", ".xls")):
            raise HTTPException(status_code=400, detail=f"'{upload.filename}' is not an Excel file")

        data = await upload.read()
        fhash = file_hash(data)

        try:
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Cannot open {upload.filename}: {e}")

        sheets_data = []
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            p1 = pass1_vocabulary(ws, sheet_name=sheet_name, filename=upload.filename)
            p2 = pass2_structural(ws, upload.filename)

            # Final sheet verdict
            if p1["verdict"] == "disqualified":
                sheet_verdict = "not_sales"
            elif p2["crosshair"]["verdict"] == "units_sold":
                sheet_verdict = "sales_units"
            elif p2["crosshair"]["verdict"] == "percentages":
                sheet_verdict = "not_sales"
            elif p2["crosshair"]["verdict"] in ("dollar_values",):
                sheet_verdict = "not_sales"
            elif p1["verdict"] == "proceed":
                sheet_verdict = "likely_sales_needs_review"
            else:
                sheet_verdict = "uncertain"

            sheets_data.append({
                "name": sheet_name,
                "verdict": sheet_verdict,
                "pass1": p1,
                "pass2": p2,
            })

        cross = cross_sheet_analysis(sheets_data)

        results.append({
            "filename": upload.filename,
            "file_hash": fhash,
            "sheet_count": len(sheets_data),
            "sheets": sheets_data,
            "cross_sheet_analysis": cross,
        })

    return JSONResponse(content={"status": "ok", "files": results})


@app.get("/health")
def health():
    return {"status": "ok"}
