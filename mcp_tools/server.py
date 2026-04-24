#!/usr/bin/env python3
"""
MCP Server for Timesheet Validation Agent.
Provides 7 tools that read from intermediate/loaded_data.json at call time.
Run with: python mcp_tools/server.py
"""

import asyncio
import html
import json
import os
import re
import sys
import warnings
from collections import Counter, defaultdict
from datetime import datetime

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

# Paths
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Make agents/ importable
sys.path.insert(0, os.path.join(_ROOT, "agents"))
INTERMEDIATE_PATH    = os.path.join(_ROOT, "intermediate", "loaded_data.json")
REPORTS_PATH         = os.path.join(_ROOT, "reports", "violations.html")
FUZZY_MAPPING_PATH   = os.path.join(_ROOT, "intermediate", "fuzzy_mapping.json")

server = Server("timesheet-tools")


# ─── HTML Report Renderer ─────────────────────────────────────────────────────

# Human-readable labels for violation types — used in the rendered report.
VIOLATION_LABELS = {
    "over_logging":                       "Over-Logged Workday",
    "archived_project":                   "Entry on Archived Project",
    "no_assignment":                      "No HR Assignment",
    "over_budget":                        "Project Over Budget",
    "logging_after_end_date":             "Logged After Project End Date",
    "deactivated_employee_logging":       "Deactivated Employee Logging",
    "under_billing":                      "Under-Billing (Employee)",
    "under_billing_contractor":           "Under-Billing (Contractor)",
    "over_billing":                       "Rate Mismatch (Over/Under-Billing)",
    "over_billing_on_leave_or_holiday":   "Billable Work on Leave / Holiday",
    "unauthorized_entry":                 "Unauthorized Project Entry",
    "missing_timesheet":                  "Missing Timesheet (Slack Active)",
    "fuzzy_name_mismatch":                "Project Name Mismatch",
    "client_escalation_low_hours":        "Client Escalation — Low Hours",
    "unauthorized_assignment_via_email":  "Assignment Email Not in HR Records",
    "approved_extra_time_not_logged":     "Approved Extra Time Not Logged",
    "billing_vs_sow_rate":                "Billing Rate vs SOW Contract Rate",
    "sow_resource_unmapped":              "SOW Resource Name Unmapped",
    "missing_sow":                        "Missing SOW for Active Project",
    "sow_resource_overrun":               "SOW Resource Hour Cap Overrun",
}


def _label(vtype: str) -> str:
    return VIOLATION_LABELS.get(vtype, vtype.replace("_", " ").title())


# ─── Business categories ─────────────────────────────────────────────────────
# Group violation types into business-meaningful buckets so the report can be
# scanned by concern area rather than as a flat list of 20+ type names.

CATEGORY_OTHER = "Other"

CATEGORY_ORDER = [
    "Overbilling Exposure",
    "Missed Revenue",
    "Contract Rate Gaps",
    "Unauthorized Billing",
    "Client Escalation Risk",
    "Data Integrity",
]

CATEGORY_SUBTITLE = {
    "Overbilling Exposure":    "Hours or rates billed above what was worked or approved — creates client credit/refund exposure.",
    "Missed Revenue":          "Work performed or approved but not captured in timesheets — revenue left on the table.",
    "Contract Rate Gaps":      "SOW and budget deviations where billed amounts conflict with signed contracts.",
    "Unauthorized Billing":    "Time logged to projects without proper authorisation — billing legitimacy risk.",
    "Client Escalation Risk":  "Direct client signals of under-delivery — potential penalties or contract renegotiation.",
    "Data Integrity":          "Pipeline and data quality defects that reduce confidence in billing figures.",
    CATEGORY_OTHER:            "Violation types not yet mapped to an invoicing gap category.",
}

CATEGORY_MAP = {
    # Overbilling Exposure
    "over_logging":                      "Overbilling Exposure",
    "over_billing":                      "Overbilling Exposure",
    "over_billing_on_leave_or_holiday":  "Overbilling Exposure",
    # Missed Revenue
    "missing_timesheet":                 "Missed Revenue",
    "under_billing":                     "Missed Revenue",
    "under_billing_contractor":          "Missed Revenue",
    "approved_extra_time_not_logged":    "Missed Revenue",
    # Contract Rate Gaps
    "billing_vs_sow_rate":               "Contract Rate Gaps",
    "missing_sow":                       "Contract Rate Gaps",
    "sow_resource_overrun":              "Contract Rate Gaps",
    "over_budget":                       "Contract Rate Gaps",
    "logging_after_end_date":            "Contract Rate Gaps",
    # Unauthorized Billing
    "unauthorized_entry":                "Unauthorized Billing",
    "no_assignment":                     "Unauthorized Billing",
    "unauthorized_assignment_via_email": "Unauthorized Billing",
    "deactivated_employee_logging":      "Unauthorized Billing",
    "archived_project":                  "Unauthorized Billing",
    # Client Escalation Risk
    "client_escalation_low_hours":       "Client Escalation Risk",
    # Data Integrity
    "fuzzy_name_mismatch":               "Data Integrity",
    "sow_resource_unmapped":             "Data Integrity",
    # sow_parse_failure intentionally omitted: handled by LLM in Phase 3 §3d
}

_uncategorized_warned: set = set()


def _category_for(vtype: str) -> str:
    cat = CATEGORY_MAP.get(vtype)
    if cat is None:
        if vtype not in _uncategorized_warned:
            _uncategorized_warned.add(vtype)
            warnings.warn(f"uncategorized violation type: {vtype!r} → routed to {CATEGORY_OTHER!r}")
        return CATEGORY_OTHER
    return cat


def _category_slug(name: str) -> str:
    return "cat-" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


# ─── Render helpers ──────────────────────────────────────────────────────────

SEV_COLOR = {"HIGH": "#dc2626", "MEDIUM": "#d97706", "LOW": "#2563eb"}
SEV_BG    = {"HIGH": "#fef2f2", "MEDIUM": "#fffbeb", "LOW": "#eff6ff"}
SEV_BADGE_STYLE = (
    "color:#fff;padding:2px 8px;border-radius:4px;"
    "font-size:0.75rem;font-weight:700"
)


def _esc(s: object) -> str:
    return html.escape(str(s))


def _badge(sev: str) -> str:
    color = SEV_COLOR.get(sev, "#6b7280")
    return f'<span style="background:{color};{SEV_BADGE_STYLE}">{_esc(sev)}</span>'


def _evidence_html(text: str) -> str:
    escaped = html.escape(text)
    for label, color in [
        ("CONFIRMING", "#dc2626"),
        ("CONTRADICTING", "#16a34a"),
        ("NEUTRAL", "#6b7280"),
    ]:
        escaped = escaped.replace(
            label,
            f'<span style="color:{color};font-weight:700">{label}</span>',
        )
    return escaped


def _compute_user_stats(violations: list) -> dict:
    stats: dict = defaultdict(lambda: {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "total": 0})
    for v in violations:
        u = v.get("employee")
        if not u:
            continue
        sev = v.get("severity", "LOW")
        if sev not in ("HIGH", "MEDIUM", "LOW"):
            sev = "LOW"
        stats[u][sev] += 1
        stats[u]["total"] += 1
    return stats


def _compute_project_stats(violations: list) -> dict:
    stats: dict = defaultdict(lambda: {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "total": 0})
    for v in violations:
        p = (v.get("project") or "").strip()
        if not p:
            continue
        sev = v.get("severity", "LOW")
        if sev not in ("HIGH", "MEDIUM", "LOW"):
            sev = "LOW"
        stats[p][sev] += 1
        stats[p]["total"] += 1
    return stats


def _leaderboard_rows(stats: dict, limit: int = 10) -> str:
    ranked = sorted(stats.items(), key=lambda x: (-x[1]["total"], -x[1]["HIGH"]))[:limit]
    if not ranked:
        return '<p style="color:#6b7280;font-style:italic;font-size:0.85rem">No data.</p>'

    rows = []
    for rank, (name, s) in enumerate(ranked, 1):
        hi_style = "color:#dc2626;font-weight:700" if s["HIGH"] else "color:#9ca3af"
        rows.append(f"""
    <tr>
      <td style="padding:6px 8px;color:#9ca3af;font-size:0.8rem;text-align:center">{rank}</td>
      <td style="padding:6px 8px;font-weight:600;font-size:0.85rem">{_esc(name)}</td>
      <td style="padding:6px 8px;text-align:right;{hi_style};font-size:0.85rem">{s['HIGH']}</td>
      <td style="padding:6px 8px;text-align:right;color:#d97706;font-size:0.85rem">{s['MEDIUM']}</td>
      <td style="padding:6px 8px;text-align:right;color:#2563eb;font-size:0.85rem">{s['LOW']}</td>
      <td style="padding:6px 8px;text-align:right;font-weight:700;font-size:0.85rem">{s['total']}</td>
    </tr>""")

    th = "padding:6px 8px;font-size:0.7rem;text-transform:uppercase;letter-spacing:.04em;color:#9ca3af;border-bottom:2px solid #f3f4f6"
    return f"""
  <table style="width:100%;border-collapse:collapse">
    <thead><tr>
      <th style="{th}">#</th>
      <th style="{th}">Name</th>
      <th style="{th};text-align:right">High</th>
      <th style="{th};text-align:right">Med</th>
      <th style="{th};text-align:right">Low</th>
      <th style="{th};text-align:right">Total</th>
    </tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>"""


def _collapsible_card(card_id: str, heading: str, body: str,
                       subtitle: str = "", start_collapsed: bool = False,
                       extra_attrs: str = "") -> str:
    """Render a collapsible .card with a ▾/▸ toggle button."""
    icon = "▸" if start_collapsed else "▾"
    body_style = "display:none;margin-top:14px" if start_collapsed else "margin-top:14px"
    sub_html = (f'<p style="margin:4px 0 0;font-size:0.82rem;color:#6b7280">{subtitle}</p>'
                if subtitle else "")
    return (
        f'<div class="card collapsible-card" id="{card_id}" {extra_attrs}>'
        f'<div style="display:flex;align-items:flex-start;justify-content:space-between;gap:8px">'
        f'<div style="flex:1">'
        f'<h2 style="margin:0 0 0;font-size:1rem;font-weight:700">{heading}</h2>{sub_html}'
        f'</div>'
        f'<button class="collapse-toggle" onclick="toggleSection(this)" title="{"Expand" if start_collapsed else "Collapse"}" '
        f'style="flex-shrink:0;margin-top:2px;background:none;border:1px solid #e5e7eb;'
        f'border-radius:6px;padding:2px 10px;font-size:1rem;cursor:pointer;'
        f'color:#6b7280;line-height:1.4">{icon}</button>'
        f'</div>'
        f'<div class="section-body" style="{body_style}">{body}</div>'
        f'</div>'
    )


