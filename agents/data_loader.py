#!/usr/bin/env python3
"""
data_loader.py — Phases 1 and 2 implementation.

Called by pipeline.py. Not the primary entrypoint — use pipeline.py instead.

  load      Read CSVs + documents → intermediate/loaded_data.json
  validate  Run deterministic checks  → intermediate/violations_det.json
  all       load → validate (legacy)
"""

import argparse
import json
import os
import sys

# Allow running from any working directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from loaders import load_all

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DATA_DIR   = os.path.join(_ROOT, "data")
INTERMEDIATE_DIR   = os.path.join(_ROOT, "intermediate")
LOADED_DATA_PATH   = os.path.join(INTERMEDIATE_DIR, "loaded_data.json")
VIOLATIONS_PATH    = os.path.join(INTERMEDIATE_DIR, "violations_det.json")


# ══════════════════════════════════════════════════════════════════════════════
# Phase 1 — Load (CSVs + regex-parsed documents → loaded_data.json)
# ══════════════════════════════════════════════════════════════════════════════

def phase_load(data_dir: str):
    data = load_all(data_dir)

    os.makedirs(INTERMEDIATE_DIR, exist_ok=True)
    with open(LOADED_DATA_PATH, "w") as f:
        json.dump(data, f, indent=2, default=str)

    print(f"[load] Written: {LOADED_DATA_PATH}")
    print(f"  timesheets:          {len(data['timesheets'])} rows")
    print(f"  hr_employees:        {len(data['hr_employees'])} rows")
    print(f"  slack_activity:      {len(data['slack_activity'])} daily summaries")
    print(f"  emails:              {len(data['emails'])} rows")
    print(f"  calendar_leave:      {len(data['calendar_leave'])} rows")
    print(f"  contractor_invoices: {len(data['contractor_invoices'])} invoices")
    print(f"  sows:                {len(data['sows'])} documents")
    print(f"  holiday_dates:       {len(data['holiday_dates'])} dates")
    print(f"  rate_mismatches:     {len(data['rate_mismatches'])} employees")

    partial_sows = [s for s in data["sows"] if s.get("parse_status") != "ok"]
    if partial_sows:
        print(f"  [warn] {len(partial_sows)} SOWs with parse_status != ok:")
        for s in partial_sows:
            print(f"    {s['source_file']}: {s.get('parse_status')}")


# ══════════════════════════════════════════════════════════════════════════════
# Phase 3 — Validate (deterministic checks → violations_det.json)
# ══════════════════════════════════════════════════════════════════════════════

def phase_validate():
    from checks import run_all_checks

    if not os.path.exists(LOADED_DATA_PATH):
        print(f"[validate] {LOADED_DATA_PATH} not found — run --phase load first.")
        sys.exit(1)

    with open(LOADED_DATA_PATH) as f:
        data = json.load(f)

    violations = run_all_checks(data)

    os.makedirs(INTERMEDIATE_DIR, exist_ok=True)
    with open(VIOLATIONS_PATH, "w") as f:
        json.dump(violations, f, indent=2)

    high   = sum(1 for v in violations if v.get("severity") == "HIGH")
    medium = sum(1 for v in violations if v.get("severity") == "MEDIUM")
    low    = sum(1 for v in violations if v.get("severity") == "LOW")
    print(f"[validate] Written: {VIOLATIONS_PATH}")
    print(f"  {len(violations)} deterministic violations — HIGH:{high}  MEDIUM:{medium}  LOW:{low}")


# ══════════════════════════════════════════════════════════════════════════════
# CLI entry point
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Timesheet audit data pipeline — phase-based execution.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--phase",
        choices=["load", "validate", "all"],
        default="all",
        help="Pipeline phase to run (default: all)",
    )
    parser.add_argument(
        "--data-dir",
        default=DEFAULT_DATA_DIR,
        help="Path to data directory (default: %(default)s)",
    )
    args = parser.parse_args()

    phase = args.phase
    data_dir = args.data_dir

    if phase in ("load", "all"):
        print("── Phase 1: Load ─────────────────────────────────────")
        phase_load(data_dir)

    if phase in ("validate", "all"):
        print("── Phase 2: Validate ─────────────────────────────────")
        phase_validate()


if __name__ == "__main__":
    main()
