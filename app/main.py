"""
Employee Attendance & Analytics API
====================================

A small HR attendance backend built with **FastAPI + synchronous PyMongo + MongoDB**.
All application code lives in this single file on purpose (that is the assignment's
contract). The file is organised into clearly bannered sections, in this order:

    1.  Configuration and MongoDB client
    2.  Constants (every business-rule number lives here, named)
    3.  Time conversion  (epoch-ms <-> BSON datetime, IST handling)
    4.  Business-rule helpers  (R1 attendance date, R2 late, R3 overtime, R4 hours, R5 half day)
    5.  Rounding helpers  (half-up, because MongoDB $round and float round() are not half-up)
    6.  Validation helpers  (turn business-rule violations into FastAPI 422 bodies)
    7.  Request models  (Pydantic; strict parsing of the HTTP contract)
    8.  Response serialisers  (every response goes through one of these; never leak _id)
    9.  Index creation + lifespan  (indexes are created idempotently at startup)
    10. CRUD endpoints          (health, employees, punch-in, attendance list, punch-out, PATCH)
    11. Pipeline builders       (one builder per analytics pipeline; reused by /admin/explain)
    12. Analytics endpoints     (employee monthly, department summary, leaderboard, trend)
    13. Explain endpoint        (proves the queries use indexes)

Design choices (explained where they are made):

* **Synchronous PyMongo with plain ``def`` endpoints.** FastAPI runs ``def`` endpoints in a
  thread pool and PyMongo is thread-safe, so blocking calls are fine and simplest to reason
  about. We never write ``async def`` endpoints that call blocking PyMongo.
* **``tz_aware=True``** on the client, so every datetime read back from MongoDB is timezone
  aware (UTC). That prevents the classic bug of subtracting a naive datetime from an aware
  one (which raises ``TypeError`` -> a 500).
* **Pure, database-free rule helpers** so they can be unit tested against the spec examples
  and the nine sample documents.
* **One builder per analytics pipeline**, called by both the real endpoint and the explain
  endpoint, so the two can never drift apart.
"""

from __future__ import annotations

import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date as date_cls
from datetime import datetime, time as time_cls, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

import bson.json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

logger = logging.getLogger("attendance")


# =========================================================================== #
# 1. CONFIGURATION AND MONGODB CLIENT
# =========================================================================== #
# MONGO_URI / MONGO_DB come from real environment variables. A local .env is a
# convenience: load_dotenv() does NOT override variables that are already set in
# the environment, so the grader's injected values always win.
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True  -> datetimes come back as UTC-aware, never naive.
# serverSelectionTimeoutMS=3000 -> a dead database fails fast (so /health can answer 503
# well within the grader's 20-second readiness budget instead of hanging).
client: MongoClient = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[MONGO_DB]


# =========================================================================== #
# 2. CONSTANTS
# =========================================================================== #
# Every magic number from the business rules is named here. The live interview
# asks for a small change (e.g. "make the grace period 15 minutes"); keeping the
# numbers here makes each such change a one-line edit.

IST = timezone(timedelta(hours=5, minutes=30))          # India Standard Time, fixed offset (no DST).
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)       # UTC-aware epoch for ms<->datetime math.

GRACE_SECONDS = 600                                     # R2: 10-minute grace, compared in SECONDS.
OVERTIME_MIN_MINUTES = 30                               # R3: overtime only counts if >= 30 minutes.
HALF_DAY_HOURS = Decimal("4.50")                        # R5: < 4.50 rounded hours is a half day.

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")       # R6: these count as "present".
NON_PRESENCE_STATUSES = ("ABSENT", "LEAVE")
ALL_STATUSES = PRESENCE_STATUSES + NON_PRESENCE_STATUSES

MAX_PUNCH_SPAN = timedelta(hours=24)                    # punch_out must be within 24h of punch_in.
MAX_TREND_DAYS = 92                                     # department trend range cap (inclusive).
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100                                     # R10.
DEFAULT_LEADERBOARD_LIMIT = 10
MAX_LEADERBOARD_LIMIT = 50

# Epoch-millisecond bounds shared by every instant in the API. 1e11 ms is 1973-03-03,
# which is safely above any "seconds-sized" value (~1e9..1e10) a client might send by
# mistake; 4102444800000 ms is 2100-01-01. Anything outside is a 422.
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

DATE_RE = r"^\d{4}-\d{2}-\d{2}$"                         # YYYY-MM-DD shape (value still checked below).
MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"                   # YYYY-MM.
HHMM_RE = r"^([01]\d|2[0-3]):[0-5]\d$"                  # 24-hour HH:MM.
EMP_CODE_RE = r"^EMP\d{4,6}$"                            # EMP followed by 4-6 digits.
EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


# =========================================================================== #
# 3. TIME CONVERSION
# =========================================================================== #
# The API speaks epoch milliseconds; MongoDB stores BSON datetimes (UTC). We convert
# at the boundary and never mix the two representations inside the app.

def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> UTC-aware datetime, truncated to whole seconds.

    R1 requires truncation to whole seconds before any calculation, and the
    truncated value is what we store and return. ``ms // 1000`` does exactly that
    truncation (integer floor), so e.g. ...500900 ms is stored as ...500000 ms.

    Example: 1783312500900 -> 2026-07-06 04:35:00+00:00  (the .900 ms is dropped).
    """
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """UTC datetime -> epoch milliseconds (integer), or None -> None.

    Uses pure integer arithmetic (no float) so the value round-trips exactly. A naive
    datetime is treated as UTC (defensive: our client is tz_aware, so this rarely fires).

    Example: 2026-07-06 04:35:00Z -> 1783312500000.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    """'Now' as a UTC-aware datetime truncated to whole seconds.

    Never ``datetime.now()`` (naive, server-local) or naive ``fromtimestamp`` - those pull
    in the server's local zone and corrupt both the stored instant and the R1 date.
    """
    return datetime.now(timezone.utc).replace(microsecond=0)


