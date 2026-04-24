# Change Plan: Expose Raw Slack Message Texts to LLM

**Date:** 2026-04-06
**Scope:** Enrich `get_leave_and_activity` MCP tool with raw Slack message texts to enable qualitative reasoning during timesheet violation detection.

---

## Background

The v3 `slack_activity.csv` schema introduced a `text` column containing natural language Slack messages. The current `aggregate_slack_activity()` function in `agents/data_loader.py` discards all text content, reducing each day's activity to two numbers (`messages`, `reactions`). As a result, Claude reasons about Slack presence purely quantitatively and cannot distinguish a deployment announcement from a casual "brb" message.

---

## Current Pipeline

```
slack_activity.csv  (user, date, ts, text, thread_ts, reply_count, reaction_name, reaction_count)
        ↓
aggregate_slack_activity()       ← text is DISCARDED here
        ↓
loaded_data.json stores per (user, date):
  { messages: 6, reactions: 3, first_seen: "06:43:00", last_seen: "12:49:00" }
        ↓
get_leave_and_activity MCP tool returns this summary
        ↓
Claude sees counts only → makes binary presence judgement
```

## Proposed Pipeline

```
slack_activity.csv
        ↓
aggregate_slack_activity()       ← text is PRESERVED alongside counts
        ↓
loaded_data.json stores per (user, date):
  {
    messages: 6, reactions: 3, first_seen: "06:43:00", last_seen: "12:49:00",
    texts: [
      "EOD update: completed code review tasks",
      "PR is up for review: Entain Platform",
      "Deployed Ballys Portal to production"
    ]
  }
        ↓
get_leave_and_activity MCP tool returns full record including texts
        ↓
Claude reads actual messages → reasons qualitatively
```

---

## Signals Available in `text`

| Signal Category | Example Messages |
|----------------|-----------------|
| Project mentions | "PR is up for review: Entain Platform", "Deployed Ballys Portal to production", "Can we sync on the Smarsh Analytics timeline?" |
| EOD / Standup updates | "EOD update: completed code review tasks", "Standup update: working on documentation today" |
| Work activity type | "Merged the feature branch into develop", "Reviewed the PR, left a few comments", "Fixed the CORS issue on the API gateway" |
| Presence / WFH | "Working from home today, reachable on Slack", "Taking a short break, back in 30" |
| Blockers | "Blocked on the API response format, need input", "Running into a weird bug with the date parser" |
| Deployment events | "Deployed Ballys Portal to production", "Finished the migration script, testing locally" |
| Client-facing activity | "Client demo went well, few follow-ups noted", "Sent the invoice to the client" |

---

## Before / After: Check 9 Example

**Before** — `get_leave_and_activity("sushant", "2026-03-09")` returns:
```json
{
  "slack_activity": { "messages": 20, "reactions": 8 },
  "has_timesheet": false
}
```
Claude's reasoning: *"20 messages, 8 reactions, no timesheet → MEDIUM violation, confidence 0.8"*

Resulting violation record:
```json
{
  "type": "slack_without_timesheet",
  "project": null,
  "confidence": 0.8,
  "evidence": ["slack_activity: 20 messages, 8 reactions on 2026-03-09"]
}
```

**After** — same call returns:
```json
{
  "slack_activity": {
    "messages": 20,
    "reactions": 8,
    "texts": [
      "Deployed Ballys Portal to production",
      "PR is up for review: Ballys Portal",
      "EOD update: completed development tasks",
      "Finished the migration script, testing locally",
      "Client demo went well, few follow-ups noted"
    ]
  },
  "has_timesheet": false
}
```
Claude's reasoning: *"sushant deployed to production, raised a PR, and posted an EOD summary — all referencing Ballys Portal — with zero logged hours. HIGH confidence violation; project field can be populated."*

Resulting violation record:
```json
{
  "type": "slack_without_timesheet",
  "project": "Ballys Portal",
  "confidence": 0.95,
  "evidence": [
    "slack: 'Deployed Ballys Portal to production' on 2026-03-09",
    "slack: 'PR is up for review: Ballys Portal' on 2026-03-09",
    "slack: 'EOD update: completed development tasks' on 2026-03-09"
  ]
}
```

---

## Checks Improved

| Check | Current Limitation | What Raw Text Enables |
|-------|-------------------|-----------------------|
| **Check 4** — Missing timesheet | Fires on any `messages > 0`, even a single null-text row | Distinguish EOD updates / PR announcements (high confidence) from casual messages (low confidence) |
| **Check 8** — Fuzzy name mismatch | Three name sources: timesheets, hr_assignments, pm_projects | Slack text becomes a 4th disambiguation source — "user logged 'EP' but said 'Entain Platform' 4× in Slack" = near-conclusive mapping |
| **Check 9** — Slack without timesheet | Arbitrary `messages > 5 or reactions > 2` threshold; `project` field always null | Deployment events / EOD summaries → HIGH confidence; casual messages → LOW; `project` field populated from project mentions in text |
| **Check 3** — Archived project logging | Evidence is timesheet entry alone | "Deployed [archived project]" in Slack raises confidence from ~0.7 to ~0.95 |
| **Check 7** — Logging after end date | Evidence is timesheet entry alone | Slack deployment message with matching timestamp provides independent corroboration |
| **Check 10** — High meeting, low hours | Calendar-only signal | "Blocked on API response format" explains low hours; can downgrade severity from MEDIUM to LOW |

