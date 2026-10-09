"""
Differential tests for the four analytics endpoints.

A rich, deterministic dataset is seeded once, then every endpoint's HTTP output is
compared against the independent Python reference calculator across many months,
departments, employees and date ranges. Plus a few explicit assertions for the
headline behaviours (ties at the cutoff, zero-log/zero-headcount, overnight, gap days).
"""
import pytest

from scripts.generate_data import build_dataset
from tests import reference

MONTHS = ["2026-06", "2026-07", "2026-08", "2026-09"]
DEPARTMENTS = ["Engineering", "Sales", "Support", "Finance", "People"]


@pytest.fixture(scope="module")
def dataset(server, mongo):
    """Seed the small deterministic dataset once for this module (server already up)."""
    emps, _ = build_dataset(mongo, scale="small", seed=12345)
    return emps


def assert_close(a, b, path=""):
    """Deep-compare JSON values; floats within 1e-9, everything else exact."""
    if isinstance(a, float) or isinstance(b, float):
        if a is None or b is None:
            assert a == b, f"{path}: {a!r} != {b!r}"
        else:
            assert abs(a - b) < 1e-9, f"{path}: {a!r} != {b!r}"
    elif isinstance(a, dict):
        assert isinstance(b, dict) and set(a) == set(b), f"{path}: keys {set(a)} != {set(b)}"
        for k in a:
            assert_close(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list):
        assert isinstance(b, list) and len(a) == len(b), f"{path}: len {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            assert_close(x, y, f"{path}[{i}]")
    else:
        assert a == b, f"{path}: {a!r} != {b!r}"


# --------------------------------------------------------------------------- #
# employee monthly
# --------------------------------------------------------------------------- #
def test_employee_monthly_matches_reference(client, dataset, mongo):
    codes = [e["emp_code"] for e in dataset]
    sample = codes[:25]
    for emp_code in sample:
        for month in MONTHS:
            r = client.get(f"/analytics/employees/{emp_code}/monthly", params={"month": month})
            assert r.status_code == 200, (emp_code, month, r.text)
            assert_close(r.json(), reference.employee_monthly(mongo, emp_code, month),
                         f"{emp_code}/{month}")


def test_employee_monthly_unknown_is_404(client, dataset):
    assert client.get("/analytics/employees/NOPE999/monthly", params={"month": "2026-07"}).status_code == 404


def test_employee_monthly_joined_after_month_has_null_pct(client, dataset, mongo):
    # Finance employees joined in 2027: for a 2026 month working_days=0 -> pct null.
    finance = [e["emp_code"] for e in dataset if e["department"] == "Finance"]
    assert finance
    body = client.get(f"/analytics/employees/{finance[0]}/monthly", params={"month": "2026-07"}).json()
    assert body["working_days"] == 0
    assert body["attendance_pct"] is None


# --------------------------------------------------------------------------- #
# department summary
# --------------------------------------------------------------------------- #
def test_department_summary_matches_reference(client, dataset, mongo):
    for month in MONTHS:
        r = client.get("/analytics/departments/summary", params={"month": month})
        assert r.status_code == 200
        assert_close(r.json(), reference.department_summary(mongo, month), f"summary/{month}")
        for dept in DEPARTMENTS:
            rd = client.get("/analytics/departments/summary", params={"month": month, "department": dept})
            assert_close(rd.json(), reference.department_summary(mongo, month, dept), f"summary/{month}/{dept}")


def test_department_summary_unknown_department_is_empty_not_404(client, dataset):
    r = client.get("/analytics/departments/summary", params={"month": "2026-07", "department": "DoesNotExist"})
    assert r.status_code == 200
    assert r.json() == {"month": "2026-07", "items": []}


def test_department_summary_omits_zero_headcount_dept(client, dataset):
    # Finance (all joined 2027) must be absent for a 2026 month.
    items = client.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"]
    assert "Finance" not in [i["department"] for i in items]