def _parse_hhmm(hhmm: str) -> tuple[int, int]:
    """'HH:MM' -> (hour, minute)."""
    h, m = hhmm.split(":")
    return int(h), int(m)


# =========================================================================== #
# 4. BUSINESS-RULE HELPERS  (R1 - R5)
# =========================================================================== #
# These are PURE functions: no database access, so they are directly unit-testable
# against the spec examples and the nine sample documents. Punch-in, punch-out and
# PATCH all funnel through compute_derived() at the bottom, so the rules live in one place.

def is_overnight(shift_start: str, shift_end: str) -> bool:
    """A shift is overnight when it ends at or before it starts (e.g. 22:00 -> 06:00).

    String comparison works because both are zero-padded 'HH:MM'.
    """
    return shift_end <= shift_start


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: the IST calendar date a punch-in belongs to (the record's ``date``).

    Normally it is just the IST calendar date of the punch-in. For an **overnight**
    shift, a punch-in whose IST time-of-day is *strictly earlier than* ``shift_end``
    belongs to the shift that started the *previous* day.

    Example (shift 22:00-06:00): a punch at 02:00 IST on the 7th -> date 2026-07-06
    (it belongs to the shift that began 22:00 on the 6th). A punch at 21:55 IST on the
    6th -> 2026-07-06 (21:55 is not < 06:00). A punch at exactly 06:00 IST stays on
    the same day (strictly-earlier, not <=).
    """
    ist = punch_in.astimezone(IST)
    day = ist.date()
    if is_overnight(shift_start, shift_end):
        eh, em = _parse_hhmm(shift_end)
        if ist.time() < time_cls(eh, em):
            day -= timedelta(days=1)
    return day.isoformat()


def shift_start_instant(date_str: str, shift_start: str) -> datetime:
    """The IST-aware instant at which the shift starts on the attendance date.

    The shift always *starts* on the attendance date itself (even for overnight shifts,
    the date IS the day the shift started), so no +1 day is ever needed here.
    """
    d = date_cls.fromisoformat(date_str)
    sh, sm = _parse_hhmm(shift_start)
    return datetime(d.year, d.month, d.day, sh, sm, tzinfo=IST)


def shift_end_instant(date_str: str, shift_start: str, shift_end: str) -> datetime:
    """The IST-aware instant at which the shift ends.

    For an overnight shift the end falls on the *next* calendar day (22:00 on the 6th
    runs until 06:00 on the 7th), so we add a day.
    """
    d = date_cls.fromisoformat(date_str)
    eh, em = _parse_hhmm(shift_end)
    end = datetime(d.year, d.month, d.day, eh, em, tzinfo=IST)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    return end


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str) -> int:
    """R2: whole minutes late, measured in SECONDS from the shift-start instant.

    Late only if strictly more than the 10-minute grace (600 s). When late, the count
    is floor(seconds_since_shift_start / 60) - measured from shift start, NOT from the
    end of grace. Comparing truncated *minutes* to 10 (the starter's bug) is wrong.

    Example (shift 09:30): 09:40:00 -> 0 (==600s, not strictly more); 09:40:01 -> 10
    (601s); 09:40:59 -> 10; 10:15:59 -> 45.
    """
    start = shift_start_instant(date_str, shift_start)
    seconds = int((punch_in - start).total_seconds())
    if seconds > GRACE_SECONDS:
        return seconds // 60
    return 0


def compute_overtime_minutes(
    punch_out: datetime, date_str: str, shift_start: str, shift_end: str
) -> int:
    """R3: floor minutes from the shift-end instant to punch-out, kept only if >= 30.

    Example: shift ends 18:30, out 18:35 -> 5 min -> below the 30-minute floor -> 0.
    Out 19:10 -> 40. Overnight 22:00-06:00 with out 06:40 next day -> 40.
    """
    end = shift_end_instant(date_str, shift_start, shift_end)
    seconds = int((punch_out - end).total_seconds())
    if seconds <= 0:
        return 0
    minutes = seconds // 60
    return minutes if minutes >= OVERTIME_MIN_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> Decimal:
    """R4: (punch_out - punch_in) seconds / 3600, rounded to 2 decimals HALF-UP.

    Returned as a Decimal so the caller controls the float conversion. Built from the
    integer second count via ``Decimal(seconds)`` - never from a float, which would
    reintroduce the rounding error we are trying to avoid.

    Example: 29250 s is exactly 8.125 h; half-up -> 8.13 (Python's round() gives 8.12).
    """
    seconds = int((punch_out - punch_in).total_seconds())
    return round_half_up(Decimal(seconds) / Decimal(3600), 2)


def compute_half_day(work_hours: Decimal) -> bool:
    """R5: half day when the ALREADY-ROUNDED work hours are below 4.50.

    Example: 16182 s = 4.495 h raw, rounds to 4.50 -> NOT a half day. 16181 s rounds to
    4.49 -> half day.
    """
    return work_hours < HALF_DAY_HOURS


def compute_derived(
    status: str,
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
    shift_start: str,
    shift_end: str,
    date_str: str,
) -> dict:
    """Return the four derived fields for a record, per the DATA_MODEL table.

    | Situation                         | work_hours | late_minutes | overtime | half_day |
    |-----------------------------------|------------|--------------|----------|----------|
    | presence status, no punch_out     | null       | R2           | 0        | false    |
    | presence status, with punch_out   | R4         | R2           | R3       | R5       |
    | ABSENT / LEAVE                     | null       | 0            | 0        | false    |

    This single function is called by punch-in, punch-out and PATCH, so the three can
    never compute the rules differently. ``work_hours`` is returned as a float (the BSON
    double we store) or None.
    """
    if status in PRESENCE_STATUSES:
        # A presence status always has a punch_in by the time we get here (enforced by
        # the endpoints), so late minutes are always computable.
        late = compute_late_minutes(punch_in, date_str, shift_start)
        if punch_out is None:
            return {"work_hours": None, "late_minutes": late,
                    "overtime_minutes": 0, "half_day": False}
        hours = compute_work_hours(punch_in, punch_out)
        return {
            "work_hours": float(hours),
            "late_minutes": late,
            "overtime_minutes": compute_overtime_minutes(punch_out, date_str, shift_start, shift_end),
            "half_day": compute_half_day(hours),
        }
    # ABSENT / LEAVE: not present, no punch times, everything at defaults.
    return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}


# =========================================================================== #
# 5. ROUNDING HELPERS
# =========================================================================== #
# R8 requires half-up rounding on every reported number. Both MongoDB's $round (half to
# even) and Python's float round() (also banker's, plus float error) are wrong here, so
# the pipelines return raw sums/counts and we do the final division + rounding in Python
# with Decimal built from exact values.

def round_half_up(value: Decimal, places: int) -> Decimal:
    """Quantize a Decimal to ``places`` decimal places using ROUND_HALF_UP."""
    quant = Decimal(1).scaleb(-places)        # places=2 -> Decimal('0.01'); places=4 -> '0.0001'
    return value.quantize(quant, rounding=ROUND_HALF_UP)


def ratio_or_none(numerator, denominator, places: int):
    """numerator/denominator as a half-up float with ``places`` decimals, or None if
    the denominator is 0. Inputs are coerced through str() so floats never pollute the
    Decimal (never Decimal(0.1); always Decimal('0.1'))."""
    if not denominator:
        return None
    result = round_half_up(Decimal(str(numerator)) / Decimal(str(denominator)), places)
    return float(result)


# =========================================================================== #
# 6. VALIDATION HELPERS
# =========================================================================== #
# Some validation (an epoch-ms shape, string patterns) is expressed on the Pydantic
# models below. Business-rule 422s that Pydantic cannot express are raised through
# this one helper, so the error body keeps FastAPI's DEFAULT validation shape,
# {"detail": [ {...} ]}. (404 and 409 use HTTPException, giving {"detail": "..."}.)

def raise_422(message: str, loc: tuple = ("body",)) -> None:
    """Raise a FastAPI-shaped 422 with a single error entry carrying ``message``."""
    raise RequestValidationError(
        [{"type": "value_error", "loc": list(loc), "msg": message, "input": None}]
    )


def validate_date_param(value: str, field: str) -> date_cls:
    """Validate a YYYY-MM-DD query/path parameter and return the parsed date.

    We check the regex AND date.fromisoformat because each alone is too loose:
    Pydantic's ``date`` type would accept ``2026-07-06T00:00:00``, and
    ``fromisoformat`` alone accepts the no-dash basic form ``20260706``. The regex
    pins the shape; ``fromisoformat`` rejects impossible dates like ``2026-13-40``.
    """
    if not re.match(DATE_RE, value):
        raise_422("must match YYYY-MM-DD", ("query", field))
    try:
        return date_cls.fromisoformat(value)
    except ValueError:
        raise_422("not a valid calendar date", ("query", field))


def month_bounds(month: str) -> tuple[str, str]:
    """'YYYY-MM' -> (first-day, last-day) as 'YYYY-MM-DD' strings.

    A month is matched as an inclusive STRING range on the ``date`` field
    (``>= "YYYY-MM-01"`` and ``<= "YYYY-MM-<last>"``). String order equals date order
    for YYYY-MM-DD, so this range is index-friendly and never needs date parsing in Mongo.
    """
    year, mon = int(month[:4]), int(month[5:7])
    first = date_cls(year, mon, 1)
    nxt = date_cls(year + (mon == 12), (mon % 12) + 1, 1)
    last = nxt - timedelta(days=1)
    return first.isoformat(), last.isoformat()


# =========================================================================== #
# 7. REQUEST MODELS
# =========================================================================== #
# EpochMs is strict on purpose: strict=True makes Pydantic reject "123" (string),
# 1783312500000.0 (float) and true (bool), all of which a non-strict int would coerce.
# The ge/le bounds reject seconds-sized and absurd values. Every one of those is a 422.
EpochMs = Annotated[int, Field(strict=True, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]


class EmployeeCreate(BaseModel):
    """Body for POST /employees. Mirrors openapi.yaml's EmployeeCreate exactly.

    ``extra="ignore"`` drops any unknown field a client sends (e.g. a derived field),
    which is the required behaviour.
    """
    model_config = ConfigDict(extra="ignore")

    emp_code: Annotated[str, Field(pattern=EMP_CODE_RE)]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(pattern=EMAIL_RE, max_length=120)]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: Annotated[str, Field(pattern=HHMM_RE)] = "09:30"
    shift_end: Annotated[str, Field(pattern=HHMM_RE)] = "18:30"
    # joined_on stays a plain string: PyMongo cannot store a datetime.date (it raises
    # InvalidDocument), and the data model keeps calendar dates as strings on purpose.
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _joined_on_is_a_real_date(cls, v: str) -> str:
        if not re.match(DATE_RE, v):
            raise ValueError("joined_on must match YYYY-MM-DD")
        try:
            date_cls.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on is not a valid calendar date")
        return v

    @model_validator(mode="after")
    def _shifts_must_differ(self):
        # Equal start/end is invalid (not an overnight shift); overnight is end < start.
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    """Body for POST /attendance/punch-in.

    ``emp_code`` is NOT pattern-checked here: an unknown/badly shaped code at punch-in
    is a 404 (missing resource), not a 422. ``status`` is limited to the three presence
    values. ``punched_at`` is optional (defaults to now) and strictly an epoch-ms int.
    """
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    """Body for POST /attendance/punch-out."""
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeRequest(BaseModel):
    """Body for PATCH /attendance/{emp_code}/{date}.

    Only status/punch_in/punch_out can be corrected; derived fields can never be sent.
    reason and regularized_by are required. We use ``model_fields_set`` at the endpoint
    to tell "field omitted" (keep stored value) from "field supplied".
    """
    model_config = ConfigDict(extra="ignore")

    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]


# =========================================================================== #
# 8. RESPONSE SERIALISERS
# =========================================================================== #
# Every response is built by one of these. They (a) never leak MongoDB's _id, (b)
# convert every stored BSON datetime to epoch ms, and (c) fill in defaults for legacy
# seeded documents that omit fields (missing punch/work_hours -> null, missing minutes
# -> 0, missing half_day -> false, missing history -> []).

def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]),
    }


def serialize_history_entry(entry: dict) -> dict:
    """Serialise one history entry. The nested punch_in/punch_out from/to values are
    BSON dates in storage and must become epoch ms (or null) in the API - this nested
    conversion is easy to miss."""
    changes: dict = {}
    for field, delta in (entry.get("changes") or {}).items():
        if field in ("punch_in", "punch_out"):
            changes[field] = {"from": dt_to_ms(delta.get("from")), "to": dt_to_ms(delta.get("to"))}
        else:
            changes[field] = {"from": delta.get("from"), "to": delta.get("to")}
    return {
        "at": dt_to_ms(entry["at"]),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def serialize_attendance(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),                 # None when missing/open
        "late_minutes": doc.get("late_minutes") or 0,        # missing -> 0
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),        # missing -> False
        "history": [serialize_history_entry(h) for h in (doc.get("history") or [])],
    }


# =========================================================================== #
# 9. INDEX CREATION AND LIFESPAN
# =========================================================================== #
# Indexes are created idempotently at startup, each in its own try block so that a
# single failure logs a warning instead of crashing the app (the grader must still get
# a 200 from /health). The two UNIQUE indexes are what make create-employee and punch-in
# race-safe without check-then-insert.
INDEX_SPECS = [
    # (collection, keys, options). Explicit names so re-creation is a cheap no-op.
    ("employees", [("emp_code", ASCENDING)], {"name": "uq_emp_code", "unique": True}),
    ("employees", [("department", ASCENDING), ("joined_on", ASCENDING)], {"name": "department_joined_on"}),
    ("employees", [("joined_on", ASCENDING)], {"name": "joined_on"}),
    ("attendance_logs", [("emp_code", ASCENDING), ("date", ASCENDING)], {"name": "uq_emp_code_date", "unique": True}),
    ("attendance_logs", [("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "date_desc_emp_code"}),
    ("attendance_logs", [("emp_code", ASCENDING), ("punch_in", DESCENDING)], {"name": "emp_code_punch_in"}),
]


def ensure_indexes() -> None:
    for collection, keys, options in INDEX_SPECS:
        try:
            db[collection].create_index(keys, **options)
        except PyMongoError as exc:  # pragma: no cover - defensive
            logger.warning("could not create index %s on %s: %s", options.get("name"), collection, exc)


@asynccontextmanager
async def lifespan(_app: "FastAPI"):
    # Startup: make sure the indexes we rely on exist. Shutdown: nothing to do.
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Small shared helpers used by the endpoints
# --------------------------------------------------------------------------- #
def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")
    return emp


def build_attendance_filter(emp_code, date_from, date_to, status) -> dict:
    """Build the Mongo filter for GET /attendance. Shared with /admin/explain so the
    explained query is byte-for-byte the real one."""
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        date_range: dict = {}
        if date_from is not None:
            date_range["$gte"] = date_from
        if date_to is not None:
            date_range["$lte"] = date_to
        query["date"] = date_range
    if status is not None:
        query["status"] = status
    return query


# =========================================================================== #
# 10. CRUD ENDPOINTS
# =========================================================================== #
@app.get("/health")
def health():
    """Readiness probe. 200 only when MongoDB answers a ping, else 503 (never a crash)."""
    try:
        db.command("ping")
    except PyMongoError:
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    """Create an employee. No check-then-insert: we insert and let the UNIQUE index on
    emp_code reject a duplicate with DuplicateKeyError -> 409. created_at is the server
    clock, stored and returned identically."""
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
):
    """List employees, exact case-sensitive department filter, sorted by emp_code asc.
    total reflects the filter. Sort/skip/limit are done by MongoDB."""
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    cursor = (
        db.employees.find(query)
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_employee(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    """Record a punch-in. Order: validate (Pydantic) -> 404 if employee unknown ->
    truncate the instant -> compute R1 date and R2 lateness -> insert -> 409 on the
    unique (emp_code, date) index if a record already exists for that date (including a
    seeded ABSENT/LEAVE one). The unique index is what makes concurrent punch-ins safe."""
    emp = get_employee_or_404(body.emp_code)
    instant = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    record_date = attendance_date(instant, emp["shift_start"], emp["shift_end"])
    derived = compute_derived(body.status, instant, None, emp["shift_start"], emp["shift_end"], record_date)
    doc = {
        "emp_code": body.emp_code,
        "date": record_date,
        "status": body.status,
        "punch_in": instant,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": derived["late_minutes"],
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already has a record for this date")
    return serialize_attendance(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
):
    """List attendance records. date_from/date_to are inclusive string ranges;
    date_from > date_to is 422. Sort date desc then emp_code asc, with sort/skip/limit
    all pushed to MongoDB (never load the collection into memory)."""
    if date_from is not None:
        validate_date_param(date_from, "date_from")
    if date_to is not None:
        validate_date_param(date_to, "date_to")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise_422("date_from must be on or before date_to", ("query", "date_from"))

    query = build_attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    cursor = (
        db.attendance_logs.find(query)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_attendance(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    """Close the employee's most recent OPEN-able record and compute R3-R5.

    Works across midnight (for overnight shifts) because we find the record by its
    ``punch_in`` instant, not by today's date: the most recent record whose
    ``punch_in <= punched_at`` (records with a null punch_in never match a datetime
    comparison). The final write is a ``find_one_and_update`` whose filter requires
    ``punch_out: None`` - so if two punch-outs race, exactly one matches and returns 200
    and the other gets 409.
    """
    emp = get_employee_or_404(body.emp_code)
    instant = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()

    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": instant}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(status_code=404, detail="no punch-in found to close")
    # Already-punched-out (409) is checked BEFORE the time bounds (422), per the spec.
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record is already punched out")

    punch_in_instant = record["punch_in"]
    if instant <= punch_in_instant:
        raise_422("punched_at must be after punch_in", ("body", "punched_at"))
    if instant - punch_in_instant > MAX_PUNCH_SPAN:
        raise_422("punched_at must be within 24 hours of punch_in", ("body", "punched_at"))

    derived = compute_derived(
        record["status"], punch_in_instant, instant,
        emp["shift_start"], emp["shift_end"], record["date"],
    )
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},     # the punch_out:None guard makes the race safe
        {"$set": {"punch_out": instant, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:                                 # another punch-out won the race
        raise HTTPException(status_code=409, detail="record is already punched out")
    return serialize_attendance(updated)


# Fields that can appear in a history entry's `changes`, in a stable order.
CHANGEABLE_FIELDS = ("status", "punch_in", "punch_out", "work_hours",
                     "late_minutes", "overtime_minutes", "half_day")


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeRequest):
    """Manually correct one day's record, recompute all derived fields, and append one
    audit entry to ``history`` - never overwriting it.

    Steps (mirrors the contract):
      1. validate the path date; 404 if the employee or the record is missing;
      2. work out the final state (omitted fields keep their stored value);
      3. ABSENT/LEAVE clears both punch times (supplying one is 422);
      4. a presence status needs a punch_in (supplied or stored), else 422;
      5. punch_in must still map to this attendance date under R1, else 422;
      6. punch_out must be after punch_in and within 24h (checked even if only punch_in changed);
      7. recompute the four derived fields;
      8. build `changes` for every field that actually differs; empty -> 422;
      9. write once with an optimistic filter (the values we read + a history-length
         guard) so a concurrent edit cannot silently clobber this one (409 for the loser).
    """
    validate_date_param(date, "date")
    emp = get_employee_or_404(emp_code)
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(status_code=404, detail="no attendance record for that date")

    # "Supplied" = present in the body AND non-null (an explicit null is treated as omit).
    supplied = {k for k in body.model_fields_set if getattr(body, k) is not None}

    stored_status = record["status"]
    stored_pin = record.get("punch_in")
    stored_pout = record.get("punch_out")

    final_status = body.status if "status" in supplied else stored_status
    sup_pin = "punch_in" in supplied
    sup_pout = "punch_out" in supplied

    if final_status in NON_PRESENCE_STATUSES:
        # Step 3: ABSENT/LEAVE means no punch times; supplying any is a contradiction.
        if sup_pin or sup_pout:
            raise_422(f"{final_status} cannot have punch times", ("body",))
        final_pin = None
        final_pout = None
    else:
        final_pin = ms_to_dt(body.punch_in) if sup_pin else stored_pin
        final_pout = ms_to_dt(body.punch_out) if sup_pout else stored_pout
        # Step 4
        if final_pin is None:
            raise_422(f"{final_status} requires a punch_in", ("body", "punch_in"))
        # Step 5: the corrected punch_in must still belong to this record's date.
        if attendance_date(final_pin, emp["shift_start"], emp["shift_end"]) != date:
            raise_422("punch_in no longer maps to this attendance date", ("body", "punch_in"))
        # Step 6: validate the span even when only punch_in was changed.
        if final_pout is not None:
            if final_pout <= final_pin:
                raise_422("punch_out must be after punch_in", ("body", "punch_out"))
            if final_pout - final_pin > MAX_PUNCH_SPAN:
                raise_422("punch_out must be within 24 hours of punch_in", ("body", "punch_out"))

    # Step 7: recompute from the final state.
    derived = compute_derived(final_status, final_pin, final_pout,
                              emp["shift_start"], emp["shift_end"], date)
    final_values = {
        "status": final_status,
        "punch_in": final_pin,
        "punch_out": final_pout,
        "work_hours": derived["work_hours"],
        "late_minutes": derived["late_minutes"],
        "overtime_minutes": derived["overtime_minutes"],
        "half_day": derived["half_day"],
    }
    # Stored values with the same defaults the serialiser uses, so comparisons are fair.
    current_values = {
        "status": stored_status,
        "punch_in": stored_pin,
        "punch_out": stored_pout,
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes") or 0,
        "overtime_minutes": record.get("overtime_minutes") or 0,
        "half_day": bool(record.get("half_day", False)),
    }

    # Step 8: one `{from, to}` per field that actually changed. punch_in/punch_out keep
    # their BSON-datetime values here and are converted to epoch ms only on serialisation.
    changes = {}
    set_fields = {}
    for field in CHANGEABLE_FIELDS:
        before, after = current_values[field], final_values[field]
        if before != after:
            changes[field] = {"from": before, "to": after}
            set_fields[field] = after
    if not changes:
        raise_422("the correction changes nothing", ("body",))

    history_len = len(record.get("history") or [])
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Step 9: optimistic write. The filter pins the values we read AND asserts history has
    # not grown (history.<n> must not yet exist). A concurrent correction changes one of
    # these, so its competitor matches nothing and gets 409 - no lost history.
    updated = db.attendance_logs.find_one_and_update(
        {
            "emp_code": emp_code,
            "date": date,
            "status": stored_status,
            "punch_in": stored_pin,
            "punch_out": stored_pout,
            f"history.{history_len}": {"$exists": False},
        },
        {"$set": set_fields, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="record was modified concurrently")
    return serialize_attendance(updated)


# =========================================================================== #
# 11. PIPELINE BUILDERS
# =========================================================================== #
# One builder per analytics query. The real endpoint AND /admin/explain call the same
# builder, so the explained plan is exactly the plan the endpoint runs. Pipelines return
# RAW sums and counts; the final division + half-up rounding is done in Python with
# Decimal (MongoDB's $round is half-to-even, which R8 forbids).

def _presence_expr():
    """Aggregation expr: is this record a presence status (R6)?"""
    return {"$in": ["$status", list(PRESENCE_STATUSES)]}


def _weekday_expr(date_field: str = "$date"):
    """Aggregation expr: is the date string a Mon-Fri? Uses $isoDayOfWeek with NO timezone
    (the string is a calendar date; applying IST would shift it)."""
    return {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": date_field}}}, 5]}


def _present_value_expr(weekday_only: bool):
    """Aggregation expr: the 'present value' of one record - 0.5 for a half day, else 1
    for a presence status, else 0 (R5/R6). ``weekday_only`` additionally requires Mon-Fri
    (used by the monthly/department 'present_days'); the trend counts any day of week."""
    condition = {"$and": [_presence_expr(), _weekday_expr()]} if weekday_only else _presence_expr()
    half_or_full = {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}
    return {"$cond": [condition, half_or_full, 0]}


def _late_flag_expr():
    """Aggregation expr: does this record count as 'late' (late_minutes > 0)?"""
    return {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}


def _present_with_hours_expr():
    """Aggregation expr: presence status AND a non-null work_hours (for avg_work_hours)."""
    return {"$and": [_presence_expr(), {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]}


def employee_monthly_pipeline(emp_code: str, first: str, last: str) -> list:
    """One employee, one month. Match on (emp_code, date-range) hits the unique
    (emp_code, date) index. A single $group yields the raw aggregates; working_days and
    attendance_pct are finished in Python."""
    return [
        # Narrow to this employee's records within the month (index: uq_emp_code_date).
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            # present_days: Mon-Fri presence records only, half day = 0.5 (R5-R7).
            "present_days": {"$sum": _present_value_expr(weekday_only=True)},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            # late_count / totals span ALL days incl. weekends (R7).
            "late_count": {"$sum": {"$cond": [_late_flag_expr(), 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def late_leaderboard_pipeline(first: str, last: str, limit: int, department) -> list:
    """Late-comers ranking with standard competition ranking (1,2,2,4). The $match on the
    month range hits the (date, emp_code) index. Ranking order matters, hence the stage
    order below."""
    pipeline = [
        # 1. Month range + only records that are actually late (index: date_desc_emp_code).
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        # 2. Totals per employee.
        {"$group": {"_id": "$emp_code",
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "late_count": {"$sum": 1}}},
        # 3. Attach the employee; $unwind (no preserve) DROPS logs whose employee is gone.
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    # 4. Restrict to a department BEFORE ranking, so ranking happens inside it.
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        # 5. Competition rank by total desc. (A $match, not a $sort, follows - deliberately:
        #    a $sort right after $setWindowFields on the same key can corrupt $rank.)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        # 6. limit applies AFTER ranking, so ties at the cutoff can exceed `limit` rows.
        {"$match": {"rank": {"$lte": limit}}},
        # 7. Final row order.
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id",
                      "name": "$emp.name", "department": "$emp.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


def department_summary_pipeline(first: str, last: str, department) -> list:
    """Department summary for a month. STARTS from employees (so zero-log people still
    count), joins each employee's month of logs into a single stats sub-document, then
    re-aggregates by department."""
    employee_match = {"joined_on": {"$lte": last}}            # R9: joined on or before month end.
    if department is not None:
        employee_match["department"] = department
    return [
        # Index: department_joined_on (with dept) or joined_on (without).
        {"$match": employee_match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code", "foreignField": "emp_code",   # uses (emp_code, date) index
            "pipeline": [
                {"$match": {"$expr": {"$and": [{"$gte": ["$date", first]}, {"$lte": ["$date", last]}]}}},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": _present_value_expr(weekday_only=True)},
                    "late_count": {"$sum": {"$cond": [_late_flag_expr(), 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                    # Carry the Decimal sum and the count SEPARATELY; divide once in Python
                    # so exact ties stay exact (9.12 + 9.13 -> 9.13, never 9.12).
                    "work_sum": {"$sum": {"$cond": [_present_with_hours_expr(),
                                                    {"$toDecimal": "$work_hours"}, {"$toDecimal": 0}]}},
                    "work_count": {"$sum": {"$cond": [_present_with_hours_expr(), 1, 0]}},
                }},
            ],
            "as": "stats",
        }},
        # Keep employees with no logs (their stats sub-doc is simply absent).
        {"$unwind": {"path": "$stats", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},                            # counts employees, not logs
            "present_days": {"$sum": {"$ifNull": ["$stats.present_days", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$stats.late_count", 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$stats.total_late_minutes", 0]}},
            "leave_count": {"$sum": {"$ifNull": ["$stats.leave_count", 0]}},
            "on_duty_count": {"$sum": {"$ifNull": ["$stats.on_duty_count", 0]}},
            "work_sum": {"$sum": {"$ifNull": ["$stats.work_sum", 0]}},
            "work_count": {"$sum": {"$ifNull": ["$stats.work_count", 0]}},
        }},
        {"$sort": {"_id": 1}},                                   # department ascending
    ]


def department_trend_pipeline(department, from_str, to_str, from_dt, to_plus1_dt, dept_codes) -> list:
    """Daily series with gap filling and a 7-day moving average, all in MongoDB.

    The day 'spine' is built from a row that is GUARANTEED to exist - one department
    employee - projected to a single ``day`` equal to ``from``, then $densify fills every
    day up to ``to`` (upper bound exclusive). Statistics are joined onto the spine per day,
    so a range with zero logs still yields one row per day with present_count 0.
    """
    return [
        {"$match": {"department": department}},       # index: department_joined_on
        {"$limit": 1},
        {"$project": {"_id": 0, "day": {"$dateFromString": {"dateString": from_str}}}},
        # One document per calendar day in [from, to] (to_plus1 is exclusive).
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [from_dt, to_plus1_dt]}}},
        {"$set": {
            "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},   # no tz: pure calendar day
        }},
        # headcount = dept employees who had joined on or before this day (R9).
        {"$lookup": {"from": "employees", "let": {"d": "$date"}, "pipeline": [
            {"$match": {"department": department}},                      # index prefix
            {"$match": {"$expr": {"$lte": ["$joined_on", "$$d"]}}},
            {"$count": "n"},
        ], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$first": "$hc.n"}, 0]}}},
        # present_count / late_count = this day's records for the department (any weekday).
        {"$lookup": {"from": "attendance_logs", "let": {"d": "$date"}, "pipeline": [
            {"$match": {"emp_code": {"$in": dept_codes}}},               # index: emp_code prefix
            {"$match": {"$expr": {"$eq": ["$date", "$$d"]}}},
            {"$group": {"_id": None,
                        "present_count": {"$sum": _present_value_expr(weekday_only=False)},
                        "late_count": {"$sum": {"$cond": [_late_flag_expr(), 1, 0]}}}},
        ], "as": "att"}},
        {"$set": {
            "present_count": {"$ifNull": [{"$first": "$att.present_count"}, 0]},
            "late_count": {"$ifNull": [{"$first": "$att.late_count"}, 0]},
        }},
        # Raw (unrounded) attendance rate: only on working days with headcount > 0, else null.
        # A working day with no records is present_count 0 / headcount -> 0.0 (not null).
        {"$set": {"rate_raw": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]},
            None,
        ]}}},
        # Moving average over the current + up to 6 preceding rows; $avg skips nulls.
        # (No trailing $sort on `day`: densify already orders the spine, and a $sort here
        #  would risk the 8.x window/sort optimiser quirk.)
        {"$setWindowFields": {"sortBy": {"day": 1},
                              "output": {"moving_avg_raw": {"$avg": "$rate_raw",
                                                            "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
                      "present_count": 1, "late_count": 1, "rate_raw": 1, "moving_avg_raw": 1}},
    ]


def _to_decimal(value) -> Decimal:
    """Coerce a Mongo numeric (Decimal128 / int / float) to a Python Decimal exactly."""
    if isinstance(value, Decimal128):
        return value.to_decimal()
    return Decimal(str(value))


def count_working_days(joined_on: str, first: str, last: str) -> int:
    """R7/R9: number of Mon-Fri dates in the month, counted from the later of the month
    start and ``joined_on``, with no holiday calendar and no cap at today. 0 if the
    employee joined after the month ended."""
    start = max(joined_on, first)       # string comparison == date comparison for YYYY-MM-DD
    if start > last:
        return 0
    cursor = date_cls.fromisoformat(start)
    end = date_cls.fromisoformat(last)
    count = 0
    while cursor <= end:
        if cursor.weekday() < 5:        # Mon..Fri
            count += 1
        cursor += timedelta(days=1)
    return count


# =========================================================================== #
# 12. ANALYTICS ENDPOINTS
# =========================================================================== #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_RE)):
    """Monthly summary for one employee. 404 if unknown; a month with no logs is zeros."""
    emp = get_employee_or_404(emp_code)
    first, last = month_bounds(month)
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, first, last)))
    agg = rows[0] if rows else {}

    present_days = float(agg.get("present_days", 0) or 0)
    working_days = count_working_days(emp["joined_on"], first, last)
    attendance_pct = None
    if working_days:
        pct = Decimal(str(present_days)) * Decimal(100) / Decimal(working_days)
        attendance_pct = float(round_half_up(pct, 2))

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(agg.get("leave_days", 0) or 0),
        "late_count": int(agg.get("late_count", 0) or 0),
        "total_late_minutes": int(agg.get("total_late_minutes", 0) or 0),
        "total_overtime_minutes": int(agg.get("total_overtime_minutes", 0) or 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_RE), department: Optional[str] = None):
    """Department summary for a month; one item per department, sorted ascending.
    An unknown department filter returns an empty items list (not 404)."""
    first, last = month_bounds(month)
    rows = list(db.employees.aggregate(department_summary_pipeline(first, last, department)))
    items = []
    for row in rows:
        work_count = int(row.get("work_count", 0) or 0)
        avg_work_hours = None
        if work_count:
            avg_work_hours = float(round_half_up(_to_decimal(row["work_sum"]) / Decimal(work_count), 2))
        items.append({
            "department": row["_id"],
            "headcount": int(row["headcount"]),
            "present_days": float(row.get("present_days", 0) or 0),
            "avg_work_hours": avg_work_hours,
            "late_count": int(row.get("late_count", 0) or 0),
            "total_late_minutes": int(row.get("total_late_minutes", 0) or 0),
            "leave_count": int(row.get("leave_count", 0) or 0),
            "on_duty_count": int(row.get("on_duty_count", 0) or 0),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_RE),
    limit: int = Query(DEFAULT_LEADERBOARD_LIMIT, ge=1, le=MAX_LEADERBOARD_LIMIT),
    department: Optional[str] = None,
):
    """Late-comers leaderboard (standard competition ranking; limit applies after ranking)."""
    first, last = month_bounds(month)
    rows = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(first, last, limit, department)))
    items = [{
        "rank": int(r["rank"]),
        "emp_code": r["emp_code"],
        "name": r["name"],
        "department": r["department"],
        "total_late_minutes": int(r["total_late_minutes"]),
        "late_count": int(r["late_count"]),
    } for r in rows]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from"),       # 'from' is a Python keyword
    to: str = Query(...),
):
    """Daily attendance trend for a department, one row per day incl. weekends and empty days."""
    from_date = validate_date_param(from_, "from")
    to_date = validate_date_param(to, "to")
    if to_date < from_date:
        raise_422("to must be on or after from", ("query", "to"))
    if (to_date - from_date).days + 1 > MAX_TREND_DAYS:        # inclusive day count
        raise_422(f"range must not exceed {MAX_TREND_DAYS} days", ("query", "to"))
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(status_code=404, detail="department not found")

    dept_codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"emp_code": 1, "_id": 0})]
    from_dt = datetime(from_date.year, from_date.month, from_date.day, tzinfo=timezone.utc)
    to_plus1_dt = datetime(to_date.year, to_date.month, to_date.day, tzinfo=timezone.utc) + timedelta(days=1)

    rows = list(db.employees.aggregate(
        department_trend_pipeline(department, from_, to, from_dt, to_plus1_dt, dept_codes)
    ))
    items = []
    for row in rows:
        rate_raw = row.get("rate_raw")
        moving_raw = row.get("moving_avg_raw")
        items.append({
            "date": row["date"],
            "is_working_day": bool(row["is_working_day"]),
            "headcount": int(row["headcount"]),
            "present_count": float(row.get("present_count", 0) or 0),
            "late_count": int(row.get("late_count", 0) or 0),
            "attendance_rate": None if rate_raw is None else float(round_half_up(Decimal(str(rate_raw)), 4)),
            "moving_avg_7d": None if moving_raw is None else float(round_half_up(Decimal(str(moving_raw)), 4)),
        })
    return {"department": department, "items": items}


# =========================================================================== #
# 13. EXPLAIN ENDPOINT
# =========================================================================== #
# Proves the real queries use indexes. For each named endpoint we rebuild its main
# query/pipeline with the SAME builder the endpoint uses, run MongoDB's `explain` with
# executionStats verbosity, and return the raw plan. The raw document contains BSON types
# (Timestamp, Int64, ...) that FastAPI cannot serialise, so we round-trip it through
# bson.json_util first. The grader asserts the plan has IXSCAN and no COLLSCAN at 100k.

EXPLAIN_ENDPOINTS = ("attendance_list", "employee_monthly", "department_summary",
                     "late_leaderboard", "department_trend")


def _explain_find(collection, filter_, sort, skip, limit):
    command = {"find": collection, "filter": filter_, "sort": sort, "skip": skip, "limit": limit}
    return collection, db.command("explain", command, verbosity="executionStats")


def _explain_aggregate(collection, pipeline):
    command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    return collection, db.command("explain", command, verbosity="executionStats")


def _require(value, name):
    if value is None:
        raise_422(f"{name} is required for this endpoint", ("query", name))
    return value


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(DEFAULT_LEADERBOARD_LIMIT, ge=1, le=MAX_LEADERBOARD_LIMIT),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
):
    if endpoint not in EXPLAIN_ENDPOINTS:
        raise_422(f"endpoint must be one of {', '.join(EXPLAIN_ENDPOINTS)}", ("path", "endpoint"))

    if endpoint == "attendance_list":
        if date_from is not None:
            validate_date_param(date_from, "date_from")
        if date_to is not None:
            validate_date_param(date_to, "date_to")
        if date_from is not None and date_to is not None and date_from > date_to:
            raise_422("date_from must be on or before date_to", ("query", "date_from"))
        filter_ = build_attendance_filter(emp_code, date_from, date_to, status)
        collection, explain = _explain_find(
            "attendance_logs", filter_,
            {"date": -1, "emp_code": 1}, (page - 1) * page_size, page_size,
        )

    elif endpoint == "employee_monthly":
        _require(emp_code, "emp_code")
        _require(month, "month")
        first, last = month_bounds(month)
        collection, explain = _explain_aggregate(
            "attendance_logs", employee_monthly_pipeline(emp_code, first, last))

    elif endpoint == "department_summary":
        _require(month, "month")
        first, last = month_bounds(month)
        collection, explain = _explain_aggregate(
            "employees", department_summary_pipeline(first, last, department))

    elif endpoint == "late_leaderboard":
        _require(month, "month")
        first, last = month_bounds(month)
        collection, explain = _explain_aggregate(
            "attendance_logs", late_leaderboard_pipeline(first, last, limit, department))

    else:  # department_trend
        _require(department, "department")
        _require(from_, "from")
        _require(to, "to")
        from_date = validate_date_param(from_, "from")
        to_date = validate_date_param(to, "to")
        dept_codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"emp_code": 1, "_id": 0})]
        from_dt = datetime(from_date.year, from_date.month, from_date.day, tzinfo=timezone.utc)
        to_plus1_dt = datetime(to_date.year, to_date.month, to_date.day, tzinfo=timezone.utc) + timedelta(days=1)
        collection, explain = _explain_aggregate(
            "employees",
            department_trend_pipeline(department, from_, to, from_dt, to_plus1_dt, dept_codes),
        )

    # BSON -> plain JSON (Timestamp/Int64/Binary become JSON-safe).
    explain_json = json.loads(bson.json_util.dumps(explain))
    return {"endpoint": endpoint, "collection": collection, "explain": explain_json}
