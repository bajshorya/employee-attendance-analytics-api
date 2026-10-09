"""Functional tests for POST /attendance/punch-out and PATCH /attendance/{emp}/{date}."""
from datetime import datetime, timezone

from tests.conftest import make_employee

ATTENDANCE_FIELDS = {"emp_code", "date", "status", "punch_in", "punch_out",
                     "work_hours", "late_minutes", "overtime_minutes", "half_day", "history"}


def ms(y, mo, d, h, mi, s=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp() * 1000)


def _punch_in(client, emp, when):
    return client.post("/attendance/punch-in", json={"emp_code": emp, "punched_at": when})


# --------------------------------------------------------------------------- #
# punch-out
# --------------------------------------------------------------------------- #
def test_punch_out_computes_r3_r4_r5(client, clean):
    make_employee(client, emp_code="EMP0003")
    _punch_in(client, "EMP0003", ms(2026, 7, 6, 4, 35))        # 10:05 IST, late 35
    r = client.post("/attendance/punch-out",
                    json={"emp_code": "EMP0003", "punched_at": ms(2026, 7, 6, 13, 40)})  # 19:10 IST
    assert r.status_code == 200
    body = r.json()
    assert set(body) == ATTENDANCE_FIELDS
    assert body["work_hours"] == 9.08
    assert body["late_minutes"] == 35
    assert body["overtime_minutes"] == 40
    assert body["half_day"] is False
    assert body["history"] == []     # punch-out never writes history


def test_punch_out_half_day(client, clean):
    make_employee(client, emp_code="EMP0004")
    _punch_in(client, "EMP0004", ms(2026, 7, 7, 4, 0))         # 09:30 IST
    r = client.post("/attendance/punch-out",
                    json={"emp_code": "EMP0004", "punched_at": ms(2026, 7, 7, 7, 30)})  # 3.5h
    assert r.json()["work_hours"] == 3.5
    assert r.json()["half_day"] is True


def test_punch_out_overnight_across_midnight(client, clean):
    make_employee(client, emp_code="EMP0005", shift_start="22:00", shift_end="06:00")
    _punch_in(client, "EMP0005", ms(2026, 7, 6, 16, 25))       # 21:55 IST, date 2026-07-06
    r = client.post("/attendance/punch-out",
                    json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 7, 1, 10)})  # 06:40 IST next day
    body = r.json()
    assert body["date"] == "2026-07-06"
    assert body["work_hours"] == 8.75
    assert body["overtime_minutes"] == 40


def test_punch_out_unknown_employee_404(client, clean):
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP9999"}).status_code == 404


def test_punch_out_no_open_record_404(client, clean):
    make_employee(client, emp_code="EMP0001")
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001",
                       "punched_at": ms(2026, 7, 6, 10, 0)}).status_code == 404


def test_punch_out_already_out_409(client, clean):
    make_employee(client, emp_code="EMP0001")
    _punch_in(client, "EMP0001", ms(2026, 7, 6, 4, 0))
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001",
                       "punched_at": ms(2026, 7, 6, 13, 0)}).status_code == 200
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001",
                       "punched_at": ms(2026, 7, 6, 13, 30)}).status_code == 409


def test_punch_out_time_bounds_422(client, clean):
    make_employee(client, emp_code="EMP0001")
    _punch_in(client, "EMP0001", ms(2026, 7, 6, 4, 0))
    # equal to punch_in
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001",
                       "punched_at": ms(2026, 7, 6, 4, 0)}).status_code == 422
    # more than 24h after
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001",
                       "punched_at": ms(2026, 7, 7, 4, 1)}).status_code == 422


def test_punch_out_exactly_24h_allowed(client, clean):
    make_employee(client, emp_code="EMP0001")
    _punch_in(client, "EMP0001", ms(2026, 7, 6, 4, 0))
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001",
                    "punched_at": ms(2026, 7, 7, 4, 0)})
    assert r.status_code == 200


