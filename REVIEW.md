# REVIEW.md

Review of the starter `app/main.py`, read as if it were a teammate's pull request.
Each defect lists where it is, what is wrong, how you'd notice it (a concrete input or
symptom), and how I fixed it. Verified: every row below was reproduced before fixing,
and the fixes are exercised by the test suite (`tests/`).

## Defects

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Compares **truncated minutes** (`int(seconds/60)`) to 10, so any lateness in the 10th minute is lost. | Shift 09:30, punch 09:40:30 → returns `0`; spec says `10`. | Compare in **seconds** (`> 600`); late minutes = `seconds // 60`. |
| 2 | `compute_late_minutes` | Builds the shift-start with `punch_in.replace(hour,min)` in **whatever zone the datetime carries**, not IST. A UTC punch gets a UTC shift start. | A UTC instant for 10:05 IST returns `0` instead of `35`. | Build the shift-start as an **IST-aware instant on the attendance date** (`shift_start_instant`). |
| 3 | `compute_late_minutes` | No overnight support and no whole-second truncation (R1). | Sub-second punches shift the boundary; overnight shifts compute against the wrong day. | Truncate at the boundary (`ms_to_dt`), pass the attendance date in, compute from the IST shift-start. |
| 4 | `compute_work_hours` | Uses `round()`, which is banker's rounding on a float, not half-up (R4). | 8h07m30s (29250 s = 8.125 h) → `8.12`; spec says `8.13`. | `Decimal(seconds)/Decimal(3600)` quantized `ROUND_HALF_UP`. |
| 5 | `compute_overtime` | No 30-minute minimum (R3). | Out 18:35 vs shift end 18:30 → `5`; spec says `0`. | Floor to minutes, keep only if `>= 30`, else `0`. |
| 6 | `compute_overtime` | Puts the overnight shift-end on the **attendance date**, not the next day; also uses a naive IST `replace`. | Overnight 22:00–06:00, out 06:40 → `1480`; spec says `40`. | `shift_end_instant` adds a day for overnight shifts and is IST-aware. |
| 7 | `EmployeeIn` | No patterns or length limits; allows `shift_start == shift_end`. | `emp_code:"E1"`, empty `name`, `email:"nope"`, equal shifts all accepted (should be 422). | `EmployeeCreate` applies every openapi pattern/length; a model validator rejects equal shifts; `joined_on` is checked as a real date. |
| 8 | `PunchInIn.punched_at` | Plain `Optional[int]`: coerces `"123"`, floats and `true`, and has no range, so seconds-sized values pass. | `punched_at:"1783312500000"` / `1783312500000.0` / `true` / `1783312500` all accepted. | Strict epoch-ms type: `Field(strict=True, ge=1e11, le=4102444800000)`. |
| 9 | `PunchInIn.status` | Accepts any string, including `ABSENT`/`LEAVE`. | Punch-in with `status:"ABSENT"` creates a "present" record with a punch time. | `Literal["PRESENT","WFH","ON_DUTY"]`. |
| 10 | `/health` | Never touches MongoDB, so it returns 200 even when the DB is down. | Stop MongoDB → `/health` still 200; grader expects 503. | `db.command("ping")`, 503 on any `PyMongoError`. |
| 11 | `create_employee` | Check-then-insert with no unique index → a race creates several employees with the same `emp_code`. | 20 parallel identical creates → many 201s. | UNIQUE index on `emp_code`; insert and catch `DuplicateKeyError` → 409 (no pre-check). |
| 12 | `create_employee` | Stores `datetime.now()` (naive, server-local) and returns `created_at` as a raw datetime, not epoch ms. | `created_at` comes back as `"2026-..."` string, server-zone dependent. | `now_utc()` (UTC, whole seconds); serializer returns epoch ms. |
| 13 | `list_employees` | `skip = page * page_size`, so page 1 skips the first page. | `?page=1&page_size=2` returns the 3rd–4th employees. | `skip = (page - 1) * page_size`. |
| 14 | `list_employees` | `total = count_documents({})` ignores the `department` filter (R10). | Filter to a 1-person department, `total` still shows everyone. | `count_documents(query)` with the same filter. |
| 15 | `list_employees` | No sort and no bounds on `page`/`page_size`. | Order is arbitrary; `page_size=10000` allowed. | Sort `emp_code` asc; `Query(ge=1)` / `Query(ge=1, le=100)` → 422 out of range. |
| 16 | `punch_in` | Looks up the employee but never checks for `None`, then indexes `emp["shift_start"]` → 500 for an unknown code. | Punch-in for `EMP9999` → 500; spec says 404. | `get_employee_or_404`. |
| 17 | `punch_in` | `datetime.fromtimestamp(ms/1000)` is naive **server-local** time, so the stored instant and the R1 date are wrong. | On a UTC server a 00:30 IST punch is stored as the previous day. | `ms_to_dt` (UTC) + `attendance_date` (IST). |
| 18 | `punch_in` | Ignores the overnight rule and does not truncate to whole seconds (R1). | Overnight early-morning punches land on the wrong date; sub-second parts persist. | `attendance_date` handles overnight; `ms_to_dt` truncates. |
| 19 | `punch_in` | `if body.punched_at:` is a truthiness test where `is not None` is meant. | A falsy-but-valid value would be treated as "use now". | `if body.punched_at is not None`. |
| 20 | `punch_in` | Check-then-insert with no unique index on `(emp_code, date)`. | Two simultaneous punch-ins for the same day both succeed. | UNIQUE `(emp_code, date)` index; catch `DuplicateKeyError` → 409. |
| 21 | `punch_in` | Returns an `id` field and raw datetimes. | Response has `id` and datetime strings instead of epoch ms. | `serialize_attendance`: no id, instants as epoch ms. |
| 22 | `list_attendance` | Loads every matching doc into memory, then sorts and slices in Python. | Fine on 9 docs, dies on 100k. | Push `sort`/`skip`/`limit` to MongoDB, backed by an index. |
| 23 | `list_attendance` | Sorts by `date` only — no `emp_code` tiebreak, so same-day order is unstable. | Two records on one date flip order between calls. | Sort `[(date, -1), (emp_code, 1)]`. |
| 24 | `list_attendance` | Returns `id`, raw datetimes, and legacy docs missing `half_day`/`history`. | Legacy record → `KeyError`/missing fields in the response. | Serializer fills defaults (null / 0 / false / `[]`). |
| 25 | `list_attendance` | No validation: date format, `date_from > date_to`, `status`, paging bounds. | `date_from=not-a-date`, reversed range, `status=NOPE` all 200. | Validate date shape, reject reversed range, `Literal` status, paging bounds. |
| 26 | module | Nothing creates any index — the unique guarantees and fast queries depend on them. | Races in #11/#20; collection scans at 100k. | `ensure_indexes()` in the FastAPI `lifespan`, idempotent, each in its own try. |

## Examined and decided **not** a defect

- **`load_dotenv()` does not override existing env vars.** This is the documented default, so the grader's real `MONGO_URI`/`MONGO_DB` always win over a local `.env`. Correct as-is; kept it.
- **Ignoring unknown body fields.** Pydantic drops them by default, which is exactly the required behaviour for read-only derived fields. Kept (made explicit with `extra="ignore"`).
- **Fixed-offset IST (`timezone(timedelta(hours=5, minutes=30))`).** India has no daylight saving, so a fixed offset is correct; a named tz library would add a dependency for no benefit.
- **Plain `def` endpoints calling blocking PyMongo.** FastAPI runs `def` endpoints in a thread pool and `MongoClient` is thread-safe, so this is safe under concurrency. Kept this style deliberately.
- **`list_attendance`'s `total` *value*.** The number it reported was correct; only the *way* it was computed (count a Python list after loading everything) was the problem. Fixed the method, not the meaning.
- **`doc.pop("_id")` after `insert_one`.** Needed, because PyMongo mutates the inserted dict to add `_id`. I achieve the same by serialising an explicit field list, so `_id` can never leak.
