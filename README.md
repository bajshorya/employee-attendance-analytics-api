# Employee Attendance & Analytics API

A small HR attendance backend: employees punch in and out, HR corrects records (with an
audit trail), and four analytics endpoints answer questions over the data using MongoDB
aggregation pipelines. Built with **FastAPI + synchronous PyMongo + MongoDB**. All
application code lives in `app/main.py`, organised into bannered sections.

## Running it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"     # or your Atlas URI
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```

`GET /health` returns 200 once MongoDB answers a ping (503 otherwise). Indexes are
created idempotently at startup, so the app can be pointed straight at a database that
already holds data. Requires MongoDB 6.0+ (tested on 7.0 and 8.2).

### Configuration

| Variable | Meaning | Default |
|---|---|---|
| `MONGO_URI` | MongoDB connection string | `mongodb://localhost:27017` |
| `MONGO_DB` | Database name | `attendance_db` |

Real environment variables always win: a local `.env` is loaded for convenience but
`load_dotenv()` does not override variables that are already set.

## Design in brief

- **Synchronous PyMongo with plain `def` endpoints.** FastAPI runs them in a thread pool
  and `MongoClient` is thread-safe, so concurrency is handled without `async` complexity.
- **`tz_aware=True`.** Every datetime read back from MongoDB is UTC-aware, so naive/aware
  values are never mixed. The API speaks epoch milliseconds; conversion happens only at
  the boundary, and instants are truncated to whole seconds (R1).
- **Pure rule helpers** (R1–R5) with a single `compute_derived()` used by punch-in,
  punch-out and PATCH, so the three can never diverge. They are unit-tested against the
  spec examples and all nine sample documents.
- **One builder per analytics pipeline**, shared by the real endpoint and `/admin/explain`
  so the explained plan is exactly the plan that runs.
- **Half-up rounding in Python.** Pipelines return raw sums/counts; the final division and
  rounding use `Decimal` (MongoDB `$round` and float `round()` are half-to-even).
- **Races handled by the database:** unique indexes for create/punch-in, a
  `find_one_and_update` guard for punch-out, and an optimistic filter (read values +
  history-length guard) for PATCH — never check-then-insert.

## Indexes

Created at startup (`ensure_indexes`), each with an explicit name so re-creation is a no-op:

| Collection | Index | Serves |
|---|---|---|
| `employees` | `{emp_code: 1}` **unique** | identity; makes concurrent create safe |
| `employees` | `{department: 1, joined_on: 1}` | department summary/trend headcount by join date |
| `employees` | `{joined_on: 1}` | department summary when no department filter |
| `attendance_logs` | `{emp_code: 1, date: 1}` **unique** | one record per employee/day; makes punch-in race-safe; employee-monthly match |
| `attendance_logs` | `{date: -1, emp_code: 1}` | list sort + month-range matches (leaderboard) |
| `attendance_logs` | `{emp_code: 1, punch_in: -1}` | punch-out's "latest record for this employee" lookup |

A single-field `status` index was deliberately **not** added — it is low-selectivity and
would tempt the planner into an in-memory sort.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest            # unit, contract, concurrency, differential, explain + scale
```

The suite runs against a real `uvicorn` process (DB `attendance_test`) and a ~110k-record
dataset (`attendance_load`, auto-built by `scripts/generate_data.py`). Highlights:

- **Rule unit tests** reproduce the spec examples and all nine sample documents.
- **Concurrency tests** fire 20 parallel requests: exactly one 201 for create and
  punch-in, one 200 for punch-out, and no lost history for parallel PATCHes.
- **Differential analytics tests** compare every endpoint against an independent Python
  reference calculator across many months, departments, employees and ranges.
- **Explain tests** assert `IXSCAN` present / `COLLSCAN` absent for all five names on 100k.

## Assumptions (where the contract is ambiguous)

- `moving_avg_7d` averages the **unrounded** daily rates and is rounded to 4 decimals once
  at the end (R8 speaks of "reported numbers"; R5 names rounding explicitly where it wants
  a rounded intermediate).
- The 92-day trend limit counts days **inclusively**, so a range that would return more
  than 92 rows is 422.
- A PATCH that supplies punch times for a stored `ABSENT`/`LEAVE` record **without**
  changing the status is 422 (the final status is still non-presence).
- In punch-out, the already-punched-out check (409) runs **before** the time-bound check (422).
- In PATCH, an explicit `null` for `status`/`punch_in`/`punch_out` is treated as omitted.
- `present_days` / `present_count` / `attendance_pct` / rates are returned as JSON numbers
  (floats), since they can be fractional (half days, percentages).

## What is missing

Nothing from the contract is known to be missing. All endpoints, rules R1–R10, the explain
endpoint and the index/concurrency guarantees are implemented and covered by passing tests.
There is no authentication (the assignment does not ask for it; `history[].by` is free text).
