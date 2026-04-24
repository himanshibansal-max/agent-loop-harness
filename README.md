# Timesheet Validation Agent

> **Note on data:** All data in this repository — employees, clients, projects, emails, Slack messages, timesheets, SOWs, and invoices — is synthetic. Any resemblance to real companies or individuals is coincidental.

An AI-powered audit agent that cross-references employee timesheet data from Kimai against HR records, project management data, Slack activity, emails, and calendar events to detect anomalies and produce a structured HTML violations report.

The agent is Claude Code itself — no orchestration framework, no separate LLM API calls. Claude reads `CLAUDE.md` for its instructions, runs a phase-based data loader to normalise inputs, calls MCP tools for lookups and caching, and writes the final report.

---

## Architecture

The pipeline has four phases. Phases 1, 2, and 4 are Python scripts; Phase 3 is Claude Code acting as the judgment agent.

```
PHASE 1 — LOAD  (pipeline.py --phase load)
  agents/loaders.py reads all CSVs and extracts raw text from DOCX/PDF
  Writes intermediate/loaded_data.json

PHASE 2 — VALIDATE  (pipeline.py --phase validate)
  agents/checks.py runs 13 deterministic rule-based checks
  Writes intermediate/violations_det.json

PHASE 3 — JUDGMENT  (Claude Code, via /validate skill)
  Claude calls MCP tools to read loaded_data.json and the extraction cache
  3a  get_guidelines  → extract HR policy from uncached guideline files
  3b  load/build fuzzy project-name mapping
  3c  get_sow_resources → map SOW resource names to employee usernames
  3d  get_sows(partial) → extract and cache unrecognised SOW fields
  3e  Run judgment checks A, B, H, I (Slack classification, rate comparison)
  3f  Writes intermediate/judgment_violations.json

PHASE 4 — REPORT  (pipeline.py --phase report)
  Merges violations_det.json + judgment_violations.json
  Re-sequences violation IDs, builds executive summary
  Writes reports/violations.html
```

### Extraction cache

Unstructured documents (SOWs, HR guidelines, contractor invoices, email bodies, Slack message texts) are parsed by Claude Code during Phase 3. Results are cached in `intermediate/extraction_cache.json` keyed by SHA-256 of the source content. On subsequent runs, unchanged files return cached extractions instantly — Claude only re-extracts files that have changed.

### Fuzzy mapping persistence

Project-name aliases (SOW names ↔ Kimai names) and SOW resource→username mappings are saved to `intermediate/fuzzy_mapping.json` after Phase 3b/3c. Subsequent runs reload the saved mapping rather than re-deriving it from scratch.

---

## Project Structure

```
agent-loop-harness/
├── CLAUDE.md                        # Agent instructions: phases, checks, violation schema
├── pipeline.py                      # Top-level orchestrator (phases 1, 2, 4)
├── requirements.txt                 # Python dependencies
├── .claude/
│   ├── settings.json                # Registers MCP server as "timesheet-tools"
│   └── commands/
│       └── validate.md              # /validate skill: triggers the full 4-phase pipeline
├── agents/
│   ├── data_loader.py               # Phase 1 + 2 entry points called by pipeline.py
│   ├── loaders.py                   # CSV + document loading (raw_text preserved for docs)
│   ├── checks.py                    # 13 deterministic violation checks
│   └── extractor.py                 # ContentHashCache utility (no API calls)
├── mcp_tools/
│   └── server.py                    # 20 MCP tools exposed via stdio protocol
├── data/
│   ├── kimai_timesheets.csv
│   ├── hr_assignments.csv
│   ├── pm_projects.csv
│   ├── hr_employees.csv
│   ├── hr_leave.csv
│   ├── slack_activity.csv
│   ├── calendar_leave.csv
│   ├── calendar_holidays.csv
│   ├── emails.csv
│   └── documents/
│       ├── guidelines/              # HR policy PDFs + DOCX files
│       ├── contractor_invoices/     # Contractor invoice DOCX files
│       └── sow/                     # Statement-of-Work DOCX files (3 templates)
├── intermediate/
│   ├── loaded_data.json             # Phase 1 output
│   ├── violations_det.json          # Phase 2 output (deterministic violations)
│   ├── judgment_violations.json     # Phase 3 output (Claude's judgment violations)
│   ├── extraction_cache.json        # Per-file extraction cache (SHA-256 keyed)
│   └── fuzzy_mapping.json           # Persisted project-name + SOW resource mappings
└── reports/
    └── violations.html              # Phase 4 output — final audit report
```

