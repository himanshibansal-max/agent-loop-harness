#!/usr/bin/env python3
"""
pipeline.py — top-level orchestrator for the timesheet validation pipeline.

Phases
------
  load       Phase 1: Read CSVs + documents → intermediate/loaded_data.json
  validate   Phase 2: Run deterministic checks → intermediate/violations_det.json
  report     Phase 4: Merge violations + render → reports/violations.html
             Requires intermediate/judgment_violations.json from Phase 3.

Phase 3 (judgment) is run by Claude Code — see CLAUDE.md §3 Phase 3.
Claude writes its judgment violations to intermediate/judgment_violations.json.

Usage
-----
  python3 pipeline.py                    # show pipeline status
  python3 pipeline.py --phase load
  python3 pipeline.py --phase validate
  python3 pipeline.py --phase report
"""

import argparse
import json
import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)

LOADED_DATA_PATH    = os.path.join(_ROOT, "intermediate", "loaded_data.json")
DET_VIOLATIONS_PATH = os.path.join(_ROOT, "intermediate", "violations_det.json")
JUDGMENT_PATH       = os.path.join(_ROOT, "intermediate", "judgment_violations.json")
FUZZY_MAP_PATH      = os.path.join(_ROOT, "intermediate", "fuzzy_mapping.json")
REPORT_PATH         = os.path.join(_ROOT, "reports", "violations.html")
DATA_DIR            = os.path.join(_ROOT, "data")


# ── helpers ───────────────────────────────────────────────────────────────────

def _exists(path: str) -> bool:
    return os.path.exists(path)


def _violation_count(path: str) -> int | None:
    """Return violation count from a JSON file (list or {violations: [...]} dict)."""
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, list):
            return len(data)
        if isinstance(data, dict):
            return len(data.get("violations", []))
    except Exception:
        pass
    return None


def _load_violations(path: str) -> tuple[list, list, dict]:
    """Return (violations, fuzzy_mapping, data_range) from a violations JSON file."""
    with open(path) as f:
        data = json.load(f)
    if isinstance(data, list):
        return data, [], {}
    return (
        data.get("violations", []),
        data.get("fuzzy_mapping", []),
        data.get("data_range", {}),
    )


# ── phase status dashboard ────────────────────────────────────────────────────

def phase_status():
    """Print a dashboard showing which phases are complete."""
    print("── Pipeline Status ───────────────────────────────────────────────")

    phases = [
        ("Phase 1 — Load",     LOADED_DATA_PATH,    "python3 pipeline.py --phase load"),
        ("Phase 2 — Validate", DET_VIOLATIONS_PATH, "python3 pipeline.py --phase validate"),
        ("Phase 3 — Judgment", JUDGMENT_PATH,        "Claude Code  (see CLAUDE.md §3 Phase 3)"),
        ("Phase 4 — Report",   REPORT_PATH,          "python3 pipeline.py --phase report"),
    ]

    for label, path, cmd in phases:
        if _exists(path):
            count = _violation_count(path)
            note = f"  ({count} violations)" if count is not None else ""
            print(f"  ✓  {label}{note}")
        else:
            print(f"  ✗  {label}  →  {cmd}")

    print()
    if not _exists(JUDGMENT_PATH):
        print("  Next step: run Claude Code judgment phase — see CLAUDE.md §3 Phase 3")
        print("  When done, run: python3 pipeline.py --phase report")
    elif not _exists(REPORT_PATH):
        print("  Next step: python3 pipeline.py --phase report")
    else:
        print("  Pipeline complete. Open reports/violations.html to review.")


# ── phase 1: load ─────────────────────────────────────────────────────────────

def phase_load():
    print("── Phase 1: Load ─────────────────────────────────────────────────")
    from agents.data_loader import phase_load as _load
    _load(DATA_DIR)


# ── phase 2: validate ─────────────────────────────────────────────────────────

def phase_validate():
    print("── Phase 2: Validate (deterministic checks) ──────────────────────")
    if not _exists(LOADED_DATA_PATH):
        print(f"[validate] {LOADED_DATA_PATH} not found — run --phase load first.")
        sys.exit(1)
    from agents.data_loader import phase_validate as _validate
    _validate()



# ── phase 4: report ───────────────────────────────────────────────────────────

