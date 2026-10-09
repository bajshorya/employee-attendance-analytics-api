"""
Unit tests for the pure business-rule helpers (R1 - R5).

These need no database: they import the helper functions directly and check them
against (a) the worked examples in the problem statement and (b) the stored derived
values of all nine DATA_MODEL.md sample documents. If any of these fail, the rule
engine is wrong and every endpoint built on it is wrong.
"""
from datetime import datetime, timezone
from decimal import Decimal

from app.main import (
    attendance_date,
    compute_derived,
    compute_half_day,
    compute_late_minutes,
    compute_overtime_minutes,
    compute_work_hours,
    is_overnight,
    ms_to_dt,
    dt_to_ms,
)


def utc(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Time conversion + truncation (R1)
# --------------------------------------------------------------------------- #
def test_ms_truncates_to_whole_seconds():
    # 2026-07-06T04:35:00.900Z -> the .900 ms is dropped.
    ms = dt_to_ms(utc(2026, 7, 6, 4, 35, 0)) + 900
    assert dt_to_ms(ms_to_dt(ms)) == dt_to_ms(utc(2026, 7, 6, 4, 35, 0))


def test_ms_round_trip():
    dt = utc(2026, 7, 6, 4, 35, 0)
    assert ms_to_dt(dt_to_ms(dt)) == dt


# --------------------------------------------------------------------------- #
# R1: attendance date (incl. overnight)
# --------------------------------------------------------------------------- #
def test_attendance_date_day_shift():
    # 03:58Z = 09:28 IST -> same IST day.
    assert attendance_date(utc(2026, 7, 6, 3, 58), "09:30", "18:30") == "2026-07-06"


def test_attendance_date_after_midnight_ist_is_still_prev_ist_day_for_day_shift():
    # 20:30Z on the 5th = 02:00 IST on the 6th -> the 6th for a normal day shift.
    assert attendance_date(utc(2026, 7, 5, 20, 30), "09:30", "18:30") == "2026-07-06"


def test_attendance_date_overnight_evening_punch():
    # 16:25Z = 21:55 IST, overnight shift -> the day the shift started (sample 7).
    assert attendance_date(utc(2026, 7, 6, 16, 25), "22:00", "06:00") == "2026-07-06"


def test_attendance_date_overnight_early_morning_punch_belongs_to_previous_day():
    # 20:30Z on the 6th = 02:00 IST on the 7th, earlier than shift_end 06:00 -> previous day.
    assert attendance_date(utc(2026, 7, 6, 20, 30), "22:00", "06:00") == "2026-07-06"


def test_attendance_date_overnight_exactly_shift_end_stays_same_day():
    # 00:30Z on the 7th = 06:00 IST exactly; strictly-earlier test -> NOT previous day.
    assert attendance_date(utc(2026, 7, 7, 0, 30), "22:00", "06:00") == "2026-07-07"


def test_is_overnight():
    assert is_overnight("22:00", "06:00")
    assert not is_overnight("09:30", "18:30")


# --------------------------------------------------------------------------- #
# R2: late minutes (spec examples)
# --------------------------------------------------------------------------- #
def test_late_boundary_examples():
    # shift 09:30 IST == 04:00Z.
    assert compute_late_minutes(utc(2026, 7, 6, 4, 10, 0), "2026-07-06", "09:30") == 0   # 09:40:00
    assert compute_late_minutes(utc(2026, 7, 6, 4, 10, 1), "2026-07-06", "09:30") == 10  # 09:40:01
    assert compute_late_minutes(utc(2026, 7, 6, 4, 10, 59), "2026-07-06", "09:30") == 10  # 09:40:59
    assert compute_late_minutes(utc(2026, 7, 6, 4, 45, 59), "2026-07-06", "09:30") == 45  # 10:15:59


def test_late_before_shift_is_zero():
    assert compute_late_minutes(utc(2026, 7, 6, 3, 58), "2026-07-06", "09:30") == 0


# --------------------------------------------------------------------------- #
# R3: overtime (spec examples)
# --------------------------------------------------------------------------- #
def test_overtime_below_floor_is_zero():
    # shift end 18:30 IST = 13:00Z; out 18:35 IST = 13:05Z -> 5 min < 30 -> 0.
    assert compute_overtime_minutes(utc(2026, 7, 6, 13, 5), "2026-07-06", "09:30", "18:30") == 0


def test_overtime_counted():
    # out 19:10 IST = 13:40Z -> 40 min.
    assert compute_overtime_minutes(utc(2026, 7, 6, 13, 40), "2026-07-06", "09:30", "18:30") == 40


def test_overtime_overnight():
    # shift 22:00-06:00 on 2026-07-06; end is 06:00 IST on the 7th = 00:30Z on the 7th.
    # out 06:40 IST on the 7th = 01:10Z -> 40 min.
    assert compute_overtime_minutes(utc(2026, 7, 7, 1, 10), "2026-07-06", "22:00", "06:00") == 40


# --------------------------------------------------------------------------- #
# R4 / R5: work hours half-up + half day
# --------------------------------------------------------------------------- #
def test_work_hours_half_up():
    # 29250 s = 8.125 h exactly -> half-up -> 8.13 (round() would give 8.12).
    pin = utc(2026, 7, 6, 0, 0, 0)
    pout = pin.__class__.fromtimestamp(pin.timestamp() + 29250, tz=timezone.utc)
    assert compute_work_hours(pin, pout) == Decimal("8.13")


def test_half_day_boundary():
    assert compute_half_day(Decimal("4.50")) is False   # 16182 s rounds to 4.50 -> not half
    assert compute_half_day(Decimal("4.49")) is True    # 16181 s rounds to 4.49 -> half


# --------------------------------------------------------------------------- #
# compute_derived against ALL NINE sample documents (the authoritative check)
# --------------------------------------------------------------------------- #
SHIFTS = {  # emp_code -> (shift_start, shift_end)
    "EMP0001": ("09:30", "18:30"),
    "EMP0002": ("09:30", "18:30"),
    "EMP0003": ("09:30", "18:30"),
    "EMP0004": ("09:30", "18:30"),
    "EMP0005": ("22:00", "06:00"),
    "EMP0006": ("09:30", "18:30"),
}

# (emp, date, status, punch_in, punch_out, expected work_hours/late/ot/half)
SAMPLES = [
    ("EMP0001", "2026-07-06", "PRESENT", utc(2026, 7, 6, 3, 58), utc(2026, 7, 6, 13, 5), 9.12, 0, 0, False),
    ("EMP0003", "2026-07-06", "PRESENT", utc(2026, 7, 6, 4, 35), utc(2026, 7, 6, 13, 40), 9.08, 35, 40, False),
    ("EMP0006", "2026-07-07", "PRESENT", utc(2026, 7, 7, 4, 5), None, None, 0, 0, False),
    ("EMP0004", "2026-07-07", "WFH", utc(2026, 7, 7, 4, 0), utc(2026, 7, 7, 7, 30), 3.5, 0, 0, True),
    ("EMP0004", "2026-07-08", "ABSENT", None, None, None, 0, 0, False),
    ("EMP0003", "2026-07-08", "LEAVE", None, None, None, 0, 0, False),
    ("EMP0005", "2026-07-06", "PRESENT", utc(2026, 7, 6, 16, 25), utc(2026, 7, 7, 1, 10), 8.75, 0, 40, False),
    ("EMP0001", "2026-07-07", "PRESENT", utc(2026, 7, 7, 3, 58), utc(2026, 7, 7, 13, 0), 9.03, 0, 0, False),
    ("EMP0002", "2026-07-14", "PRESENT", utc(2026, 7, 14, 4, 2), utc(2026, 7, 14, 13, 1), 8.98, 0, 0, False),
]


def test_compute_derived_reproduces_all_samples():
    for emp, d, status, pin, pout, wh, late, ot, half in SAMPLES:
        ss, se = SHIFTS[emp]
        got = compute_derived(status, pin, pout, ss, se, d)
        assert got["work_hours"] == wh, (emp, d, "work_hours", got)
        assert got["late_minutes"] == late, (emp, d, "late", got)
        assert got["overtime_minutes"] == ot, (emp, d, "overtime", got)
        assert got["half_day"] == half, (emp, d, "half_day", got)


def test_overnight_sample_date_matches():
    # Sample 7's punch-in must map to its stored date.
    ss, se = SHIFTS["EMP0005"]
    assert attendance_date(utc(2026, 7, 6, 16, 25), ss, se) == "2026-07-06"
