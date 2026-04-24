#!/usr/bin/env python3
"""
checks.py — all deterministic timesheet validation checks.

Each check is a function:  check_<name>(data: dict) -> list[dict]

The returned dicts follow the violation schema from CLAUDE.md.
Every check here is fully deterministic — pure logic, math, or structured
field lookups.  Three categories of LLM work are intentionally left to Claude:

  Extraction-dependent  — rate/field extraction from partial SOW raw_text,
                          email body parsing where regex is insufficient.
  Genuine LLM judgment  — Slack text classification, fuzzy project name mapping
                          across sources, ambiguous SOW resource → username
                          resolution, HR policy extraction from PDFs.

Public API
----------
  run_all_checks(data)  -> list[dict]   # run every check, return all violations
  CHECKS                                # ordered list of (name, fn) for selective runs
"""

import re as _re
from collections import defaultdict
from datetime import date as _date, timedelta as _td
from html import unescape as _unescape

# ── helpers ───────────────────────────────────────────────────────────────────

def _s(val) -> str:
    """Safe string coercion — returns '' for None/NaN/float-NaN."""
    if val is None:
        return ""
    if isinstance(val, float):
        import math
        return "" if math.isnan(val) else str(val)
    return str(val).strip()


def _f(val, default=0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _weekdays_in_range(start: _date, end: _date, holiday_dates: set) -> list:
    """Return list of ISO date strings that are weekdays and not public holidays."""
    result = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5 and cur.isoformat() not in holiday_dates:
            result.append(cur.isoformat())
        cur += _td(days=1)
    return result


def _strip_html(html: str) -> str:
    """Strip HTML tags and unescape entities."""
    return _unescape(_re.sub(r"<[^>]+>", " ", html or "")).strip()


def _week_bounds(d: _date) -> tuple:
    """Return (monday, sunday) for the week containing d."""
    monday = d - _td(days=d.weekday())
    return monday, monday + _td(days=6)


def _violation(vid, vtype, severity, employee=None, project=None,
                date=None, hours=None, confidence=0.95,
                evidence=None, context=None, recommendation=""):
    return {
        "id": vid,
        "type": vtype,
        "severity": severity,
        "employee": employee,
        "project": project,
        "date": date,
        "hours": hours,
        "confidence": confidence,
        "evidence": evidence or [],
        "context": context or {},
        "recommendation": recommendation,
    }


_id_counter = [0]


def _next_id() -> str:
    _id_counter[0] += 1
    return f"V{_id_counter[0]:03d}"


def _reset_ids():
    _id_counter[0] = 0


# ══════════════════════════════════════════════════════════════════════════════
# Check 1 — Over-Logging
# ══════════════════════════════════════════════════════════════════════════════

def check_over_logging(data: dict) -> list:
    """Flag days where an employee's total logged hours exceed their contract_hrs.

    Leave days and public holidays are intentionally excluded — those are
    handled by ``check_over_billing_on_leave_or_holiday``, which captures the
    "work logged on a day off" concern with the correct framing (financial).
    Double-flagging the same day as both "over-logged" and "billed on leave"
    produces noise without adding signal.
    """
    contract = {
        _s(e.get("username")).lower(): _f(e.get("contract_hrs"), 8.0)
        for e in data.get("hr_employees", [])
    }

    # Build leave + holiday exclusion set
    holiday_dates = set(data.get("holiday_dates") or [])
    leave_days: set = set()
    for r in data.get("hr_leave", []):
        if _s(r.get("status")).lower() in ("approved", "confirmed", ""):
            leave_days.add((_s(r.get("user")).lower(), _s(r.get("date"))))
    for r in data.get("calendar_leave", []):
        if _s(r.get("status")).lower() in ("confirmed", "approved"):
            leave_days.add((_s(r.get("user")).lower(), _s(r.get("date"))))

    # aggregate hours per (user, date)
    daily: dict = defaultdict(float)
    for r in data.get("timesheets", []):
        key = (_s(r.get("user")).lower(), _s(r.get("date")))
        daily[key] += _f(r.get("hours"))

    violations = []
    for (user, date), total in daily.items():
        # Skip leave days and public holidays — covered by
        # check_over_billing_on_leave_or_holiday
        if date in holiday_dates:
            continue
        if (user, date) in leave_days:
            continue

        limit = contract.get(user, 8.0)
        if total <= limit:
            continue
        over = total - limit
        severity = "HIGH" if over > 2 else "MEDIUM"
        violations.append(_violation(
            _next_id(), "over_logging", severity,
            employee=user, date=date, hours=total,
            confidence=0.95,
            evidence=[
                f"kimai_timesheets: {user} logged {total}h on {date}",
                f"hr_employees: contract_hrs={limit}",
            ],
            context={"contract_hrs": limit, "total_logged": total, "over_by": round(over, 2)},
            recommendation=(
                f"Review {user}'s entries on {date} — logged {total}h against "
                f"{limit}h contract. Ask employee to correct or explain."
            ),
        ))
    return violations


# ══════════════════════════════════════════════════════════════════════════════
# Check 3 — Archived Project Logging
# ══════════════════════════════════════════════════════════════════════════════

def check_archived_project(data: dict) -> list:
    """Flag timesheet entries logged against archived projects."""
    project_status = {
        _s(p.get("name")).lower(): _s(p.get("status")).lower()
        for p in data.get("pm_projects", [])
    }
    violations = []
    for r in data.get("timesheets", []):
        proj = _s(r.get("project"))
        if project_status.get(proj.lower()) == "archived":
            violations.append(_violation(
                _next_id(), "archived_project", "HIGH",
                employee=_s(r.get("user")),
                project=proj,
                date=_s(r.get("date")),
                hours=_f(r.get("hours")),
                confidence=0.99,
                evidence=[
                    f"kimai_timesheets: {r.get('user')} logged {r.get('hours')}h on {r.get('date')}",
                    f"pm_projects: status=archived for project '{proj}'",
                ],
                context={"project_status": "archived"},
                recommendation=f"Remove or reassign entries for archived project '{proj}'. Notify PM.",
            ))
    return violations


# ══════════════════════════════════════════════════════════════════════════════
# Check 5 — No Assignment (project-level)
# ══════════════════════════════════════════════════════════════════════════════

def check_no_assignment(data: dict) -> list:
    """Flag projects that have timesheet entries but zero assignments in hr_assignments."""
    assigned_projects = {_s(r.get("project")).lower() for r in data.get("hr_assignments", [])}

    loggers_by_project: dict = defaultdict(set)
    for r in data.get("timesheets", []):
        proj = _s(r.get("project"))
        if proj.lower() not in assigned_projects:
            loggers_by_project[proj].add(_s(r.get("user")))

    violations = []
    for proj, employees in loggers_by_project.items():
        violations.append(_violation(
            _next_id(), "no_assignment", "MEDIUM",
            project=proj,
            confidence=0.9,
            evidence=[
                f"kimai_timesheets: project '{proj}' has timesheet entries",
                f"hr_assignments: no assignments found for '{proj}'",
                f"Employees who logged: {', '.join(sorted(employees))}",
            ],
            context={"employees_logged": sorted(employees)},
            recommendation=f"Project '{proj}' has no HR assignments. Create assignments or investigate whether this is a name mismatch.",
        ))
    return violations


# ══════════════════════════════════════════════════════════════════════════════
# Check 6 — Over Budget
# ══════════════════════════════════════════════════════════════════════════════

def check_over_budget(data: dict) -> list:
    """Flag projects where total logged hours exceed pm_projects.budget_hours."""
    budget = {
        _s(p.get("name")).lower(): (_s(p.get("name")), _f(p.get("budget_hours")))
        for p in data.get("pm_projects", [])
        if p.get("budget_hours") is not None
    }

    hours_by_project = data.get("summary", {}).get("hours_by_project") or {}
    # rebuild canonical-name lookup
    logged_by_lower = {}
    for proj, hrs in hours_by_project.items():
        logged_by_lower[proj.lower()] = (proj, hrs)

    violations = []
    for lower_key, (canonical_name, budget_hrs) in budget.items():
        if budget_hrs <= 0:
            continue
        proj, logged = logged_by_lower.get(lower_key, (canonical_name, 0.0))
        if logged <= budget_hrs:
            continue
        pct = round((logged - budget_hrs) / budget_hrs * 100, 1)
        severity = "HIGH" if pct > 20 else "MEDIUM"
        violations.append(_violation(
            _next_id(), "over_budget", severity,
            project=canonical_name,
            hours=logged,
            confidence=0.95,
            evidence=[
                f"kimai_timesheets: {logged}h logged to '{canonical_name}'",
                f"pm_projects: budget_hours={budget_hrs}",
                f"Overage: {pct}% over budget",
            ],
            context={"logged_hours": logged, "budget_hours": budget_hrs, "overage_pct": pct},
            recommendation=f"'{canonical_name}' is {pct}% over budget ({logged}h vs {budget_hrs}h). Escalate to PM for budget review.",
        ))
    return violations


# ══════════════════════════════════════════════════════════════════════════════
# Check 7 — Logging After Project End Date
# ══════════════════════════════════════════════════════════════════════════════

def check_logging_after_end_date(data: dict) -> list:
    """Flag timesheet entries dated after the project's end_date in pm_projects."""
    end_dates = {
        _s(p.get("name")).lower(): _s(p.get("end_date"))
        for p in data.get("pm_projects", [])
        if p.get("end_date") and _s(p.get("end_date")) not in ("", "NaT", "None")
    }

    # Build date_extension email overrides: project_lower -> new_end_date
    date_extensions: dict = {}
    for email in data.get("emails", []):
        if _s(email.get("category")).lower() != "date_extension":
            continue
        subj = _s(email.get("subject"))
        m = __import__("re").search(
            r"Date Extension[:\s]+(.+?)\s*[-–]\s*revised end date\s+(\d{4}-\d{2}-\d{2})",
            subj, __import__("re").IGNORECASE,
        )
        if m:
            proj_name = m.group(1).strip().lower()
            new_end = m.group(2).strip()
            # Keep the latest extension if multiple
            if proj_name not in date_extensions or new_end > date_extensions[proj_name]:
                date_extensions[proj_name] = new_end

    violations = []
    for r in data.get("timesheets", []):
        proj = _s(r.get("project"))
        entry_date = _s(r.get("date"))
        if not entry_date or entry_date in ("NaT", "None"):
            continue
        proj_lower = proj.lower()
        end = date_extensions.get(proj_lower) or end_dates.get(proj_lower)
        if not end or entry_date <= end:
            continue

        # Check if date_extension email overrides the original end date
        extended = proj_lower in date_extensions
        severity = "MEDIUM" if extended else "HIGH"
        ev = [
            f"kimai_timesheets: {r.get('user')} logged {r.get('hours')}h on {entry_date}",
            f"pm_projects: end_date={end_dates.get(proj_lower, 'unknown')}",
        ]
        if extended:
            ev.append(f"emails/date_extension: revised end_date={date_extensions[proj_lower]}")

        violations.append(_violation(
            _next_id(), "logging_after_end_date", severity,
            employee=_s(r.get("user")),
            project=proj,
            date=entry_date,
            hours=_f(r.get("hours")),
            confidence=0.95,
            evidence=ev,
            context={
                "original_end_date": end_dates.get(proj_lower),
                "extended_end_date": date_extensions.get(proj_lower),
                "date_extension_email": extended,
            },
            recommendation=f"Entry on {entry_date} is after project end date. {'Date extension email found — verify with PM.' if extended else 'Remove entry or request project extension.'}",
        ))
    return violations


# ══════════════════════════════════════════════════════════════════════════════
# Check 11 — Deactivated Employee Logging Time
# ══════════════════════════════════════════════════════════════════════════════

def check_deactivated_employee_logging(data: dict) -> list:
    """Flag timesheet entries from employees whose status is not 'active'."""
    statuses = {
        _s(e.get("username")).lower(): _s(e.get("status")).lower()
        for e in data.get("hr_employees", [])
    }

    violations = []
    for r in data.get("timesheets", []):
        user = _s(r.get("user")).lower()
        status = statuses.get(user, "active")
        if status == "active":
            continue
        violations.append(_violation(
            _next_id(), "deactivated_employee_logging", "HIGH",
            employee=user,
            project=_s(r.get("project")),
            date=_s(r.get("date")),
            hours=_f(r.get("hours")),
            confidence=0.95,
            evidence=[
                f"kimai_timesheets: {user} logged {r.get('hours')}h on {r.get('date')}",
                f"hr_employees: status={status}",
            ],
            context={"employee_status": status, "leave": None, "slack_active": None, "calendar_events": None},
            recommendation=f"Deactivated employee '{user}' (status={status}) has logged time. Escalate to HR and verify system access is revoked.",
        ))
    return violations


# ══════════════════════════════════════════════════════════════════════════════
# Check 17 — Under-Billing (employee period-level aggregate)
# ══════════════════════════════════════════════════════════════════════════════

def check_under_billing(data: dict) -> list:
    """Flag employees whose total logged hours fall significantly short of expected hours."""
    employees = {
        _s(e.get("username")).lower(): e
        for e in data.get("hr_employees", [])
        if _s(e.get("status")).lower() == "active"
    }
    if not employees:
        return []

    holiday_dates = set(data.get("holiday_dates") or [])
    timesheets = data.get("timesheets", [])

    # Hours per (user, project) — used to pro-rate gap across projects
    hours_by_user_project: dict = defaultdict(lambda: defaultdict(float))
    for r in timesheets:
        u = _s(r.get("user")).lower()
        p = _s(r.get("project"))
        if p:
            hours_by_user_project[u][p] += _f(r.get("hours"))

    # Assigned projects per user (fallback when no timesheet hours exist)
    assigned_projects_by_user: dict = defaultdict(list)
    for a in data.get("hr_assignments", []):
        u = _s(a.get("user")).lower()
        p = _s(a.get("project"))
        if u and p:
            assigned_projects_by_user[u].append(p)

    # Determine period from timesheet date range
    dates = [_s(r.get("date")) for r in timesheets if _s(r.get("date")) not in ("", "NaT", "None")]
    if not dates:
        return []
    period_start = _date.fromisoformat(min(dates))
    period_end   = _date.fromisoformat(max(dates))
    working_days = _weekdays_in_range(period_start, period_end, holiday_dates)

    # Leave days per employee
    leave_days_by_user: dict = defaultdict(set)
    for r in data.get("hr_leave", []):
        if _s(r.get("status")).lower() in ("approved", "confirmed"):
            leave_days_by_user[_s(r.get("user")).lower()].add(_s(r.get("date")))
    for r in data.get("calendar_leave", []):
        if _s(r.get("status")).lower() in ("confirmed", "approved") and _s(r.get("all_day")).lower() in ("true", "1", "yes"):
            leave_days_by_user[_s(r.get("user")).lower()].add(_s(r.get("date")))

    # Actual hours per employee
    actual_by_user: dict = defaultdict(float)
    for r in timesheets:
        actual_by_user[_s(r.get("user")).lower()] += _f(r.get("hours"))

    # Escalation emails
    escalation_subjects: dict = defaultdict(list)
    for email in data.get("emails", []):
        if _s(email.get("category")).lower() == "escalation":
            subj = _s(email.get("subject"))
            m = _re.search(r"low hours for ([^\s,]+)", subj, _re.IGNORECASE)
            if m:
                escalation_subjects[m.group(1).lower()].append(subj)

    # Slack days without timesheet per employee
    ts_dates_by_user: dict = defaultdict(set)
    for r in timesheets:
        ts_dates_by_user[_s(r.get("user")).lower()].add(_s(r.get("date")))

    slack_gaps: dict = defaultdict(list)
    for sa in data.get("slack_activity", []):
        user = _s(sa.get("user")).lower()
        d = _s(sa.get("date"))
        if (sa.get("messages") or 0) > 0 and d not in ts_dates_by_user[user]:
            slack_gaps[user].append(d)

    MIN_GAP_PCT = 15.0
    MIN_GAP_HOURS = 16.0

    violations = []
    for user, emp in employees.items():
        contract_hrs = _f(emp.get("contract_hrs"), 8.0)
        leave_set = leave_days_by_user.get(user, set())
        billable_days = [d for d in working_days if d not in leave_set]
        expected = len(billable_days) * contract_hrs
        if expected <= 0:
            continue
        actual = round(actual_by_user.get(user, 0.0), 4)
        gap = round(expected - actual, 4)
        gap_pct = round(gap / expected * 100, 2)

        if gap_pct < MIN_GAP_PCT or gap < MIN_GAP_HOURS:
            continue

        escalations = escalation_subjects.get(user, [])
        slack_miss = slack_gaps.get(user, [])

        if gap_pct > 25 and escalations:
            severity = "HIGH"
            confidence = 0.95
        elif gap_pct > 15 and slack_miss:
            severity = "MEDIUM"
            confidence = 0.85
        else:
            severity = "LOW"
            confidence = 0.7

        ev = [
            f"kimai_timesheets: {actual}h logged for period {period_start} to {period_end}",
            f"Expected: {expected}h ({len(billable_days)} billable days × {contract_hrs}h)",
            f"Gap: {gap}h ({gap_pct}%)",
        ]
        if escalations:
            ev.append(f"emails/escalation: {escalations[0]}")
        if slack_miss:
            ev.append(f"slack_activity: active on {len(slack_miss)} day(s) with no timesheet")

        # Pro-rate hour gap across projects by actual hours logged per project.
        # Fall back to equal split across assigned projects if no hours exist.
        billing_rate = _f(emp.get("rate"), 0.0)
        financial_impact = round(gap * billing_rate, 2) if billing_rate else 0.0
        user_project_hours = hours_by_user_project.get(user, {})
        total_project_h = sum(user_project_hours.values()) or 0.0
        if total_project_h > 0:
            projects_breakdown = {
                proj: round(gap * (ph / total_project_h), 4)
                for proj, ph in user_project_hours.items()
                if proj
            }
        else:
            assigned = assigned_projects_by_user.get(user, [])
            if assigned:
                share = round(gap / len(assigned), 4)
                projects_breakdown = {p: share for p in assigned}
            else:
                projects_breakdown = {}

        violations.append(_violation(
            _next_id(), "under_billing", severity,
            employee=user,
            hours=actual,
            confidence=confidence,
            evidence=ev,
            context={
                "expected_hours": expected,
                "actual_hours": actual,
                "gap_hours": gap,
                "gap_pct": gap_pct,
                "billable_days": len(billable_days),
                "leave_days": len(leave_set),
                "escalation_emails": len(escalations),
                "slack_days_without_timesheet": len(slack_miss),
                "billing_rate": billing_rate,
                "financial_impact_usd": financial_impact,
                "projects_breakdown": projects_breakdown,
            },
            recommendation=f"{user} logged {actual}h vs expected {expected}h (gap {gap_pct}%). {'Client escalation on record. ' if escalations else ''}Investigate with employee.",
        ))
    return violations


# ══════════════════════════════════════════════════════════════════════════════
# Check 18 — Under-Billing Contractor (invoice vs Kimai)
# ══════════════════════════════════════════════════════════════════════════════

def check_under_billing_contractor(data: dict) -> list:
    """Flag contractor invoices whose billed hours don't match Kimai entries."""
    invoices = data.get("contractor_invoices") or []
    if not invoices:
        return []

    # actual hours per contractor (lower-keyed username)
    actual_by_user: dict = defaultdict(float)
    for r in data.get("timesheets", []):
        actual_by_user[_s(r.get("user")).lower()] += _f(r.get("hours"))

    # employee username lookup by partial name match
    emp_names = {_s(e.get("username")).lower() for e in data.get("hr_employees", [])}

    def _match_user(contractor_name: str) -> str:
        cn_lower = contractor_name.lower()
        for u in emp_names:
            # match on last name or first segment
            parts = cn_lower.replace(".", " ").split()
            if any(p in u for p in parts if len(p) > 2):
                return u
        return cn_lower

    violations = []
    for inv in invoices:
        billed = _f(inv.get("hours_billed"))
        if billed <= 0:
            continue
        contractor = _s(inv.get("contractor_name"))
        user = _match_user(contractor)
        actual = round(actual_by_user.get(user, 0.0), 4)
        delta = round(billed - actual, 4)
        if abs(delta) < 2:  # 2h tolerance
            continue
        pct = round(abs(delta) / billed * 100, 1)
        severity = "HIGH" if pct > 10 or actual == 0 else "MEDIUM"
        inv_rate = _f(inv.get("rate"), 0.0)
        financial_impact = round(abs(delta) * inv_rate, 2) if inv_rate else 0.0
        violations.append(_violation(
            _next_id(), "under_billing_contractor", severity,
            employee=user,
            project=_s(inv.get("project")),
            hours=actual,
            confidence=0.85,
            evidence=[
                f"contractor_invoices/{inv.get('source_file')}: invoice_id={inv.get('invoice_id')}, billed={billed}h @ ${inv_rate}/h",
                f"kimai_timesheets: {actual}h logged for matched user '{user}'",
                f"Discrepancy: {delta}h ({pct}%)" + (f" = ${financial_impact:.2f}" if financial_impact else ""),
            ],
            context={
                "invoice_id": inv.get("invoice_id"),
                "invoice_hours": billed,
                "kimai_hours": actual,
                "delta_hours": delta,
                "delta_pct": pct,
                "invoice_rate": inv_rate,
                "financial_impact_usd": financial_impact,
                "invoice_project": inv.get("project"),
            },
            recommendation=f"Invoice {inv.get('invoice_id')} claims {billed}h but Kimai shows {actual}h for '{user}'. Verify before payment.",
        ))
    return violations


# ══════════════════════════════════════════════════════════════════════════════
# Check 19 — Over-Billing (rate mismatch)
# ══════════════════════════════════════════════════════════════════════════════

def check_over_billing(data: dict) -> list:
    """Flag employees where Kimai hourly_rate differs from HR contracted rate."""
    mismatches = data.get("rate_mismatches") or []
    violations = []
    for m in mismatches:
        impact = abs(_f(m.get("financial_impact")))
        if impact < 50:
            severity = "LOW"
            confidence = 0.8
        elif impact < 200:
            severity = "MEDIUM"
            confidence = 0.9
        else:
            severity = "HIGH"
            confidence = 0.95

        delta = _f(m.get("delta"))
        direction = "over_billing" if delta > 0 else "under_billing_rate"
        violations.append(_violation(
            _next_id(), "over_billing", severity,
            employee=_s(m.get("employee")),
            hours=_f(m.get("total_hours")),
            confidence=confidence,
            evidence=[
                f"kimai_timesheets: hourly_rate={m.get('kimai_rate')}",
                f"hr_employees: contracted rate={m.get('contract_rate')}",
                f"Delta: {delta:+.2f}/h × {m.get('total_hours')}h = ${impact:.2f} financial impact",
            ],
            context={
                "kimai_rate": m.get("kimai_rate"),
                "contract_rate": m.get("contract_rate"),
                "delta": delta,
                "total_hours": m.get("total_hours"),
                "financial_impact_usd": round(impact, 2),
                "direction": direction,
                "affected_entries": m.get("entries"),
            },
            recommendation=(
                f"{'Over-billing' if delta > 0 else 'Under-billing'}: {m.get('employee')} "
                f"billed at ${m.get('kimai_rate')}/h vs contracted ${m.get('contract_rate')}/h. "
                f"Financial impact: ${impact:.2f}. Audit Kimai rate and issue credit memo if needed."
            ),
        ))
    return violations


# ══════════════════════════════════════════════════════════════════════════════
# Check 20 — Over-Billing on Leave or Holiday
# ══════════════════════════════════════════════════════════════════════════════

def check_over_billing_on_leave_or_holiday(data: dict) -> list:
    """Flag billable work logged on leave days or public holidays.

    Aggregated at ``(user, date)`` grain — one violation per employee per
    affected day, regardless of how the day was split across Kimai rows.
    The violation rolls up total billable hours, total $ impact, and the
    list of affected projects, so splitting a day into multiple entries
    never inflates the count.

    Half-day leave handling: if either source marks the day as half-day
    (and no source contradicts it as full-day), up to 4 billable hours
    are allowed. Days with ≤ 4h on a half-day leave are not flagged.
    Public holidays have no half-day concept and are always flagged.
    """
    holiday_dates = set(data.get("holiday_dates") or [])

    leave_days: set = set()
    leave_meta: dict = {}
    # Per (user, date): True = half-day, False = full-day. Full-day wins
    # if any source says full-day; default absent → treated as full-day.
    leave_is_half: dict = {}

    def _merge_half(key, is_half_from_source):
        if key in leave_is_half:
            leave_is_half[key] = leave_is_half[key] and is_half_from_source
        else:
            leave_is_half[key] = is_half_from_source

    for r in data.get("hr_leave", []):
        if _s(r.get("status")).lower() in ("approved", "confirmed", ""):
            key = (_s(r.get("user")).lower(), _s(r.get("date")))
            leave_days.add(key)
            leave_meta[key] = {"type": _s(r.get("type")), "source": "hr_leave"}
            _merge_half(key, _s(r.get("type")).lower().startswith("half"))
    for r in data.get("calendar_leave", []):
        if _s(r.get("status")).lower() in ("confirmed", "approved"):
            key = (_s(r.get("user")).lower(), _s(r.get("date")))
            leave_days.add(key)
            if key not in leave_meta:
                leave_meta[key] = {"type": _s(r.get("leave_type")), "source": "calendar_leave"}
            all_day_str = _s(r.get("all_day")).lower()
            leave_type_str = _s(r.get("leave_type")).lower()
            is_half = all_day_str in ("false", "0", "no") or leave_type_str.startswith("half")
            _merge_half(key, is_half)

    # client_holiday email approval dates
    client_approved = set()
    for email in data.get("emails", []):
        if _s(email.get("category")).lower() == "client_holiday":
            d = _s(email.get("date"))
            if d:
                client_approved.add(d)

    # Group billable entries by (user, date) and accumulate totals
    aggregates: dict = defaultdict(lambda: {
        "hours": 0.0,
        "financial_impact": 0.0,
        "projects": defaultdict(float),   # project -> hours
        "entry_count": 0,
        "rates": set(),
    })

    for r in data.get("timesheets", []):
        user = _s(r.get("user")).lower()
        date = _s(r.get("date"))
        rate = _f(r.get("hourly_rate"))
        if rate <= 0:
            continue  # not billable

        is_holiday = date in holiday_dates
        is_leave = (user, date) in leave_days
        if not is_holiday and not is_leave:
            continue

        if date in client_approved:
            continue  # approved by client — not a violation

        hours = _f(r.get("hours"))
        agg = aggregates[(user, date)]
        agg["hours"] += hours
        agg["financial_impact"] += hours * rate
        agg["projects"][_s(r.get("project"))] += hours
        agg["entry_count"] += 1
        agg["rates"].add(rate)

    violations = []
    for (user, date), agg in aggregates.items():
        is_holiday = date in holiday_dates
        is_leave = (user, date) in leave_days

        # Half-day leave allowance: up to 4 billable hours is legitimate.
        # Holidays have no half-day concept, so this only applies when the
        # day is leave but not also a public holiday.
        if (
            is_leave
            and not is_holiday
            and leave_is_half.get((user, date), False)
            and agg["hours"] <= 4.0 + 1e-9
        ):
            continue

        severity = "HIGH" if is_holiday else "MEDIUM"
        reason = ("public holiday" if is_holiday else
                  f"approved leave ({leave_meta.get((user, date), {}).get('type', 'unknown')})")

        total_hours = round(agg["hours"], 2)
        impact = round(agg["financial_impact"], 2)
        projects_breakdown = {p: round(h, 2) for p, h in agg["projects"].items()}
        project_list = sorted(projects_breakdown.keys())
        rate_display = (
            f"${min(agg['rates'])}/h"
            if len(agg["rates"]) == 1
            else f"${min(agg['rates'])}–${max(agg['rates'])}/h"
        )

        violations.append(_violation(
            _next_id(), "over_billing_on_leave_or_holiday", severity,
            employee=user,
            project=", ".join(project_list) if project_list else None,
            date=date,
            hours=total_hours,
            confidence=0.9,
            evidence=[
                f"kimai_timesheets: {total_hours}h billed at {rate_display} "
                f"across {agg['entry_count']} entr{'y' if agg['entry_count']==1 else 'ies'} on {date}",
                f"Reason: {reason}",
                f"Projects: {projects_breakdown}",
                f"Financial impact: ${impact:.2f}",
            ],
            context={
                "is_holiday": is_holiday,
                "is_leave": is_leave,
                "client_approved": False,
                "total_hours": total_hours,
                "financial_impact_usd": impact,
                "entry_count": agg["entry_count"],
                "projects": projects_breakdown,
                "leave_type": leave_meta.get((user, date), {}).get("type") if is_leave else None,
                "leave_source": leave_meta.get((user, date), {}).get("source") if is_leave else None,
            },
            recommendation=(
                f"{total_hours}h of billable work logged on {reason} "
                f"({date}) across {len(project_list)} project(s); "
                f"${impact:.2f} may be invoiced incorrectly. "
                f"Remove entries or obtain client approval."
            ),
        ))
    return violations


# ══════════════════════════════════════════════════════════════════════════════
# Check — Fuzzy Name Mismatch
# ══════════════════════════════════════════════════════════════════════════════

def check_fuzzy_name_mismatch(data: dict) -> list:
    """Flag timesheet project names with no exact match in pm_projects.

    Near-matches (abbreviations, spacing differences, typos) require LLM
    fuzzy reasoning and are not detected here.
    """
    canonical = {
        _s(p.get("name")).lower()
        for p in data.get("pm_projects", [])
        if _s(p.get("name"))
    }
    ts_projects = sorted({
        _s(r.get("project"))
        for r in data.get("timesheets", [])
        if _s(r.get("project"))
    })

    violations = []
    for proj in ts_projects:
        if proj.lower() in canonical:
            continue
        violations.append(_violation(
            _next_id(), "fuzzy_name_mismatch", "MEDIUM",
            project=proj,
            confidence=0.60,
            evidence=[
                f"kimai_timesheets: project name '{proj}'",
                "pm_projects: no exact match — LLM to assess near-matches",
            ],
            context={
                "leave": None, "slack_active": None, "calendar_events": None,
                "sources": {"timesheets": proj},
            },
            recommendation=(
                f"'{proj}' has no exact match in pm_projects. "
                f"LLM to check for abbreviations or spelling variants."
            ),
        ))
    return violations


# ══════════════════════════════════════════════════════════════════════════════
# Check — SOW Parse Failure
# ══════════════════════════════════════════════════════════════════════════════

# check_sow_parse_failure removed: partial/unrecognized SOWs are handled by the LLM in
# Phase 3 §3d (get_sows → raw_text extraction → save_extraction). Flagging them as
# violations creates noise because the LLM resolves them before the report is generated.


# ══════════════════════════════════════════════════════════════════════════════
# Check — Client Escalation with Low Hours
# ══════════════════════════════════════════════════════════════════════════════

def check_client_escalation_low_hours(data: dict) -> list:
    """Flag escalation emails where the hours shortfall is not explained by leave.

    Parses email subject with a fixed regex pattern. Emails whose subject
    doesn't match the pattern are skipped; LLM can re-examine body_html for
    those cases via CLAUDE.md judgment instructions.
    """
    _ESC_RE = _re.compile(
        r"low hours for\s+([a-zA-Z0-9_.\-]+)\s+on\s+(.+?)(?:\s*[.\-]|$)",
        _re.IGNORECASE,
    )
    emp_contract = {
        _s(e.get("username")).lower(): _f(e.get("contract_hrs"), 8.0)
        for e in data.get("hr_employees", [])
    }
    emp_known = {_s(e.get("username")).lower() for e in data.get("hr_employees", [])}
    holiday_dates = set(data.get("holiday_dates") or [])

    leave_dates: dict = defaultdict(set)
    for r in data.get("hr_leave", []):
        if _s(r.get("status")).lower() in ("approved", "confirmed", ""):
            leave_dates[_s(r.get("user")).lower()].add(_s(r.get("date")))
    for r in data.get("calendar_leave", []):
        if _s(r.get("status")).lower() in ("confirmed", "approved"):
            leave_dates[_s(r.get("user")).lower()].add(_s(r.get("date")))

    violations = []
    for em in data.get("emails", []):
        if _s(em.get("category")).lower() != "escalation":
            continue
        subj = _s(em.get("subject"))
        body = _strip_html(_s(em.get("body_html")))
        m = _ESC_RE.search(subj) or _ESC_RE.search(body)
        if not m:
            continue
        emp_raw, proj_raw = m.group(1), m.group(2).strip()
        emp_l = emp_raw.lower()
        if emp_l not in emp_known:
            continue

        try:
            em_date = _date.fromisoformat(_s(em.get("date")))
        except ValueError:
            continue

        week_start, week_end = _week_bounds(em_date)
        contract = emp_contract.get(emp_l, 8.0)

        working_days = sum(
            1 for i in range(7)
            if (week_start + _td(days=i)).weekday() < 5
            and (week_start + _td(days=i)).isoformat() not in holiday_dates
        )
        leave_in_week = sum(
            1 for i in range(7)
            if (week_start + _td(days=i)).isoformat() in leave_dates.get(emp_l, set())
            and (week_start + _td(days=i)).weekday() < 5
            and (week_start + _td(days=i)).isoformat() not in holiday_dates
        )
        expected = (working_days - leave_in_week) * contract
        actual = round(sum(
            _f(r.get("hours"))
            for r in data.get("timesheets", [])
            if _s(r.get("user")).lower() == emp_l
            and week_start.isoformat() <= _s(r.get("date")) <= week_end.isoformat()
        ), 2)

        if expected > 0 and (actual / expected * 100) >= 70:
            continue

        shortfall_pct = round(100 - (actual / expected * 100 if expected else 100), 1)
        severity = "HIGH" if leave_in_week == 0 else "MEDIUM"

        violations.append(_violation(
            _next_id(), "client_escalation_low_hours", severity,
            employee=emp_l, project=proj_raw, date=_s(em.get("date")),
            hours=actual, confidence=0.92,
            evidence=[
                f"emails: '{subj}' from {_s(em.get('from_email'))} on {_s(em.get('date'))}",
                f"kimai_timesheets: {actual}h logged {week_start}..{week_end} "
                f"(expected {expected}h)",
                f"hr_leave/calendar_leave: {leave_in_week} leave day(s) in week",
            ],
            context={
                "leave": leave_in_week, "slack_active": None, "calendar_events": None,
                "expected_hours": expected, "actual_hours": actual,
                "shortfall_pct": shortfall_pct,
                "week_start": week_start.isoformat(),
                "week_end":   week_end.isoformat(),
            },
            recommendation=(
                f"Client escalation matches a {shortfall_pct}% hours shortfall for "
                f"{emp_l} on '{proj_raw}'. Review with employee and PM."
            ),
        ))
    return violations


# ══════════════════════════════════════════════════════════════════════════════
# Check — Unauthorized Assignment via Email
# ══════════════════════════════════════════════════════════════════════════════

def check_unauthorized_assignment_via_email(data: dict) -> list:
    """Flag assignment emails where (employee, project) is not in hr_assignments."""
    assignments: set = set()
    for r in data.get("hr_assignments", []):
        assignments.add((_s(r.get("user")).lower(), _s(r.get("project")).lower()))

    pm_projects = sorted({
        _s(p.get("name")) for p in data.get("pm_projects", []) if _s(p.get("name"))
    })
    emp_known = {_s(e.get("username")).lower() for e in data.get("hr_employees", [])}

    ts_by_user_proj: dict = defaultdict(set)
    for r in data.get("timesheets", []):
        ts_by_user_proj[
            (_s(r.get("user")).lower(), _s(r.get("project")).lower())
        ].add(_s(r.get("date")))

    violations = []
    for em in data.get("emails", []):
        if _s(em.get("category")).lower() != "assignment":
            continue
        body = _strip_html(_s(em.get("body_html")))
        subj = _s(em.get("subject"))
        combined = (subj + " " + body).lower()

        project = next((p for p in pm_projects if p.lower() in combined), None)
        if not project:
            continue

        body_l = body.lower()
        found_users = {
            u for u in emp_known
            if _re.search(rf"\b{_re.escape(u)}\b", body_l)
        }

        em_date = _s(em.get("date"))
        for u in sorted(found_users):
            if (u, project.lower()) in assignments:
                continue
            logged_dates = sorted(ts_by_user_proj.get((u, project.lower()), set()))
            is_logging = bool(logged_dates)

            violations.append(_violation(
                _next_id(), "unauthorized_assignment_via_email",
                "HIGH" if is_logging else "MEDIUM",
                employee=u, project=project, date=em_date,
                confidence=0.88,
                evidence=[
                    f"emails: '{subj}' on {em_date} (category: assignment)",
                    f"hr_assignments: no record of {u} on '{project}'",
                    f"kimai_timesheets: {u} "
                    f"{'has' if is_logging else 'has no'} entries on '{project}'"
                    + (f" ({len(logged_dates)} day(s))" if is_logging else ""),
                ],
                context={
                    "leave": None, "slack_active": None, "calendar_events": None,
                    "logging_to_project": is_logging,
                    "logged_dates": logged_dates,
                },
                recommendation=(
                    f"Email onboarding for {u} on '{project}' not in hr_assignments. "
                    + ("Employee is already logging time — formalize assignment immediately."
                       if is_logging
                       else "Add assignment record to keep HR data in sync.")
                ),
            ))
    return violations


# ══════════════════════════════════════════════════════════════════════════════
# Check — Approved Extra-Time Not Logged
# ══════════════════════════════════════════════════════════════════════════════

def check_approved_extra_time_not_logged(data: dict) -> list:
    """Flag approved extra-time emails where the employee didn't log beyond contract_hrs."""
    _EXTRA_RE = _re.compile(
        r"extra time for\s+([a-zA-Z0-9_.\-]+)\s+on\s+(.+?)\s*[-–]\s*(\d{4}-\d{2}-\d{2})",
        _re.IGNORECASE,
    )
    emp_contract = {
        _s(e.get("username")).lower(): _f(e.get("contract_hrs"), 8.0)
        for e in data.get("hr_employees", [])
    }
    emp_known = {_s(e.get("username")).lower() for e in data.get("hr_employees", [])}

    ts_lookup: dict = defaultdict(list)
    for r in data.get("timesheets", []):
        ts_lookup[(_s(r.get("user")).lower(), _s(r.get("date")))].append(r)

    violations = []
    for em in data.get("emails", []):
        if _s(em.get("category")).lower() != "extra_time":
            continue
        subj = _s(em.get("subject"))
        body = _strip_html(_s(em.get("body_html")))
        m = _EXTRA_RE.search(subj) or _EXTRA_RE.search(body)
        if not m:
            continue
        emp_raw, proj_raw, date_s = m.group(1), m.group(2).strip(), m.group(3)
        emp_l = emp_raw.lower()
        if emp_l not in emp_known:
            continue

        contract = emp_contract.get(emp_l, 8.0)
        total = round(sum(_f(r.get("hours")) for r in ts_lookup.get((emp_l, date_s), [])), 2)
        if total > contract:
            continue  # extra time was logged — not a violation

        violations.append(_violation(
            _next_id(), "approved_extra_time_not_logged", "LOW",
            employee=emp_l, project=proj_raw, date=date_s, hours=total,
            confidence=0.90,
            evidence=[
                f"emails: '{subj}' on {_s(em.get('date'))} (category: extra_time)",
                f"kimai_timesheets: {emp_l} logged {total}h on {date_s} "
                f"(contract {contract}h/day)",
            ],
            context={
                "leave": None, "slack_active": None, "calendar_events": None,
                "approved_date": date_s,
                "logged_hours": total,
                "contract_hrs": contract,
            },
            recommendation=(
                f"Approved extra time for {emp_l} on {date_s} appears unbilled. "
                f"Confirm with employee and log extra hours if work occurred."
            ),
        ))
    return violations



# ══════════════════════════════════════════════════════════════════════════════
# Registry + runner
# ══════════════════════════════════════════════════════════════════════════════

# Ordered list of (check_name, function).
# All checks here are fully deterministic — pure logic, math, or structured
# field lookups with no LLM reasoning required.
#
# Judgment phase (LLM + extraction cache) — not listed here:
#   - unauthorized_entry        Phase 3 owns detect + classify end-to-end;
#                               candidates exposed via get_unauthorized_candidates
#   - missing_timesheet         Phase 3 owns detect + calibrate end-to-end;
#                               candidates exposed via get_missing_timesheet_candidates
#   - billing_vs_sow_rate       needs cache for partial SOW rates + resource mapping
#   - Fuzzy project mapping     abbreviations, typos across sources
#   - SOW resource → username   ambiguous name resolution
#   - SOW field extraction      partial/unrecognized template raw_text
#   - Guidelines extraction     HR policy PDFs
#
# NOTE on leave/holiday checks:
#   check_logging_on_leave and check_logging_on_public_holiday have been
#   removed. Kimai holds leave/holiday records as a distinct category from
#   work, so a Kimai entry on a leave or holiday day is expected behaviour.
#   The real concern — billable work on those days — is captured by
#   check_over_billing_on_leave_or_holiday (gated on hourly_rate > 0).
CHECKS = [
    # ── original deterministic checks ──────────────────────────────────────
    ("over_logging",                          check_over_logging),
    ("archived_project",                      check_archived_project),
    ("no_assignment",                         check_no_assignment),
    ("over_budget",                           check_over_budget),
    ("logging_after_end_date",                check_logging_after_end_date),
    ("deactivated_employee_logging",          check_deactivated_employee_logging),
    ("under_billing",                         check_under_billing),
    ("under_billing_contractor",              check_under_billing_contractor),
    ("over_billing",                          check_over_billing),
    ("over_billing_on_leave_or_holiday",      check_over_billing_on_leave_or_holiday),
    # ── deterministic detection for the remaining types ────────────────────
    ("fuzzy_name_mismatch",                   check_fuzzy_name_mismatch),
    ("client_escalation_low_hours",           check_client_escalation_low_hours),
    ("unauthorized_assignment_via_email",     check_unauthorized_assignment_via_email),
    ("approved_extra_time_not_logged",        check_approved_extra_time_not_logged),
    # billing_vs_sow_rate → judgment phase (needs extraction cache for partial SOWs)
]


def run_all_checks(data: dict) -> list:
    """Run every registered deterministic check and return combined violations."""
    _reset_ids()
    all_violations = []
    for name, fn in CHECKS:
        try:
            results = fn(data)
            all_violations.extend(results)
        except Exception as exc:
            import warnings as _w
            _w.warn(f"checks: error in {name}: {exc}")
    return all_violations
