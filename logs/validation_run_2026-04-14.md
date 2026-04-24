# Timesheet Validation Run — 2026-04-14

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
  727 deterministic violations — HIGH:141  MEDIUM:568  LOW:18
```

**Violation types found:**
| Type | Count |
|------|-------|
| over_logging | 378 |
| unauthorized_entry | 121 |
| over_billing_on_leave_or_holiday | 119 |
| missing_timesheet | 51 |
| client_escalation_low_hours | 16 |
| over_billing | 25 |
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
- Result: **fully cached** (all guideline files already extracted)
- No extraction needed

**Policy extracted:**
- **Holidays:** 8 fixed + 13 optional (2 optional days allowed, 6-month eligibility)
- **Leave policy:** 24 days annual entitlement, carry-forward max 5, max consecutive 15 days
- **Timesheet policy:** 8 hrs/day, 40 hrs/week, weekends require prior approval

---

### 3b — Fuzzy Project Mapping

- Called `load_fuzzy_mapping`
- Mapping found (saved: 2026-04-13T15:32:05Z) — **current, no rebuild needed**
- Called `get_all_project_names` to verify coverage

**Project sources:**
| Source | Count |
|--------|-------|
| kimai_timesheets | 18 projects |
| hr_assignments | 17 projects |
| pm_projects | 18 projects |
| sows | 17 projects |

**Project map (SOW name → Kimai name):**
| SOW Name | Kimai Name |
|----------|------------|
| Canvara Archway K8s Migration | Canvara Archway |
| Kredora Payments & Rewards | Kredora |
| The Ledger - Ad Revenue | Ledger - Ads |
| Ledger InLife Retention | Ledger - In Life |
| Bettercroft CRM Transformation | Bettercroft - CRM |
| Bettercroft Cashier JDK Migration | Bettercroft - Java Upgrade |
| Wellnest Wellnest Notify | Wellnest Notify |
| SummitCapital Portfolio Platform | Summit Capital - Main |
| SummitCapital Admin Migration | Summit Capital - Migration Admin Apps |
| Summit Capital Sprout Data Intelligence | Summit Capital - Sprout Stream |
| Mobiscape - Apple MDM | Mobiscape - Apple Stream |
| Mobiscape - Data Protection | Mobiscape - PII/PHI Detection |
| Quotera CPQ Platform | Quotera |
| Parcelly Rate System | Parcelly - Rate Card |
| Parcelly Shipping Squad | Parcelly - Shipping Squad |
| Team Beach - Internal L&D | Team Beach |
| Nimbus - Ether Platform | Nimbus Ether |

---

### 3c — SOW Resource Mapping

- Called `get_sow_resources`
- **56 active usernames** in system
- All SOW resources already mapped in existing resource_map

**Notable mapping notes:**
| SOW Key | Username | Confidence | Notes |
|---------|----------|------------|-------|
| TG-SOW-2026-013\|Omkar | onkar.date | 0.65 | Omkar vs Onkar spelling variant — below 0.70 threshold → Check H |
| TG-SOW-2026-009\|Saagar | sagar.yadav | 0.70 | Ambiguous; sagar.sahasrabuddhe committed to Bettercroft CRM |
| TG-SOW-2026-009\|Rahul K. | rahul.dhuri | 0.78 | K initial doesn't match Dhuri surname |
| TG-SOW-2026-004\|Abhimanyu S. | abhimanyu.patil | 0.80 | S initial unverified vs Patil |

---

### 3d — Partial SOW Extraction

- Called `get_sows(status="partial")` and `get_sows(status="unrecognized_template")`
- **All 7 partial SOWs: fully cached** — no extraction needed

**Cached partial SOW data:**
| SOW Ref | Project | Client | Start | End | Resources |
|---------|---------|--------|-------|-----|-----------|
| TG-SOW-2026-003 | Wellnest Wellnest Notify | Wellnest Notify Inc. | 2026-01-01 | 2026-03-25 | Mahesh Lal ($100), Pathik ($65), Priyanka M. ($55) |
| TG-SOW-2026-008 | SummitCapital Admin Migration | Summit Capital Partners | 2026-01-01 | 2026-03-22 | Mahesh Lal ($85), Imraan ($55), Prachi ($45), Shubam ($45) |
| TG-SOW-2026-005 | Mobiscape - Data Protection | Mobiscape Technologies | 2026-02-01 | 2026-03-22 | Mahesh Lal ($100), Utkarsh R. ($55) |
| TG-SOW-2026-014 | Quotera CPQ Platform | Quotera Inc. | 2026-02-01 | 2026-06-30 | Mahesh Lal ($85), Mayank D. ($55), Navsheel ($55), Sounak ($55), Chaitrali ($45), Aalay ($45), Pranay ($45) |
| TG-SOW-2026-017 | Parcelly Shipping Squad | Parcelly Pty Ltd | 2026-01-01 | 2026-03-22 | Rishabh A. ($45) |
| TG-SOW-2026-011 | Team Beach - Internal L&D | Brightwave Labs (Internal) | 2026-01-01 | 2026-03-25 | Saroj ($50), Satyam ($35), Atharv ($35), Sakshi ($35), Animesh ($35) |
| TG-SOW-2026-009 | The Ledger - Ad Revenue | The Ledger Group | 2026-01-01 | 2026-06-30 | Mahesh Lal ($100), Ashutosh ($80), Rahul K. ($55), Saagar ($55) |

---

### 3e — Judgment Checks

#### Check A — Unauthorized Entry (121 violations)

**Method:** For each (employee, date) pair with an `unauthorized_entry` flag, fetched Slack activity via `get_leave_and_activity`. Classified Slack texts by checking whether the unauthorized project name or its keywords appeared.

**Classification logic:**
- **CONFIRMING:** Slack texts contain unauthorized project keywords → genuine engagement (conf 0.97)
- **CONTRADICTING:** Slack texts mention only authorized project keywords → likely data entry error (conf 0.75)
- **NEUTRAL:** No project keywords in texts, or no Slack activity → unclear intent (conf 0.90)

**Results:**
| Classification | Count | Action |
|----------------|-------|--------|
| CONFIRMING | 1 | Escalate to manager, audit project access |
| CONTRADICTING | 16 | Ask employee to correct entry, flag for billing review |
| NEUTRAL | 104 | Investigate with employee, hold billing pending confirmation |

**CONFIRMING example:** `abhimanyu.patil` on 2026-03-09 — Slack text: *"Worked over the weekend to hit the Mobiscape - Apple Stream deadline"* while logging to Summit Capital - Migration Admin Apps.

---

#### Check B — Missing Timesheet (51 violations)

**Method:** Extracted Slack texts already embedded in Phase 2 evidence. Classified signal quality using keyword patterns.

**High-signal keywords (conf → 0.9):** eod, deployed, PR, pull request, merged, standup, sprint, completed, finished, shipped, release, pipeline, tested, migration, hotfix

**Low-signal keywords (conf → 0.5):** brb, back in, lunch, afk, wfh

**Results:**
| Signal Quality | Confidence | Count |
|----------------|------------|-------|
| High | 0.90 | 19 |
| Medium | 0.70–0.80 | 29 |
| Low | 0.50 | 3 |

---

#### Check H — SOW Resource Unmapped (1 violation)

**Resource:** `Omkar` in TG-SOW-2026-013 (Bettercroft Cashier JDK Migration)
- **Best candidate:** `onkar.date` (confidence 0.65 — below 0.70 threshold)
- **Reason for low confidence:** "Omkar" vs "Onkar" are different (though phonetically similar) Indian names. Only candidate in hr_assignments for Bettercroft - Java Upgrade.
- **Signals examined:** email headers (no match), hr_assignments (single candidate), Slack (no "Omkar" mention)
- **Severity:** LOW

---

#### Check I — Billing Rate vs SOW Rate (51 violations)

**Method:** For each (employee, project) pair with a SOW resource mapping (confidence ≥ 0.70), compared the Kimai `hourly_rate` to the SOW contract rate. Flagged where `|kimai − sow| / sow > 5%`.

**Thresholds:**
- `>20%` delta → HIGH severity
- `5–20%` delta → MEDIUM severity
- Mapping confidence × 0.95 = final confidence (max 0.92)

**Top violations:**

| Employee | Project | Kimai Rate | SOW Rate | Delta | Direction | Severity |
|----------|---------|-----------|----------|-------|-----------|----------|
| vivek.agrawal | Nimbus Ether | $79.50/hr | $45/hr | 76.7% | over | HIGH |
| nupur.banakar | Nimbus Ether | $75/hr | $45/hr | 66.7% | over | HIGH |
| vineet.singhal | Nimbus Ether | $80/hr | $55/hr | 45.5% | over | HIGH |
| pranav | Bettercroft - CRM | $75/hr | $55/hr | 36.4% | over | HIGH |
| shreyas.barhanpurkar | Nimbus Ether | $75/hr | $65/hr | 15.4% | over | MEDIUM |

**Full count:** 51 violations across multiple projects

---

### 3f — Write judgment_violations.json

Written: `intermediate/judgment_violations.json`

```json
{
  "violations": [...],       // 224 violations
  "fuzzy_mapping": [...],    // 29 project map entries
  "data_range": {
    "start": "2026-03-09",
    "end":   "2026-03-27"
  }
}
```

**Judgment violation summary:**
| Type | Count |
|------|-------|
| unauthorized_entry | 121 |
| missing_timesheet | 51 |
| billing_vs_sow_rate | 51 |
| sow_resource_unmapped | 1 |
| **Total** | **224** |

| Severity | Count |
|----------|-------|
| HIGH | 165 |
| MEDIUM | 58 |
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
  951 total  —  HIGH:306  MEDIUM:626  LOW:19
  727 deterministic  +  224 judgment
```

---

## Final Summary

| Phase | Violations | HIGH | MEDIUM | LOW |
|-------|-----------|------|--------|-----|
| Phase 2 (deterministic) | 727 | 141 | 568 | 18 |
| Phase 3 (judgment) | 224 | 165 | 58 | 1 |
| **Total** | **951** | **306** | **626** | **19** |

**Report:** `reports/violations.html`