---

## Data Sources

### CSV files (`data/`)

| File | Key Columns | Purpose |
|------|-------------|---------|
| `kimai_timesheets.csv` | user, date, begin, end, hours, project, activity, description, hourly_rate, submitted_at | Raw timesheet entries — primary audit target |
| `hr_assignments.csv` | user, project | Which employees are authorised for which projects |
| `pm_projects.csv` | name, customer, budget_hours, budget_cost, end_date, status | Project master data |
| `hr_employees.csv` | username, role, rate, status, timezone, contract_hrs | Employee contract hours, billing rates, active status |
| `hr_leave.csv` | user, date, type, status | Approved leave records |
| `slack_activity.csv` | user, date, channel, ts, text, … | Raw Slack messages — aggregated to daily summaries |
| `calendar_leave.csv` | user, date, title, leave_type, all_day, status | Calendar leave (cross-checked with hr_leave) |
| `calendar_holidays.csv` | date, name, type | Public and optional holiday list |
| `emails.csv` | from_email, to_email, date, subject, body_html, category | Internal emails (assignment, escalation, extra_time, …) |

### Document files (`data/documents/`)

| Directory | Format | Extracted fields |
|-----------|--------|-----------------|
| `guidelines/` | PDF + DOCX | holidays (fixed/optional), leave policy, timesheet policy |
| `contractor_invoices/` | DOCX | invoice_id, contractor_name, period, hours_billed, rate, total |
| `sow/` | DOCX (3 templates) | sow_reference, client, start/end dates, resources with rates |

Documents are loaded as `raw_text` by the Python pipeline. Claude Code extracts structured fields during validation and caches results per file.

---

## Setup

### Prerequisites

- Python 3.10+
- Claude Code CLI (`claude` command available)

### Install dependencies

```bash
pip install -r requirements.txt
```

Installs: `pandas`, `openpyxl`, `python-docx`, `pdfminer.six`, `mcp`, `anthropic`

> `anthropic` is listed as a dependency for future direct-SDK use but is not required for the current Claude Code–based extraction flow.

### Verify MCP server registration

The MCP server is pre-configured in `.claude/settings.json`:

```json
{
  "mcpServers": {
    "timesheet-tools": {
      "command": "python3",
      "args": ["mcp_tools/server.py"]
    }
  }
}
```

Claude Code picks this up automatically when run from the project root.

---

## Running the Agent

Open Claude Code from the project root and use the `/validate` skill:

```bash
cd /path/to/agent-loop-harness
claude
```

```
/validate
```

Claude executes the full 4-phase pipeline:

1. **Phase 1** — `python3 pipeline.py --phase load` — reads all data sources into `loaded_data.json`
2. **Phase 2** — `python3 pipeline.py --phase validate` — runs 13 deterministic checks, writes `violations_det.json`
3. **Phase 3** (Claude) — calls MCP tools to extract guidelines, build fuzzy mappings, extract partial SOWs, run judgment checks A/B/H/I, writes `judgment_violations.json`
4. **Phase 4** — `python3 pipeline.py --phase report` — merges both violation files and renders `reports/violations.html`

### Pipeline status dashboard

```bash
python3 pipeline.py          # shows which phases are complete and what to run next
```

### Running phases manually

