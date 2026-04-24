# Timesheet Validation Run — 2026-04-22

## Phase 1 — Load Data

```
python3 pipeline.py --phase load
```

**Output:**
```
── Phase 1: Load ─────────────────────────────────────────────────
[load] Written: intermediate/loaded_data.json
  timesheets:          2010 rows
  hr_employees:        57 rows
  slack_activity:      1107 daily summaries
  emails:              73 rows
  calendar_leave:      194 rows
  contractor_invoices: 2 invoices
  sows:                17 documents
  holiday_dates:       2 dates
  rate_mismatches:     25 employees
  [warn] 7 SOWs with parse_status != ok:
    SOW_Wellnest_Home_Health_Notify.docx: partial
    SOW_SummitCapital_Admin_Migration.docx: partial
    SOW_Mobiscape_-_Data_Protection.docx: partial
    SOW_Quotera_CPQ_Platform.docx: partial
    SOW_Parcelly_Shipping_Squad.docx: partial
    SOW_Team_Beach_-_Internal_L&D.docx: partial
    SOW_The_Ledger_-_Ad_Revenue.docx: partial
```

---

## Phase 2 — Deterministic Checks

```
python3 pipeline.py --phase validate
```

**Output:**
```
── Phase 2: Validate (deterministic checks) ──────────────────────
[validate] Written: intermediate/violations_det.json
  555 deterministic violations — HIGH:20  MEDIUM:517  LOW:18
```

**Violation types found:**
| Type | Count |
|------|-------|
| over_logging | 378 |
| over_billing_on_leave_or_holiday | 119 |
| over_billing | 25 |
| client_escalation_low_hours | 16 |
| under_billing | 5 |
| deactivated_employee_logging | 4 |
| over_budget | 3 |
| no_assignment | 2 |
| approved_extra_time_not_logged | 2 |
| unauthorized_assignment_via_email | 1 |

---

## Phase 3 — Judgment Checks

### 3a — Guidelines

- Called `get_guidelines`
- Result: **fully cached** (0 uncached files)
- No extraction needed

**Policy extracted:**
- **Holidays:** 8 fixed + 13 optional (2 optional days allowed, 6-month eligibility)
- **Leave policy:** 24 days annual entitlement, carry-forward max 5, encashment max 20
- **Timesheet policy:** 8 hrs/day daily, weekend rules per policy

---

### 3b — Fuzzy Project Mapping

- Called `load_fuzzy_mapping`
- Mapping found (saved: 2026-04-13T15:32:05Z) — **current, no rebuild needed**

**Project sources:**
| Source | Count |
|--------|-------|
| kimai_timesheets | 18 projects |
| hr_assignments | 17 projects |
| pm_projects | 18 projects |
| sows | 17 projects |

**Project map (SOW name → Kimai name):** 17 full-name mappings + 11 abbreviation aliases (29 entries total).

---

### 3c — SOW Resource Mapping

- Called `get_sow_resources`
- **35 SOW resources** total across 17 SOWs; all pre-mapped in existing `resource_map` (61 keyed entries including multi-SOW overlaps)
- Active usernames checked against hr_employees

**Low-confidence mappings (flagged for Check H):**
| SOW Key | Username | Confidence | Notes |
|---------|----------|------------|-------|
| TG-SOW-2026-013\|Omkar | onkar.date | 0.65 | Omkar vs Onkar spelling variant — below 0.70 threshold → Check H |

Other near-threshold mappings (conf 0.70–0.85 — kept in scope for Check I):
| SOW Key | Username | Confidence |
|---------|----------|------------|
| TG-SOW-2026-009\|Saagar | sagar.yadav | 0.70 |
| TG-SOW-2026-009\|Rahul K. | rahul.dhuri | 0.78 |
| TG-SOW-2026-004\|Abhimanyu S. | abhimanyu.patil | 0.80 |
| TG-SOW-2026-003\|Priyanka M. | priyanka.pakhale | 0.82 |
| TG-SOW-2026-005\|Utkarsh R. | utkarsh.srivastava | 0.85 |

---

### 3d — Partial SOW Extraction

- Called `get_sows(status="partial")` → **7 partial SOWs, all from cache** (no extraction needed)
- Called `get_sows(status="unrecognized_template")` → **0 SOWs**

**Cached partial SOW data (one row per SOW):**
| SOW Ref | Project | Client | Start | End |
|---------|---------|--------|-------|-----|
| TG-SOW-2026-003 | Wellnest Wellnest Notify | Wellnest Notify Inc. | 2026-01-01 | 2026-03-25 |
| TG-SOW-2026-008 | SummitCapital Admin Migration | Summit Capital Partners | 2026-01-01 | 2026-03-22 |
| TG-SOW-2026-005 | Mobiscape - Data Protection | Mobiscape Technologies | 2026-02-01 | 2026-03-22 |
| TG-SOW-2026-014 | Quotera CPQ Platform | Quotera Inc. | 2026-02-01 | 2026-06-30 |
| TG-SOW-2026-017 | Parcelly Shipping Squad | Parcelly Pty Ltd | 2026-01-01 | 2026-03-22 |
| TG-SOW-2026-011 | Team Beach - Internal L&D | Brightwave Labs (Internal) | 2026-01-01 | 2026-03-25 |
| TG-SOW-2026-009 | The Ledger - Ad Revenue | The Ledger Group | 2026-01-01 | 2026-06-30 |

---

### 3e — Judgment Checks

#### Check A — Unauthorized Entry (121 violations)

**Method:** Called `get_unauthorized_candidates` once (121 candidates). For each, called `get_leave_and_activity(employee, first_date)` and classified the combined Slack text + channel list against the unauthorized project and the employee's authorized projects. Matching uses normalized substring comparison (lowercase, punctuation stripped) to avoid false positives from common engineering tokens like "migration" or "admin".