# --------------------------------------------------------------------------- #
# late leaderboard
# --------------------------------------------------------------------------- #
def test_leaderboard_matches_reference(client, dataset, mongo):
    for month in MONTHS:
        for limit in [1, 2, 3, 5, 10, 50]:
            r = client.get("/analytics/leaderboard/late", params={"month": month, "limit": limit})
            assert r.status_code == 200
            assert_close(r.json(), reference.late_leaderboard(mongo, month, limit), f"lb/{month}/{limit}")
            for dept in DEPARTMENTS:
                rd = client.get("/analytics/leaderboard/late",
                                params={"month": month, "limit": limit, "department": dept})
                assert_close(rd.json(), reference.late_leaderboard(mongo, month, limit, dept),
                             f"lb/{month}/{limit}/{dept}")


def test_leaderboard_tie_at_cutoff_returns_extra_rows(client, dataset):
    # People dept in 2026-08 was built with totals 100, 60, 60, 40, 40 -> ranks 1,2,2,4,4.
    body = client.get("/analytics/leaderboard/late",
                      params={"month": "2026-08", "limit": 3, "department": "People"}).json()
    ranks = [i["rank"] for i in body["items"]]
    totals = [i["total_late_minutes"] for i in body["items"]]
    assert ranks == [1, 2, 2]              # limit=3 but only ranks <= 3 exist; two tie at 2
    assert totals == [100, 60, 60]
    # rows ordered by total desc then emp_code asc
    codes = [i["emp_code"] for i in body["items"]]
    assert codes[1] < codes[2]


# --------------------------------------------------------------------------- #
# department trend
# --------------------------------------------------------------------------- #
def test_trend_matches_reference(client, dataset, mongo):
    ranges = [
        ("Engineering", "2026-07-01", "2026-07-31"),
        ("Support", "2026-07-01", "2026-08-15"),       # overnight dept, spans a month boundary
        ("People", "2026-08-01", "2026-08-31"),
        ("Finance", "2026-07-01", "2026-07-10"),       # exists but headcount 0 in 2026
        ("Sales", "2026-06-20", "2026-07-05"),
    ]
    for dept, frm, to in ranges:
        r = client.get(f"/analytics/departments/{dept}/trend", params={"from": frm, "to": to})
        assert r.status_code == 200, (dept, frm, to, r.text)
        assert_close(r.json(), reference.department_trend(mongo, dept, frm, to), f"trend/{dept}/{frm}/{to}")


def test_trend_one_row_per_day_including_gaps(client, dataset):
    body = client.get("/analytics/departments/Engineering/trend",
                      params={"from": "2026-07-01", "to": "2026-07-31"}).json()
    assert len(body["items"]) == 31
    assert [i["date"] for i in body["items"]][:1] == ["2026-07-01"]
    assert body["items"][-1]["date"] == "2026-07-31"


def test_trend_finance_headcount_zero_rate_null(client, dataset):
    body = client.get("/analytics/departments/Finance/trend",
                      params={"from": "2026-07-01", "to": "2026-07-07"}).json()
    assert body["department"] == "Finance"
    assert all(i["headcount"] == 0 for i in body["items"])
    assert all(i["attendance_rate"] is None for i in body["items"])
    assert all(i["moving_avg_7d"] is None for i in body["items"])


def test_trend_validation(client, dataset):
    assert client.get("/analytics/departments/Nope/trend",
                      params={"from": "2026-07-01", "to": "2026-07-02"}).status_code == 404
    assert client.get("/analytics/departments/Engineering/trend",
                      params={"from": "2026-07-10", "to": "2026-07-01"}).status_code == 422
    assert client.get("/analytics/departments/Engineering/trend",
                      params={"from": "2026-01-01", "to": "2026-12-31"}).status_code == 422  # > 92 days


def test_trend_92_day_boundary(client, dataset):
    # exactly 92 days inclusive is allowed; 93 is not.
    ok = client.get("/analytics/departments/Engineering/trend",
                    params={"from": "2026-07-01", "to": "2026-09-30"})   # 92 days
    assert ok.status_code == 200
    assert len(ok.json()["items"]) == 92
    bad = client.get("/analytics/departments/Engineering/trend",
                     params={"from": "2026-07-01", "to": "2026-10-01"})  # 93 days
    assert bad.status_code == 422
