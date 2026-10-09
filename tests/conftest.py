"""
Shared pytest fixtures.

The suite runs against a REAL uvicorn process (not FastAPI's in-process TestClient)
because the concurrency tests need the app's real thread pool and a real MongoDB. The
test database is ``attendance_test`` (override with MONGO_URI / TEST_MONGO_DB).
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

ROOT = pathlib.Path(__file__).resolve().parent.parent
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
TEST_DB = os.getenv("TEST_MONGO_DB", "attendance_test")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def mongo():
    """Direct PyMongo handle to the test DB (for seeding and assertions)."""
    client = MongoClient(MONGO_URI, tz_aware=True)
    yield client[TEST_DB]
    client.close()


@pytest.fixture(scope="session")
def server(mongo):
    """Launch `uvicorn app.main:app` against the test DB and wait for /health."""
    port = _free_port()
    env = {**os.environ, "MONGO_URI": MONGO_URI, "MONGO_DB": TEST_DB}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
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
        out = proc.stdout.read().decode() if proc.stdout else ""
        raise RuntimeError(f"server did not become ready:\n{out}")
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture
def client(server):
    with httpx.Client(base_url=server, timeout=15) as c:
        yield c


@pytest.fixture
def clean(mongo):
    """Wipe both collections before a test (indexes, created at startup, survive)."""
    mongo.employees.delete_many({})
    mongo.attendance_logs.delete_many({})
    return mongo


def make_employee(client, emp_code="EMP0001", department="Engineering",
                  shift_start="09:30", shift_end="18:30", joined_on="2026-01-05",
                  name="Test User", email=None):
    body = {
        "emp_code": emp_code, "name": name,
        "email": email or f"{emp_code.lower()}@example.com",
        "department": department, "shift_start": shift_start,
        "shift_end": shift_end, "joined_on": joined_on,
    }
    return client.post("/employees", json=body)