# --------------------------------------------------------------------------- #
# PATCH / regularize
# --------------------------------------------------------------------------- #
def _full_record(client, emp="EMP0001"):
    make_employee(client, emp_code=emp)
    _punch_in(client, emp, ms(2026, 7, 6, 4, 35))             # 10:05 IST, late 35
    client.post("/attendance/punch-out", json={"emp_code": emp, "punched_at": ms(2026, 7, 6, 13, 0)})


def test_patch_corrects_punch_in_and_writes_history(client, clean):
    _full_record(client)
    r = client.patch("/attendance/EMP0001/2026-07-06",
                     json={"punch_in": ms(2026, 7, 6, 3, 58),   # 09:28 IST -> no longer late
                           "reason": "biometric glitch", "regularized_by": "hr.admin"})
    assert r.status_code == 200
    body = r.json()
    assert body["late_minutes"] == 0
    assert len(body["history"]) == 1
    entry = body["history"][0]
    assert set(entry) == {"at", "by", "reason", "changes"}
    assert entry["by"] == "hr.admin"
    assert isinstance(entry["at"], int)
    # punch_in change recorded as epoch ms (not a datetime)
    assert entry["changes"]["punch_in"]["to"] == ms(2026, 7, 6, 3, 58)
    assert entry["changes"]["late_minutes"] == {"from": 35, "to": 0}


def test_patch_absent_clears_punch_times(client, clean):
    _full_record(client)
    r = client.patch("/attendance/EMP0001/2026-07-06",
                     json={"status": "ABSENT", "reason": "marked absent", "regularized_by": "hr"})
    assert r.status_code == 200
    body = r.json()
    assert body["punch_in"] is None and body["punch_out"] is None
    assert body["work_hours"] is None and body["late_minutes"] == 0


def test_patch_absent_with_punch_time_is_422(client, clean):
    _full_record(client)
    r = client.patch("/attendance/EMP0001/2026-07-06",
                     json={"status": "LEAVE", "punch_in": ms(2026, 7, 6, 4, 0),
                           "reason": "leave day", "regularized_by": "hr"})
    assert r.status_code == 422


def test_patch_no_change_is_422(client, clean):
    _full_record(client)
    r = client.patch("/attendance/EMP0001/2026-07-06",
                     json={"reason": "nothing to see", "regularized_by": "hr"})
    assert r.status_code == 422


def test_patch_punch_in_off_date_is_422(client, clean):
    _full_record(client)
    # move punch_in to a time whose IST date is the 7th -> no longer maps to 2026-07-06
    r = client.patch("/attendance/EMP0001/2026-07-06",
                     json={"punch_in": ms(2026, 7, 6, 20, 0),   # 01:30 IST on the 7th
                           "reason": "wrong day attempt", "regularized_by": "hr"})
    assert r.status_code == 422


def test_patch_unknown_record_404(client, clean):
    make_employee(client, emp_code="EMP0001")
    r = client.patch("/attendance/EMP0001/2026-07-06",
                     json={"status": "ABSENT", "reason": "no record", "regularized_by": "hr"})
    assert r.status_code == 404
    r2 = client.patch("/attendance/EMP9999/2026-07-06",
                      json={"status": "ABSENT", "reason": "no emp", "regularized_by": "hr"})
    assert r2.status_code == 404


def test_patch_reason_length_validation(client, clean):
    _full_record(client)
    r = client.patch("/attendance/EMP0001/2026-07-06",
                     json={"status": "ABSENT", "reason": "x", "regularized_by": "hr"})
    assert r.status_code == 422   # reason < 5 chars


def test_patch_history_accumulates(client, clean):
    _full_record(client)
    client.patch("/attendance/EMP0001/2026-07-06",
                 json={"punch_in": ms(2026, 7, 6, 4, 0), "reason": "first fix", "regularized_by": "hr"})
    r = client.patch("/attendance/EMP0001/2026-07-06",
                     json={"status": "WFH", "reason": "second fix", "regularized_by": "hr"})
    assert len(r.json()["history"]) == 2
