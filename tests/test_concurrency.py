"""
Concurrency tests. These fire 20 identical/parallel requests from threads against the
real uvicorn server and assert the race guarantees:

  * employee creation  -> exactly one 201
  * punch-in           -> exactly one 201
  * punch-out          -> exactly one 200
  * parallel PATCH     -> no history entries are silently lost
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx

from tests.conftest import make_employee

N = 20


def ms(y, mo, d, h, mi, s=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp() * 1000)


def _fire(server, method, path, bodies):
    """Send one request per body concurrently; return the list of status codes."""
    def worker(body):
        with httpx.Client(base_url=server, timeout=20) as c:
            return c.request(method, path, json=body).status_code
    with ThreadPoolExecutor(max_workers=len(bodies)) as pool:
        return list(pool.map(worker, bodies))


def test_concurrent_employee_create_one_winner(server, clean):
    body = {"emp_code": "EMP0001", "name": "Race", "email": "r@e.co",
            "department": "Eng", "joined_on": "2026-01-01"}
    codes = _fire(server, "POST", "/employees", [body] * N)
    assert codes.count(201) == 1
    assert codes.count(409) == N - 1
    assert clean.employees.count_documents({"emp_code": "EMP0001"}) == 1


def test_concurrent_punch_in_one_winner(server, clean):
    with httpx.Client(base_url=server, timeout=20) as c:
        make_employee(c, emp_code="EMP0001")
    body = {"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 4, 0)}
    codes = _fire(server, "POST", "/attendance/punch-in", [body] * N)
    assert codes.count(201) == 1
    assert codes.count(409) == N - 1
    assert clean.attendance_logs.count_documents({"emp_code": "EMP0001"}) == 1


def test_concurrent_punch_out_one_winner(server, clean):
    with httpx.Client(base_url=server, timeout=20) as c:
        make_employee(c, emp_code="EMP0001")
        c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 4, 0)})
    body = {"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 13, 0)}
    codes = _fire(server, "POST", "/attendance/punch-out", [body] * N)
    assert codes.count(200) == 1
    assert codes.count(409) == N - 1


def test_concurrent_patch_no_lost_history(server, clean):
    with httpx.Client(base_url=server, timeout=20) as c:
        make_employee(c, emp_code="EMP0001")
        c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 4, 0)})
        c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 13, 0)})
    # each PATCH makes a DISTINCT real change (a different punch_in second).
    bodies = [{"punch_in": ms(2026, 7, 6, 3, 0, i), "reason": f"fix number {i}",
               "regularized_by": "hr"} for i in range(N)]
    codes = _fire(server, "PATCH", "/attendance/EMP0001/2026-07-06", bodies)
    successes = codes.count(200)
    assert successes >= 1
    assert 500 not in codes
    # The invariant that matters: the stored history length equals the number of
    # successful corrections. Nothing was silently overwritten.
    doc = clean.attendance_logs.find_one({"emp_code": "EMP0001", "date": "2026-07-06"})
    assert len(doc.get("history", [])) == successes
