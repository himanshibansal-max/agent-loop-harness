Validate the timesheets and produce reports/violations.html by running the 4-phase pipeline below. Complete every phase in order.

## Phase 1 — Load data
```
python3 pipeline.py --phase load
```

## Phase 2 — Deterministic checks
```
python3 pipeline.py --phase validate
```

## Phase 3 — Judgment checks (your responsibility)

Follow CLAUDE.md §3 Phase 3. In summary:

1. **3a** — Call `get_guidelines`. Extract and cache any uncached guideline files.
2. **3b** — Call `load_fuzzy_mapping`. If missing or stale, rebuild via `get_all_project_names` and `save_fuzzy_mapping`.
3. **3c** — Call `get_sow_resources`. Map SOW resource names to usernames. Save via `save_fuzzy_mapping`.
4. **3d** — Call `get_sows(status="partial")` and `get_sows(status="unrecognized_template")`. Extract and cache any uncached SOW fields.
5. **3e** — Run judgment checks:
   - Call `get_unauthorized_candidates` **once** to fetch all candidates. Then, for each candidate, call `get_leave_and_activity(employee, first_date)`, classify the Slack texts as CONFIRMING / CONTRADICTING / NEUTRAL, and emit one complete `unauthorized_entry` violation.
   - Call `get_missing_timesheet_candidates` **once** to fetch all candidates (Slack texts are included in each record — no extra tool call needed). For each candidate, calibrate confidence from `slack_texts` and emit one complete `missing_timesheet` violation.
   - Run `billing_vs_sow_rate` using `get_sow_for_project` (overlays cache for all SOW types)
6. **3f** — Write all judgment violations to `intermediate/judgment_violations.json`:
   ```json
   { "violations": [...], "fuzzy_mapping": [...], "data_range": {"start": "...", "end": "..."} }
   ```

## Phase 4 — Generate report
```
python3 pipeline.py --phase report
```
