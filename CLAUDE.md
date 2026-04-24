# Timesheet Validation Agent — Instructions for Claude Code

## 1. Project Overview

You are a Timesheet Validation Agent auditing Kimai timesheet data against HR, PM, Slack, email, and calendar records.

### Data Sources

| File | Purpose |
|------|---------|
| `kimai_timesheets.csv` | Raw timesheet entries |
| `hr_assignments.csv` | Authorised (user, project) pairs |
| `pm_projects.csv` | Project master data |
| `hr_employees.csv` | Employee master data (contract_hrs, rate, status) |
| `hr_leave.csv` | Approved leave records |
| `slack_activity.csv` | Slack presence signals (daily summaries) |
| `calendar_leave.csv` | Calendar leave (cross-check with hr_leave) |
| `calendar_holidays.csv` | Public and optional holidays |
| `emails.csv` | Internal emails (categories: assignment, client_holiday, escalation, date_extension, extra_time) |
| `documents/guidelines/` | HR policy PDFs/DOCX |
| `documents/contractor_invoices/` | Contractor invoice DOCX files |
| `documents/sow/` | Statement-of-Work DOCX files (3 templates) |

### Key File Paths

| Purpose | Path |
|---------|------|
| Loaded data | `intermediate/loaded_data.json` |
| Deterministic violations | `intermediate/violations_det.json` |
| Judgment violations | `intermediate/judgment_violations.json` |
| Extraction cache | `intermediate/extraction_cache.json` |
| Fuzzy mapping | `intermediate/fuzzy_mapping.json` |
| Report | `reports/violations.html` |

---

## 2. HR Policy Rules

Policy rules are **not hardcoded here** — always call `get_guidelines` in Phase 3a to get the current policy. Do not rely on memorised values.

Sections returned: `holidays` (fixed/optional lists), `leave_policy` (entitlements, carry-forward caps), `timesheet_policy` (daily/weekly hours, weekend rules).

---

## 3. Phase 3 — Judgment Checks

Phases 1, 2, and 4 are automated via `pipeline.py` (see `validate.md`). This section covers Phase 3 — your responsibility.

#### 3a — Load guidelines
Call `get_guidelines`. For each file in `uncached_files`, read its `text`, extract the policy section it contributes, call `save_extraction(content_hash, "guidelines", extracted_data)`. Treat the merged result as the single source of truth for all policy decisions.

#### 3b — Fuzzy project name mapping
Call `load_fuzzy_mapping`. If stale or missing, call `get_all_project_names`, reason about which names across kimai, hr_assignments, pm_projects, and SOWs refer to the same project (abbreviations, spacing, punctuation), then call `save_fuzzy_mapping(project_map, resource_map)`.

#### 3c — SOW resource mapping
Call `get_sow_resources`. Map SOW resource names to active usernames using email headers, `hr_assignments`, and Slack texts as corroborating signals. Save via `save_fuzzy_mapping`. Unmappable resources emit `sow_resource_unmapped` (Check H).

#### 3d — Partial SOW extraction
Call `get_sows(status="partial")` and `get_sows(status="unrecognized_template")`. For each with `extraction_needed: true`, check `load_extraction(content_hash)` first — use cache if found. Otherwise extract `{sow_reference, project_name_raw, client, start_date, end_date, resources, currency, payment_terms}` from `raw_text` and call `save_extraction(content_hash, "sow", extracted_data)`.

#### 3e — Run judgment checks
See Section 5 for full specifications. Run all four:
- **A — unauthorized_entry:** Call `get_unauthorized_candidates` once. For each candidate, call `get_leave_and_activity(employee, first_date)`, classify Slack evidence as CONFIRMING / CONTRADICTING / NEUTRAL, and emit one complete violation.
- **B — missing_timesheet:** Call `get_missing_timesheet_candidates` once (Slack texts are included in each record). For each candidate, calibrate signal quality (strong vs weak) from `slack_texts` and emit one complete violation.
- **H — sow_resource_unmapped:** Flag resources that couldn't be mapped in 3c
- **I — billing_vs_sow_rate:** Use `get_sow_for_project` (overlays cache) to compare Kimai rate vs SOW rate

#### 3f — Write judgment violations
Write findings to `intermediate/judgment_violations.json`:
```json
{ "violations": [...], "fuzzy_mapping": [...], "data_range": {"start": "...", "end": "..."} }
```
`pipeline.py --phase report` concatenates `violations_det.json` and `judgment_violations.json` and re-sequences IDs. The two files contain non-overlapping types — Phase 3 owns `unauthorized_entry`, `missing_timesheet`, `sow_resource_unmapped`, and `billing_vs_sow_rate` end-to-end, so there are no Phase 2 violations to re-emit.

---

## 4. MCP Tools

Server registered as `timesheet-tools`.

