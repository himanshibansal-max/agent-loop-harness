#!/usr/bin/env python3
"""
loaders.py — single source of truth for all data loading and parsing.

Covers:
  - CSV files (pandas-based)
  - Slack activity aggregation
  - Guideline documents (PDF + DOCX, stdlib only)
  - Contractor invoices (DOCX)
  - Pre-computed summaries (rate mismatches, holiday dates)

Public API
----------
  load_all(data_dir) -> dict        # main entry point used by data_loader.py
  load_csv(path, date_cols)         # single CSV -> list[dict]
  aggregate_slack_activity(rows)    # message-level rows -> daily summaries
"""

import os
import re
import subprocess
import warnings
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timezone as _tz
from typing import Optional

import pandas as pd


# ══════════════════════════════════════════════════════════════════════════════
# CSV loaders
# ══════════════════════════════════════════════════════════════════════════════

def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [c.strip().lower() for c in df.columns]
    return df


def _parse_date_columns(df: pd.DataFrame, date_cols: list) -> pd.DataFrame:
    for col in date_cols:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.strftime("%Y-%m-%d")
    return df


def load_csv(path: str, date_cols: list = None) -> list:
    """Load a single CSV file. Returns list of dicts; NaN → None."""
    df = pd.read_csv(path)
    df = _normalize_columns(df)
    if date_cols:
        df = _parse_date_columns(df, date_cols)
    df = df.where(pd.notnull(df), None)
    return df.to_dict(orient="records")


def _load(data_dir: str, filename: str, date_cols: list = None) -> list:
    """Load a CSV from data_dir/filename. Warns and returns [] if missing."""
    path = os.path.join(data_dir, filename)
    if not os.path.exists(path):
        warnings.warn(f"loaders: file not found, skipping: {path}")
        return []
    return load_csv(path, date_cols=date_cols)


def load_all_csvs(data_dir: str) -> dict:
    """Load every known CSV source. Returns raw data dict (no summaries yet)."""
    slack_raw = _load(data_dir, "slack_activity.csv", date_cols=["date"])
    return {
        "timesheets":       _load(data_dir, "kimai_timesheets.csv",  date_cols=["date", "submitted_at"]),
        "hr_assignments":   _load(data_dir, "hr_assignments.csv"),
        "pm_projects":      _load(data_dir, "pm_projects.csv",       date_cols=["end_date"]),
        "hr_employees":     _load(data_dir, "hr_employees.csv"),
        "hr_leave":         _load(data_dir, "hr_leave.csv",          date_cols=["date"]),
        "slack_activity":   aggregate_slack_activity(slack_raw),
        "calendar_leave":   _load(data_dir, "calendar_leave.csv",    date_cols=["date"]),
        "calendar_holidays":_load(data_dir, "calendar_holidays.csv", date_cols=["date"]),
        "emails":           _load(data_dir, "emails.csv",            date_cols=["date"]),
        # backward-compat stub (file was removed)
        "calendar_events":  [],
    }


# ══════════════════════════════════════════════════════════════════════════════
# Slack aggregation
# ══════════════════════════════════════════════════════════════════════════════

