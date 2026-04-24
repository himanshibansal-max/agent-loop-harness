# Timesheet Validation Agent — Implementation Plan

> **Approach:** Claude Code as the agent. No SDK, no orchestrator, no API calls.
> You provide context + tools + data. Claude decides everything else.

---

## How It Works

```
You say: "Validate timesheets for March 2026"
         ↓
Claude Code reads CLAUDE.md (project context + schema + anomaly rules)
         ↓
Claude decides the plan:
  ├── runs data_loader.py via Bash (loads + normalizes all sources)
  ├── analyzes project names across all sources for fuzzy matches
  ├── calls MCP tools for grounded lookups
  ├── cross-references all 7 sources to find violations
  ├── uses leave + calendar data to resolve ambiguous missing timesheets
  ├── filters false positives itself
  └── writes reports/violations.md
```

No orchestration code. No LLM API calls in your code. Claude IS the LLM.

---

## Data Sources (Real Files)

All files in `/Users/himanshibansal/Downloads/kimai-audit-data-v1.0.0/`

| File | Rows | Key Columns | Role in Validation |
|---|---|---|---|
| `kimai_timesheets.csv` | ~159 | user, date, begin, end, hours, project, activity, description | Primary — what was logged |
| `hr_assignments.csv` | ~9 | user, project | Who is authorized for which project |
| `pm_projects.csv` | ~4 | name, customer, budget_hours, budget_cost, end_date, status | Budget limits + project status |
| `hr_employees.csv` | ~5 | username, role, rate, status, timezone, contract_hrs | Employee contract hours |
| `hr_leave.csv` | ~5 | user, date, type, status | Approved leave — resolves missing timesheets |
| `slack_activity.csv` | ~139 | user, date, messages, reactions, first_seen, last_seen | Activity signals — was user active? |
| `calendar_events.csv` | ~48 | user, date, time, duration_min, title, status | Meeting load — context for low logging days |

> **Note:** `slack_activity.csv` contains activity metrics, not message content.
> It is used as a corroborating signal: if an employee had 0 Slack activity AND 0 timesheet hours on a day, it strengthens a missing timesheet finding.

---

## What You Build

```
agent-loop-harness/
├── CLAUDE.md                  ← project context, anomaly rules, violation schema
├── .claude/
│   └── settings.json          ← registers MCP server
├── mcp_tools/
│   └── server.py              ← 6 custom Python tools exposed via MCP
├── agents/
│   └── data_loader.py         ← pure Python, no LLM, Claude runs via Bash
├── data/                      ← symlink or copy of kimai-audit-data-v1.0.0/
│   ├── kimai_timesheets.csv
│   ├── hr_assignments.csv
│   ├── pm_projects.csv
│   ├── hr_employees.csv
│   ├── hr_leave.csv
│   ├── slack_activity.csv
│   └── calendar_events.csv
├── intermediate/              ← Claude writes runtime JSON here
│   └── loaded_data.json
├── reports/
│   └── violations.md
└── requirements.txt
```

---

## Anomaly Types to Detect

| # | Type | Data Sources Used | Difficulty |
|---|---|---|---|
| 1 | Over-logging (hours > contract_hrs/day) | timesheets + hr_employees | Easy |
| 2 | Unauthorized project entry | timesheets + hr_assignments | Medium |
| 3 | Logging to archived/ended project | timesheets + pm_projects (end_date, status) | Medium |
| 4 | Missing timesheet on working day | timesheets + hr_leave + hr_employees + slack_activity | Medium |
| 5 | Project has timesheets but no assignment | timesheets + hr_assignments | Medium |
| 6 | Over budget limit | timesheets + pm_projects (budget_hours) | Medium |
| 7 | Logging after project end_date | timesheets + pm_projects | Medium |
| 8 | Fuzzy project name mismatch | timesheets + hr_assignments + pm_projects (dynamic analysis) | Hard |
| 9 | Slack activity without timesheet | slack_activity + timesheets | Medium |
| 10 | High meeting load + low hours logged | calendar_events + timesheets | Medium |

---

## Fuzzy Project Name Matching — Dynamic Approach

**No hardcoded dictionary.** Instead, Claude analyzes project names across all sources at the start of validation.

### How It Works

The `get_all_project_names` MCP tool returns every unique project name from every source:

```json
{
  "kimai_timesheets": ["Mobile App", "Website Redesign", "ERP Integration", "Legacy Migration"],
  "hr_assignments":   ["Mobile App", "Website Redesign", "ERP Integration"],
  "pm_projects":      ["Mobile App", "Website Redesign", "ERP Integration", "Legacy Migration"]
}
```

Claude then:
1. Compares names across sources
2. Identifies variants (e.g., `"Mobile Application"` vs `"Mobile App"`)
3. Builds a mapping for the session
4. Applies normalization before every cross-reference check

### Why Dynamic Over Dictionary

- Real data may have variants we haven't seen yet
- The mapping should be derived from evidence, not assumptions
- Claude can reason about semantic equivalence better than string distance alone
- If a name appears in timesheets but not in any other source, Claude flags it as suspicious rather than silently mapping it

---

## MCP Tools (`mcp_tools/server.py`)

Six tools. Pure Python logic. Claude calls these for grounded lookups.

### Tool 1: `get_all_project_names`
```
Input:  {} (no args)
Output: {source_name: [project_names]} for all 3 sources that have projects
Logic:  reads intermediate/loaded_data.json
        returns unique project names per source
Why:    enables Claude to do dynamic fuzzy analysis at session start
```

### Tool 2: `lookup_employee_assignments`
```
Input:  {"employee": "john"}
Output: [{"project": "Mobile App"}, {"project": "Website Redesign"}]
Logic:  reads intermediate/loaded_data.json, filters hr_assignments by user
Why:    targeted lookup — avoids loading full dataset per check
```

### Tool 3: `get_project_details`
```
Input:  {"project": "Legacy Migration"}
Output: {budget_hours, budget_cost, end_date, status, customer}
        or {"missing": true, "project": "Legacy Migration"}
Logic:  reads intermediate/loaded_data.json, looks up pm_projects
Why:    explicit signal for archived projects, budget overruns, missing entries
```

### Tool 4: `aggregate_project_hours`
```
Input:  {"project": "Mobile App"}
Output: {"project": "Mobile App", "total_hours": 124.5, "by_user": {"john": 60.2, ...}}
Logic:  reads timesheets from intermediate/loaded_data.json, sums hours
Why:    budget overrun check requires aggregation across all employees
```

### Tool 5: `get_leave_and_activity`
```
Input:  {"employee": "john", "date": "2026-03-10"}
Output: {
  "leave": {"type": "sick_leave", "status": "approved"},
  "slack_activity": {"messages": 0, "reactions": 0},
  "has_timesheet": false
}
Logic:  joins hr_leave + slack_activity + timesheets for a given employee+date
Why:    resolves missing timesheet ambiguity — leave vs skip vs ghost logging
```

### Tool 6: `get_employee_details`
```
Input:  {"employee": "john"}
Output: {username, role, rate, status, timezone, contract_hrs}
Logic:  reads intermediate/loaded_data.json, looks up hr_employees
Why:    contract_hrs determines daily max — not always 8hrs for all employees
```

---

## `agents/data_loader.py` — What It Does

Pure Python. No LLM. Claude runs it via Bash:

```
python agents/data_loader.py > intermediate/loaded_data.json
```

**Responsibilities:**
1. Load all 7 CSV files
2. Normalize column names (strip whitespace, lowercase)
3. Parse date columns consistently to `YYYY-MM-DD`
4. Pre-compute summary:
   - `hours_by_project` — total hours per project across all employees
   - `hours_by_user_date` — per-employee per-day totals (for over-logging check)
   - `projects_with_no_assignment` — projects in timesheets but absent from hr_assignments
   - `users_with_no_timesheet_dates` — weekdays where assigned employee has 0 hours
5. Output single JSON to stdout

**Output shape:**
```json
{
  "timesheets": [...],
  "assignments": [...],
  "projects": [...],
  "employees": [...],
  "leave": [...],
  "slack_activity": [...],
  "calendar_events": [...],
  "summary": {
    "hours_by_project": {"Mobile App": 124.5},
    "hours_by_user_date": {"john": {"2026-03-02": 8.03}},
    "projects_with_no_assignment": ["Legacy Migration"],
    "date_range": {"start": "2026-03-01", "end": "2026-03-31"}
  }
}
```

> MCP tools read from this file at call time (not import time).
> `data_loader.py` must run before any MCP tool is called.

---

## Data Flow