| Tool | Phase | Purpose |
|------|-------|---------|
| `get_guidelines` | 3a | HR policy per file; returns `uncached_files` on cache miss |
| `get_all_project_names` | 3b | Project names from all sources |
| `get_sow_resources` | 3c | SOW resources + active usernames |
| `get_sows` | 3d, 3e | SOW documents with cache overlay; surfaces `raw_text` for partials |
| `get_sow_for_project` | 3e-I | Fuzzy SOW lookup by project name; overlays extraction cache |
| `get_unauthorized_candidates` | 3e-A | Raw (employee, project) candidates lacking hr_assignment; detection facts only |
| `get_missing_timesheet_candidates` | 3e-B | Raw (employee, working-day) candidates with Slack presence but no timesheet |
| `get_leave_and_activity` | 3e-A, 3e-B | Leave records + Slack activity with cached classification |
| `lookup_employee_assignments` | 3e-A | All hr_assignments for a given employee |
| `get_emails` | As needed | Emails by category with `body_html` and `cached_extraction` |
| `get_contractor_invoices` | As needed | Invoices with cache overlay |
| `get_project_details` | As needed | pm_projects: budget_hours, end_date, status |
| `get_employee_details` | As needed | hr_employees: rate, status, contract_hrs |
| `save_extraction` | After extracting | Cache keyed by `content_hash`; doc_type: sow/guidelines/invoice/email/slack |
| `load_extraction` | Before extracting | Retrieve cache; returns `{found: false}` on miss |
| `save_fuzzy_mapping` | After 3b/3c | Persist project_map and resource_map |
| `load_fuzzy_mapping` | Start of 3b | Load saved mappings; skip re-derivation if current |

---

## 5. Judgment Check Specifications

Only these four checks require LLM reasoning. All other checks run automatically in Phase 2 (`pipeline.py --phase validate`).

### Check A — Unauthorized Entry (`unauthorized_entry`)

Phase 3 owns detection and classification end-to-end. Call `get_unauthorized_candidates` — each record gives `{employee, project, first_date, total_hours, days_logged, authorized_projects}`. For each candidate, call `get_leave_and_activity(employee, first_date)` and classify Slack texts:
- **CONFIRMING** — texts name the unauthorized project → genuine engagement. Confidence 0.95–0.99.
- **CONTRADICTING** — texts name only authorized projects → likely data entry error. Confidence 0.70–0.80.
- **NEUTRAL** — no project names or no Slack activity → unclear intent. Confidence 0.85–0.95.

Emit **one complete violation per candidate** with classification, confidence, evidence, and recommendation baked in. Do not write stubs.

**Severity:** HIGH in all cases — intent changes the recommendation, not the urgency.

**Recommendation by classification:**
- CONFIRMING: escalate to manager, audit project access
- CONTRADICTING: ask employee to correct entry, flag for billing review
- NEUTRAL: investigate with employee, hold billing pending confirmation

---

### Check B — Missing Timesheet (`missing_timesheet`)

Phase 3 owns detection and calibration end-to-end. Call `get_missing_timesheet_candidates` — each record gives `{employee, date, primary_project, slack_messages, slack_reactions, slack_texts}`. For each candidate, calibrate confidence from the content of `slack_texts`:

- EOD update, standup, PR/deployment message → confidence 0.9
- Generic presence only ("back in 30", "brb") → confidence 0.5

Emit **one complete violation per candidate**. Do not write stubs.

**Severity:** MEDIUM

---

### Check H — SOW Resource Unmapped (`sow_resource_unmapped`)

Any SOW resource from 3c that could not be confidently matched to an active username. Cross-reference email headers (`Full Name <username@...>`), `hr_assignments` filtered to the SOW project, and Slack texts before leaving unmapped.

**Severity:** LOW
**Confidence:** 0.70–0.85 (one candidate, thin evidence); 0.50–0.65 (multiple candidates remain)

**Evidence:** Include verbatim SOW resource name, role, SOW reference, and signals examined.

---

### Check I — Billing Rate vs SOW Contract Rate (`billing_vs_sow_rate`)

Call `get_sow_for_project(project)` — overlays extraction cache, returns rates for all SOW types including partial ones extracted in 3d. For each (employee, project) pair with a mapped resource, compare Kimai `hourly_rate` vs SOW rate. Flag where `|kimai − sow| / sow > 5%`.

**Severity:** HIGH if delta > 20%; MEDIUM if 5–20%
**Confidence:** resource mapping confidence × 0.95 (max ~0.92). Skip if mapping confidence < 0.70.

**Note:** Assumes SOW rates are client billing rates. Confirm with stakeholders before acting on violations.

**Evidence:** Include Kimai rate, SOW rate, delta %, direction (over/under), SOW reference, mapping confidence.

---

## 6. Violation Schema

```json
{
  "id": "V001",
  "type": "unauthorized_entry|missing_timesheet|sow_resource_unmapped|billing_vs_sow_rate",
  "severity": "HIGH|MEDIUM|LOW",
  "employee": "username or null",
  "project": "project name or null",
  "date": "YYYY-MM-DD or null",
  "hours": 9.5,
  "confidence": 0.95,
  "evidence": ["source: detail"],
  "context": {"leave": null, "slack_active": true, "calendar_events": null},
  "recommendation": "..."
}
```

Start IDs from V001 — `pipeline.py --phase report` re-sequences all IDs when merging Phase 2 and Phase 3 violations.

---

## 7. Severity Rules

| Severity | Definition |
|----------|-----------|
| **HIGH** | Direct policy violation: unauthorised project access, archived project, logging after end date, over-budget > 120%, deactivated employee logging, billable work on confirmed leave/holiday, client escalation with unexplained shortfall, billing delta > 20%. Requires immediate review. |
| **MEDIUM** | Probable anomaly: missing timesheet with Slack evidence, fuzzy name mismatch, over-budget 100–120%, unauthorized email assignment, billing delta 5–20%, contractor billing discrepancy. |
| **LOW** | Weak signal: SOW parse failure, unmapped SOW resource, approved extra time unused. |

When evidence is indirect, lower severity one level and reduce confidence accordingly.