def _top10_leaderboard(user_stats: dict, project_stats: dict) -> str:
    body = f"""
  <div style="display:flex;gap:24px;flex-wrap:wrap">
    <div style="flex:1;min-width:280px">
      <h3 style="margin:0 0 10px;font-size:0.85rem;font-weight:600;color:#374151;text-transform:uppercase;letter-spacing:.04em">Employees</h3>
      {_leaderboard_rows(user_stats)}
    </div>
    <div style="flex:1;min-width:280px">
      <h3 style="margin:0 0 10px;font-size:0.85rem;font-weight:600;color:#374151;text-transform:uppercase;letter-spacing:.04em">Projects</h3>
      {_leaderboard_rows(project_stats)}
    </div>
  </div>"""
    return _collapsible_card("top10", "Top 10 by Invoicing Risk", body)


def _compute_invoicing_stats(violations: list) -> dict:
    """Aggregate per-project financial figures for the invoicing gaps table."""
    stats: dict = defaultdict(lambda: {
        "overbilling_usd": 0.0,
        "missed_usd": 0.0,
        "excess_hours": 0.0,
        "missing_days": 0,
        "rate_gap_count": 0,
        "total_violations": 0,
        "has_high": False,
    })
    for v in violations:
        vtype = v.get("type") or ""
        sev = v.get("severity", "LOW")
        ctx = v.get("context") or {}
        hours = float(v.get("hours") or 0.0)

        # over_billing_on_leave_or_holiday stores per-project hours in context.projects
        # Use that to attribute financial impact to each project individually instead of
        # treating the comma-joined project string as a single project name.
        if vtype == "over_billing_on_leave_or_holiday":
            project_breakdown: dict = ctx.get("projects") or {}
            financial_impact = float(ctx.get("financial_impact_usd") or 0.0)
            total_h = sum(project_breakdown.values()) if project_breakdown else hours or 1.0
            if project_breakdown:
                for proj, proj_hours in project_breakdown.items():
                    proj = proj.strip()
                    if not proj:
                        continue
                    fraction = (proj_hours / total_h) if total_h else 0.0
                    s = stats[proj]
                    s["total_violations"] += 1
                    if sev == "HIGH":
                        s["has_high"] = True
                    s["overbilling_usd"] += financial_impact * fraction
                    s["excess_hours"] += proj_hours
            else:
                # Fallback: no breakdown available, skip multi-name rows
                project = (v.get("project") or "").strip()
                if project and "," not in project:
                    s = stats[project]
                    s["total_violations"] += 1
                    if sev == "HIGH":
                        s["has_high"] = True
                    s["overbilling_usd"] += financial_impact
                    s["excess_hours"] += hours
            continue

        # under_billing: pro-rated across projects via projects_breakdown in context
        if vtype == "under_billing":
            project_breakdown: dict = ctx.get("projects_breakdown") or {}
            financial_impact = float(ctx.get("financial_impact_usd") or 0.0)
            total_gap_h = sum(project_breakdown.values()) if project_breakdown else 0.0
            if project_breakdown:
                for proj, proj_gap_h in project_breakdown.items():
                    proj = proj.strip()
                    if not proj:
                        continue
                    fraction = (proj_gap_h / total_gap_h) if total_gap_h else 0.0
                    s = stats[proj]
                    s["total_violations"] += 1
                    if sev == "HIGH":
                        s["has_high"] = True
                    s["missed_usd"] += financial_impact * fraction
            continue

        # All other types — skip null/multi-value project fields
        project = (v.get("project") or "").strip()
        if not project or "," in project:
            continue

        s = stats[project]
        s["total_violations"] += 1
        if sev == "HIGH":
            s["has_high"] = True

        if vtype == "over_billing":
            s["overbilling_usd"] += float(ctx.get("financial_impact_usd") or 0.0)
            s["excess_hours"] += hours
        elif vtype == "over_logging":
            s["excess_hours"] += float(ctx.get("over_by") or 0.0)
        elif vtype == "missing_timesheet":
            s["missing_days"] += 1
        elif vtype == "under_billing_contractor":
            s["missed_usd"] += float(ctx.get("financial_impact_usd") or 0.0)
        elif vtype == "billing_vs_sow_rate":
            s["rate_gap_count"] += 1
    return dict(stats)


def _financial_kpi_row(violations: list) -> str:
    """Second KPI row showing monetary invoicing gap figures."""
    total_overbilling = 0.0
    total_excess_hours = 0.0
    missing_timesheet_days = 0
    rate_gap_count = 0
    for v in violations:
        vtype = v.get("type") or ""
        ctx = v.get("context") or {}
        hours = float(v.get("hours") or 0.0)
        if vtype in ("over_billing", "over_billing_on_leave_or_holiday"):
            total_overbilling += float(ctx.get("financial_impact_usd") or 0.0)
            total_excess_hours += hours
        elif vtype == "over_logging":
            total_excess_hours += float(ctx.get("over_by") or 0.0)
        elif vtype == "missing_timesheet":
            missing_timesheet_days += 1
        elif vtype == "billing_vs_sow_rate":
            rate_gap_count += 1
    return f"""
  <div style="display:flex;gap:16px;flex-wrap:wrap;margin-top:14px;padding-top:14px;border-top:1px solid #e5e7eb">
    <div class="stat" style="background:#fff0f0;flex:1;min-width:150px">
      <div class="num" style="color:#dc2626;font-size:1.6rem">${total_overbilling:,.0f}</div>
      <div class="lbl" style="color:#b91c1c">Quantified Overbilling</div>
    </div>
    <div class="stat" style="background:#fff7ed;flex:1;min-width:150px">
      <div class="num" style="color:#ea580c;font-size:1.6rem">{total_excess_hours:,.1f}h</div>
      <div class="lbl" style="color:#c2410c">Excess Hours Billed</div>
    </div>
    <div class="stat" style="background:#f0fdf4;flex:1;min-width:150px">
      <div class="num" style="color:#16a34a;font-size:1.6rem">{missing_timesheet_days}</div>
      <div class="lbl" style="color:#15803d">Missing Timesheet Days</div>
    </div>
    <div class="stat" style="background:#eff6ff;flex:1;min-width:150px">
      <div class="num" style="color:#2563eb;font-size:1.6rem">{rate_gap_count}</div>
      <div class="lbl" style="color:#1d4ed8">SOW Rate Mismatches</div>
    </div>
  </div>"""


def _project_invoicing_gap_card(violations: list) -> str:
    """Per-project invoicing gap table — the primary revenue intelligence view."""
    stats = _compute_invoicing_stats(violations)
    if not stats:
        return ""

    def _sort_key(item):
        s = item[1]
        return -(s["overbilling_usd"] + s["missed_usd"] + s["excess_hours"] * 50 + s["missing_days"] * 100)

    # Only include projects that have at least one quantifiable financial signal
    ranked = sorted(
        [(p, s) for p, s in stats.items()
         if s["overbilling_usd"] or s["excess_hours"] or s["missed_usd"]
         or s["missing_days"] or s["rate_gap_count"]],
        key=_sort_key,
    )

    rows = []
    for rank, (project, s) in enumerate(ranked, 1):
        risk = "HIGH" if s["has_high"] else ("MEDIUM" if s["total_violations"] > 3 else "LOW")
        risk_color = SEV_COLOR.get(risk, "#6b7280")
        overbilling = f"${s['overbilling_usd']:,.2f}" if s["overbilling_usd"] else "—"
        missed = f"${s['missed_usd']:,.2f}" if s["missed_usd"] else "—"
        excess_h = f"{s['excess_hours']:.1f}h" if s["excess_hours"] else "—"
        missing_d = str(s["missing_days"]) if s["missing_days"] else "—"
        rate_gaps = str(s["rate_gap_count"]) if s["rate_gap_count"] else "—"
        rows.append(
            f'<tr>'
            f'<td style="padding:8px 12px;color:#9ca3af;font-size:0.8rem;text-align:center">{rank}</td>'
            f'<td style="padding:8px 12px;font-weight:600">{_esc(project)}</td>'
            f'<td style="padding:8px 12px;text-align:right;color:#dc2626;font-weight:600">{_esc(overbilling)}</td>'
            f'<td style="padding:8px 12px;text-align:right;color:#ea580c">{_esc(excess_h)}</td>'
            f'<td style="padding:8px 12px;text-align:right;color:#16a34a;font-weight:600">{_esc(missed)}</td>'
            f'<td style="padding:8px 12px;text-align:right;color:#374151">{_esc(missing_d)}</td>'
            f'<td style="padding:8px 12px;text-align:right;color:#6b7280">{_esc(rate_gaps)}</td>'
            f'<td style="padding:8px 12px">'
            f'<span style="background:{risk_color};color:#fff;padding:2px 8px;border-radius:4px;'
            f'font-size:0.72rem;font-weight:700">{_esc(risk)}</span>'
            f'</td>'
            f'</tr>'
        )

    th = ("padding:8px 12px;font-size:0.7rem;text-transform:uppercase;letter-spacing:.04em;"
          "color:#6b7280;border-bottom:2px solid #e5e7eb;white-space:nowrap")
    table_html = f"""
  <div style="overflow-x:auto;border:1px solid #e5e7eb;border-radius:6px">
  <table>
    <thead><tr>
      <th style="{th}">#</th>
      <th style="{th}">Project</th>
      <th style="{th};text-align:right;color:#dc2626">Overbilling ($)</th>
      <th style="{th};text-align:right;color:#ea580c">Excess Hours</th>
      <th style="{th};text-align:right;color:#16a34a">Missed Revenue ($)</th>
      <th style="{th};text-align:right">Missing Days</th>
      <th style="{th};text-align:right">Rate Gaps</th>
      <th style="{th}">Risk</th>
    </tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  </div>"""
    return _collapsible_card(
        "invoicing-gaps-project",
        "Invoicing Gaps by Project",
        table_html,
        subtitle=(
            "Quantified overbilling and missed revenue per project. Dollar figures sourced from "
            "billing records where rate data is available; excess hours are a proxy for unquantified "
            "exposure. Missing Days = Slack-active days with no timesheet. "
            "Rate Gaps = SOW contract rate deviations. Projects with no financial signals excluded."
        ),
    )