def phase_report():
    print("── Phase 4: Report ───────────────────────────────────────────────")

    if not _exists(DET_VIOLATIONS_PATH):
        print(f"[report] {DET_VIOLATIONS_PATH} not found — run --phase validate first.")
        sys.exit(1)
    if not _exists(JUDGMENT_PATH):
        print(f"[report] {JUDGMENT_PATH} not found — run Phase 3 (Claude judgment) first.")
        sys.exit(1)

    det_violations, _, _ = _load_violations(DET_VIOLATIONS_PATH)
    judgment_violations, _jfm, data_range = _load_violations(JUDGMENT_PATH)

    # Load fuzzy mapping from its dedicated file (written by Phase 3 save_fuzzy_mapping).
    # Convert {project_map: {ts_name: canonical}} → [{timesheet_name, canonical_name, confidence}]
    fuzzy_mapping: list = []
    if os.path.exists(FUZZY_MAP_PATH):
        with open(FUZZY_MAP_PATH) as _f:
            _fm = json.load(_f)
        for ts_name, value in (_fm.get("project_map") or {}).items():
            if isinstance(value, dict):
                fuzzy_mapping.append({
                    "timesheet_name": ts_name,
                    "canonical_name": value.get("canonical_name") or value.get("name") or str(value),
                    "confidence": value.get("confidence", "—"),
                })
            elif isinstance(value, str):
                fuzzy_mapping.append({
                    "timesheet_name": ts_name,
                    "canonical_name": value,
                    "confidence": "—",
                })

    # Merge and re-sequence IDs so numbering is consistent
    all_violations = det_violations + judgment_violations
    for i, v in enumerate(all_violations, start=1):
        v["id"] = f"V{i:03d}"

    # Severity breakdown for auto-generated summary
    counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    type_counts: dict = {}
    for v in all_violations:
        sev = v.get("severity", "LOW")
        counts[sev] = counts.get(sev, 0) + 1
        vtype = v.get("type", "unknown")
        type_counts[vtype] = type_counts.get(vtype, 0) + 1

    # Categorise types into invoicing gap buckets for the executive summary
    OVERBILLING_TYPES = {"over_logging", "over_billing", "over_billing_on_leave_or_holiday"}
    MISSED_REV_TYPES  = {"missing_timesheet", "under_billing", "under_billing_contractor",
                         "approved_extra_time_not_logged"}
    CONTRACT_TYPES    = {"billing_vs_sow_rate", "missing_sow", "sow_resource_overrun",
                         "over_budget", "logging_after_end_date"}

    overbilling_count = sum(n for t, n in type_counts.items() if t in OVERBILLING_TYPES)
    missed_rev_count  = sum(n for t, n in type_counts.items() if t in MISSED_REV_TYPES)
    contract_count    = sum(n for t, n in type_counts.items() if t in CONTRACT_TYPES)

    # Compute quantified overbilling from violation context
    total_overbilling_usd = sum(
        float((v.get("context") or {}).get("financial_impact_usd") or 0.0)
        for v in all_violations
        if v.get("type") in ("over_billing", "over_billing_on_leave_or_holiday")
    )

    top_types = sorted(type_counts.items(), key=lambda x: -x[1])[:3]
    top_str   = ", ".join(f"{t} ({n})" for t, n in top_types)
    overbilling_str = f"${total_overbilling_usd:,.0f}" if total_overbilling_usd else "unquantified"
    summary = (
        f"Revenue intelligence analysis detected {len(all_violations)} invoicing gaps "
        f"({counts['HIGH']} requiring immediate action, {counts['MEDIUM']} for pre-invoice review, "
        f"{counts['LOW']} low-confidence signals). "
        f"Overbilling exposure: {overbilling_count} gaps ({overbilling_str} quantified). "
        f"Missed revenue signals: {missed_rev_count} gaps. "
        f"Contract rate deviations: {contract_count} gaps. "
        f"Top issue types: {top_str}. "
        f"Resolve HIGH gaps before issuing the next client invoice."
    )

    # Lazy import — avoids requiring mcp package at module load time
    try:
        sys.path.insert(0, os.path.join(_ROOT, "mcp_tools"))
        from server import _render_report
    except ImportError as e:
        print(f"[report] Cannot import renderer: {e}")
        print("[report] Ensure mcp package is installed: pip install mcp")
        sys.exit(1)

    html = _render_report(all_violations, fuzzy_mapping, data_range, summary)
    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"[report] Written: {REPORT_PATH}")
    print(f"  {len(all_violations)} total  —  HIGH:{counts['HIGH']}  "
          f"MEDIUM:{counts['MEDIUM']}  LOW:{counts['LOW']}")
    print(f"  {len(det_violations)} deterministic  +  {len(judgment_violations)} judgment")


# ── CLI entry point ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Timesheet validation pipeline — 4-phase orchestrator.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--phase",
        choices=["load", "validate", "report"],
        help="Pipeline phase to run (omit to show status dashboard)",
    )
    args = parser.parse_args()

    if args.phase is None:
        phase_status()
    elif args.phase == "load":
        phase_load()
    elif args.phase == "validate":
        phase_validate()
    elif args.phase == "report":
        phase_report()


if __name__ == "__main__":
    main()
