#!/usr/bin/env python3
"""
candidates.py — Phase 3 candidate finders.

These functions scan loaded_data.json and return raw detection facts — not
violation objects. Phase 3 (Claude) wraps each candidate with Slack
classification and emits a complete violation to judgment_violations.json.

Kept out of the Phase 2 CHECKS registry on purpose: unauthorized_entry and
missing_timesheet require LLM reasoning on Slack texts, so their full
violation shape (severity, confidence, evidence, recommendation) is decided
in Phase 3. Writing stubs in Phase 2 would duplicate with Phase 3's enriched
output.

Exposed to Claude via MCP tools:
  - get_unauthorized_candidates        → find_unauthorized_candidates
  - get_missing_timesheet_candidates   → find_missing_timesheet_candidates
"""

from collections import defaultdict
from datetime import date as _date

from checks import _s, _f, _weekdays_in_range


def find_unauthorized_candidates(data: dict) -> list:
    """Return raw (employee, project) candidates lacking an hr_assignment.

    Returns detection facts only — no severity, confidence, evidence, or
    recommendation. Phase 3 (Claude) classifies Slack evidence per candidate
    and emits a single complete ``unauthorized_entry`` violation to
    ``judgment_violations.json``.
    """
    assignments: set = set()
    for r in data.get("hr_assignments", []):
        u = _s(r.get("user")).lower()
        p = _s(r.get("project")).lower()
        if u and p:
            assignments.add((u, p))

    user_projects: dict = defaultdict(set)
    for r in data.get("hr_assignments", []):
        user_projects[_s(r.get("user")).lower()].add(_s(r.get("project")))

    unauth: dict = defaultdict(list)
    for r in data.get("timesheets", []):
        u = _s(r.get("user")).lower()
        p = _s(r.get("project"))
        if not u or not p:
            continue
        if (u, p.lower()) not in assignments:
            unauth[(u, p)].append(r)

    candidates = []
    for (u, p), rows in sorted(unauth.items()):
        rows.sort(key=lambda r: _s(r.get("date")))
        first_date = _s(rows[0].get("date"))
        total_hours = round(sum(_f(r.get("hours")) for r in rows), 2)
        days_logged = sorted({_s(r.get("date")) for r in rows})
        authorized = sorted(user_projects.get(u, set()))
        candidates.append({
            "employee": u,
            "project": p,
            "first_date": first_date,
            "total_hours": total_hours,
            "days_logged": days_logged,
            "authorized_projects": authorized,
        })
    return candidates


def find_missing_timesheet_candidates(data: dict) -> list:
    """Return raw (employee, working-day) candidates with Slack presence but no timesheet.

    Returns detection facts only — no severity, confidence, evidence, or
    recommendation. Phase 3 (Claude) calibrates signal quality from Slack
    texts and emits a single complete ``missing_timesheet`` violation to
    ``judgment_violations.json``.
    """
    timesheets = data.get("timesheets", [])
    raw_dates = [_s(r.get("date")) for r in timesheets
                 if _s(r.get("date")) not in ("", "NaT", "None")]
    if not raw_dates:
        return []
    date_start = _date.fromisoformat(min(raw_dates))
    date_end   = _date.fromisoformat(max(raw_dates))
    holiday_dates = set(data.get("holiday_dates") or [])

    hours_by_user_project: dict = defaultdict(lambda: defaultdict(float))
    for r in timesheets:
        u = _s(r.get("user")).lower()
        p = _s(r.get("project"))
        if p:
            hours_by_user_project[u][p] += _f(r.get("hours"))

    first_assigned: dict = {}
    for a in data.get("hr_assignments", []):
        u = _s(a.get("user")).lower()
        if u not in first_assigned:
            first_assigned[u] = _s(a.get("project"))

    def _primary_project(user: str) -> str:
        proj_hours = hours_by_user_project.get(user, {})
        if proj_hours:
            return max(proj_hours, key=proj_hours.__getitem__)
        return first_assigned.get(user, "")

    employees = {
        _s(e.get("username")).lower()
        for e in data.get("hr_employees", [])
        if _s(e.get("status")).lower() == "active"
    }

    leave_days: set = set()
    for r in data.get("hr_leave", []):
        if _s(r.get("status")).lower() in ("approved", "confirmed", ""):
            leave_days.add((_s(r.get("user")).lower(), _s(r.get("date"))))
    for r in data.get("calendar_leave", []):
        if _s(r.get("status")).lower() in ("confirmed", "approved"):
            leave_days.add((_s(r.get("user")).lower(), _s(r.get("date"))))

    ts_dates: dict = defaultdict(set)
    for r in timesheets:
        ts_dates[_s(r.get("user")).lower()].add(_s(r.get("date")))

    slack_lookup: dict = {}
    for r in data.get("slack_activity", []):
        slack_lookup[(_s(r.get("user")).lower(), _s(r.get("date")))] = r

    candidates = []
    for user in sorted(employees):
        for d in _weekdays_in_range(date_start, date_end, holiday_dates):
            if (user, d) in leave_days:
                continue
            if d in ts_dates.get(user, set()):
                continue
            slack_rec = slack_lookup.get((user, d))
            if not slack_rec:
                continue
            msgs = int(slack_rec.get("messages") or 0)
            if msgs <= 0:
                continue
            texts = slack_rec.get("texts", []) or []
            primary_proj = _primary_project(user)
            candidates.append({
                "employee": user,
                "date": d,
                "primary_project": primary_proj or None,
                "slack_messages": msgs,
                "slack_reactions": int(slack_rec.get("reactions") or 0),
                "slack_texts": texts,
            })
    return candidates
