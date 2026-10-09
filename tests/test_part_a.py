"""
Contract tests for Part A: /health, /employees (create + list), /attendance/punch-in,
and GET /attendance. These assert exact field sets, epoch-ms integers, status codes and
the fixed bugs from the starter (pagination, sort, validation, no _id/id leakage).
"""
from datetime import datetime, timezone

from tests.conftest import make_employee

EMPLOYEE_FIELDS = {"emp_code", "name", "email", "department", "shift_start",
                   "shift_end", "joined_on", "created_at"}
ATTENDANCE_FIELDS = {"emp_code", "date", "status", "punch_in", "punch_out",
                     "work_hours", "late_minutes", "overtime_minutes", "half_day", "history"}


def ms(y, mo, d, h, mi, s=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp() * 1000)


# --------------------------------------------------------------------------- #
# /health
# --------------------------------------------------------------------------- #
def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# --------------------------------------------------------------------------- #
# POST /employees
# --------------------------------------------------------------------------- #
def test_create_employee_shape_and_epoch_created_at(client, clean):
    r = make_employee(client)
    assert r.status_code == 201
    body = r.json()
    assert set(body) == EMPLOYEE_FIELDS
    assert "_id" not in body and "id" not in body
    assert isinstance(body["created_at"], int)       # epoch ms, not a datetime string
    assert body["created_at"] > 100_000_000_000


def test_create_employee_duplicate_is_409(client, clean):
    assert make_employee(client).status_code == 201
    assert make_employee(client).status_code == 409


def test_create_employee_rejects_bad_input(client, clean):
    bad = [
        {"emp_code": "E1", "name": "x", "email": "a@b.co", "department": "D", "joined_on": "2026-01-01"},   # emp_code pattern
        {"emp_code": "EMP0001", "name": "", "email": "a@b.co", "department": "D", "joined_on": "2026-01-01"},  # empty name
        {"emp_code": "EMP0001", "name": "x", "email": "nope", "department": "D", "joined_on": "2026-01-01"},   # bad email
        {"emp_code": "EMP0001", "name": "x", "email": "a@b.co", "department": "D", "joined_on": "2026-13-40"},  # bad date
        {"emp_code": "EMP0001", "name": "x", "email": "a@b.co", "department": "D", "joined_on": "2026-01-01",
         "shift_start": "09:30", "shift_end": "09:30"},  # equal shifts
    ]
    for body in bad:
        assert client.post("/employees", json=body).status_code == 422, body


def test_create_employee_ignores_unknown_fields(client, clean):
    body = {"emp_code": "EMP0009", "name": "x", "email": "a@b.co", "department": "D",
            "joined_on": "2026-01-01", "work_hours": 99, "created_at": 123}
    r = client.post("/employees", json=body)
    assert r.status_code == 201
    assert r.json()["created_at"] != 123   # server clock wins; client value ignored


# --------------------------------------------------------------------------- #
# GET /employees  (pagination + sort fixes)
# --------------------------------------------------------------------------- #
def test_list_employees_pagination_and_sort(client, clean):
    for i in range(1, 6):
        make_employee(client, emp_code=f"EMP000{i}", department="Eng" if i % 2 else "Sales")
    r = client.get("/employees", params={"page": 1, "page_size": 2})
    body = r.json()
    assert body["total"] == 5
    assert [e["emp_code"] for e in body["items"]] == ["EMP0001", "EMP0002"]  # page 1 is first, sorted asc
    r2 = client.get("/employees", params={"page": 2, "page_size": 2})
    assert [e["emp_code"] for e in r2.json()["items"]] == ["EMP0003", "EMP0004"]


def test_list_employees_total_respects_filter(client, clean):
    make_employee(client, emp_code="EMP0001", department="Eng")
    make_employee(client, emp_code="EMP0002", department="Sales")
    r = client.get("/employees", params={"department": "Eng"})
    assert r.json()["total"] == 1
    assert len(r.json()["items"]) == 1


def test_list_employees_rejects_bad_paging(client, clean):
    assert client.get("/employees", params={"page": 0}).status_code == 422
    assert client.get("/employees", params={"page_size": 101}).status_code == 422


# --------------------------------------------------------------------------- #
# POST /attendance/punch-in
# --------------------------------------------------------------------------- #
def test_punch_in_unknown_employee_is_404(client, clean):
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP9999"})
    assert r.status_code == 404


def test_punch_in_shape_and_date_and_late(client, clean):
    make_employee(client, emp_code="EMP0003")
    # 10:05 IST = 04:35 UTC, 35 min after a 09:30 shift.
    r = client.post("/attendance/punch-in",
                    json={"emp_code": "EMP0003", "punched_at": ms(2026, 7, 6, 4, 35)})
    assert r.status_code == 201
    body = r.json()
    assert set(body) == ATTENDANCE_FIELDS
    assert "id" not in body and "_id" not in body
    assert body["date"] == "2026-07-06"
    assert body["punch_in"] == ms(2026, 7, 6, 4, 35)
    assert body["punch_out"] is None and body["work_hours"] is None
    assert body["late_minutes"] == 35
    assert body["overtime_minutes"] == 0 and body["half_day"] is False
    assert body["history"] == []


def test_punch_in_truncates_subsecond(client, clean):
    make_employee(client, emp_code="EMP0001")
    r = client.post("/attendance/punch-in",
                    json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 4, 35) + 900})
    assert r.json()["punch_in"] == ms(2026, 7, 6, 4, 35)   # .900 dropped


def test_punch_in_duplicate_date_is_409(client, clean):
    make_employee(client, emp_code="EMP0001")
    p = {"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 4, 0)}
    assert client.post("/attendance/punch-in", json=p).status_code == 201
    assert client.post("/attendance/punch-in", json=p).status_code == 409


def test_punch_in_rejects_bad_timestamp(client, clean):
    make_employee(client, emp_code="EMP0001")
    for bad in ["1783312500000", 1783312500000.0, True, 1783312500, 10 ** 15]:
        r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": bad})
        assert r.status_code == 422, bad


def test_punch_in_rejects_non_presence_status(client, clean):
    make_employee(client, emp_code="EMP0001")
    for bad in ["ABSENT", "LEAVE", "NOPE"]:
        r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": bad})
        assert r.status_code == 422, bad


# --------------------------------------------------------------------------- #
# GET /attendance
# --------------------------------------------------------------------------- #
def test_list_attendance_sort_and_filter(client, clean, mongo):
    make_employee(client, emp_code="EMP0001")
    make_employee(client, emp_code="EMP0002")
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 4, 0)})
    client.post("/attendance/punch-in", json={"emp_code": "EMP0002", "punched_at": ms(2026, 7, 6, 4, 0)})
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 4, 0)})
    r = client.get("/attendance")
    items = r.json()["items"]
    assert r.json()["total"] == 3
    # date desc, then emp_code asc
    keys = [(i["date"], i["emp_code"]) for i in items]
    assert keys == [("2026-07-07", "EMP0001"), ("2026-07-06", "EMP0001"), ("2026-07-06", "EMP0002")]
    for i in items:
        assert set(i) == ATTENDANCE_FIELDS


def test_list_attendance_date_range_validation(client, clean):
    assert client.get("/attendance", params={"date_from": "2026-07-10", "date_to": "2026-07-01"}).status_code == 422
    assert client.get("/attendance", params={"date_from": "not-a-date"}).status_code == 422
    assert client.get("/attendance", params={"status": "NOPE"}).status_code == 422