def _check_distribution_chart(violations: list) -> str:
    counts = Counter(v.get("type") or "unknown" for v in violations)
    if not counts:
        return ""

    items = sorted(counts.items(), key=lambda x: -x[1])
    max_count = max(counts.values())

    # Map each violation type to its dominant severity for bar colour
    type_sev: dict = {}
    for v in violations:
        t = v.get("type") or "unknown"
        sev = v.get("severity", "LOW")
        if t not in type_sev:
            type_sev[t] = sev
        else:
            order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
            if order.get(sev, 3) < order.get(type_sev[t], 3):
                type_sev[t] = sev

    rows = []
    for vtype, count in items:
        sev = type_sev.get(vtype, "LOW")
        color = SEV_COLOR.get(sev, "#6b7280")
        pct = max(1, round(count / max_count * 100))
        label = _label(vtype)
        rows.append(f"""
  <div style="display:flex;align-items:center;gap:10px;margin:5px 0">
    <span style="font-size:0.8rem;font-weight:600;color:#374151;width:260px;text-align:right;flex-shrink:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="{_esc(vtype)}">{_esc(label)}</span>
    <div style="flex:1;background:#f3f4f6;border-radius:4px;height:18px;min-width:0">
      <div style="width:{pct}%;height:100%;background:{color};border-radius:4px;opacity:0.82;min-width:4px"></div>
    </div>
    <span style="font-size:0.8rem;font-weight:700;color:#111827;width:46px;text-align:right;flex-shrink:0">{count:,}</span>
    <span style="font-size:0.72rem;color:#9ca3af;white-space:nowrap;font-family:ui-monospace,Menlo,monospace">{_esc(vtype)}</span>
  </div>""")

    body = f'<div style="max-width:960px">{"".join(rows)}</div>'
    return _collapsible_card("gap-breakdown", "Invoicing Gap Breakdown by Issue Type", body,
                              start_collapsed=True)


def _summary_table(stats: dict, kind: str) -> str:
    """Per-employee or per-project full summary table."""
    ranked = sorted(stats.items(), key=lambda x: (-x[1]["total"], -x[1]["HIGH"]))
    if not ranked:
        return ""

    th = "padding:8px 12px;font-size:0.7rem;text-transform:uppercase;letter-spacing:.04em;color:#6b7280;border-bottom:2px solid #e5e7eb"
    rows = []
    for rank, (name, s) in enumerate(ranked, 1):
        bg = "#fef2f2" if s["HIGH"] > 0 else "#fff"
        hi_style = "color:#dc2626;font-weight:700" if s["HIGH"] else "color:#9ca3af"
        rows.append(
            f'<tr style="background:{bg}">'
            f'<td style="padding:6px 12px;color:#9ca3af;font-size:0.8rem;text-align:center">{rank}</td>'
            f'<td style="padding:6px 12px;font-weight:600">{_esc(name)}</td>'
            f'<td style="padding:6px 12px;text-align:right;{hi_style}">{s["HIGH"]}</td>'
            f'<td style="padding:6px 12px;text-align:right;color:#d97706">{s["MEDIUM"]}</td>'
            f'<td style="padding:6px 12px;text-align:right;color:#2563eb">{s["LOW"]}</td>'
            f'<td style="padding:6px 12px;text-align:right;font-weight:700">{s["total"]}</td>'
            f'</tr>'
        )

    title = "Per-Employee Invoicing Risk" if kind == "employee" else "Per-Project Invoicing Risk"
    head_label = "Employee" if kind == "employee" else "Project"
    sub = f"{len(ranked)} {kind}s with invoicing gaps"
    card_id = f"summary-{'employee' if kind == 'employee' else 'project'}"

    body = f"""
  <div style="overflow-x:auto;overflow-y:auto;max-height:400px;border:1px solid #e5e7eb;border-radius:6px">
  <table>
    <thead style="position:sticky;top:0;z-index:1;background:#f9fafb"><tr>
      <th style="{th}">#</th>
      <th style="{th}">{head_label}</th>
      <th style="{th};text-align:right">High</th>
      <th style="{th};text-align:right">Medium</th>
      <th style="{th};text-align:right">Low</th>
      <th style="{th};text-align:right">Total</th>
    </tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  </div>"""
    return _collapsible_card(card_id, title, body, subtitle=sub, start_collapsed=True)


def _filter_controls(violations: list, grouped: dict) -> str:
    types = sorted({v.get("type") or "" for v in violations if v.get("type")})
    type_opts = "".join(
        f'<option value="{_esc(t)}">{_esc(_label(t))}</option>' for t in types
    )
    cat_opts = "".join(
        f'<option value="{_esc(cat)}">{_esc(cat)}</option>'
        for cat, vs in grouped.items() if vs
    )
    sel = "padding:6px 10px;border:1px solid #e5e7eb;border-radius:6px;font-size:0.82rem;background:#fff;color:#374151"
    inp = "padding:6px 10px;border:1px solid #e5e7eb;border-radius:6px;font-size:0.82rem;background:#fff;color:#374151;width:150px"
    filter_body = f"""
  <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
    <select id="f-cat" style="{sel}">
      <option value="">All categories</option>
      {cat_opts}
    </select>
    <select id="f-sev" style="{sel}">
      <option value="">All severities</option>
      <option value="HIGH">HIGH</option>
      <option value="MEDIUM">MEDIUM</option>
      <option value="LOW">LOW</option>
    </select>
    <select id="f-type" style="{sel}">
      <option value="">All violation types</option>
      {type_opts}
    </select>
    <input id="f-usr"  type="text" placeholder="Filter by employee…" style="{inp}">
    <input id="f-proj" type="text" placeholder="Filter by project…"  style="{inp}">
    <button id="f-clear" style="padding:6px 12px;border:1px solid #e5e7eb;border-radius:6px;font-size:0.82rem;background:#f9fafb;cursor:pointer">Clear</button>
    <span style="margin-left:4px;font-size:0.82rem;color:#6b7280">Showing <b id="visible-count">—</b> violations</span>
  </div>"""
    return _collapsible_card("filter-controls", "Filter Invoicing Gaps", filter_body,
                              subtitle="Filters apply across every category section below.",
                              start_collapsed=True)


_FILTER_JS = """
<script>
(function(){
  var rows = document.querySelectorAll('tr[data-category]:not([data-empty])');

  // ── collapse / expand helper ─────────────────────────────────────────────
  window.toggleSection = function(btn){
    var card    = btn.closest('.collapsible-card');
    var body    = card.querySelector('.section-body');
    var collapsed = body.style.display === 'none';
    body.style.display = collapsed ? '' : 'none';
    btn.textContent = collapsed ? '▾' : '▸';
    btn.title       = collapsed ? 'Collapse' : 'Expand';
  };

  function expandCard(card){
    var body = card.querySelector('.section-body');
    var btn  = card.querySelector('.collapse-toggle');
    if(body){ body.style.display = ''; }
    if(btn){ btn.textContent = '▾'; btn.title = 'Collapse'; }
  }
  function collapseCard(card){
    var body = card.querySelector('.section-body');
    var btn  = card.querySelector('.collapse-toggle');
    if(body){ body.style.display = 'none'; }
    if(btn){ btn.textContent = '▸'; btn.title = 'Expand'; }
  }

  // ── row filter (dropdowns + text inputs) ─────────────────────────────────
  function applyFilters(){
    var sev  = document.getElementById('f-sev').value;
    var typ  = document.getElementById('f-type').value;
    var usr  = document.getElementById('f-usr').value.toLowerCase().trim();
    var proj = document.getElementById('f-proj').value.toLowerCase().trim();
    var vis  = 0;
    rows.forEach(function(tr){
      var show = true;
      if(sev  && tr.dataset.severity !== sev)            show = false;
      if(typ  && tr.dataset.type     !== typ)            show = false;
      if(usr  && tr.dataset.user.indexOf(usr)    === -1) show = false;
      if(proj && tr.dataset.project.indexOf(proj)=== -1) show = false;
      tr.style.display = show ? '' : 'none';
      if(show) vis++;
    });
    var cnt = document.getElementById('visible-count');
    if(cnt) cnt.textContent = vis.toLocaleString();
  }

  ['f-sev','f-type'].forEach(function(id){
    var el = document.getElementById(id);
    if(el) el.addEventListener('change', applyFilters);
  });
  ['f-usr','f-proj'].forEach(function(id){
    var el = document.getElementById(id);
    if(el) el.addEventListener('input', applyFilters);
  });
  var clearBtn = document.getElementById('f-clear');
  if(clearBtn) clearBtn.addEventListener('click', function(){
    ['f-sev','f-type'].forEach(function(id){
      var el = document.getElementById(id); if(el) el.value = '';
    });
    ['f-usr','f-proj'].forEach(function(id){
      var el = document.getElementById(id); if(el) el.value = '';
    });
    // Reset pills to "All"
    document.querySelectorAll('#cat-pill-bar .pill').forEach(function(p){ p.classList.remove('pill-active'); });
    var allPill = document.querySelector('#cat-pill-bar .pill[data-cat=""]');
    if(allPill) allPill.classList.add('pill-active');
    document.querySelectorAll('.collapsible-card[data-category-card]').forEach(expandCard);
    applyFilters();
  });

  // ── pill navigation — collapse others, expand target ─────────────────────
  document.querySelectorAll('#cat-pill-bar .pill').forEach(function(pill){
    pill.addEventListener('click', function(){
      document.querySelectorAll('#cat-pill-bar .pill').forEach(function(p){ p.classList.remove('pill-active'); });
      pill.classList.add('pill-active');
      var cat = pill.dataset.cat;
      document.querySelectorAll('.collapsible-card[data-category-card]').forEach(function(card){
        if(!cat || card.dataset.categoryCard === cat){
          expandCard(card);
          if(cat) card.scrollIntoView({behavior:'smooth', block:'start'});
        } else {
          collapseCard(card);
        }
      });
      applyFilters();
    });
  });

  applyFilters();
})();
</script>"""


