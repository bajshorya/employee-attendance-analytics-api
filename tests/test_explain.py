"""
Explain + scale tests, run against a ~100k-record database (`attendance_load`).

For all five explain names across parameter combinations, assert the plan contains
IXSCAN and no COLLSCAN. Also checks the explain response shape, the 422 error cases,
and records (prints) response times for the list and analytics endpoints so we can see
they stay fast at scale.
"""
import os
import pathlib
import socket
import subprocess
import sys
import time

import httpx
import pytest
from pymongo import MongoClient

from scripts.generate_data import build_dataset

ROOT = pathlib.Path(__file__).resolve().parent.parent
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
LOAD_DB = os.getenv("LOAD_MONGO_DB", "attendance_load")
MIN_LOGS = 100_000


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def load_server():
    """Ensure a ~100k dataset exists in attendance_load and serve the app against it."""
    client = MongoClient(MONGO_URI, tz_aware=True)
    db = client[LOAD_DB]
    if db.attendance_logs.estimated_document_count() < MIN_LOGS:
        build_dataset(db, scale="large", seed=12345)
    port = _free_port()
    env = {**os.environ, "MONGO_URI": MONGO_URI, "MONGO_DB": LOAD_DB}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)],
        cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    ready = False
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            if httpx.get(base + "/health", timeout=1).status_code == 200:
                ready = True
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    if not ready:
        proc.terminate()
        raise RuntimeError("load server did not start:\n" + (proc.stdout.read().decode() if proc.stdout else ""))
    with httpx.Client(base_url=base, timeout=30) as c:
        yield c, db
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _assert_indexed(body):
    blob = __import__("json").dumps(body["explain"])
    assert "IXSCAN" in blob, f"no IXSCAN in {body['endpoint']}"
    assert "COLLSCAN" not in blob, f"COLLSCAN present in {body['endpoint']}"
    assert set(body) == {"endpoint", "collection", "explain"}


EXPLAIN_CASES = [
    ("attendance_list", {}),
    ("attendance_list", {"status": "PRESENT"}),
    ("attendance_list", {"emp_code": "EMP00007"}),
    ("attendance_list", {"date_from": "2026-07-01", "date_to": "2026-07-31"}),
    ("attendance_list", {"emp_code": "EMP00007", "date_from": "2026-07-01",
                         "date_to": "2026-07-31", "status": "PRESENT"}),
    ("employee_monthly", {"emp_code": "EMP00001", "month": "2026-08"}),
    ("department_summary", {"month": "2026-08"}),
    ("department_summary", {"month": "2026-08", "department": "Engineering"}),
    ("late_leaderboard", {"month": "2026-08"}),
    ("late_leaderboard", {"month": "2026-08", "department": "People"}),
    ("department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-31"}),
]


@pytest.mark.parametrize("endpoint,params", EXPLAIN_CASES)
def test_explain_uses_index_no_collscan(load_server, endpoint, params):
    client, _ = load_server
    r = client.get(f"/admin/explain/{endpoint}", params=params)
    assert r.status_code == 200, (endpoint, params, r.text)
    _assert_indexed(r.json())


def test_explain_bad_endpoint_is_422(load_server):
    client, _ = load_server
    assert client.get("/admin/explain/not_a_real_endpoint").status_code == 422


def test_explain_missing_required_params_422(load_server):
    client, _ = load_server
    assert client.get("/admin/explain/employee_monthly", params={"month": "2026-08"}).status_code == 422
    assert client.get("/admin/explain/employee_monthly", params={"emp_code": "EMP00001"}).status_code == 422
    assert client.get("/admin/explain/department_summary").status_code == 422
    assert client.get("/admin/explain/department_trend",
                      params={"department": "Engineering", "from": "2026-07-01"}).status_code == 422


def test_endpoints_fast_at_scale(load_server):
    client, db = load_server
    n = db.attendance_logs.estimated_document_count()
    assert n >= MIN_LOGS, f"only {n} logs; expected >= {MIN_LOGS}"
    timings = {}
    probes = {
        "GET /attendance": ("/attendance", {"page_size": 100}),
        "GET /attendance?emp": ("/attendance", {"emp_code": "EMP00007"}),
        "employee_monthly": ("/analytics/employees/EMP00001/monthly", {"month": "2026-08"}),
        "department_summary": ("/analytics/departments/summary", {"month": "2026-08"}),
        "late_leaderboard": ("/analytics/leaderboard/late", {"month": "2026-08"}),
        "department_trend": ("/analytics/departments/Engineering/trend",
                             {"from": "2026-07-01", "to": "2026-09-30"}),
    }
    for label, (path, params) in probes.items():
        t0 = time.perf_counter()
        r = client.get(path, params=params)
        dt = time.perf_counter() - t0
        assert r.status_code == 200, (label, r.text)
        timings[label] = round(dt * 1000, 1)
        assert dt < 5.0, f"{label} took {dt:.2f}s"
    print(f"\n[scale {n} logs] response times (ms): {timings}")