**Classification logic:**
- **CONFIRMING:** Slack text/channel references the unauthorized project → genuine engagement (conf 0.92–0.97)
- **CONTRADICTING:** Slack text/channel references only authorized project(s) → likely data entry error (conf 0.75)
- **NEUTRAL:** No project references, or no Slack activity → unclear intent (conf 0.88–0.90)

**Results:**
| Classification | Count | Action |
|----------------|-------|--------|
| CONFIRMING | 0 | — |
| CONTRADICTING | 119 | Ask employee to correct entry; flag for billing review |
| NEUTRAL | 2 | Investigate with employee; hold billing pending confirmation |

> The strong CONTRADICTING skew reflects that each employee's slack channel (`#bettercroft---crm`, `#summit-capital---main`, etc.) matches their HR-authorized project — so timesheet entries on other projects are overwhelmingly data-entry errors rather than genuine side work.

---

#### Check B — Missing Timesheet (51 violations)

**Method:** Called `get_missing_timesheet_candidates` once. Calibrated confidence from the embedded `slack_texts` content.

**High-signal markers (conf → 0.9):** eod, deployed, pull request, merged, pr, deploy, release, review, fixed the, completed, shipped, commit, refactored, implemented, migration script, hotfix, standup.

**Low-signal markers (conf → 0.5):** only generic presence signals ("back in 30", "brb", "lunch", "afk", "wfh") or no meaningful texts at all.

**Results:**
| Signal Quality | Confidence | Count |
|----------------|------------|-------|
| High (strong markers) | 0.90 | 43 |
| Low (generic / none) | 0.50 | 8 |

---

#### Check H — SOW Resource Unmapped (1 violation)

**Resource:** `Omkar` in TG-SOW-2026-013 (Bettercroft Cashier JDK Migration)
- **Best candidate:** `onkar.date` (mapping confidence 0.65 — below 0.70 threshold)
- **Reason:** "Omkar" vs "Onkar" are different (though phonetically similar) names; only other member in hr_assignments for Bettercroft - Java Upgrade → possible SOW transcription error
- **Signals examined:** email headers, hr_assignments (single candidate), Slack texts
- **Severity:** LOW · **Confidence:** 0.75 (one candidate, thin evidence)

---

#### Check I — Billing Rate vs SOW Rate (28 violations)

**Method:** For each (employee, project) pair with SOW mapping confidence ≥ 0.70, compared Kimai `hourly_rate` to SOW contract rate (rates pulled from `resource_map` → SOW resource entries, including cached extractions for 7 partial SOWs). Flagged where `|kimai − sow| / sow > 5%`.

**Thresholds:**
- `>20%` delta → HIGH severity
- `5–20%` delta → MEDIUM severity
- Final confidence = mapping confidence × 0.95 (max 0.92)

**Results:**
| Severity | Count | Direction |
|----------|-------|-----------|
| HIGH (>20%) | 23 | 23 over-billing |
| MEDIUM (5–20%) | 5 | 4 over, 1 under |

**Top 10 by delta:**
| Employee | Project | Kimai Rate | SOW Rate | Delta | Severity |
|----------|---------|-----------|----------|-------|----------|
| rekha.kumari | Ledger - In Life | $75 | $45 | 66.7% over | HIGH |
| aditya | Mobiscape - Apple Stream | $75 | $45 | 66.7% over | HIGH |
| gaurav | Parcelly - Rate Card | $75 | $45 | 66.7% over | HIGH |
| nupur.banakar | Nimbus Ether | $75 | $45 | 66.7% over | HIGH |
| vivek.agrawal | Nimbus Ether | $70 | $45 | 55.6% over | HIGH |
| shreekant | Nimbus Ether | $70 | $45 | 55.6% over | HIGH |
| vineet.singhal | Nimbus Ether | $80 | $55 | 45.5% over | HIGH |
| gaurav.shewale | Kredora | $75 | $55 | 36.4% over | HIGH |
| santosh.mudavat | Ledger - In Life | $75 | $55 | 36.4% over | HIGH |
| pranav | Bettercroft - CRM | $75 | $55 | 36.4% over | HIGH |

> **Note:** Assumes SOW rates are client billing rates — confirm with commercial lead before acting.

---

### 3f — Write judgment_violations.json

Written: `intermediate/judgment_violations.json`

```json
{
  "violations": [...],       // 201 violations
  "fuzzy_mapping": [...],    // 28 project map entries
  "data_range": {
    "start": "2026-03-02",
    "end":   "2026-03-31"
  }
}
```

**Judgment violation summary:**
| Type | Count |
|------|-------|
| unauthorized_entry | 121 |
| missing_timesheet | 51 |
| billing_vs_sow_rate | 28 |
| sow_resource_unmapped | 1 |
| **Total** | **201** |

| Severity | Count |
|----------|-------|
| HIGH | 144 |
| MEDIUM | 56 |
| LOW | 1 |

---

## Phase 4 — Generate Report

```
python3 pipeline.py --phase report
```

**Output:**
```
── Phase 4: Report ───────────────────────────────────────────────
[report] Written: reports/violations.html
  756 total  —  HIGH:164  MEDIUM:573  LOW:19
  555 deterministic  +  201 judgment
```

---

## Final Summary

| Phase | Violations | HIGH | MEDIUM | LOW |
|-------|-----------|------|--------|-----|
| Phase 2 (deterministic) | 555 | 20 | 517 | 18 |
| Phase 3 (judgment) | 201 | 144 | 56 | 1 |
| **Total** | **756** | **164** | **573** | **19** |

**Report:** `reports/violations.html`