def _all_violations_rows(sorted_violations: list) -> str:
    rows = []
    for v in sorted_violations:
        sev   = v.get("severity", "LOW")
        bg    = SEV_BG.get(sev, "#fff")
        vtype = v.get("type") or ""
        cat   = _category_for(vtype)
        emp_l = (v.get("employee") or "").lower()
        prj_l = (v.get("project") or "").lower()
        rows.append(
            f'<tr style="background:{bg}" '
            f'data-severity="{_esc(sev)}" '
            f'data-type="{_esc(vtype)}" '
            f'data-category="{_esc(cat)}" '
            f'data-user="{_esc(emp_l)}" '
            f'data-project="{_esc(prj_l)}">'
            f'<td style="padding:6px 12px;white-space:nowrap;font-size:0.8rem;color:#9ca3af">{_esc(v.get("id") or "")}</td>'
            f'<td style="padding:6px 12px;white-space:nowrap">{_badge(sev)}</td>'
            f'<td style="padding:6px 12px;white-space:nowrap;font-weight:600">{_esc(_label(vtype))}</td>'
            f'<td style="padding:6px 12px;white-space:nowrap">{_esc(v.get("employee") or "—")}</td>'
            f'<td style="padding:6px 12px;white-space:nowrap">{_esc(v.get("project") or "—")}</td>'
            f'<td style="padding:6px 12px;white-space:nowrap">{_esc(v.get("date") or "—")}</td>'
            f'<td style="padding:6px 12px;text-align:right">{_esc(v.get("hours") if v.get("hours") is not None else "—")}</td>'
            f'<td style="padding:6px 12px;text-align:right;color:#6b7280">{_esc(v.get("confidence") if v.get("confidence") is not None else "—")}</td>'
            f'</tr>'
        )
    return "".join(rows)


def _detail_card(v: dict) -> str:
    """Detailed card for a single violation — keeps the full evidence list."""
    sev   = v.get("severity", "LOW")
    color = SEV_COLOR.get(sev, "#6b7280")
    bg    = SEV_BG.get(sev, "#fff")
    vid   = _esc(v.get("id") or "")
    vtype = v.get("type") or ""
    label = _esc(_label(vtype))

    employee = _esc(v.get("employee") or "—")
    project  = _esc(v.get("project") or "—")
    date     = _esc(v.get("date") or "—")
    hours    = v.get("hours")
    hours_s  = _esc(hours) if hours is not None else "—"
    conf     = v.get("confidence")
    conf_s   = _esc(conf) if conf is not None else "—"

    evidence = v.get("evidence") or []
    context  = v.get("context") or {}
    rec      = _esc(v.get("recommendation") or "")

    evidence_items = "".join(
        f'<li style="margin:3px 0;font-family:ui-monospace,Menlo,monospace;font-size:0.82rem;color:#374151">{_evidence_html(e)}</li>'
        for e in evidence
    ) or '<li style="color:#9ca3af;font-style:italic">No evidence recorded.</li>'

    if context:
        ctx_str = ", ".join(f"{_esc(k)}={_esc(json.dumps(val))}" for k, val in context.items())
    else:
        ctx_str = "—"

    meta_td = "padding:4px 10px;font-size:0.8rem;color:#374151"
    meta_th = "padding:4px 10px;font-size:0.72rem;text-transform:uppercase;letter-spacing:.04em;color:#9ca3af;text-align:left;font-weight:600"

    return f"""
<div id="{vid}" style="border:1px solid #e5e7eb;border-left:4px solid {color};border-radius:6px;margin-bottom:14px;overflow:hidden;background:#fff">
  <div style="padding:10px 16px;background:{bg};display:flex;align-items:center;gap:10px;flex-wrap:wrap;border-bottom:1px solid #e5e7eb">
    <span style="font-weight:700;color:#111827">{vid}</span>
    <span style="color:#6b7280">·</span>
    <span style="font-weight:700;color:{color}">{label}</span>
    {_badge(sev)}
    <span style="margin-left:auto;font-family:ui-monospace,Menlo,monospace;font-size:0.72rem;color:#6b7280;background:#fff;padding:2px 8px;border-radius:4px;border:1px solid #e5e7eb">{_esc(vtype)}</span>
  </div>
  <div style="padding:12px 16px">
    <table style="width:100%;border-collapse:collapse;margin:0 0 10px">
      <tr>
        <th style="{meta_th}">Employee</th>
        <th style="{meta_th}">Project</th>
        <th style="{meta_th}">Date</th>
        <th style="{meta_th};text-align:right">Hours</th>
        <th style="{meta_th};text-align:right">Confidence</th>
      </tr>
      <tr>
        <td style="{meta_td};font-weight:600">{employee}</td>
        <td style="{meta_td}">{project}</td>
        <td style="{meta_td};white-space:nowrap">{date}</td>
        <td style="{meta_td};text-align:right">{hours_s}</td>
        <td style="{meta_td};text-align:right">{conf_s}</td>
      </tr>
    </table>
    <div style="font-size:0.78rem;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:#6b7280;margin:8px 0 4px">Evidence</div>
    <ul style="margin:0;padding-left:1.2em">{evidence_items}</ul>
    <div style="margin-top:10px;font-size:0.8rem;color:#6b7280"><span style="font-weight:600;color:#374151">Context:</span> {ctx_str}</div>
    <div style="margin-top:10px;background:#eff6ff;border-left:3px solid #2563eb;padding:8px 12px;border-radius:4px;font-size:0.85rem;color:#1e3a8a">
      <span style="font-weight:700">Recommendation:</span> {rec}
    </div>
  </div>
</div>"""


def _detail_type_blocks(scoped_violations: list) -> str:
    """Render per-type detail blocks for one category's violations.

    Returns a sequence of <div>s (no outer .card wrapper) so the caller can
    embed them inside the parent category card.
    """
    grouped: dict = defaultdict(list)
    for v in scoped_violations:
        grouped[v.get("type") or "unknown"].append(v)

    # Order groups by worst severity, then by count desc
    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    def _group_key(item):
        vtype, vs = item
        worst = min(order.get(v.get("severity", "LOW"), 3) for v in vs)
        return (worst, -len(vs))

    blocks = []
    for vtype, vs in sorted(grouped.items(), key=_group_key):
        cards = "".join(_detail_card(v) for v in vs)
        blocks.append(f"""
<div style="margin-top:18px">
  <h3 style="margin:0 0 10px;font-size:0.92rem;font-weight:700;color:#111827">{_esc(_label(vtype))}
    <span style="font-weight:400;color:#6b7280;font-size:0.78rem;margin-left:8px">{len(vs)} violation{'s' if len(vs) != 1 else ''} &nbsp;·&nbsp; <code style="font-size:0.74rem;background:#f3f4f6;padding:1px 6px;border-radius:3px">{_esc(vtype)}</code></span>
  </h3>
  {cards}
</div>""")
    return "".join(blocks)


def _group_by_category(sorted_violations: list) -> dict:
    """Partition pre-sorted violations into category buckets, preserving order.

    Returns an insertion-ordered dict keyed by category name. Categories are
    inserted in CATEGORY_ORDER first; any "Other" bucket (unknown types) is
    appended last only if it actually has rows.
    """
    grouped: dict = {name: [] for name in CATEGORY_ORDER}
    other: list = []
    for v in sorted_violations:
        cat = _category_for(v.get("type") or "")
        if cat in grouped:
            grouped[cat].append(v)
        else:
            other.append(v)
    if other:
        grouped[CATEGORY_OTHER] = other
    return grouped


def _category_section(category: str, violations: list) -> str:
    """Render one category as a single .card containing table + detail blocks."""
    slug = _category_slug(category)
    subtitle = CATEGORY_SUBTITLE.get(category, "")
    count = len(violations)

    if violations:
        rows_html = _all_violations_rows(violations)
        details_html = _detail_type_blocks(violations)
    else:
        rows_html = (
            f'<tr data-category="{_esc(category)}" data-empty="1">'
            f'<td colspan="8" style="padding:14px 12px;color:#9ca3af;font-style:italic;text-align:center">'
            f'No violations in this category.'
            f'</td></tr>'
        )
        details_html = ""

    return f"""
<div class="card collapsible-card" id="{slug}" data-category-card="{_esc(category)}">
  <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:8px">
    <div>
      <h2 style="margin:0 0 4px;font-size:1.05rem;font-weight:700;color:#111827">
        {_esc(category)}
        <span style="font-weight:400;color:#6b7280;font-size:0.85rem;margin-left:8px">{count} gap{'s' if count != 1 else ''}</span>
      </h2>
      <p style="margin:0 0 0;font-size:0.82rem;color:#6b7280">{_esc(subtitle)}</p>
    </div>
    <button class="collapse-toggle" onclick="toggleSection(this)"
      title="Collapse"
      style="flex-shrink:0;margin-top:2px;background:none;border:1px solid #e5e7eb;border-radius:6px;
             padding:2px 8px;font-size:1rem;cursor:pointer;color:#6b7280;line-height:1.4">▾</button>
  </div>
  <div class="section-body" style="margin-top:14px">
    <div style="overflow-x:auto;overflow-y:auto;max-height:420px;border:1px solid #e5e7eb;border-radius:6px">
    <table>
      <thead style="position:sticky;top:0;z-index:1;background:#f9fafb"><tr>
        <th>ID</th><th>Severity</th><th>Type</th><th>Employee</th><th>Project</th><th>Date</th><th style="text-align:right">Hours</th><th style="text-align:right">Confidence</th>
      </tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
    </div>
    {details_html}
  </div>
</div>"""


def _category_sections(grouped: dict) -> str:
    return "".join(_category_section(cat, vs) for cat, vs in grouped.items())