```bash
python3 pipeline.py --phase load      # Phase 1: read CSVs + docs → loaded_data.json
python3 pipeline.py --phase validate  # Phase 2: deterministic checks → violations_det.json
# Phase 3: run /validate in Claude Code (or ask Claude to run judgment checks)
python3 pipeline.py --phase report    # Phase 4: merge + render → reports/violations.html
```

---

## Anomaly Checks

### Deterministic checks — Phase 2 (`pipeline.py --phase validate` → `agents/checks.py`)

| Type | Severity | Description |
|------|----------|-------------|
| `over_logging` | HIGH / MEDIUM | Daily hours exceed contracted hours |
| `no_assignment` | MEDIUM | Time logged to a project with no HR assignments |
| `over_budget` | HIGH / MEDIUM | Project hours exceed budget (HIGH > 120%, MEDIUM 100–120%) |
| `deactivated_employee_logging` | HIGH | Inactive employee has timesheet entries |
| `under_billing` | MEDIUM | Employee hours significantly below contracted amount |
| `over_billing` | MEDIUM / LOW | Kimai hourly rate differs from HR contracted rate |
| `over_billing_on_leave_or_holiday` | HIGH / MEDIUM | Billable hours logged on confirmed leave or public holiday |
| `client_escalation_low_hours` | HIGH | Client escalation email matches a week with unexplained hours shortfall |
| `unauthorized_entry` | HIGH | Time logged to a project not in hr_assignments (flagged; enriched in Phase 3) |
| `missing_timesheet` | MEDIUM | Working day with Slack activity but no timesheet entry (flagged; enriched in Phase 3) |
| `unauthorized_assignment_via_email` | MEDIUM | Assignment email references a (user, project) pair absent from hr_assignments |
| `approved_extra_time_not_logged` | LOW | Extra-time approval email with no matching timesheet hours |
| `under_billing_contractor` | MEDIUM | Contractor hours below invoice amount |

### Judgment checks — Phase 3 (Claude Code)

Claude enriches Phase 2 flags and runs two additional checks using MCP tools and LLM reasoning.

| Check | Type | Severity | Description |
|-------|------|----------|-------------|
| A | `unauthorized_entry` | HIGH | Enriches Phase 2 flags with Slack classification: CONFIRMING / CONTRADICTING / NEUTRAL |
| B | `missing_timesheet` | MEDIUM | Calibrates confidence from Slack signal strength: STRONG (0.9) → WEAK (0.5) |
| H | `sow_resource_unmapped` | LOW | SOW resource name that could not be confidently matched to an active username |
| I | `billing_vs_sow_rate` | HIGH / MEDIUM | Kimai hourly rate differs from SOW contract rate by > 5% |

---

## MCP Tools

All 20 tools are registered as `timesheet-tools` in `.claude/settings.json` and served via `mcp_tools/server.py` over stdio. All tools read from `intermediate/loaded_data.json`; document tools also overlay `intermediate/extraction_cache.json`.

### Data access tools

| Tool | Key inputs | Purpose |
|------|-----------|---------|
| `get_all_project_names` | — | Project names from all four sources (kimai, hr_assignments, pm_projects, SOWs) for fuzzy mapping |
| `get_sows` | `client_filter`, `status` | SOW documents; overlays cached extractions; surfaces `raw_text` + `content_hash` for partial/unrecognised SOWs |
| `get_sow_for_project` | `project` | Fuzzy SOW lookup by project name; overlays extraction cache |
| `get_sow_resources` | — | All SOW resources + list of active usernames for resource→username mapping |
| `get_guidelines` | `section` | HR policy per guideline file; per-file cache check; returns `uncached_files` on miss |
| `get_emails` | `category` | Emails with `body_html`, `content_hash`, and `cached_extraction` overlay |
| `get_contractor_invoices` | `contractor` | Invoices with `raw_text`, `content_hash`, and cached extraction overlay |
| `get_leave_and_activity` | `employee`, `date` | HR leave + calendar leave + Slack activity for a given employee-date |
| `lookup_employee_assignments` | `employee` | All projects an employee is authorised for |
| `get_project_details` | `project` | Budget hours, end date, status from pm_projects |
| `aggregate_project_hours` | `project` | Total logged hours per project, broken down by employee |
| `get_employee_details` | `employee` | Role, rate, status, contract hours from hr_employees |
| `get_rate_mismatches` | `employee` | Employees where Kimai hourly_rate ≠ HR contracted rate, with financial impact |
| `check_employee_billing_hours` | `employee`, `period_start`, `period_end` | Expected vs actual hours, leave days, Slack-active days without timesheet |
| `run_deterministic_checks` | — | Runs all deterministic checks; returns violations list (alternative to `pipeline.py --phase validate`) |