def aggregate_slack_activity(raw_rows: list) -> list:
    """Aggregate message-level Slack rows into per-(user, date) daily summaries.

    Input columns : user, date, channel, ts, text, thread_ts,
                    reply_count, reaction_name, reaction_count
    Output keys   : user, date, messages, reactions, first_seen,
                    last_seen, texts, channels
    """
    grouped = defaultdict(lambda: {
        "ts_vals": [], "messages": 0, "reactions": 0,
        "texts": [], "channels": set(),
    })

    for row in raw_rows:
        key = (row.get("user") or "", row.get("date") or "")
        g = grouped[key]

        ts = row.get("ts")
        if ts is not None:
            try:
                g["ts_vals"].append(float(ts))
                g["messages"] += 1
            except (TypeError, ValueError):
                pass

        try:
            g["reactions"] += int(row.get("reaction_count") or 0)
        except (TypeError, ValueError):
            pass

        text = row.get("text")
        if text and str(text).strip():
            g["texts"].append(str(text).strip())

        channel = row.get("channel")
        if channel and str(channel).strip():
            g["channels"].add(str(channel).strip())

    result = []
    for (user, date), g in sorted(grouped.items()):
        ts_vals = g["ts_vals"]
        first_seen = last_seen = None
        if ts_vals:
            first_seen = datetime.fromtimestamp(min(ts_vals), tz=_tz.utc).strftime("%H:%M:%S")
            last_seen  = datetime.fromtimestamp(max(ts_vals), tz=_tz.utc).strftime("%H:%M:%S")
        result.append({
            "user": user, "date": date,
            "messages": g["messages"], "reactions": g["reactions"],
            "first_seen": first_seen, "last_seen": last_seen,
            "texts": g["texts"], "channels": sorted(g["channels"]),
        })
    return result


# ══════════════════════════════════════════════════════════════════════════════
# Document loaders — DOCX + PDF (stdlib only, no python-docx / pdfplumber)
# ══════════════════════════════════════════════════════════════════════════════

def _extract_docx_text(path: str) -> str:
    """Read plain text from a .docx via zipfile + ElementTree."""
    try:
        with zipfile.ZipFile(path, "r") as z:
            with z.open("word/document.xml") as f:
                tree = ET.parse(f)
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        warnings.warn(f"loaders: cannot read docx {path}: {exc}")
        return ""
    parts = [
        elem.text for elem in
        tree.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t")
        if elem.text
    ]
    return " ".join(parts)


def _extract_pdf_text(path: str) -> Optional[str]:
    """Extract text from a PDF via the pdftotext CLI. Returns None on failure."""
    try:
        result = subprocess.run(["pdftotext", path, "-"], capture_output=True, text=True)
    except FileNotFoundError:
        warnings.warn("loaders: pdftotext not found — PDF parsing skipped")
        return None
    if result.returncode != 0:
        warnings.warn(f"loaders: pdftotext exited {result.returncode} for {path}")
        return None
    return result.stdout


# ── Holiday parsing ──────────────────────────────────────────────────────────

_MONTH_MAP = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "jun": "06", "jul": "07", "aug": "08", "sep": "09",
    "oct": "10", "nov": "11", "dec": "12",
}