def _category_pill_strip(grouped: dict) -> str:
    """Tab-style pill strip — clicking a pill shows only that category's section."""
    total = sum(len(vs) for vs in grouped.values())
    pill_style = (
        "display:inline-flex;align-items:center;gap:6px;padding:6px 13px;"
        "border-radius:999px;font-size:0.78rem;font-weight:600;cursor:pointer;"
        "border:1px solid;transition:background .15s,color .15s"
    )
    pills = [
        f'<button class="pill pill-active" data-cat="" '
        f'style="{pill_style};background:#1e3a8a;color:#fff;border-color:#1e3a8a">'
        f'All <span style="font-weight:700">{total}</span></button>'
    ]
    for cat, vs in grouped.items():
        n = len(vs)
        muted = n == 0
        bg = "#f3f4f6" if muted else "#eef2ff"
        fg = "#9ca3af" if muted else "#3730a3"
        border = "#e5e7eb" if muted else "#c7d2fe"
        pills.append(
            f'<button class="pill" data-cat="{_esc(cat)}" '
            f'style="{pill_style};background:{bg};color:{fg};border-color:{border}">'
            f'{_esc(cat)} <span style="font-weight:700">{n}</span></button>'
        )
    return f"""
  <div id="cat-pill-bar" style="display:flex;gap:8px;flex-wrap:wrap;margin-top:14px">
    {''.join(pills)}
  </div>"""


_SUPPRESSED_TYPES = {
    # Handled by Phase 3 LLM extraction — not an actionable business violation.
    "sow_parse_failure",
}


