"""
Deterministic attendance data generator.

Builds a dataset in MongoDB ONLY (it never writes a dump file) that exercises every
awkward case the business rules describe, so the differential tests and the 100k
explain/perf check have something realistic to run against:

  * ties in late totals at several ranks, including at the `limit` cutoff
  * weekend records (which still add to late/overtime totals)
  * employees with no logs at all
  * mid-month joiners, and employees who join AFTER the test period
  * overnight shifts (22:00-06:00)
  * legacy documents with no `half_day` / no `history`
  * logs whose employee does not exist (orphans)
  * missing weekdays (gaps)
  * ABSENT and LEAVE records
  * open records (punched in, not out)
  * half days
  * a department where nobody has joined yet (headcount 0 for 2026)

Usage:
    python scripts/generate_data.py                 # ~100k logs into $MONGO_DB
    python scripts/generate_data.py --scale small    # a few thousand (fast)

The derived fields are filled with the app's own rule engine (app.main.compute_derived),
exactly as a punch-out would, so the stored values are self-consistent. The analytics are
defined OVER those stored values, so the differential tests compare the aggregation logic,
not the rule engine (which has its own unit tests).
"""
from __future__ import annotations

import argparse
import os
import random
import sys
from datetime import date, datetime, timedelta, timezone

# Allow running as a plain script from anywhere in the repo.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from pymongo import MongoClient

from app.main import IST, attendance_date, compute_derived, dt_to_ms  # noqa: E402

DEPARTMENTS = ["Engineering", "Sales", "Support", "Finance", "People"]


def _instant(day: date, hh: int, mm: int, ss: int = 0, day_offset: int = 0) -> datetime:
    """An IST wall-clock time on `day` (+offset days), returned as a UTC-aware instant
    truncated to whole seconds (what the API would store)."""
    local = datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=IST) + timedelta(days=day_offset)
    return local.astimezone(timezone.utc).replace(microsecond=0)


def _make_record(emp, day, status, punch_in, punch_out, *, legacy=False):
    """Build one attendance_logs document with correct stored derived values."""
    ss, se = emp["shift_start"], emp["shift_end"]
    record_date = attendance_date(punch_in, ss, se) if punch_in is not None else day.isoformat()
    derived = compute_derived(status, punch_in, punch_out, ss, se, record_date)
    doc = {
        "emp_code": emp["emp_code"],
        "date": record_date,
        "status": status,
        "punch_in": punch_in,
        "punch_out": punch_out,
        "work_hours": derived["work_hours"],
        "late_minutes": derived["late_minutes"],
        "overtime_minutes": derived["overtime_minutes"],
        "half_day": derived["half_day"],
        "history": [],
    }
    if legacy:
        # Older records simply omit these two fields entirely.
        doc.pop("half_day")
        doc.pop("history")
    return doc