def _normalise_date(raw: str) -> Optional[str]:
    raw = raw.strip()
    for pat, fn in [
        (r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", lambda g: (g[2], _MONTH_MAP.get(g[0].lower()), g[1])),
        (r"(\d{1,2})\s+([A-Za-z]+),?\s+(\d{4})", lambda g: (g[2], _MONTH_MAP.get(g[1].lower()), g[0])),
        (r"(\d{1,2})[-/]([A-Za-z]+)[-/](\d{2,4})", lambda g: (
            ("20" + g[2] if len(g[2]) == 2 else g[2]), _MONTH_MAP.get(g[1].lower()), g[0])),
        (r"(\d{4})-(\d{2})-(\d{2})", lambda g: (g[0], g[1], g[2])),
        (r"(\d{1,2})/(\d{1,2})/(\d{4})", lambda g: (g[2], g[1], g[0])),
    ]:
        m = re.match(pat, raw)
        if m:
            year, mon, day = fn(m.groups())
            if mon:
                return f"{year}-{int(mon):02d}-{int(day):02d}"
    return None


def _parse_holidays_text(text: str) -> dict:
    fixed, optional = [], []
    current = "fixed"
    date_pats = [
        r"([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
        r"(\d{1,2}[-/][A-Za-z]+[-/]\d{2,4})",
        r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
        r"(\d{4}-\d{2}-\d{2})",
    ]
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        lower = s.lower()
        if re.search(r"\boptional\b", lower):
            current = "optional"
        elif re.search(r"\bfixed\b|\bmandatory\b|\bnational\b|\bgazetted\b", lower):
            current = "fixed"
        for pat in date_pats:
            m = re.search(pat, s)
            if not m:
                continue
            iso = _normalise_date(m.group(1))
            if not iso:
                continue
            name = s[m.end():].strip(" -–—|") or s[:m.start()].strip(" -–—|")
            if not name or re.match(r"^[\d\s]+$", name):
                continue
            (fixed if current == "fixed" else optional).append(
                {"date": iso, "name": name, "type": current}
            )
            break
    return {"fixed": fixed, "optional": optional}


def _parse_leave_policy_text(text: str) -> dict:
    def _num(pat, default):
        m = re.search(pat, text, re.IGNORECASE)
        try:
            return int(m.group(1)) if m else default
        except (IndexError, ValueError):
            return default

    return {
        "annual_entitlement_days": 24 if re.search(r"\b24\b", text) else
            _num(r"(\d+)\s+(?:annual|casual|privilege|pl|cl)\b", 24),
        "bereavement_days": _num(r"bereavement[^.]{0,60}?(\d+)\s+(?:working\s+)?days", 5),
        "carry_forward_max": _num(r"carry[- ]?forward[^.]{0,60}?(\d+)", 5),
        "encashment_max":    _num(r"encash(?:ment)?[^.]{0,60}?(\d+)", 20),
    }


def _parse_timesheet_policy_text(text: str) -> dict:
    def _num(pat, default):
        m = re.search(pat, text, re.IGNORECASE)
        try:
            return int(m.group(1)) if m else default
        except (IndexError, ValueError):
            return default

    return {
        "daily_hours":  _num(r"(\d+)\s*hours?\s*(?:per\s*day|/day|daily)", 8),
        "weekly_hours": _num(r"(\d+)\s*hours?\s*(?:per\s*week|/week|weekly)", 40),
        "weekend_logging_requires_approval": bool(
            re.search(r"weekend.{0,80}(?:approval|approved|prior\s+approval)", text, re.IGNORECASE)
        ),
    }


def load_guidelines(guidelines_dir: str) -> dict:
    """Parse guideline documents in guidelines_dir.

    Uses regex for structured fields where it works reliably, and always
    includes raw_text so the validation agent (Claude Code) can extract
    anything the regex misses — with results cached via the save_extraction
    MCP tool to avoid re-extraction on subsequent runs.
    """
    result = {
        "holidays": {"fixed": [], "optional": []},
        "leave_policy": {"annual_entitlement_days": 24, "bereavement_days": 5,
                         "carry_forward_max": 5, "encashment_max": 20},
        "timesheet_policy": {"daily_hours": 8, "weekly_hours": 40,
                             "weekend_logging_requires_approval": True},
        "raw_texts": {},  # fname -> text, available for Claude Code extraction
    }
    if not os.path.isdir(guidelines_dir):
        return result

    for fname in os.listdir(guidelines_dir):
        fpath = os.path.join(guidelines_dir, fname)
        lower = fname.lower()
        try:
            if "holiday" in lower and lower.endswith(".pdf"):
                text = _extract_pdf_text(fpath)
                if text:
                    result["raw_texts"][fname] = text
                    parsed = _parse_holidays_text(text)
                    if parsed["fixed"] or parsed["optional"]:
                        result["holidays"] = parsed

            elif "leave" in lower and lower.endswith(".pdf"):
                text = _extract_pdf_text(fpath)
                if text:
                    result["raw_texts"][fname] = text
                    result["leave_policy"] = _parse_leave_policy_text(text)

            elif "timesheet" in lower and lower.endswith(".docx"):
                text = _extract_docx_text(fpath)
                if text.strip():
                    result["raw_texts"][fname] = text
                    result["timesheet_policy"] = _parse_timesheet_policy_text(text)

        except Exception as exc:
            warnings.warn(f"loaders: error processing guideline {fname}: {exc}")

    return result


# ── SOW parsing ──────────────────────────────────────────────────────────────

def _extract_docx_tables(path: str) -> list:
    """Extract tables from a .docx as a list of tables.
    Each table is a list of rows; each row is a list of cell text strings.
    """
    try:
        with zipfile.ZipFile(path, "r") as z:
            with z.open("word/document.xml") as f:
                tree = ET.parse(f)
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        warnings.warn(f"loaders: cannot read docx tables {path}: {exc}")
        return []

    _NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    tables = []
    for tbl in tree.iter(f"{_NS}tbl"):
        rows = []
        for tr in tbl.iter(f"{_NS}tr"):
            cells = []
            for tc in tr.iter(f"{_NS}tc"):
                cell_text = " ".join(
                    t.text for t in tc.iter(f"{_NS}t") if t.text
                ).strip()
                cells.append(cell_text)
            if cells:
                rows.append(cells)
        if rows:
            tables.append(rows)
    return tables


def _stem_fallback(filename: str) -> str:
    """Derive a project name from a SOW filename when no name was extracted."""
    stem = re.sub(r"^SOW_", "", filename, flags=re.IGNORECASE)
    stem = re.sub(r"\.docx$", "", stem, flags=re.IGNORECASE)
    return stem.replace("_", " ").strip()


def _parse_rate(raw: str) -> Optional[float]:
    """Extract a numeric rate from strings like '$100', '100/hr', '100.00'."""
    if not raw:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", raw.replace(",", ""))
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


def _parse_pct(raw: str) -> Optional[float]:
    """Extract percentage value as float 0–100."""
    if not raw:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", raw)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


def _find_resource_table(tables: list, template: Optional[int]) -> Optional[list]:
    """Locate the resource/allocation table among all document tables."""
    if template == 2:
        # Template 2 header: ID | Consultant | Level | Alloc | Rate/Hr | Hrs/Mo
        for tbl in tables:
            if not tbl:
                continue
            header = " ".join(tbl[0]).lower()
            if ("consultant" in header or "name" in header) and "rate" in header:
                return tbl
    else:
        # Templates 1/3: Name | Role | Rate
        for tbl in tables:
            if not tbl:
                continue
            header = " ".join(tbl[0]).lower()
            if "name" in header and "rate" in header:
                return tbl
    return None


def _parse_sow_document(path: str) -> dict:
    """Parse a single SOW .docx file and return a structured dict.

    Regex handles the majority of SOWs.  For partial/unrecognized results,
    raw_text is included so Claude Code can extract the missing fields and
    cache the result via save_extraction MCP tool.
    """
    filename = os.path.basename(path)
    text = _extract_docx_text(path)
    tables = _extract_docx_tables(path)

    # ── Template detection ──────────────────────────────────────────────────
    if re.search(r"Contract Details", text):
        template: Optional[int] = 2
    elif re.search(r"Effective Date", text, re.IGNORECASE):
        template = 1
    elif re.search(r"entered into between", text, re.IGNORECASE):
        template = 3
    else:
        template = None

    # ── SOW reference ───────────────────────────────────────────────────────
    m = re.search(r"(TG-SOW-\d{4}-\d{3})", text)
    sow_reference = m.group(1) if m else None

    # ── Project name ────────────────────────────────────────────────────────
    project_name_raw = None
    for pat in [
        r"Project\s*(?:Name|Title)?[:\s]+([^\n|]{3,80})",
        r"Statement\s+of\s+Work\s*[-–—]\s*([^\n]{3,80})",
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            cand = m.group(1).strip().strip("-–—").strip()
            if cand and not re.match(r"^(?:between|for|with)\b", cand, re.IGNORECASE):
                project_name_raw = cand[:100]
                break
    # Fallback: derive from filename
    if not project_name_raw:
        stem = re.sub(r"^SOW_", "", filename, flags=re.IGNORECASE)
        stem = re.sub(r"\.docx$", "", stem, flags=re.IGNORECASE)
        project_name_raw = stem.replace("_", " ").strip()

    # ── Client ──────────────────────────────────────────────────────────────
    client = None
    for pat in [
        r"Client\s*(?:Name)?[:\s]+([^\n|]{3,80})",
        r"Customer[:\s]+([^\n|]{3,80})",
        r"Prepared\s+for[:\s]+([^\n|]{3,80})",
        r"between[^.]+?and\s+([A-Z][^\n,\.]{2,60})",
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            cand = m.group(1).strip()
            if cand and len(cand) > 2:
                client = cand[:100]
                break

    # ── Dates ───────────────────────────────────────────────────────────────
    start_date: Optional[str] = None
    end_date: Optional[str] = None

    if template == 1:
        m = re.search(r"Effective\s+Date[:\s]+([A-Za-z0-9 ,/\-]{4,30})", text, re.IGNORECASE)
        if m:
            start_date = _normalise_date(m.group(1).strip())
        m = re.search(r"End\s+Date[:\s]+([A-Za-z0-9 ,/\-]{4,30})", text, re.IGNORECASE)
        if m:
            end_date = _normalise_date(m.group(1).strip())
    elif template == 2:
        m = re.search(r"(?:Start|Begin)\s*Date?[:\s]+([A-Za-z0-9 ,/\-]{4,30})", text, re.IGNORECASE)
        if m:
            start_date = _normalise_date(m.group(1).strip())
        m = re.search(r"End\s*Date?[:\s]+([A-Za-z0-9 ,/\-]{4,30})", text, re.IGNORECASE)
        if m:
            end_date = _normalise_date(m.group(1).strip())
    elif template == 3:
        for pat in [
            r"(?:commence|effective|begin)[s\w\s]*?(?:on|from)?\s+([A-Za-z]+ \d{1,2},? \d{4})",
            r"effective\s+(?:as\s+of\s+)?([A-Za-z]+ \d{1,2},? \d{4}|\d{1,2}\s+[A-Za-z]+\s+\d{4})",
        ]:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                start_date = _normalise_date(m.group(1).strip())
                break
        for pat in [
            r"(?:conclud|terminat)[e\w\s]*?(?:on)?\s+([A-Za-z]+ \d{1,2},? \d{4})",
            r"(?:through|until|to)\s+([A-Za-z]+ \d{1,2},? \d{4}|\d{1,2}\s+[A-Za-z]+\s+\d{4})",
        ]:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                end_date = _normalise_date(m.group(1).strip())
                break

    # Generic fallback for any template
    if not start_date:
        m = re.search(
            r"(?:start|from|effective)\s*(?:date)?[:\s]+"
            r"([A-Za-z]+ \d{1,2},? \d{4}|\d{1,2}[-/ ][A-Za-z]+[-/ ]\d{2,4}|\d{4}-\d{2}-\d{2})",
            text, re.IGNORECASE,
        )
        if m:
            start_date = _normalise_date(m.group(1).strip())
    if not end_date:
        m = re.search(
            r"(?:end|until|through)\s*(?:date)?[:\s]+"
            r"([A-Za-z]+ \d{1,2},? \d{4}|\d{1,2}[-/ ][A-Za-z]+[-/ ]\d{2,4}|\d{4}-\d{2}-\d{2})",
            text, re.IGNORECASE,
        )
        if m:
            end_date = _normalise_date(m.group(1).strip())

    # ── Resources ──────────────────────────────────────────────────────────
    resources: list = []
    currency: Optional[str] = None
    payment_terms: Optional[str] = None
    scope_items: list = []

    resource_table = _find_resource_table(tables, template)
    if resource_table and len(resource_table) > 1:
        header = [h.lower() for h in resource_table[0]]
        if template == 2:
            # ID | Consultant | Level | Alloc | Rate/Hr | Hrs/Mo
            name_idx  = next((i for i, h in enumerate(header) if "consultant" in h or "name" in h), 1)
            role_idx  = next((i for i, h in enumerate(header) if "level" in h or "role" in h), 2)
            alloc_idx = next((i for i, h in enumerate(header) if "alloc" in h), -1)
            rate_idx  = next((i for i, h in enumerate(header) if "rate" in h), -1)
            hrs_idx   = next((i for i, h in enumerate(header) if "hrs" in h or "hour" in h), -1)
            for row in resource_table[1:]:
                if len(row) <= max(name_idx, 0):
                    continue
                name = row[name_idx].strip() if name_idx < len(row) else None
                if not name or re.match(r"^(?:total|subtotal|notes?)\b", name, re.IGNORECASE):
                    continue
                resources.append({
                    "name": name,
                    "role": row[role_idx].strip() if role_idx >= 0 and role_idx < len(row) else None,
                    "rate": _parse_rate(row[rate_idx]) if rate_idx >= 0 and rate_idx < len(row) else None,
                    "allocation_pct": _parse_pct(row[alloc_idx]) if alloc_idx >= 0 and alloc_idx < len(row) else None,
                    "hours_per_month": _parse_rate(row[hrs_idx]) if hrs_idx >= 0 and hrs_idx < len(row) else None,
                })
        else:
            # Templates 1/3: Name | Role | Rate
            name_idx = next((i for i, h in enumerate(header) if "name" in h), 0)
            role_idx = next((i for i, h in enumerate(header) if "role" in h or "title" in h or "designation" in h), 1)
            rate_idx = next((i for i, h in enumerate(header) if "rate" in h or "cost" in h), 2)
            for row in resource_table[1:]:
                if len(row) <= name_idx:
                    continue
                name = row[name_idx].strip() if name_idx < len(row) else None
                if not name or re.match(r"^(?:total|subtotal|notes?)\b", name, re.IGNORECASE):
                    continue
                resources.append({
                    "name": name,
                    "role": row[role_idx].strip() if role_idx < len(row) else None,
                    "rate": _parse_rate(row[rate_idx]) if rate_idx < len(row) else None,
                    "allocation_pct": None,
                    "hours_per_month": None,
                })

    # Template 2 extra fields
    if template == 2:
        m = re.search(r"Currency[:\s]+([A-Z]{3})", text)
        if m:
            currency = m.group(1)
        m = re.search(r"Payment\s+Terms?[:\s]+([^\n]{3,200})", text, re.IGNORECASE)
        if m:
            payment_terms = m.group(1).strip()[:200]
        scope_items = re.findall(r"\bS-\d+[:\s]+([^\n]+)", text)

    # ── Parse status ────────────────────────────────────────────────────────
    if template is None:
        parse_status = "unrecognized_template"
    elif start_date and end_date and resources:
        parse_status = "ok"
    else:
        parse_status = "partial"

    if parse_status != "ok":
        warnings.warn(f"loaders: SOW parse_status={parse_status} for {filename}")

    return {
        "source_file": filename,
        "sow_reference": sow_reference,
        "project_name_raw": project_name_raw,
        "client": client,
        "start_date": start_date,
        "end_date": end_date,
        "currency": currency,
        "payment_terms": payment_terms,
        "resources": resources,
        "scope_items": scope_items,
        "raw_text": text,
        "parse_status": parse_status,
    }


def parse_sow_documents(sow_dir: str) -> list:
    """Parse all .docx SOW files in sow_dir. Returns list of SOW dicts."""
    results = []
    if not os.path.isdir(sow_dir):
        return results
    for fname in sorted(os.listdir(sow_dir)):
        if not fname.lower().endswith(".docx"):
            continue
        path = os.path.join(sow_dir, fname)
        try:
            results.append(_parse_sow_document(path))
        except Exception as exc:
            warnings.warn(f"loaders: error parsing SOW {fname}: {exc}")
            results.append({
                "source_file": fname,
                "sow_reference": None,
                "project_name_raw": None,
                "client": None,
                "start_date": None,
                "end_date": None,
                "currency": None,
                "payment_terms": None,
                "resources": [],
                "scope_items": [],
                "raw_text": "",
                "parse_status": "unrecognized_template",
            })
    return results


# ── Contractor invoice parsing ────────────────────────────────────────────────

def _parse_invoice_text(text: str, filename: str) -> dict:
    def _search(pat, flags=re.IGNORECASE):
        m = re.search(pat, text, flags)
        return m.group(1).strip() if m else None

    invoice_id = (
        _search(r"\b([A-Z]{1,5}-(?:INV-)?20\d{2}-[A-Z]{2,3}-\d{1,3})\b", re.IGNORECASE)
        or _search(r"Invoice\s*(?:No|Number|#)[:\s]+([A-Z0-9\-]+)", re.IGNORECASE)
    )
    contractor_name = (
        _search(r"Invoice\s+from[:\s]+([^\n]+)")
        or _search(r"Prepared\s+by[:\s]+([^\n]+)")
        or _search(r"Consultant[:\s]+([^\n]+)")
    )
    period = _search(r"Period[:\s]+([A-Za-z]+\s+\d{4})")
    project = _search(r"Project[:\s]+([^\n]+)")

    hours_billed = None
    hs = _search(r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?)\b")
    if hs:
        try:
            hours_billed = float(hs)
        except ValueError:
            pass

    rate = None
    rs = _search(r"\$\s*(\d+(?:\.\d+)?)\s*/\s*h(?:r|our)") or _search(r"[Rr]ate[:\s]+\$?\s*(\d+(?:\.\d+)?)")
    if rs:
        try:
            rate = float(rs)
        except ValueError:
            pass

    total = None
    ts = _search(r"Total[:\s]+\$?\s*(\d[\d,]*(?:\.\d+)?)")
    if ts:
        try:
            total = float(ts.replace(",", ""))
        except ValueError:
            pass

    return {
        "source_file": os.path.basename(filename),
        "invoice_id": invoice_id,
        "contractor_name": contractor_name,
        "period": period,
        "project": project,
        "hours_billed": hours_billed,
        "rate": rate,
        "total": total,
        "raw_text": text,  # kept for extraction cache — Claude extracts what regex misses
    }


def load_contractor_invoices(invoices_dir: str) -> list:
    """Parse all .docx contractor invoice files in invoices_dir."""
    results = []
    if not os.path.isdir(invoices_dir):
        return results
    for fname in sorted(os.listdir(invoices_dir)):
        if not fname.lower().endswith(".docx"):
            continue
        path = os.path.join(invoices_dir, fname)
        try:
            text = _extract_docx_text(path)
            if text.strip():
                results.append(_parse_invoice_text(text, path))
            else:
                warnings.warn(f"loaders: empty invoice text from {fname}")
        except Exception as exc:
            warnings.warn(f"loaders: error parsing invoice {fname}: {exc}")
    return results


# ══════════════════════════════════════════════════════════════════════════════
# Pre-computed summaries
# ══════════════════════════════════════════════════════════════════════════════

def build_summaries(data: dict) -> dict:
    """Compute derived summaries from already-loaded raw data."""
    timesheets = data.get("timesheets", [])
    hr_employees = data.get("hr_employees", [])
    hr_assignments = data.get("hr_assignments", [])
    calendar_holidays = data.get("calendar_holidays", [])
    guidelines = data.get("guidelines", {})

    # hours_by_project
    hours_by_project: dict = defaultdict(float)
    for r in timesheets:
        try:
            hours_by_project[r.get("project") or ""] += float(r.get("hours") or 0)
        except (TypeError, ValueError):
            pass
    hours_by_project = {k: round(v, 4) for k, v in hours_by_project.items()}

    # hours_by_user_date
    hours_by_user_date: dict = defaultdict(lambda: defaultdict(float))
    for r in timesheets:
        try:
            hours_by_user_date[r.get("user") or ""][r.get("date") or ""] += float(r.get("hours") or 0)
        except (TypeError, ValueError):
            pass
    hours_by_user_date = {
        u: {d: round(h, 4) for d, h in dates.items()}
        for u, dates in hours_by_user_date.items()
    }

    # projects_with_no_assignment
    assigned = {r.get("project") or "" for r in hr_assignments}
    timesheet_projects = {r.get("project") or "" for r in timesheets}
    projects_with_no_assignment = sorted(
        [p for p in timesheet_projects - assigned if isinstance(p, str)],
        key=str.lower,
    )

    # date_range
    dates = [r.get("date") for r in timesheets if r.get("date") and r.get("date") != "NaT"]
    date_range = {"start": min(dates) if dates else None, "end": max(dates) if dates else None}

    # holiday_dates — union of calendar_holidays + guideline fixed holidays
    holiday_set = set()
    for r in calendar_holidays:
        d = r.get("date")
        if d and str(d) != "NaT":
            holiday_set.add(str(d))
    for h in (guidelines.get("holidays") or {}).get("fixed", []):
        if h.get("date"):
            holiday_set.add(str(h["date"]))

    # rate_mismatches
    emp_rates = {}
    for emp in hr_employees:
        u = (emp.get("username") or "").strip().lower()
        try:
            emp_rates[u] = float(emp.get("rate") or 0)
        except (TypeError, ValueError):
            pass

    mismatch_acc: dict = defaultdict(lambda: {
        "kimai_rate": None, "contract_rate": None, "delta": None,
        "total_hours": 0.0, "financial_impact": 0.0, "entries": 0,
    })
    for r in timesheets:
        user = (r.get("user") or "").strip().lower()
        if user not in emp_rates:
            continue
        try:
            kr = float(r.get("hourly_rate") or 0)
        except (TypeError, ValueError):
            continue
        cr = emp_rates[user]
        if abs(kr - cr) < 0.001:
            continue
        try:
            h = float(r.get("hours") or 0)
        except (TypeError, ValueError):
            h = 0.0
        delta = kr - cr
        m = mismatch_acc[user]
        m["kimai_rate"] = kr
        m["contract_rate"] = cr
        m["delta"] = round(delta, 4)
        m["total_hours"] = round(m["total_hours"] + h, 4)
        m["financial_impact"] = round(m["financial_impact"] + delta * h, 4)
        m["entries"] += 1

    rate_mismatches = [
        {"employee": u, **v} for u, v in mismatch_acc.items()
    ]

    return {
        "hours_by_project": hours_by_project,
        "hours_by_user_date": hours_by_user_date,
        "projects_with_no_assignment": projects_with_no_assignment,
        "date_range": date_range,
        "holiday_dates": sorted(holiday_set),
        "rate_mismatches": rate_mismatches,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Main entry point
# ══════════════════════════════════════════════════════════════════════════════

def load_all(data_dir: str) -> dict:
    """Load everything and return the complete data dict ready for JSON serialisation."""
    data = load_all_csvs(data_dir)

    docs_dir = os.path.join(data_dir, "documents")
    data["guidelines"] = load_guidelines(os.path.join(docs_dir, "guidelines"))
    data["contractor_invoices"] = load_contractor_invoices(os.path.join(docs_dir, "contractor_invoices"))
    data["sows"] = parse_sow_documents(os.path.join(docs_dir, "sow"))

    summary = build_summaries(data)
    data["holiday_dates"] = summary.pop("holiday_dates")
    data["rate_mismatches"] = summary.pop("rate_mismatches")
    data["summary"] = summary

    return data