def _render_report(violations: list, fuzzy_mapping: list, data_range: dict, summary: str) -> str:
    """Render violations as a self-contained HTML report string."""
    violations = [v for v in violations if (v.get("type") or "") not in _SUPPRESSED_TYPES]
    severity_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}

    sorted_violations = sorted(
        violations,
        key=lambda v: (
            severity_order.get(v.get("severity", "LOW"), 2),
            v.get("type") or "",
            v.get("date") or "9999-99-99",
        ),
    )

    counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for v in violations:
        sev = v.get("severity", "LOW")
        if sev in counts:
            counts[sev] += 1

    user_stats    = _compute_user_stats(violations)
    project_stats = _compute_project_stats(violations)

    grouped_by_category = _group_by_category(sorted_violations)

    leaderboard_html         = _top10_leaderboard(user_stats, project_stats)
    chart_html               = _check_distribution_chart(violations)
    employee_table           = _summary_table(user_stats, "employee")
    project_table            = _summary_table(project_stats, "project")
    filter_controls_html     = _filter_controls(violations, grouped_by_category)
    category_pills_html      = _category_pill_strip(grouped_by_category)
    category_sections_html   = _category_sections(grouped_by_category)
    financial_kpis_html      = _financial_kpi_row(violations)
    invoicing_gap_card_html  = _project_invoicing_gap_card(violations)

    mapping_rows = "".join(
        '<tr><td style="padding:6px 12px;border-bottom:1px solid #f3f4f6">{}</td>'
        '<td style="padding:6px 12px;border-bottom:1px solid #f3f4f6">{}</td>'
        '<td style="padding:6px 12px;border-bottom:1px solid #f3f4f6;text-align:right;color:#6b7280">{}</td></tr>'.format(
            _esc(m.get("timesheet_name", "")),
            _esc(m.get("canonical_name", "")),
            _esc(m.get("confidence", "")),
        )
        for m in (fuzzy_mapping or [])
    )
    if not mapping_rows:
        mapping_rows = '<tr><td colspan="3" style="padding:10px 12px;color:#9ca3af;font-style:italic">No fuzzy mappings recorded.</td></tr>'

    # Derive date range from violations when the JSON doesn't carry it
    _dr_start = (data_range or {}).get("start")
    _dr_end   = (data_range or {}).get("end")
    if not _dr_start or not _dr_end:
        _dates = sorted(v.get("date") for v in violations if v.get("date"))
        if _dates:
            _dr_start = _dr_start or _dates[0]
            _dr_end   = _dr_end   or _dates[-1]
    start = _esc(_dr_start or "—")
    end   = _esc(_dr_end   or "—")
    generated = datetime.now().strftime("%Y-%m-%d")
    summary_escaped = _esc(summary or "")

    total = len(violations)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Revenue Intelligence Report — Invoicing Gaps — {generated}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: system-ui, -apple-system, sans-serif; margin: 0; padding: 24px; background: #f3f4f6; color: #111827; }}
  .card {{ background: #fff; border-radius: 10px; box-shadow: 0 1px 4px rgba(0,0,0,.08); padding: 24px; margin-bottom: 24px; }}
  h1 {{ margin: 0 0 4px; font-size: 1.4rem; }}
  .subtitle {{ color: #6b7280; font-size: 0.9rem; margin-bottom: 20px; }}
  .stat-grid {{ display: flex; gap: 16px; flex-wrap: wrap; }}
  .stat {{ flex: 1; min-width: 140px; border-radius: 8px; padding: 16px 20px; }}
  .stat .num {{ font-size: 2rem; font-weight: 800; }}
  .stat .lbl {{ font-size: 0.8rem; text-transform: uppercase; letter-spacing: .05em; margin-top: 2px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.875rem; }}
  thead tr {{ background: #f9fafb; }}
  th {{ padding: 10px 12px; text-align: left; font-size: 0.75rem; text-transform: uppercase; letter-spacing: .05em; color: #6b7280; border-bottom: 2px solid #e5e7eb; }}
  td {{ border-bottom: 1px solid #f3f4f6; }}
  tbody tr:hover {{ filter: brightness(0.97); }}
  h2 {{ font-size: 1rem; }}
  .summary-box {{ background: #fffbeb; border-left: 4px solid #f59e0b; padding: 12px 16px; border-radius: 4px; color: #374151; line-height: 1.55; font-size: 0.92rem; }}
  .pill {{ background:none; border:none; padding:0; margin:0; font:inherit; }}
  .pill-active {{ background:#1e3a8a !important; color:#fff !important; border-color:#1e3a8a !important; }}
  .collapse-toggle:hover {{ background:#f3f4f6 !important; }}
</style>
</head>
<body>

<div class="card">
  <h1>Revenue Intelligence Report — Invoicing Gaps</h1>
  <p class="subtitle">generated {generated} &nbsp;·&nbsp; data range: {start} → {end}</p>
  <div class="stat-grid">
    <div class="stat" style="background:#f0fdf4">
      <div class="num" style="color:#16a34a">{total:,}</div>
      <div class="lbl" style="color:#15803d">Total Violations</div>
    </div>
    <div class="stat" style="background:#fef2f2">
      <div class="num" style="color:#dc2626">{counts['HIGH']:,}</div>
      <div class="lbl" style="color:#b91c1c">High Severity</div>
    </div>
    <div class="stat" style="background:#fffbeb">
      <div class="num" style="color:#d97706">{counts['MEDIUM']:,}</div>
      <div class="lbl" style="color:#b45309">Medium Severity</div>
    </div>
    <div class="stat" style="background:#eff6ff">
      <div class="num" style="color:#2563eb">{counts['LOW']:,}</div>
      <div class="lbl" style="color:#1d4ed8">Low Severity</div>
    </div>
  </div>
  {financial_kpis_html}
  {category_pills_html}
</div>

<div class="card collapsible-card" id="executive-summary">
  <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:8px">
    <h2 style="margin:0;font-size:1rem;font-weight:700">Executive Summary</h2>
    <button class="collapse-toggle" onclick="toggleSection(this)" title="Collapse"
      style="flex-shrink:0;background:none;border:1px solid #e5e7eb;border-radius:6px;
             padding:2px 10px;font-size:1rem;cursor:pointer;color:#6b7280;line-height:1.4">▾</button>
  </div>
  <div class="section-body" style="margin-top:12px">
    <div class="summary-box">{summary_escaped}</div>
  </div>
</div>

{invoicing_gap_card_html}

{leaderboard_html}

{chart_html}

{employee_table}

{project_table}

{filter_controls_html}

{category_sections_html}

<div class="card collapsible-card" id="appendix-fuzzy">
  <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:8px">
    <div>
      <h2 style="margin:0;font-size:1rem;font-weight:700">Appendix — Project Name Reconciliation</h2>
      <p style="margin:4px 0 0;font-size:0.82rem;color:#6b7280">Timesheet project names fuzzy-mapped to canonical pm_projects names.</p>
    </div>
    <button class="collapse-toggle" onclick="toggleSection(this)" title="Expand"
      style="flex-shrink:0;margin-top:2px;background:none;border:1px solid #e5e7eb;border-radius:6px;
             padding:2px 10px;font-size:1rem;cursor:pointer;color:#6b7280;line-height:1.4">▸</button>
  </div>
  <div class="section-body" style="display:none;margin-top:14px">
    <div style="overflow-x:auto;border:1px solid #e5e7eb;border-radius:6px">
    <table>
      <thead><tr>
        <th>Timesheet Name</th>
        <th>Mapped To (pm_projects)</th>
        <th style="text-align:right">Confidence</th>
      </tr></thead>
      <tbody>{mapping_rows}</tbody>
    </table>
    </div>
  </div>
</div>

{_FILTER_JS}
</body>
</html>"""


def load_data() -> dict:
    """Load intermediate/loaded_data.json at call time (not import time)."""
    with open(INTERMEDIATE_PATH, "r") as f:
        return json.load(f)


# ─── Tool Definitions ────────────────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="get_all_project_names",
            description=(
                "Returns unique project names from each data source: "
                "kimai_timesheets, hr_assignments, pm_projects, and sows. "
                "Use this first before any cross-source comparison so you can "
                "build a fuzzy name-equivalence mapping that covers SOW titles too."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="lookup_employee_assignments",
            description=(
                "Returns all hr_assignments records for a given employee. "
                "Use to verify whether an employee is authorised for a project."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "employee": {
                        "type": "string",
                        "description": "Username / employee identifier (e.g. 'john')",
                    }
                },
                "required": ["employee"],
            },
        ),
        types.Tool(
            name="get_project_details",
            description=(
                "Returns the pm_projects record for a project name, including "
                "budget_hours, budget_cost, end_date, and status. "
                "Returns {missing: true, project: '...'} when not found."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name as it appears in pm_projects (e.g. 'Mobile App')",
                    }
                },
                "required": ["project"],
            },
        ),
        types.Tool(
            name="aggregate_project_hours",
            description=(
                "Aggregates total timesheet hours logged against a project "
                "and breaks them down by employee. "
                "Use to detect over-budget or unexpected logging."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name as it appears in kimai_timesheets",
                    }
                },
                "required": ["project"],
            },
        ),
        types.Tool(
            name="get_leave_and_activity",
            description=(
                "Returns leave record, Slack activity, and whether a timesheet "
                "entry exists for a specific employee on a specific date. "
                "Use to detect logging on leave days, or active Slack without a timesheet."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "employee": {
                        "type": "string",
                        "description": "Username / employee identifier",
                    },
                    "date": {
                        "type": "string",
                        "description": "Date in YYYY-MM-DD format",
                    },
                },
                "required": ["employee", "date"],
            },
        ),
        types.Tool(
            name="get_employee_details",
            description=(
                "Returns the hr_employees record for an employee, including "
                "role, rate, status, timezone, and contract_hrs. "
                "Use to verify hourly rates, active status, or contract hours."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "employee": {
                        "type": "string",
                        "description": "Username / employee identifier",
                    }
                },
                "required": ["employee"],
            },
        ),
        types.Tool(
            name="generate_report",
            description=(
                "Renders all violations as a self-contained HTML report and writes it to "
                "reports/violations.html. Call this as the final step after all checks are complete. "
                "Handles sorting (HIGH → MEDIUM → LOW, then by date), severity counts, "
                "Slack classification colouring, and the fuzzy name mapping appendix automatically."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "violations": {
                        "type": "array",
                        "description": "List of violation objects matching the schema in CLAUDE.md Section 6.",
                        "items": {"type": "object"},
                    },
                    "fuzzy_mapping": {
                        "type": "array",
                        "description": (
                            "List of {timesheet_name, canonical_name, confidence} objects "
                            "built during the fuzzy name matching step."
                        ),
                        "items": {"type": "object"},
                    },
                    "data_range": {
                        "type": "object",
                        "description": "Date range of the timesheet data: {start: 'YYYY-MM-DD', end: 'YYYY-MM-DD'}",
                        "properties": {
                            "start": {"type": "string"},
                            "end": {"type": "string"},
                        },
                    },
                    "summary": {
                        "type": "string",
                        "description": "2–4 sentence narrative of the most significant findings.",
                    },
                },
                "required": ["violations", "fuzzy_mapping", "data_range", "summary"],
            },
        ),
        types.Tool(
            name="get_guidelines",
            description=(
                "Returns HR policy guidelines: holiday lists, leave entitlements, timesheet rules. "
                "Checks the extraction cache first (populated by save_extraction). "
                "When cache misses, returns the regex result alongside raw_texts and "
                "extraction_needed=true — in that case read the raw_texts, extract the policy, "
                "then call save_extraction(content_hash, 'guidelines', data) to cache it "
                "so future runs skip re-extraction."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "section": {
                        "type": "string",
                        "enum": ["holidays", "leave_policy", "timesheet_policy", "all"],
                        "description": "Which section to return. Defaults to 'all'.",
                        "default": "all",
                    }
                },
                "required": [],
            },
        ),
        types.Tool(
            name="get_contractor_invoices",
            description=(
                "Returns parsed contractor invoice data for billing reconciliation. "
                "Each record contains invoice_id, contractor_name, period, project, "
                "hours_billed, rate, and total."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "contractor": {
                        "type": "string",
                        "description": "Optional employee username to filter by (fuzzy match on contractor_name).",
                    }
                },
                "required": [],
            },
        ),
        types.Tool(
            name="check_employee_billing_hours",
            description=(
                "Computes billing hour analysis for one employee: expected vs actual hours, "
                "gap percentage, corroborating evidence from emails and Slack."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "employee": {
                        "type": "string",
                        "description": "Username / employee identifier",
                    },
                    "period_start": {
                        "type": "string",
                        "description": "Start date of the billing period (YYYY-MM-DD)",
                    },
                    "period_end": {
                        "type": "string",
                        "description": "End date of the billing period (YYYY-MM-DD)",
                    },
                },
                "required": ["employee", "period_start", "period_end"],
            },
        ),
        types.Tool(
            name="get_rate_mismatches",
            description=(
                "Returns employees where Kimai hourly_rate differs from HR contracted rate, "
                "with financial impact calculated as delta × total hours logged."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "employee": {
                        "type": "string",
                        "description": "Optional username to filter results.",
                    }
                },
                "required": [],
            },
        ),
        types.Tool(
            name="get_sows",
            description=(
                "Returns parsed Statement-of-Work documents loaded from data/documents/sow/. "
                "Filter by client name substring and/or parse_status. "
                "Use parse_status='partial' or 'unrecognized_template' to find SOWs that "
                "failed to parse cleanly."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "client_filter": {
                        "type": "string",
                        "description": "Optional substring to filter by client name (case-insensitive).",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["ok", "partial", "unrecognized_template", "all"],
                        "description": "Filter by parse_status. Defaults to 'all'.",
                        "default": "all",
                    },
                },
                "required": [],
            },
        ),
        types.Tool(
            name="get_sow_for_project",
            description=(
                "Returns the SOW for a given project by fuzzy-matching the query against "
                "project_name_raw across all parsed SOWs. "
                "Returns {missing: true, project: '...'} when no plausible match is found."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name to look up (e.g. 'Quotera', 'Canvara Archway').",
                    }
                },
                "required": ["project"],
            },
        ),
        types.Tool(
            name="get_sow_resources",
            description=(
                "Returns every named resource across all parsed SOWs, together with the list "
                "of active hr_employees usernames. Use this to build the SOW-resource → "
                "username mapping in Step 3: reason over full names vs short usernames, "
                "corroborating evidence from emails/Slack/hr_assignments."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="run_deterministic_checks",
            description=(
                "Runs all deterministic validation checks from agents/checks.py and returns "
                "a list of violations. Covers: over_logging, archived_project, no_assignment, "
                "over_budget, logging_after_end_date, deactivated_employee_logging, "
                "logging_on_leave, logging_on_public_holiday, under_billing, "
                "under_billing_contractor, over_billing, over_billing_on_leave_or_holiday. "
                "Call this early in validation — results can be included directly in the final report."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="get_unauthorized_candidates",
            description=(
                "Returns raw (employee, project) candidates whose timesheet entries lack a "
                "matching hr_assignment. Each record has {employee, project, first_date, "
                "total_hours, days_logged, authorized_projects} — detection facts only, no "
                "severity or recommendation. Phase 3 classifies Slack evidence per candidate "
                "(CONFIRMING / CONTRADICTING / NEUTRAL) and emits one complete "
                "unauthorized_entry violation to judgment_violations.json."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="get_missing_timesheet_candidates",
            description=(
                "Returns raw (employee, working-day) candidates with Slack presence but no "
                "timesheet and no approved leave. Each record has {employee, date, "
                "primary_project, slack_messages, slack_reactions, slack_texts} — detection "
                "facts only. Phase 3 calibrates Slack signal quality (strong vs weak) per "
                "candidate and emits one complete missing_timesheet violation to "
                "judgment_violations.json."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="get_emails",
            description=(
                "Returns emails filtered by category with structured extraction support. "
                "Each email includes body_html, content_hash, and cached_extraction if "
                "previously saved via save_extraction. When cached_extraction is null, "
                "read body_html, extract {employee, project, date, extra fields}, then "
                "call save_extraction(content_hash, 'email', data) to cache it. "
                "Categories: assignment, escalation, extra_time, client_holiday, date_extension."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": (
                            "Filter by email category. "
                            "One of: assignment, escalation, extra_time, client_holiday, "
                            "date_extension. Omit to return all emails."
                        ),
                    },
                },
                "required": [],
            },
        ),
        types.Tool(
            name="save_extraction",
            description=(
                "Cache Claude Code's structured extraction of a document so it does not "
                "need to be re-extracted on subsequent runs. Key is content_hash "
                "(SHA-256 of raw_text), so the cache auto-invalidates when the source "
                "file changes. Use for SOW partial/unrecognized results, guideline "
                "documents, and contractor invoices where regex fell short."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "content_hash": {
                        "type": "string",
                        "description": "SHA-256 hex digest of the document's raw_text",
                    },
                    "doc_type": {
                        "type": "string",
                        "enum": ["sow", "guidelines", "invoice", "email", "slack"],
                        "description": "Document category",
                    },
                    "data": {
                        "type": "object",
                        "description": "The structured extraction result to cache",
                    },
                },
                "required": ["content_hash", "doc_type", "data"],
            },
        ),
        types.Tool(
            name="load_extraction",
            description=(
                "Retrieve a previously cached extraction by content hash. "
                "Returns {found: true, data: {...}} on hit, {found: false} on miss. "
                "Call this before extracting from raw_text to avoid redundant work."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "content_hash": {
                        "type": "string",
                        "description": "SHA-256 hex digest of the document's raw_text",
                    },
                },
                "required": ["content_hash"],
            },
        ),
        types.Tool(
            name="save_fuzzy_mapping",
            description=(
                "Persist the fuzzy project-name and SOW resource→username mappings to "
                "intermediate/fuzzy_mapping.json so they can be reused across validation "
                "phases without re-derivation. Call this after building the mappings in Step 3."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_map": {
                        "type": "object",
                        "description": (
                            "Dict mapping canonical project name → list of aliases found across "
                            "kimai_timesheets, hr_assignments, pm_projects, and SOWs. "
                            "Example: {\"Mobile App\": [\"mobile-app\", \"MobileApp\", \"Mobile Application\"]}"
                        ),
                        "additionalProperties": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "resource_map": {
                        "type": "object",
                        "description": (
                            "Dict mapping SOW resource name → resolved hr_employees username "
                            "(or null when ambiguous). "
                            "Example: {\"Sumit Verma\": \"sumitv\", \"Onkar\": null}"
                        ),
                        "additionalProperties": {"type": ["string", "null"]},
                    },
                },
                "required": ["project_map", "resource_map"],
            },
        ),
        types.Tool(
            name="load_fuzzy_mapping",
            description=(
                "Load previously saved fuzzy mappings from intermediate/fuzzy_mapping.json. "
                "Returns project_map, resource_map, and the saved_at timestamp. "
                "Returns empty maps when the file has not yet been created."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
    ]


# ─── Tool Handler ─────────────────────────────────────────────────────────────

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> dict:
    data = load_data()

    if name == "get_all_project_names":
        timesheet_projects = sorted(
            {r["project"] for r in data["timesheets"] if r.get("project") and isinstance(r["project"], str)},
            key=str.lower,
        )
        assignment_projects = sorted(
            {r["project"] for r in data["hr_assignments"] if r.get("project") and isinstance(r["project"], str)},
            key=str.lower,
        )
        pm_project_names = sorted(
            {r["name"] for r in data["pm_projects"] if r.get("name") and isinstance(r["name"], str)},
            key=str.lower,
        )
        sow_project_names = sorted(
            {r["project_name_raw"] for r in data.get("sows", [])
             if r.get("project_name_raw") and isinstance(r["project_name_raw"], str)},
            key=str.lower,
        )
        return {
            "kimai_timesheets": timesheet_projects,
            "hr_assignments": assignment_projects,
            "pm_projects": pm_project_names,
            "sows": sow_project_names,
        }

    elif name == "get_sows":
        sys.path.insert(0, os.path.join(_ROOT, "agents"))
        from extractor import _cache as _ext_cache, hash_text as _hash_text

        sows = data.get("sows") or []
        client_filter = (arguments.get("client_filter") or "").strip().lower()
        status_filter = (arguments.get("status") or "all").strip().lower()
        if client_filter:
            sows = [s for s in sows if client_filter in (s.get("client") or "").lower()]
        if status_filter != "all":
            sows = [s for s in sows if (s.get("parse_status") or "") == status_filter]

        result = []
        for s in sows:
            raw_text = s.get("raw_text") or ""
            content_hash = _hash_text(raw_text) if raw_text else None
            parse_status = s.get("parse_status") or "partial"

            # Check extraction cache for every SOW
            cached = _ext_cache.get(f"sow:{content_hash}") if content_hash else None
            if cached:
                # Overlay cached fields — regex result replaced with Claude's extraction
                row = {**s, **cached, "from_cache": True}
                row.pop("raw_text", None)
                result.append(row)
            elif parse_status != "ok":
                # Cache miss on a partial/unrecognized SOW — surface raw_text for extraction
                row = {k: v for k, v in s.items()}
                row["extraction_needed"] = True
                row["content_hash"] = content_hash
                result.append(row)
            else:
                # Regex parsed it fully — no raw_text needed in response
                row = {k: v for k, v in s.items() if k != "raw_text"}
                result.append(row)

        return {"sows": result, "count": len(result)}

    elif name == "get_sow_for_project":
        project_query = (arguments.get("project") or "").strip().lower()
        sows = data.get("sows") or []
        best = None
        best_score = 0
        for s in sows:
            raw = (s.get("project_name_raw") or "").lower()
            if not raw:
                continue
            # Exact match
            if raw == project_query:
                best = s
                break
            # Substring containment (either direction)
            if project_query in raw or raw in project_query:
                score = min(len(project_query), len(raw)) / max(len(project_query), len(raw))
                if score > best_score:
                    best_score = score
                    best = s
            else:
                # Token overlap
                q_tokens = set(project_query.split())
                r_tokens = set(raw.split())
                overlap = len(q_tokens & r_tokens) / max(len(q_tokens | r_tokens), 1)
                if overlap > best_score:
                    best_score = overlap
                    best = s
        if best is None or best_score < 0.25:
            return {"missing": True, "project": arguments.get("project")}

        sys.path.insert(0, os.path.join(_ROOT, "agents"))
        from extractor import _cache as _ext_cache, hash_text as _hash_text
        raw_text = best.get("raw_text") or ""
        content_hash = _hash_text(raw_text) if raw_text else None
        cached = _ext_cache.get(f"sow:{content_hash}") if content_hash else None
        if cached:
            return {**best, **cached, "from_cache": True, "raw_text": None}
        row = {k: v for k, v in best.items() if k != "raw_text"}
        if (best.get("parse_status") or "partial") != "ok" and content_hash:
            row["extraction_needed"] = True
            row["content_hash"] = content_hash
            row["raw_text"] = raw_text
        return row

    elif name == "get_sow_resources":
        sows = data.get("sows") or []
        resources = []
        for s in sows:
            for res in (s.get("resources") or []):
                resources.append({
                    "sow_reference": s.get("sow_reference"),
                    "project": s.get("project_name_raw"),
                    "client": s.get("client"),
                    "name": res.get("name"),
                    "role": res.get("role"),
                    "rate": res.get("rate"),
                    "allocation_pct": res.get("allocation_pct"),
                    "hours_per_month": res.get("hours_per_month"),
                })
        active_usernames = sorted(
            r.get("username") for r in data.get("hr_employees", [])
            if (r.get("status") or "").lower() == "active" and r.get("username")
        )
        return {"resources": resources, "active_usernames": active_usernames}

    elif name == "lookup_employee_assignments":
        employee = (arguments.get("employee") or "").strip().lower()
        results = [
            r for r in data["hr_assignments"]
            if (r.get("user") or "").strip().lower() == employee
        ]
        return {"employee": arguments.get("employee"), "assignments": results}

    elif name == "get_project_details":
        project = (arguments.get("project") or "").strip()
        for r in data["pm_projects"]:
            if (r.get("name") or "").strip() == project:
                return r
        return {"missing": True, "project": project}

    elif name == "aggregate_project_hours":
        project = (arguments.get("project") or "").strip()
        by_user: dict[str, float] = {}
        total = 0.0
        for r in data["timesheets"]:
            proj = r.get("project")
            if not isinstance(proj, str) or proj.strip() != project:
                continue
            user = r.get("user") or "unknown"
            hours = 0.0
            try:
                hours = float(r.get("hours") or 0)
            except (TypeError, ValueError):
                pass
            by_user[user] = round(by_user.get(user, 0.0) + hours, 4)
            total = round(total + hours, 4)
        return {"project": project, "total_hours": total, "by_user": by_user}

    elif name == "get_leave_and_activity":
        employee = (arguments.get("employee") or "").strip().lower()
        date = (arguments.get("date") or "").strip()

        def _str(val) -> str:
            return str(val).strip() if val is not None and not (isinstance(val, float)) else ""

        hr_leave_record = next(
            (
                r for r in data["hr_leave"]
                if _str(r.get("user")).lower() == employee
                and _str(r.get("date")) == date
            ),
            None,
        )
        calendar_leave_record = next(
            (
                r for r in data.get("calendar_leave", [])
                if _str(r.get("user")).lower() == employee
                and _str(r.get("date")) == date
            ),
            None,
        )
        slack = next(
            (
                r for r in data["slack_activity"]
                if _str(r.get("user")).lower() == employee
                and _str(r.get("date")) == date
            ),
            None,
        )
        has_timesheet = any(
            _str(r.get("user")).lower() == employee
            and _str(r.get("date")) == date
            for r in data["timesheets"]
        )
        # Add content_hash for Slack texts so Claude can cache its classification
        # (intent, mentioned_projects) via save_extraction / load_extraction.
        sys.path.insert(0, os.path.join(_ROOT, "agents"))
        from extractor import _cache as _ext_cache, hash_text as _hash_text
        slack_cache_hit = None
        slack_content_hash = None
        if slack and slack.get("texts"):
            slack_content_hash = _hash_text("|".join(slack["texts"]))
            cached = _ext_cache.get(f"slack:{slack_content_hash}")
            if cached:
                slack_cache_hit = cached

        return {
            "employee": arguments.get("employee"),
            "date": date,
            "hr_leave": hr_leave_record,
            "calendar_leave": calendar_leave_record,
            # backward-compat alias
            "leave": hr_leave_record,
            "slack_activity": {
                **(slack or {}),
                "content_hash": slack_content_hash,
                "cached_classification": slack_cache_hit,
            } if slack else None,
            "has_timesheet": has_timesheet,
        }

    elif name == "get_employee_details":
        employee = (arguments.get("employee") or "").strip().lower()
        record = next(
            (
                r for r in data["hr_employees"]
                if (r.get("username") or "").strip().lower() == employee
            ),
            None,
        )
        if record is None:
            return {"missing": True, "employee": arguments.get("employee")}
        return record

    elif name == "get_guidelines":
        section = (arguments.get("section") or "all").strip().lower()
        guidelines = data.get("guidelines") or {}
        raw_texts  = guidelines.pop("raw_texts", {})  # {fname: text}

        sys.path.insert(0, os.path.join(_ROOT, "agents"))
        from extractor import _cache as _ext_cache, hash_text as _hash_text

        # ── Per-file cache check ──────────────────────────────────────────────
        # Each guideline file gets its own cache key: guidelines:{hash(file_text)}
        # Changing one file invalidates only that file's entry; others stay cached.
        merged: dict = {}           # section data accumulated from cached files
        uncached: dict = {}         # {fname: {text, content_hash}} — need extraction

        for fname, text in raw_texts.items():
            content_hash = _hash_text(text)
            cached_file = _ext_cache.get(f"guidelines:{content_hash}")
            if cached_file:
                # Each file's cached value is a partial dict like {holidays: {...}}
                # or {leave_policy: {...}}. Deep-merge into merged.
                for k, v in cached_file.items():
                    if isinstance(v, dict) and isinstance(merged.get(k), dict):
                        merged[k] = {**merged[k], **v}
                    elif v:
                        merged[k] = v
            else:
                uncached[fname] = {"text": text, "content_hash": content_hash}

        if not uncached:
            # Full cache hit — every file was cached
            result = {**guidelines, **merged}
            if section != "all":
                result = {section: result.get(section)}
            return {**result, "extraction_needed": False, "from_cache": True}

        # ── Partial or full cache miss ────────────────────────────────────────
        # Return regex baseline + merged cached sections + uncached files for extraction.
        # Save each file separately: save_extraction(content_hash, "guidelines", {section: ...})
        regex_has_data = bool(
            guidelines.get("holidays", {}).get("fixed")
            or guidelines.get("holidays", {}).get("optional")
        )
        result = {**guidelines, **merged}
        if section != "all":
            result = {section: result.get(section)}
        return {
            **result,
            "extraction_needed": not regex_has_data or bool(uncached),
            "from_cache": False,
            # Files that still need extraction — keyed by filename
            "uncached_files": uncached,
        }

    elif name == "get_contractor_invoices":
        sys.path.insert(0, os.path.join(_ROOT, "agents"))
        from extractor import _cache as _ext_cache, hash_text as _hash_text

        contractor_filter = (arguments.get("contractor") or "").strip().lower()
        invoices = data.get("contractor_invoices") or []
        if contractor_filter:
            invoices = [
                inv for inv in invoices
                if contractor_filter in (inv.get("contractor_name") or "").lower()
            ]

        result = []
        for inv in invoices:
            raw_text = inv.get("raw_text") or ""
            content_hash = _hash_text(raw_text) if raw_text else None
            cached = _ext_cache.get(f"invoice:{content_hash}") if content_hash else None
            if cached:
                row = {**inv, **cached, "from_cache": True}
                row.pop("raw_text", None)
            else:
                # Regex result — include raw_text + hash so Claude can extract & cache
                row = {**inv, "extraction_needed": True, "content_hash": content_hash}
            result.append(row)

        return {"invoices": result, "count": len(result),
                **({"filter": contractor_filter} if contractor_filter else {})}

    elif name == "check_employee_billing_hours":
        from datetime import date as _date, timedelta as _timedelta

        employee = (arguments.get("employee") or "").strip().lower()
        period_start_str = (arguments.get("period_start") or "").strip()
        period_end_str = (arguments.get("period_end") or "").strip()

        def _str(val) -> str:
            return str(val).strip() if val is not None and not isinstance(val, float) else ""

        # Find employee details
        emp_record = next(
            (r for r in data["hr_employees"] if _str(r.get("username")).lower() == employee),
            None,
        )
        if emp_record is None:
            return {"error": f"Employee not found: {employee}"}

        contract_hrs = 8.0
        try:
            contract_hrs = float(emp_record.get("contract_hrs") or 8)
        except (TypeError, ValueError):
            pass

        # Parse period dates
        try:
            start_date = _date.fromisoformat(period_start_str)
            end_date = _date.fromisoformat(period_end_str)
        except ValueError as exc:
            return {"error": f"Invalid date format: {exc}"}

        # Build set of public holiday dates
        holiday_dates_set = set(data.get("holiday_dates") or [])

        # Count working days (weekdays minus public holidays) in period
        working_days = 0
        cur = start_date
        while cur <= end_date:
            if cur.weekday() < 5 and cur.isoformat() not in holiday_dates_set:
                working_days += 1
            cur += _timedelta(days=1)

        # Count leave days (hr_leave)
        hr_leave_days = sum(
            1 for r in data["hr_leave"]
            if _str(r.get("user")).lower() == employee
            and period_start_str <= _str(r.get("date")) <= period_end_str
            and (_str(r.get("status")).lower() in ("approved", "confirmed", ""))
        )
        # Count calendar leave days (calendar_leave) — may overlap with hr_leave
        cal_leave_days = sum(
            1 for r in data.get("calendar_leave", [])
            if _str(r.get("user")).lower() == employee
            and period_start_str <= _str(r.get("date")) <= period_end_str
            and _str(r.get("all_day")).lower() in ("true", "1", "yes")
            and _str(r.get("status")).lower() in ("confirmed", "approved", "")
        )
        # Use the max to avoid double-counting
        leave_days = max(hr_leave_days, cal_leave_days)

        expected_hours = (working_days - leave_days) * contract_hrs

        # Actual hours from timesheets
        actual_hours = sum(
            float(r.get("hours") or 0)
            for r in data["timesheets"]
            if _str(r.get("user")).lower() == employee
            and period_start_str <= _str(r.get("date")) <= period_end_str
        )
        actual_hours = round(actual_hours, 4)

        gap = round(expected_hours - actual_hours, 4)
        gap_pct = round((gap / expected_hours * 100) if expected_hours else 0, 2)

        # Check escalation emails mentioning this employee
        escalation_emails = [
            {"date": r.get("date"), "subject": r.get("subject"), "category": r.get("category")}
            for r in data.get("emails", [])
            if employee in (_str(r.get("from_email")) + _str(r.get("to_email")) + _str(r.get("body_html"))).lower()
            and _str(r.get("category")).lower() in ("escalation", "concern", "warning")
            and period_start_str <= _str(r.get("date")) <= period_end_str
        ]

        # Slack days with no timesheet
        slack_no_timesheet = []
        timesheet_dates = {
            _str(r.get("date"))
            for r in data["timesheets"]
            if _str(r.get("user")).lower() == employee
            and period_start_str <= _str(r.get("date")) <= period_end_str
        }
        for sa in data["slack_activity"]:
            if _str(sa.get("user")).lower() != employee:
                continue
            sa_date = _str(sa.get("date"))
            if not (period_start_str <= sa_date <= period_end_str):
                continue
            if (sa.get("messages") or 0) > 0 and sa_date not in timesheet_dates:
                slack_no_timesheet.append(sa_date)

        return {
            "employee": employee,
            "period_start": period_start_str,
            "period_end": period_end_str,
            "working_days": working_days,
            "leave_days": leave_days,
            "hr_leave_days": hr_leave_days,
            "calendar_leave_days": cal_leave_days,
            "expected_hours": expected_hours,
            "actual_hours": actual_hours,
            "gap_hours": gap,
            "gap_pct": gap_pct,
            "contract_hrs_per_day": contract_hrs,
            "escalation_emails": escalation_emails,
            "slack_days_without_timesheet": slack_no_timesheet,
        }

    elif name == "get_rate_mismatches":
        employee_filter = (arguments.get("employee") or "").strip().lower()
        mismatches = data.get("rate_mismatches") or []
        if not employee_filter:
            return {"mismatches": mismatches, "count": len(mismatches)}
        filtered = [m for m in mismatches if (m.get("employee") or "").lower() == employee_filter]
        return {"mismatches": filtered, "count": len(filtered), "filter": employee_filter}

    elif name == "run_deterministic_checks":
        from checks import run_all_checks
        violations = run_all_checks(data)
        return {"violations": violations, "count": len(violations)}

    elif name == "get_unauthorized_candidates":
        from candidates import find_unauthorized_candidates
        candidates = find_unauthorized_candidates(data)
        return {"candidates": candidates, "count": len(candidates)}

    elif name == "get_missing_timesheet_candidates":
        from candidates import find_missing_timesheet_candidates
        candidates = find_missing_timesheet_candidates(data)
        return {"candidates": candidates, "count": len(candidates)}

    elif name == "get_emails":
        sys.path.insert(0, os.path.join(_ROOT, "agents"))
        from extractor import _cache as _ext_cache, hash_text as _hash_text

        category_filter = (arguments.get("category") or "").strip().lower()
        emails = data.get("emails") or []
        if category_filter:
            emails = [e for e in emails if (e.get("category") or "").lower() == category_filter]

        result = []
        for email in emails:
            body_html = email.get("body_html") or ""
            content_hash = _hash_text(body_html) if body_html else None
            cached = _ext_cache.get(f"email:{content_hash}") if content_hash else None
            result.append({
                **email,
                "content_hash": content_hash,
                "cached_extraction": cached,
            })

        return {"emails": result, "count": len(result),
                **({"category": category_filter} if category_filter else {})}

    elif name == "save_extraction":
        sys.path.insert(0, os.path.join(_ROOT, "agents"))
        from extractor import _cache as _ext_cache
        content_hash = arguments["content_hash"]
        doc_type     = arguments["doc_type"]
        ext_data     = arguments["data"]
        cache_key    = f"{doc_type}:{content_hash}"
        _ext_cache.set(cache_key, ext_data)
        return {"saved": True, "key": cache_key}

    elif name == "load_extraction":
        sys.path.insert(0, os.path.join(_ROOT, "agents"))
        from extractor import _cache as _ext_cache
        content_hash = arguments["content_hash"]
        for doc_type in ("sow", "guidelines", "invoice", "email", "slack"):
            cached = _ext_cache.get(f"{doc_type}:{content_hash}")
            if cached is not None:
                return {"found": True, "doc_type": doc_type, "data": cached}
        return {"found": False}

    elif name == "save_fuzzy_mapping":
        project_map  = arguments.get("project_map") or {}
        resource_map = arguments.get("resource_map") or {}
        payload = {
            "project_map":  project_map,
            "resource_map": resource_map,
            "saved_at":     datetime.utcnow().isoformat() + "Z",
        }
        os.makedirs(os.path.dirname(FUZZY_MAPPING_PATH), exist_ok=True)
        with open(FUZZY_MAPPING_PATH, "w") as f:
            json.dump(payload, f, indent=2)
        return {
            "saved": True,
            "path": FUZZY_MAPPING_PATH,
            "project_entries": len(project_map),
            "resource_entries": len(resource_map),
        }

    elif name == "load_fuzzy_mapping":
        if not os.path.exists(FUZZY_MAPPING_PATH):
            return {
                "project_map": {},
                "resource_map": {},
                "saved_at": None,
                "found": False,
            }
        with open(FUZZY_MAPPING_PATH) as f:
            payload = json.load(f)
        return {
            "project_map":  payload.get("project_map", {}),
            "resource_map": payload.get("resource_map", {}),
            "saved_at":     payload.get("saved_at"),
            "found":        True,
        }

    elif name == "generate_report":
        violations   = arguments.get("violations") or []
        fuzzy_mapping = arguments.get("fuzzy_mapping") or []
        data_range   = arguments.get("data_range") or {}
        summary      = arguments.get("summary") or ""

        html_content = _render_report(violations, fuzzy_mapping, data_range, summary)
        os.makedirs(os.path.dirname(REPORTS_PATH), exist_ok=True)
        with open(REPORTS_PATH, "w", encoding="utf-8") as f:
            f.write(html_content)

        return {
            "path": REPORTS_PATH,
            "written": True,
            "total_violations": len(violations),
        }

    else:
        return {"error": f"Unknown tool: {name}"}


# ─── Entry Point ──────────────────────────────────────────────────────────────

async def main():
    import anyio
    from io import TextIOWrapper

    async def _filtered_stdin():
        """Yield stdin lines, skipping blank/whitespace-only lines.

        The MCP stdio protocol is newline-delimited JSON.  Some MCP clients
        (including Claude Code) occasionally emit bare '\\n' keepalive lines
        between messages.  Passing those to model_validate_json raises a
        pydantic ValidationError which the SDK logs as "Received exception
        from stream: … input_value='\\n'" and sends an "Internal Server Error"
        notification to the client.  Filtering them here prevents that noise.
        """
        raw = anyio.wrap_file(
            TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")
        )
        try:
            async for line in raw:
                if line.strip():
                    yield line
        except anyio.ClosedResourceError:
            pass

    async with stdio_server(stdin=_filtered_stdin()) as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
