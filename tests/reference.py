"""
Independent reference implementations of the four analytics endpoints.

These are deliberately slow and obvious: they load the documents and loop in Python,
applying the business rules literally. They share NO code with the aggregation pipelines
in app/main.py, so comparing the endpoints against them is a real differential test - the
best defence against a pipeline that looks right but is not.

Like the real analytics, they READ the stored derived values (work_hours, late_minutes,
half_day, ...); they never recompute them from punch times.
"""
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}


def _round(value, places):
    quant = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))


def _month_bounds(month):
    y, m = int(month[:4]), int(month[5:7])
    first = date(y, m, 1)
    nxt = date(y + (m == 12), (m % 12) + 1, 1)
    return first.isoformat(), (nxt - timedelta(days=1)).isoformat()


def _is_weekday(date_str):
    return date.fromisoformat(date_str).weekday() < 5


def _present_value(log, weekday_only):
    if log["status"] not in PRESENCE:
        return 0.0
    if weekday_only and not _is_weekday(log["date"]):
        return 0.0
    return 0.5 if log.get("half_day", False) else 1.0


def _working_days(joined_on, first, last):
    start = max(joined_on, first)
    if start > last:
        return 0
    cur, end = date.fromisoformat(start), date.fromisoformat(last)
    n = 0
    while cur <= end:
        if cur.weekday() < 5:
            n += 1
        cur += timedelta(days=1)
    return n


def employee_monthly(db, emp_code, month):
    emp = db.employees.find_one({"emp_code": emp_code})
    assert emp is not None
    first, last = _month_bounds(month)
    logs = [l for l in db.attendance_logs.find({"emp_code": emp_code})
            if first <= l["date"] <= last]
    present_days = sum(_present_value(l, weekday_only=True) for l in logs)
    leave_days = sum(1 for l in logs if l["status"] == "LEAVE")
    late_count = sum(1 for l in logs if (l.get("late_minutes") or 0) > 0)
    total_late = sum((l.get("late_minutes") or 0) for l in logs)
    total_ot = sum((l.get("overtime_minutes") or 0) for l in logs)
    working_days = _working_days(emp["joined_on"], first, last)
    pct = _round(present_days * 100 / working_days, 2) if working_days else None
    return {
        "emp_code": emp_code, "month": month,
        "working_days": working_days,
        "present_days": float(present_days),
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": pct,
    }


def department_summary(db, month, department=None):
    first, last = _month_bounds(month)
    emps = [e for e in db.employees.find() if e["joined_on"] <= last
            and (department is None or e["department"] == department)]
    logs_by_emp = {}
    for l in db.attendance_logs.find():
        logs_by_emp.setdefault(l["emp_code"], []).append(l)

    by_dept = {}
    for e in emps:
        d = by_dept.setdefault(e["department"], {
            "headcount": 0, "present_days": 0.0, "late_count": 0, "total_late_minutes": 0,
            "leave_count": 0, "on_duty_count": 0, "work_sum": Decimal(0), "work_count": 0,
        })
        d["headcount"] += 1
        for l in logs_by_emp.get(e["emp_code"], []):
            if not (first <= l["date"] <= last):
                continue
            d["present_days"] += _present_value(l, weekday_only=True)
            if (l.get("late_minutes") or 0) > 0:
                d["late_count"] += 1
            d["total_late_minutes"] += (l.get("late_minutes") or 0)
            if l["status"] == "LEAVE":
                d["leave_count"] += 1
            if l["status"] == "ON_DUTY":
                d["on_duty_count"] += 1
            if l["status"] in PRESENCE and l.get("work_hours") is not None:
                d["work_sum"] += Decimal(str(l["work_hours"]))
                d["work_count"] += 1

    items = []
    for dept in sorted(by_dept):
        d = by_dept[dept]
        avg = None
        if d["work_count"]:
            avg = float((d["work_sum"] / Decimal(d["work_count"])).quantize(Decimal("0.01"), ROUND_HALF_UP))
        items.append({
            "department": dept, "headcount": d["headcount"],
            "present_days": float(d["present_days"]), "avg_work_hours": avg,
            "late_count": d["late_count"], "total_late_minutes": d["total_late_minutes"],
            "leave_count": d["leave_count"], "on_duty_count": d["on_duty_count"],
        })
    return {"month": month, "items": items}


def late_leaderboard(db, month, limit=10, department=None):
    first, last = _month_bounds(month)
    emps = {e["emp_code"]: e for e in db.employees.find()}
    totals = {}
    for l in db.attendance_logs.find():
        if not (first <= l["date"] <= last):
            continue
        if (l.get("late_minutes") or 0) <= 0:
            continue
        emp = emps.get(l["emp_code"])
        if emp is None:
            continue                                  # orphan log
        if department is not None and emp["department"] != department:
            continue
        t = totals.setdefault(l["emp_code"], {"total": 0, "count": 0, "emp": emp})
        t["total"] += l["late_minutes"]
        t["count"] += 1

    entries = list(totals.items())
    ranked = []
    for code, t in entries:
        rank = 1 + sum(1 for _, o in entries if o["total"] > t["total"])  # competition ranking
        ranked.append((rank, code, t))
    ranked = [r for r in ranked if r[0] <= limit]
    ranked.sort(key=lambda r: (-r[2]["total"], r[1]))     # total desc, emp_code asc
    items = [{
        "rank": rank, "emp_code": code, "name": t["emp"]["name"],
        "department": t["emp"]["department"],
        "total_late_minutes": t["total"], "late_count": t["count"],
    } for rank, code, t in ranked]
    return {"month": month, "items": items}


def department_trend(db, department, from_str, to_str):
    emps = [e for e in db.employees.find() if e["department"] == department]
    codes = {e["emp_code"] for e in emps}
    # per-day present/late for the department
    by_date = {}
    for l in db.attendance_logs.find():
        if l["emp_code"] not in codes:
            continue
        d = by_date.setdefault(l["date"], {"present": 0.0, "late": 0})
        d["present"] += _present_value(l, weekday_only=False)
        if (l.get("late_minutes") or 0) > 0:
            d["late"] += 1

    start, end = date.fromisoformat(from_str), date.fromisoformat(to_str)
    rows = []
    raw_rates = []
    cur = start
    while cur <= end:
        ds = cur.isoformat()
        is_wd = cur.weekday() < 5
        headcount = sum(1 for e in emps if e["joined_on"] <= ds)
        present = by_date.get(ds, {}).get("present", 0.0)
        late = by_date.get(ds, {}).get("late", 0)
        rate_raw = (present / headcount) if (is_wd and headcount > 0) else None
        raw_rates.append(rate_raw)
        rows.append({"date": ds, "is_working_day": is_wd, "headcount": headcount,
                     "present_count": float(present), "late_count": late, "_rate_raw": rate_raw})
        cur += timedelta(days=1)

    items = []
    for i, row in enumerate(rows):
        window = [r for r in raw_rates[max(0, i - 6): i + 1] if r is not None]
        mov = (sum(window) / len(window)) if window else None
        items.append({
            "date": row["date"], "is_working_day": row["is_working_day"],
            "headcount": row["headcount"], "present_count": row["present_count"],
            "late_count": row["late_count"],
            "attendance_rate": None if row["_rate_raw"] is None else _round(row["_rate_raw"], 4),
            "moving_avg_7d": None if mov is None else _round(mov, 4),
        })
    return {"department": department, "items": items}
