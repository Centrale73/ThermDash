"""Schedule enumeration and temporal coverage for n8n triggers.

runs_per_year comes from enumerating the cron expression over a calendar year.
Coverage K is a *service* metric: the fraction of the year whose arrivals fall
inside at least one run's look-back window (e.g. Gmail ``newer_than:1d``).
Unprocessed items consume no inference energy, so K never enters eta.
"""
from __future__ import annotations

import datetime as dt
import math
import re

from .graph import rtype


def _cron_field(spec: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, st = part.split("/")
            step = int(st)
        if part in ("*", "?"):
            a, b = lo, hi
        elif "-" in part:
            a, b = map(int, part.split("-"))
        else:
            a = b = int(part)
        out.update(range(a, b + 1, step))
    return out


def cron_run_times(expr: str, year: int) -> list[dt.datetime]:
    """All fire times of a 5- or 6-field cron expression within ``year``."""
    f = expr.split()
    if len(f) == 6:  # n8n allows a leading seconds field
        f = f[1:]
    mi, hr, dom, mon, dow = f
    M, H = sorted(_cron_field(mi, 0, 59)), sorted(_cron_field(hr, 0, 23))
    D, MO = _cron_field(dom, 1, 31), _cron_field(mon, 1, 12)
    W = {d % 7 for d in _cron_field(dow, 0, 7)}
    dom_r, dow_r = dom not in ("*", "?"), dow not in ("*", "?")
    out, day = [], dt.date(year, 1, 1)
    while day.year == year:
        cw = (day.weekday() + 1) % 7  # cron: 0 = Sunday
        if day.month in MO:
            ok = (day.day in D or cw in W) if (dom_r and dow_r) else (day.day in D and cw in W)
            if ok:
                out += [dt.datetime(year, day.month, day.day, h, m) for h in H for m in M]
        day += dt.timedelta(days=1)
    return out


def cron_runs_per_year(expr: str, year: int) -> int:
    return len(cron_run_times(expr, year))


def lookback_minutes(query: str | None) -> int | None:
    """Parse Gmail ``newer_than:Nd|Nh|Nm|Ny`` into minutes."""
    m = re.search(r"newer_than:(\d+)([dhmy])", query or "")
    if not m:
        return None
    n, u = int(m.group(1)), m.group(2)
    return n * {"h": 60, "d": 1440, "m": 43200, "y": 525600}[u]


def temporal_coverage(times: list[dt.datetime], window_min: int, year: int) -> dict:
    """Fraction of minutes in ``year`` covered by [t - window, t) for some run t.

    Returns uniform-arrival coverage, business-hours coverage (Mon-Fri 09-18) and
    window redundancy (mean number of runs that see an arrival, given it is seen).
    """
    start = dt.datetime(year, 1, 1)
    total = int((dt.datetime(year + 1, 1, 1) - start).total_seconds() // 60)
    cov = bytearray(total)
    for t in times:
        b = int((t - start).total_seconds() // 60)
        a = max(0, b - window_min)
        cov[a:b] = b"\x01" * (b - a)
    # Ignore the first window: arrivals before the year's first run look back into
    # the previous year, which this enumeration does not include.
    skip = int((times[0] - start).total_seconds() // 60) if times else 0
    uni = sum(cov[skip:]) / max(1, total - skip)
    biz_tot = biz_cov = 0
    for i in range(skip, total, 60):  # hourly sampling is exact for minute-0 crons
        ts = start + dt.timedelta(minutes=i)
        if ts.weekday() < 5 and 9 <= ts.hour < 18:
            biz_tot += 1
            biz_cov += cov[i]
    covered = max(1, sum(cov[skip:]))
    window_sum = sum(min(window_min, int((t - start).total_seconds() // 60)) for t in times[1:])
    return {"uniform": uni, "business_hours": biz_cov / max(1, biz_tot),
            "uncovered_hours_per_year": (total - skip - sum(cov[skip:])) / 60,
            # >1 means overlapping windows: still-unread items are re-sent to the model
            # (pure prefill waste). This one DOES cost energy, unlike a coverage gap.
            "redundancy": window_sum / covered}


def schedule_info(wf: dict, year: int) -> dict:
    info = {"triggers": [], "runs_per_year": None, "basis": None, "cron": None}
    for n in wf.get("nodes", []):
        t = rtype(n)
        if t.endswith("Trigger") or t == "webhook":
            info["triggers"].append({"name": n.get("name"), "type": t})
        if t != "scheduleTrigger":
            continue
        for iv in ((n.get("parameters", {}).get("rule") or {}).get("interval") or [{}]):
            field = iv.get("field", "days")
            if field == "cronExpression":
                info["cron"] = iv["expression"]
                info["runs_per_year"] = cron_runs_per_year(iv["expression"], year)
                info["basis"] = f"cron '{iv['expression']}' enumerated over {year}"
            elif field == "days":
                info["runs_per_year"] = math.floor(365 / int(iv.get("daysInterval", 1)))
                info["basis"] = "daily interval"
            elif field == "hours":
                info["runs_per_year"] = math.floor(8760 / int(iv.get("hoursInterval", 1)))
                info["basis"] = "hourly interval"
            elif field == "minutes":
                info["runs_per_year"] = math.floor(525600 / int(iv.get("minutesInterval", 5)))
                info["basis"] = "minute interval"
            elif field == "weeks":
                k = len(iv.get("triggerAtDay", [1]) or [1])
                info["runs_per_year"] = 52 * k // int(iv.get("weeksInterval", 1))
                info["basis"] = "weekly interval"
    if info["runs_per_year"] is None:
        info["basis"] = "event-driven trigger: runs_per_year must be supplied in the config"
    return info