### New Check Enabled

**Check 11 — `project_mention_mismatch`** *(not covered by any existing check)*

Detects cases where an employee discusses Project A all day on Slack but logs hours to Project B in their timesheet. This catches wrong-project logging errors that Check 2 (unauthorized entry) and Check 8 (fuzzy names) do not cover — the employee may be authorized for both projects but simply logged to the wrong one.

---

## Required Code Changes

### 1. `agents/data_loader.py` — `aggregate_slack_activity()`

**What:** Collect non-empty text strings per `(user, date)` alongside the existing count aggregation. Add `texts` list to each output record.

**Where:** Inside the row-iteration loop, after the existing `ts` and `reaction_count` processing:

```python
# Collect non-empty message texts
text = row.get("text")
if text and str(text).strip():
    grouped[key]["texts"].append(str(text).strip())
```

And in the result-building section, add to the output dict:

```python
"texts": agg.get("texts", [])
```

**Impact:** `loaded_data.json` grows by ~5–10KB for the current dataset (each text string averages ~50 chars, ~80% of rows have non-empty text). Negligible.

---

### 2. `mcp_tools/server.py` — `get_leave_and_activity`

**What:** No code change required.

The tool already returns `slack_activity` as a raw dict:
```python
return {
    "slack_activity": slack,   # entire aggregated record returned as-is
    ...
}
```
Since the aggregated record now includes `texts`, it automatically flows through. Zero changes to `server.py`.

---

### 3. `CLAUDE.md` — Check 4, Check 9 detection descriptions

**What:** Update the detection descriptions to instruct Claude to read `slack_activity.texts` when reasoning about presence signals and to use message content for confidence scoring and project attribution.

**Check 4 — add after "Slack activity > 0 messages":**
> If `slack_activity.texts` is available, use message content to calibrate confidence: an EOD update, standup update, PR announcement, or deployment message indicates HIGH confidence the employee was working. Generic presence messages ("brb", "back in 30") should reduce confidence to 0.5 or lower.

**Check 9 — update detection and violation descriptions:**
> When `slack_activity.texts` is available, assess message quality rather than relying on count thresholds alone. Set `confidence` to 0.9–0.95 when texts include an EOD update, PR review, deployment event, or explicit project mention. Set `confidence` to 0.5–0.6 for generic presence messages only. Populate the `project` field if a project name from `pm_projects` appears in one or more message texts.

---

## Trade-offs

| Concern | Assessment |
|---------|-----------|
| **`loaded_data.json` size** | ~5–10KB added for current dataset. Negligible. |
| **MCP tool response size** | A day with 20 messages adds ~1KB to `get_leave_and_activity` response. Fine at current scale; monitor at >1000 rows/day. |
| **Non-determinism** | Claude may interpret the same texts differently across runs. Mitigate by adding explicit criteria to CLAUDE.md (e.g. "EOD update = high confidence work signal") rather than leaving it to open-ended judgement. |
| **Null/empty text rows** | Already handled — rows with empty `text` contribute to `messages` count but not to `texts`. Guard: `if text and str(text).strip()`. |
| **Privacy / compliance** | Raw message text is user-generated content. If `loaded_data.json` is committed to the repo or stored in logs, Slack message content is persisted. Review data retention policy before committing. |

---

## Implementation Order

1. Update `aggregate_slack_activity()` in `agents/data_loader.py` — add `texts` collection and output field
2. Re-run `python3 agents/data_loader.py` to regenerate `intermediate/loaded_data.json`
3. Verify `get_leave_and_activity` returns `texts` via a test call
4. Update Check 4 and Check 9 descriptions in `CLAUDE.md`
5. (Optional) Add Check 11 (`project_mention_mismatch`) definition to `CLAUDE.md` Section 5

No changes to `mcp_tools/server.py` are required at any step.

---

## Files Changed

| File | Change | Required |
|------|--------|----------|
| `agents/data_loader.py` | Add `texts` collection to `aggregate_slack_activity()` | Yes |
| `CLAUDE.md` | Update Check 4, Check 9 detection descriptions | Yes |
| `mcp_tools/server.py` | None | — |
| `intermediate/loaded_data.json` | Regenerated automatically by re-running data_loader | Yes (operational) |