### Extraction cache tools

| Tool | Key inputs | Purpose |
|------|-----------|---------|
| `save_extraction` | `content_hash`, `doc_type`, `data` | Cache Claude's structured extraction keyed by SHA-256 of source content |
| `load_extraction` | `content_hash` | Retrieve a cached extraction; returns `{found: false}` on miss |

`doc_type` values: `sow`, `guidelines`, `invoice`, `email`, `slack`

### Mapping persistence tools

| Tool | Key inputs | Purpose |
|------|-----------|---------|
| `save_fuzzy_mapping` | `project_map`, `resource_map` | Persist project-name aliases and SOW resource→username mappings to `fuzzy_mapping.json` |
| `load_fuzzy_mapping` | — | Load saved mappings; returns empty maps + `found: false` if not yet created |

### Report tool

| Tool | Key inputs | Purpose |
|------|-----------|---------|
| `generate_report` | `violations`, `fuzzy_mapping`, `data_range`, `summary` | Renders `reports/violations.html` (also called internally by `pipeline.py --phase report`) |

---

## Output Report

Written to `reports/violations.html` — self-contained HTML, no external dependencies.

- **Header** — generation date, data range, violation counts by severity
- **Summary** — 2–4 sentence narrative of the most significant findings
- **Violations** — one card per violation, sorted HIGH → MEDIUM → LOW, then by date
- **Appendix** — fuzzy name mapping used in the run

Severity colour coding: red (HIGH), orange (MEDIUM), blue (LOW).

---

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| `pipeline.py` as the orchestrator | Single entry point for all automated phases; shows a status dashboard when run without arguments, making it easy to see what has and hasn't run |
| Claude Code as the judgment agent | No orchestration framework or separate LLM API calls — Claude drives Phase 3 via Bash and MCP tool calls, with `CLAUDE.md` as its instruction set |
| Four-phase split | Phases 1/2/4 are fast and deterministic (re-run freely); Phase 3 is slow and stateful. The split means reruns after a data change only need `--phase load`, not a full re-run |
| Two violation files | `violations_det.json` (Phase 2) and `judgment_violations.json` (Phase 3) are kept separate so either can be regenerated independently. Phase 4 merges them and re-sequences IDs |
| Per-file extraction cache | Unstructured documents are expensive to re-parse; SHA-256 keying means only changed files are re-extracted — unchanged files return from cache in milliseconds |
| Per-file cache granularity | Each document gets its own cache key. Updating one SOW or one guideline file does not invalidate other files' cached extractions |
| No direct API calls | Extraction is done by Claude Code itself (the running agent), not a separate Anthropic SDK call. No API key is required in application code |
| Persisted fuzzy mapping | Project-name and SOW resource→username mappings are saved to `fuzzy_mapping.json` after Phase 3b/3c and reloaded on subsequent runs, avoiding redundant LLM reasoning |
| `get_leave_and_activity` bundles lookups | Avoids multiple round-trip tool calls per employee-date during missing-timesheet and unauthorised-entry enrichment |