```
kimai-audit-data-v1.0.0/*.csv
          ↓
  data_loader.py (Bash)
          ↓
  intermediate/loaded_data.json
          ↓
  Claude reads it + calls MCP tools for targeted lookups
          ↓
  Claude analyzes project names across sources (fuzzy check)
          ↓
  Claude runs all 10 anomaly checks
          ↓
  Claude self-reviews findings, checks leave/slack for context
          ↓
  Claude writes reports/violations.md
```

---

## Violation Schema

```json
{
  "id": "V001",
  "type": "over_logging | unauthorized_entry | archived_project | missing_timesheet | no_assignment | over_budget | logging_after_end_date | fuzzy_name_mismatch | slack_without_timesheet | high_meeting_low_hours",
  "severity": "HIGH | MEDIUM | LOW",
  "employee": "john or null",
  "project": "Mobile App or null",
  "date": "2026-03-14 or null",
  "hours": 9.5,
  "confidence": 0.95,
  "evidence": [
    "kimai_timesheets.csv: john logged 9.5hrs on 2026-03-14",
    "hr_employees.csv: john contract_hrs = 8"
  ],
  "context": {
    "leave": null,
    "slack_active": true,
    "calendar_events": 2
  },
  "recommendation": "Review with john — exceeds daily contract hours"
}
```

**Severity rules:**
- `HIGH` — deterministic, clear rule violation, financial impact
- `MEDIUM` — pattern violation with supporting evidence
- `LOW` — ambiguous, needs human confirmation (e.g. missing timesheet where leave data is absent)

---

## Final Report Format (`reports/violations.md`)

```markdown
# Timesheet Validation Report — March 2026

## Summary
| Severity | Count |
|----------|-------|
| HIGH     | N     |
| MEDIUM   | N     |
| LOW      | N     |
| **Total**| **N** |

## Fuzzy Name Analysis
(what Claude found when comparing project names across sources — any variants detected)

## HIGH Severity Violations
### V001 — Unauthorized Entry — john — Legacy Migration
...

## MEDIUM Severity Violations
...

## LOW Severity Violations
...

## Dismissed / Resolved Findings
(findings initially flagged but resolved by leave data, calendar context, or slack activity)
```

---

## `.claude/settings.json`

```json
{
  "mcpServers": {
    "timesheet-tools": {
      "command": "python",
      "args": ["mcp_tools/server.py"]
    }
  }
}
```

---

## Implementation Phases

### Phase 1 — Data Loader
- [ ] `agents/data_loader.py` — loads all 7 CSVs, pre-computes summary
- [ ] Test: `python agents/data_loader.py | python -m json.tool`
- [ ] Create `intermediate/` directory

### Phase 2 — MCP Server
- [ ] `mcp_tools/server.py` — 6 tools implemented via MCP stdio protocol
- [ ] `.claude/settings.json` — registers the server
- [ ] Restart Claude Code — verify tools appear in available tools list

### Phase 3 — CLAUDE.md
- [ ] Write `CLAUDE.md` with all sections:
  - Project overview + data source descriptions
  - Step-by-step validation instructions
  - MCP tools available and when to use each
  - Fuzzy matching instructions (dynamic, not dictionary)
  - All 10 anomaly checks with detection strategy
  - Violation schema
  - Intermediate file paths
  - Report format

### Phase 4 — End-to-End Test
- [ ] Run: `"Validate timesheets for March 2026 and write a report to reports/violations.md"`
- [ ] Check fuzzy name analysis section in report
- [ ] Check leave/slack context on LOW severity findings
- [ ] Tune `CLAUDE.md` if needed

---

## Key Design Decisions

| Decision | Rationale |
|---|---|
| Dynamic fuzzy matching | Real data variants are unknown upfront. Claude analyzes evidence across sources rather than matching a static dict. |
| Leave + slack as context for missing timesheets | `hr_leave.csv` resolves ambiguity. Slack activity (0 messages + 0 hours) strengthens the finding. |
| `get_leave_and_activity` as single tool | Bundles 3 lookups into one call — avoids Claude making 3 separate tool calls per missing-timesheet check. |
| data_loader pre-computes summaries | Rules-based checks (over-logging, budget) don't need LLM reasoning — pre-computed values save tokens. |
| No intermediate files beyond loaded_data.json | With Claude Code as the agent, intermediate state lives in Claude's context window — only one file needed. |
| CLAUDE.md as single source of truth | All behavior controlled through one file. Tune it to tune Claude's behavior. |