def build_dataset(db, *, scale="small", seed=12345):
    """Populate `db` deterministically. Returns the list of employee docs created."""
    rng = random.Random(seed)
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})

    if scale == "large":
        n_bulk_emp, n_days = 240, 420       # ~100k logs
    else:
        n_bulk_emp, n_days = 24, 150        # a few thousand logs (fast differential tests)

    employees = []
    logs = []
    emp_counter = [0]

    def add_employee(department, joined_on, *, shift=("09:30", "18:30"), has_logs=True, bulk=True):
        emp_counter[0] += 1
        code = f"EMP{emp_counter[0]:05d}"
        emp = {
            "emp_code": code, "name": f"Person {emp_counter[0]}",
            "email": f"person{emp_counter[0]}@example.com",
            "department": department, "shift_start": shift[0], "shift_end": shift[1],
            "joined_on": joined_on,
            "created_at": _instant(date.fromisoformat(joined_on), 9, 0),
        }
        emp["_has_logs"] = has_logs
        emp["_bulk"] = bulk          # whether the bulk random-log loop also fills this person
        employees.append(emp)
        return emp

    # ----- Structural fixtures (always present, both scales) ------------------
    # 1) A normal engineering team joining early 2026.
    normal = [add_employee("Engineering", "2026-01-05") for _ in range(4)]
    # 2) A mid-month joiner (joins 2026-07-13, so part of July only).
    mid_joiner = add_employee("Engineering", "2026-07-13")
    # 3) Someone with ZERO logs.
    add_employee("Sales", "2026-01-10", has_logs=False)
    # 4) Overnight-shift employees.
    overnighters = [add_employee("Support", "2026-03-10", shift=("22:00", "06:00")) for _ in range(2)]
    # 5) A department where everyone joins AFTER 2026 (headcount 0 for any 2026 month).
    add_employee("Finance", "2027-02-01", has_logs=False)
    add_employee("Finance", "2027-03-01", has_logs=False)
    # 6) A department built to produce late-total TIES in 2026-08 (ranks 1,2,2,4 and a tie
    #    at a limit=3 cutoff). Each person gets N late records of exactly 20 minutes.
    tie_people = [add_employee("People", "2026-01-01", bulk=False) for _ in range(5)]
    tie_late_counts = [5, 3, 3, 2, 2]        # totals: 100, 60, 60, 40, 40
    aug_weekdays = [date(2026, 8, d) for d in range(1, 29) if date(2026, 8, d).weekday() < 5]
    for emp, k in zip(tie_people, tie_late_counts):
        for day in aug_weekdays[:k]:
            # 20 late minutes exactly: 09:50:30 IST is 1230 s after a 09:30 shift start.
            pin = _instant(day, 9, 50, 30)
            pout = _instant(day, 18, 30)
            logs.append(_make_record(emp, day, "PRESENT", pin, pout))

    # ----- Bulk random employees ---------------------------------------------
    for _ in range(n_bulk_emp):
        dept = rng.choice(DEPARTMENTS[:3])
        joined = date(2026, 1, 1) + timedelta(days=rng.randint(0, 120))
        add_employee(dept, joined.isoformat())

    # ----- Bulk random logs ----------------------------------------------------
    start = date(2026, 6, 1)
    for emp in employees:
        if not emp.get("_has_logs") or not emp.get("_bulk", True):
            continue
        joined = date.fromisoformat(emp["joined_on"])
        is_overnight = emp["shift_end"] <= emp["shift_start"]
        for offset in range(n_days):
            day = start + timedelta(days=offset)
            if day < joined:
                continue
            # Skip ~25% of days (missing weekdays / gaps).
            if rng.random() < 0.25:
                continue
            roll = rng.random()
            if roll < 0.08:
                logs.append(_make_record(emp, day, "LEAVE", None, None))
                continue
            if roll < 0.14:
                logs.append(_make_record(emp, day, "ABSENT", None, None))
                continue
            status = rng.choice(["PRESENT", "PRESENT", "WFH", "ON_DUTY"])
            if is_overnight:
                # punch-in around 22:00 IST, out around 06:00 IST next day
                pin = _instant(day, 22, rng.randint(0, 20))
                legacy = rng.random() < 0.1
                if rng.random() < 0.12:     # open record
                    logs.append(_make_record(emp, day, status, pin, None, legacy=legacy))
                    continue
                pout = _instant(day, 6, rng.randint(0, 50), day_offset=1)
                logs.append(_make_record(emp, day, status, pin, pout, legacy=legacy))
            else:
                # punch-in sometimes late (to populate late stats, incl. weekends).
                late_push = rng.choice([0, 0, 0, 12, 25, 40, 70])
                pin = _instant(day, 9, 30) + timedelta(minutes=late_push)
                legacy = rng.random() < 0.1
                if rng.random() < 0.1:      # open record
                    logs.append(_make_record(emp, day, status, pin, None, legacy=legacy))
                    continue
                # sometimes a half day (short), sometimes overtime
                if rng.random() < 0.12:
                    pout = pin + timedelta(hours=3, minutes=rng.randint(0, 40))     # half day
                else:
                    pout = _instant(day, 18, 30) + timedelta(minutes=rng.choice([0, 5, 35, 60]))
                    if pout <= pin:
                        pout = pin + timedelta(hours=8)
                logs.append(_make_record(emp, day, status, pin, pout, legacy=legacy))

    # ----- Orphan logs (emp_code with no employee) ----------------------------
    for i in range(5):
        day = date(2026, 8, 3 + i)
        ghost = {"emp_code": f"GHOST{i:03d}", "shift_start": "09:30", "shift_end": "18:30"}
        pin = _instant(day, 10, 5)
        pout = _instant(day, 18, 40)
        logs.append(_make_record(ghost, day, "PRESENT", pin, pout))

    # ----- Write ---------------------------------------------------------------
    clean_emps = [{k: v for k, v in e.items() if not k.startswith("_")} for e in employees]
    db.employees.insert_many(clean_emps)
    # Insert logs in chunks to keep memory bounded at large scale.
    for i in range(0, len(logs), 5000):
        db.attendance_logs.insert_many(logs[i:i + 5000])
    return clean_emps, len(logs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scale", choices=["small", "large"], default="large")
    parser.add_argument("--seed", type=int, default=12345)
    args = parser.parse_args()

    load_dotenv()
    uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    dbname = os.getenv("MONGO_DB", "attendance_db")
    db = MongoClient(uri, tz_aware=True)[dbname]
    emps, n_logs = build_dataset(db, scale=args.scale, seed=args.seed)
    print(f"seeded {len(emps)} employees and {n_logs} attendance logs into '{dbname}' (scale={args.scale})")


if __name__ == "__main__":
    main()
