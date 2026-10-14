"""HROne Attendance & Analytics API -- FastAPI + MongoDB 7.0."""

import json
import os
import threading
from datetime import UTC, datetime, timedelta, timezone
from datetime import date as date_type
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

# ---------------------------------------------------------------------------
# Environment & Constants
# ---------------------------------------------------------------------------

load_dotenv(override=False)

MONGO_URI: str = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB: str = os.getenv("MONGO_DB", "attendance_db")

IST = timezone(timedelta(hours=5, minutes=30))
UTC = UTC  # shadows the stdlib import, clearer than timezone.utc everywhere

EPOCH_MS_MIN: int = 100_000_000_000
EPOCH_MS_MAX: int = 4_102_444_800_000

GRACE_PERIOD_SECONDS: int = 600        # 10-minute grace for late computation
OVERTIME_THRESHOLD_MINUTES: int = 30   # Minimum overtime to count
HALF_DAY_THRESHOLD_HOURS: float = 4.50 # Work hours below this = half day
MAX_PUNCH_SPREAD_HOURS: float = 24.0   # punch_out must be within 24h of punch_in
TREND_MAX_DAYS: int = 92               # Maximum date range for trend endpoint

# ---------------------------------------------------------------------------
# MongoDB Client & Collections
# ---------------------------------------------------------------------------

client = MongoClient(
    MONGO_URI,
    tz_aware=True,
    tzinfo=UTC,
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
    socketTimeoutMS=30000,
)
db = client[MONGO_DB]

employees_col = db["employees"]
attendance_col = db["attendance_logs"]

# ---------------------------------------------------------------------------
# Index Definitions
# ---------------------------------------------------------------------------

EMPLOYEE_INDEXES: list[dict[str, Any]] = [
    {"keys": [("emp_code", 1)], "unique": True},
    {"keys": [("department", 1)]},
    {"keys": [("joined_on", 1)]},
    {"keys": [("department", 1), ("joined_on", 1)]},
]

ATTENDANCE_INDEXES: list[dict[str, Any]] = [
    {"keys": [("emp_code", 1), ("date", 1)], "unique": True},
    {"keys": [("date", -1), ("emp_code", 1)]},
    {"keys": [("date", 1), ("status", 1)]},
    {"keys": [("emp_code", 1), ("date", 1), ("status", 1)]},
    {"keys": [("emp_code", 1), ("late_minutes", 1)]},
]

_indexes_created: bool = False
_index_lock = threading.Lock()


def _create_indexes() -> None:
    """Create indexes on both collections."""
    for idx in EMPLOYEE_INDEXES:
        employees_col.create_index(idx["keys"], unique=idx.get("unique", False))
    for idx in ATTENDANCE_INDEXES:
        attendance_col.create_index(idx["keys"], unique=idx.get("unique", False))


def _ensure_indexes() -> None:
    """Create indexes on first successful /health (double-checked locking)."""
    global _indexes_created
    if _indexes_created:
        return
    with _index_lock:
        if _indexes_created:
            return
        try:
            _create_indexes()
            _indexes_created = True
        except Exception:
            # Will retry on next /health call
            pass


# ---------------------------------------------------------------------------
# FastAPI Application & Lifespan
# ---------------------------------------------------------------------------

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Try creating indexes at startup; falls back to lazy creation on /health."""
    try:
        _create_indexes()
        global _indexes_created
        _indexes_created = True
    except Exception:
        pass
    yield


app = FastAPI(
    title="HROne Attendance & Analytics API",
    version="1.0.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------


def to_utc_dt(ms: int) -> datetime:
    """epoch ms -> UTC datetime, truncated to whole seconds."""
    secs = ms // 1000
    return datetime.fromtimestamp(secs, tz=UTC)


def to_ms(dt: datetime) -> int:
    """UTC datetime -> epoch ms (truncated to seconds)."""
    return int(dt.timestamp()) * 1000


def truncate_to_seconds(dt: datetime) -> datetime:
    """Zero out microseconds."""
    return dt.replace(microsecond=0)


def to_ist(dt_utc: datetime) -> datetime:
    """UTC -> IST (UTC+5:30)."""
    return dt_utc.astimezone(IST)


def get_ist_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """Get the attendance date in IST for a punch-in.

    For overnight shifts (end <= start), if punch-in IST is before shift end
    (early morning), we attribute it to the previous calendar day.
    """
    punch_ist = to_ist(punch_in_utc)
    shift_start_h, shift_start_m = parse_shift_time(shift_start)
    shift_end_h, shift_end_m = parse_shift_time(shift_end)

    # Determine if this is an overnight shift
    is_overnight = (shift_end_h, shift_end_m) <= (shift_start_h, shift_start_m)

    if is_overnight:
        punch_time_minutes = punch_ist.hour * 60 + punch_ist.minute
        shift_end_minutes = shift_end_h * 60 + shift_end_m
        # If punch-in is before shift_end (in the early morning after midnight),
        # the attendance date is the previous day
        if punch_time_minutes < shift_end_minutes:
            prev_day = punch_ist.date() - timedelta(days=1)
            return prev_day.strftime("%Y-%m-%d")

    return punch_ist.strftime("%Y-%m-%d")


def parse_shift_time(shift_str: str) -> tuple[int, int]:
    """'09:30' -> (9, 30)."""
    parts = shift_str.split(":")
    return int(parts[0]), int(parts[1])


def compute_late_minutes(
    punch_in_utc: datetime,
    shift_start: str,
    date_str: str | None = None,
) -> int:
    """Minutes late after grace period (F1).

    If date_str is given, anchor the shift start to the attendance date
    instead of the punch-in's calendar day (fixes overnight shift bug).
    """
    shift_h, shift_m = parse_shift_time(shift_start)

    if date_str:
        # Anchor shift start to the attendance date in IST (F1 fix)
        date_parts = date_str.split("-")
        anchor_date = date_type(
            int(date_parts[0]), int(date_parts[1]), int(date_parts[2])
        )
        shift_start_ist = datetime(
            anchor_date.year,
            anchor_date.month,
            anchor_date.day,
            shift_h,
            shift_m,
            tzinfo=IST,
        )
    else:
        # Fall back to punch-in's calendar day
        punch_ist = to_ist(punch_in_utc)
        shift_start_ist = datetime(
            punch_ist.year,
            punch_ist.month,
            punch_ist.day,
            shift_h,
            shift_m,
            tzinfo=IST,
        )

    shift_start_utc = shift_start_ist.astimezone(UTC)
    punch_in_utc_trunc = truncate_to_seconds(punch_in_utc)

    diff_seconds = (punch_in_utc_trunc - shift_start_utc).total_seconds()

    # Grace period: not late if within 10 minutes of shift start
    if diff_seconds <= GRACE_PERIOD_SECONDS:
        return 0

    return int(diff_seconds // 60)


def compute_overtime_minutes(
    punch_out_utc: datetime,
    shift_end: str,
    date_str: str,
    shift_start: str,
) -> int:
    """Overtime minutes past the 30-min threshold (R3).

    For overnight shifts (end <= start), shift end is on the next day.
    """
    shift_end_h, shift_end_m = parse_shift_time(shift_end)
    shift_start_h, shift_start_m = parse_shift_time(shift_start)

    date_parts = date_str.split("-")
    anchor_date = date_type(int(date_parts[0]), int(date_parts[1]), int(date_parts[2]))

    # Determine if overnight shift
    is_overnight = (shift_end_h, shift_end_m) <= (shift_start_h, shift_start_m)

    if is_overnight:
        # Shift end is on the next calendar day
        shift_end_date = anchor_date + timedelta(days=1)
    else:
        shift_end_date = anchor_date

    shift_end_ist = datetime(
        shift_end_date.year,
        shift_end_date.month,
        shift_end_date.day,
        shift_end_h,
        shift_end_m,
        tzinfo=IST,
    )
    shift_end_utc = shift_end_ist.astimezone(UTC)
    punch_out_utc_trunc = truncate_to_seconds(punch_out_utc)

    diff_seconds = (punch_out_utc_trunc - shift_end_utc).total_seconds()
    overtime_min = int(diff_seconds // 60)

    if overtime_min >= OVERTIME_THRESHOLD_MINUTES:
        return overtime_min
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """Work hours with half-up rounding via Decimal (R4)."""
    diff_seconds = (punch_out_utc - punch_in_utc).total_seconds()
    hours = diff_seconds / 3600.0
    return float(Decimal(str(hours)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: float | None) -> bool:
    """True if work_hours < 4.50 (R5). None -> False."""
    if work_hours is None:
        return False
    return work_hours < HALF_DAY_THRESHOLD_HOURS


def round_half_up_2(value: Any) -> float:
    """Round to 2dp with Decimal ROUND_HALF_UP (not banker's round)."""
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round_half_up_4(value: Any) -> float:
    """Round to 4dp with Decimal ROUND_HALF_UP."""
    return float(Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def is_present_status(status: str) -> bool:
    """True if status is PRESENT, WFH, or ON_DUTY."""
    return status in ("PRESENT", "WFH", "ON_DUTY")


def validate_epoch_ms(v: Any) -> int:
    """Validate epoch ms -- rejects bool/non-int and out-of-range values."""
    if isinstance(v, bool):
        raise TypeError("Epoch milliseconds must be an integer, not bool")
    if not isinstance(v, int):
        raise TypeError("Epoch milliseconds must be an integer")
    if v < EPOCH_MS_MIN or v > EPOCH_MS_MAX:
        raise ValueError(
            f"Epoch milliseconds must be between {EPOCH_MS_MIN} and {EPOCH_MS_MAX}"
        )
    return v


def working_days_in_month(year: int, month: int, joined_on: str) -> int:
    """Mon-Fri working days in a month, capped by the employee's join date (R7)."""
    month_start = date_type(year, month, 1)

    if month == 12:
        month_end = date_type(year, 12, 31)
    else:
        month_end = date_type(year, month + 1, 1) - timedelta(days=1)

    joined_parts = joined_on.split("-")
    joined_date = date_type(
        int(joined_parts[0]), int(joined_parts[1]), int(joined_parts[2])
    )

    start = max(month_start, joined_date)

    # If the employee joined after the month ends, no working days
    if start > month_end:
        return 0

    count = 0
    current = start
    while current <= month_end:
        if current.weekday() < 5:  # Mon=0, Tue=1, ..., Fri=4
            count += 1
        current += timedelta(days=1)
    return count


def _sanitize_doc(doc: dict) -> dict:
    """Strip _id and id from a MongoDB doc."""
    result = {k: v for k, v in doc.items() if k not in ("_id", "id")}
    return result


def serialize_attendance_record(doc: dict) -> dict:
    """Convert datetimes to epoch ms and strip _id."""
    result = {
        "emp_code": doc.get("emp_code"),
        "date": doc.get("date"),
        "status": doc.get("status"),
        "punch_in": to_ms(doc["punch_in"]) if doc.get("punch_in") is not None else None,
        "punch_out": to_ms(doc["punch_out"]) if doc.get("punch_out") is not None else None,
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "work_hours": doc.get("work_hours"),
        "half_day": doc.get("half_day", False),
        "regularized_by": doc.get("regularized_by"),
        "reason": doc.get("reason"),
        "history": serialize_history(doc.get("history", [])),
        "created_at": to_ms(doc["created_at"]) if doc.get("created_at") is not None else None,
        "updated_at": to_ms(doc["updated_at"]) if doc.get("updated_at") is not None else None,
    }
    return result


def serialize_history(history: list) -> list:
    """Convert history entry timestamps to epoch ms."""
    result = []
    for entry in history:
        h = dict(entry)
        if "timestamp" in h and h["timestamp"] is not None:
            h["timestamp"] = to_ms(h["timestamp"])
        # Remove any _id that might have been inserted
        h.pop("_id", None)
        result.append(h)
    return result


def serialize_employee(doc: dict) -> dict:
    """Convert employee doc to API response (epoch ms, no _id)."""
    result = {
        "emp_code": doc.get("emp_code"),
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": to_ms(doc["created_at"]) if doc.get("created_at") is not None else None,
    }
    return result


def _validate_month_str(month: str) -> tuple[int, int]:
    """Parse 'YYYY-MM' and return (year, month). Raises 422 on bad format."""
    try:
        parts = month.split("-")
        year = int(parts[0])
        mon = int(parts[1])
        if mon < 1 or mon > 12:
            raise ValueError("Month must be 1-12")
        return year, mon
    except (ValueError, IndexError):
        raise HTTPException(status_code=422, detail="Invalid month format, expected YYYY-MM")


def _month_end_str(year: int, mon: int) -> str:
    """Last day of the month as 'YYYY-MM-DD'."""
    if mon == 12:
        return f"{year}-12-31"
    next_month = date_type(year, mon + 1, 1)
    month_end = next_month - timedelta(days=1)
    return month_end.strftime("%Y-%m-%d")


def _month_start_str(year: int, mon: int) -> str:
    """First day of the month as 'YYYY-MM-DD'."""
    return f"{year}-{mon:02d}-01"


def _validate_date_str(date_str: str, field_name: str = "date") -> date_type:
    """Parse 'YYYY-MM-DD' into a date object. Raises 422 on bad format."""
    try:
        parts = date_str.split("-")
        return date_type(int(parts[0]), int(parts[1]), int(parts[2]))
    except (ValueError, IndexError):
        raise HTTPException(
            status_code=422,
            detail=f"Invalid {field_name} format, expected YYYY-MM-DD",
        )


# ---------------------------------------------------------------------------
# Pydantic Models
# ---------------------------------------------------------------------------

class EmployeeCreate(BaseModel):
    """Employee creation schema (emp_code pattern, email, shifts, join date)."""
    emp_code: str = Field(
        ...,
        pattern=r"^EMP\d{4,6}$",
        description="Employee code: EMP followed by 4-6 digits",
    )
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(
        ...,
        pattern=r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$",
        description="Valid email address",
    )
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(
        ...,
        pattern=r"^([01]\d|2[0-3]):([0-5]\d)$",
        description="Shift start time in HH:MM 24-hour format",
    )
    shift_end: str = Field(
        ...,
        pattern=r"^([01]\d|2[0-3]):([0-5]\d)$",
        description="Shift end time in HH:MM 24-hour format",
    )
    joined_on: str = Field(
        ...,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Join date in YYYY-MM-DD format",
    )

    @model_validator(mode="after")
    def validate_shifts_not_equal(self):
        """shift_start != shift_end."""
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must not equal shift_end")
        return self

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on_date(cls, v: str) -> str:
        """Check joined_on is a real calendar date."""
        try:
            parts = v.split("-")
            date_type(int(parts[0]), int(parts[1]), int(parts[2]))
        except ValueError:
            raise ValueError("Invalid joined_on date")
        return v


class PunchInRequest(BaseModel):
    """Punch-in: strict model, punched_at defaults to now, status defaults to PRESENT."""
    model_config = {"strict": True}

    emp_code: str
    punched_at: int | None = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, v):
        """Validate punched_at if provided."""
        if v is not None:
            return validate_epoch_ms(v)
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        """Status must be PRESENT, WFH, or ON_DUTY."""
        if v not in ("PRESENT", "WFH", "ON_DUTY"):
            raise ValueError("Status must be one of: PRESENT, WFH, ON_DUTY")
        return v


class PunchOutRequest(BaseModel):
    """Punch-out: strict model, punched_at defaults to now."""
    model_config = {"strict": True}

    emp_code: str
    punched_at: int | None = None

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, v):
        """Validate punched_at if provided."""
        if v is not None:
            return validate_epoch_ms(v)
        return v


class RegularizeRequest(BaseModel):
    """Regularize: strict model, needs regularized_by + reason."""
    model_config = {"strict": True}

    status: str | None = None
    punch_in: int | None = None
    punch_out: int | None = None
    regularized_by: str = Field(..., min_length=1, max_length=50)
    reason: str = Field(..., min_length=1, max_length=200)

    @field_validator("punch_in")
    @classmethod
    def validate_punch_in(cls, v):
        """Validate punch_in if provided."""
        if v is not None:
            return validate_epoch_ms(v)
        return v

    @field_validator("punch_out")
    @classmethod
    def validate_punch_out(cls, v):
        """Validate punch_out if provided."""
        if v is not None:
            return validate_epoch_ms(v)
        return v


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------
@app.get("/health")
async def health_check():
    """Ping MongoDB, lazily create indexes on first success. 503 if unreachable."""
    try:
        client.admin.command("ping")
        _ensure_indexes()
        return {"status": "ok"}
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "detail": str(e)},
        )


# ---------------------------------------------------------------------------
# POST /employees  (201)
# ---------------------------------------------------------------------------
@app.post("/employees", status_code=201)
async def create_employee(body: EmployeeCreate):
    """Create an employee. 409 if emp_code already exists."""
    now = datetime.now(UTC)
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": truncate_to_seconds(now),
    }
    try:
        employees_col.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Employee with this emp_code already exists",
        )
    return serialize_employee(doc)


# ---------------------------------------------------------------------------
# GET /employees
# ---------------------------------------------------------------------------
@app.get("/employees")
async def list_employees(
    department: str | None = Query(None, description="Filter by department"),
    page: int = Query(1, ge=1, description="Page number (1-based)"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
):
    """Paginated employee list, sorted by emp_code, optional dept filter."""
    filter_q: dict = {}
    if department:
        filter_q["department"] = department

    total = employees_col.count_documents(filter_q)
    skip = (page - 1) * page_size

    cursor = (
        employees_col.find(filter_q)
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )

    items = [serialize_employee(doc) for doc in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# POST /attendance/punch-in  (201)
# ---------------------------------------------------------------------------
@app.post("/attendance/punch-in", status_code=201)
async def punch_in(body: PunchInRequest):
    """Punch in. 404 if unknown emp, 409 if already punched in today."""
    # Verify employee exists
    emp = employees_col.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    # Determine punch_in_utc
    if body.punched_at is not None:
        punch_in_utc = to_utc_dt(body.punched_at)
    else:
        punch_in_utc = datetime.now(UTC)

    punch_in_utc = truncate_to_seconds(punch_in_utc)

    # Derive attendance date per R1
    date_str = get_ist_date(punch_in_utc, emp["shift_start"], emp["shift_end"])

    # Compute late_minutes with date_str (F1 fix)
    late_minutes = compute_late_minutes(
        punch_in_utc, emp["shift_start"], date_str=date_str
    )

    now = datetime.now(UTC)
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "work_hours": None,
        "half_day": False,
        "regularized_by": None,
        "reason": None,
        "history": [],
        "created_at": truncate_to_seconds(now),
        "updated_at": truncate_to_seconds(now),
    }

    try:
        attendance_col.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Attendance record already exists for this employee and date",
        )

    return serialize_attendance_record(doc)


# ---------------------------------------------------------------------------
# POST /attendance/punch-out
# ---------------------------------------------------------------------------
@app.post("/attendance/punch-out")
async def punch_out(body: PunchOutRequest):
    """Punch out. Validates 24h limit, computes derived fields, atomic update (F4)."""
    # Verify employee
    emp = employees_col.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    # Determine punch_out_utc
    if body.punched_at is not None:
        punch_out_utc = to_utc_dt(body.punched_at)
    else:
        punch_out_utc = datetime.now(UTC)

    punch_out_utc = truncate_to_seconds(punch_out_utc)

    # Find all records for this employee with present-type status
    present_statuses = ["PRESENT", "WFH", "ON_DUTY"]
    all_records = list(
        attendance_col.find(
            {"emp_code": body.emp_code, "status": {"$in": present_statuses}}
        ).sort("punch_in", -1)
    )

    # Find most recent record with punch_in <= punched_at and present status (F4)
    candidates = [
        r for r in all_records
        if r.get("punch_in") is not None and r["punch_in"] <= punch_out_utc
    ]

    if not candidates:
        # Check if there are any attendance records at all for this employee
        any_record = attendance_col.find_one({"emp_code": body.emp_code})
        if not any_record:
            raise HTTPException(
                status_code=404,
                detail="No attendance records found for this employee",
            )
        else:
            raise HTTPException(
                status_code=422,
                detail="Punch-out time is before all punch-in times",
            )

    # Pick the most recent candidate (already sorted by punch_in DESC)
    record = candidates[0]

    # Validate punch_out > punch_in
    if punch_out_utc <= record["punch_in"]:
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be after punch-in",
        )

    # Validate within 24 hours
    diff_hours = (punch_out_utc - record["punch_in"]).total_seconds() / 3600
    if diff_hours > MAX_PUNCH_SPREAD_HOURS:
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be within 24 hours of punch-in",
        )

    # Check already punched out
    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=409,
            detail="Employee has already punched out for this record",
        )

    # Compute derived fields
    work_hours = compute_work_hours(record["punch_in"], punch_out_utc)
    overtime_minutes = compute_overtime_minutes(
        punch_out_utc, emp["shift_end"], record["date"], emp["shift_start"]
    )
    half_day = compute_half_day(work_hours)

    now = datetime.now(UTC)

    # Atomic update: find_one_and_update with filter (F4)
    # Match the specific record that is NOT yet punched out
    filter_q = {
        "emp_code": body.emp_code,
        "date": record["date"],
        "$or": [
            {"punch_out": None},
            {"punch_out": {"$exists": False}},
        ],
    }
    update = {
        "$set": {
            "punch_out": punch_out_utc,
            "work_hours": work_hours,
            "overtime_minutes": overtime_minutes,
            "half_day": half_day,
            "updated_at": truncate_to_seconds(now),
        }
    }

    updated = attendance_col.find_one_and_update(filter_q, update)
    if not updated:
        raise HTTPException(
            status_code=409,
            detail="Concurrent update conflict - record may have already been punched out",
        )

    # Fetch the final document for response
    final = attendance_col.find_one({"emp_code": body.emp_code, "date": record["date"]})
    return serialize_attendance_record(final)


# ---------------------------------------------------------------------------
# GET /attendance
# ---------------------------------------------------------------------------
@app.get("/attendance")
async def list_attendance(
    emp_code: str | None = Query(None, description="Filter by employee code"),
    date: str | None = Query(None, description="Filter by date (YYYY-MM-DD)"),
    status: str | None = Query(None, description="Filter by status"),
    department: str | None = Query(None, description="Filter by department"),
    page: int = Query(1, ge=1, description="Page number (1-based)"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
):
    """Paginated attendance list, sorted by (date DESC, emp_code ASC)."""
    filter_q: dict = {}
    if emp_code:
        filter_q["emp_code"] = emp_code
    if date:
        filter_q["date"] = date
    if status:
        filter_q["status"] = status
    if department:
        # Look up emp_codes in this department
        dept_emp_codes = employees_col.distinct("emp_code", {"department": department})
        if dept_emp_codes:
            if "emp_code" in filter_q:
                # Intersect with existing emp_code filter
                existing = filter_q["emp_code"]
                if isinstance(existing, str):
                    if existing in dept_emp_codes:
                        filter_q["emp_code"] = existing
                    else:
                        filter_q["emp_code"] = {"$in": []}
            else:
                filter_q["emp_code"] = {"$in": dept_emp_codes}
        else:
            filter_q["emp_code"] = {"$in": []}

    total = attendance_col.count_documents(filter_q)
    skip = (page - 1) * page_size

    cursor = (
        attendance_col.find(filter_q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    items = [serialize_attendance_record(doc) for doc in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# PATCH /attendance/{emp_code}/{date}
# ---------------------------------------------------------------------------
@app.patch("/attendance/{emp_code}/{date}")
async def regularize_attendance(
    emp_code: str = Path(..., description="Employee code"),
    date: str = Path(..., description="Attendance date (YYYY-MM-DD)"),
    body: RegularizeRequest = ...,
):
    """Partial-update attendance. Recomputes derived fields, optimistic concurrency (F8)."""
    # Validate date format
    _validate_date_str(date, "date")

    # Verify employee
    emp = employees_col.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    # Fetch record
    record = attendance_col.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    # Track changes and new values
    changes: dict = {}
    new_status = record.get("status")
    new_punch_in = record.get("punch_in")
    new_punch_out = record.get("punch_out")
    new_late_minutes = record.get("late_minutes", 0)
    new_overtime_minutes = record.get("overtime_minutes", 0)
    new_work_hours = record.get("work_hours")
    new_half_day = record.get("half_day", False)

    # --- Handle status change ---
    if body.status is not None and body.status != record.get("status"):
        # Validate the status value
        valid_statuses = ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE")
        if body.status not in valid_statuses:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid status. Must be one of: {', '.join(valid_statuses)}",
            )

        new_status = body.status
        changes["status"] = new_status

        # ABSENT/LEAVE clears punches and derived fields
        if new_status in ("ABSENT", "LEAVE"):
            if new_punch_in is not None:
                changes["punch_in"] = None
            if new_punch_out is not None:
                changes["punch_out"] = None
            new_punch_in = None
            new_punch_out = None
            new_work_hours = None
            new_half_day = False
            new_late_minutes = 0
            new_overtime_minutes = 0
            changes["work_hours"] = None
            changes["half_day"] = False
            changes["late_minutes"] = 0
            changes["overtime_minutes"] = 0

    # --- Handle punch_in change ---
    if body.punch_in is not None:
        new_punch_in_val = to_utc_dt(body.punch_in)
        new_punch_in_val = truncate_to_seconds(new_punch_in_val)

        if new_punch_in_val != record.get("punch_in"):
            # Validate punch_in stays on the attendance date
            derived_date = get_ist_date(
                new_punch_in_val, emp["shift_start"], emp["shift_end"]
            )
            if derived_date != date:
                raise HTTPException(
                    status_code=422,
                    detail="punch_in must stay on the attendance date",
                )

            new_punch_in = new_punch_in_val
            changes["punch_in"] = new_punch_in

    # --- Handle punch_out change ---
    if body.punch_out is not None:
        new_punch_out_val = to_utc_dt(body.punch_out)
        new_punch_out_val = truncate_to_seconds(new_punch_out_val)

        if new_punch_out_val != record.get("punch_out"):
            new_punch_out = new_punch_out_val
            changes["punch_out"] = new_punch_out

    # --- Validate final state ---
    # Presence requires punch_in
    if is_present_status(new_status) and new_punch_in is None:
        raise HTTPException(
            status_code=422,
            detail="Present-type status requires punch_in",
        )

    # Validate punch_out > punch_in if both present
    if new_punch_in is not None and new_punch_out is not None:
        if new_punch_out <= new_punch_in:
            raise HTTPException(
                status_code=422,
                detail="punch_out must be after punch_in",
            )
        # Validate within 24 hours
        diff_hours = (new_punch_out - new_punch_in).total_seconds() / 3600
        if diff_hours > MAX_PUNCH_SPREAD_HOURS:
            raise HTTPException(
                status_code=422,
                detail="punch_out must be within 24 hours of punch_in",
            )

    # --- Compute derived fields from final state ---
    if is_present_status(new_status) and new_punch_in is not None:
        new_late_minutes = compute_late_minutes(
            new_punch_in, emp["shift_start"], date_str=date
        )
        if new_late_minutes != record.get("late_minutes", 0):
            changes["late_minutes"] = new_late_minutes

        if new_punch_out is not None:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime_minutes(
                new_punch_out, emp["shift_end"], date, emp["shift_start"]
            )
            new_half_day = compute_half_day(new_work_hours)
            if new_work_hours != record.get("work_hours"):
                changes["work_hours"] = new_work_hours
            if new_overtime_minutes != record.get("overtime_minutes", 0):
                changes["overtime_minutes"] = new_overtime_minutes
            if new_half_day != record.get("half_day", False):
                changes["half_day"] = new_half_day
        else:
            # punch_out is None — clear derived fields that depend on it
            if record.get("work_hours") is not None:
                changes["work_hours"] = None
            new_work_hours = None
            if record.get("overtime_minutes", 0) != 0:
                changes["overtime_minutes"] = 0
            new_overtime_minutes = 0
            if record.get("half_day", False) is not False:
                changes["half_day"] = False
            new_half_day = False

    # --- No actual changes → 422 ---
    if not changes:
        raise HTTPException(status_code=422, detail="No changes to apply")

    # --- Build history entry ---
    now = datetime.now(UTC)
    history_entry = {
        "timestamp": truncate_to_seconds(now),
        "regularized_by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Build update dict (only actual changes + updated_at)
    update_set = dict(changes)
    update_set["updated_at"] = truncate_to_seconds(now)

    # --- Optimistic concurrency: update_one with history length filter (F8) ---
    current_history_len = len(record.get("history", []))
    filter_q = {
        "emp_code": emp_code,
        "date": date,
        "history": {"$size": current_history_len},
    }

    result = attendance_col.update_one(
        filter_q,
        {
            "$set": update_set,
            "$push": {"history": history_entry},
        },
    )

    if result.matched_count == 0:
        raise HTTPException(
            status_code=409,
            detail="Concurrent modification conflict - please retry",
        )

    # Fetch and return the final document
    final = attendance_col.find_one({"emp_code": emp_code, "date": date})
    return serialize_attendance_record(final)


# ---------------------------------------------------------------------------
# GET /analytics/employees/{emp_code}/monthly
# ---------------------------------------------------------------------------
@app.get("/analytics/employees/{emp_code}/monthly")
async def employee_monthly(
    emp_code: str = Path(..., description="Employee code"),
    month: str = Query(..., description="Month in YYYY-MM format"),
):
    """Monthly stats: working/present/leave days, late count, attendance % (F3)."""
    # Validate month
    year, mon = _validate_month_str(month)

    # Verify employee
    emp = employees_col.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    # Compute working_days per R7
    working_days = working_days_in_month(year, mon, emp["joined_on"])

    # Build date range strings
    month_start_str = _month_start_str(year, mon)
    month_end_str = _month_end_str(year, mon)

    # Aggregation pipeline
    # MongoDB $dayOfWeek: 1=Sunday, 2=Monday, ..., 6=Friday, 7=Saturday
    # Mon-Fri = dayOfWeek in [2, 3, 4, 5, 6]
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start_str, "$lte": month_end_str},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$let": {
                                            "vars": {
                                                "dateParts": {
                                                    "$split": ["$date", "-"]
                                                }
                                            },
                                            "in": {
                                                "$and": [
                                                    {
                                                        "$gte": [
                                                            {
                                                                "$dayOfWeek": {
                                                                    "$dateFromParts": {
                                                                        "year": {"$toInt": {"$arrayElemAt": ["$$dateParts", 0]}},
                                                                        "month": {"$toInt": {"$arrayElemAt": ["$$dateParts", 1]}},
                                                                        "day": {"$toInt": {"$arrayElemAt": ["$$dateParts", 2]}},
                                                                    }
                                                                }
                                                            },
                                                            2,  # Monday
                                                        ]
                                                    },
                                                    {
                                                        "$lte": [
                                                            {
                                                                "$dayOfWeek": {
                                                                    "$dateFromParts": {
                                                                        "year": {"$toInt": {"$arrayElemAt": ["$$dateParts", 0]}},
                                                                        "month": {"$toInt": {"$arrayElemAt": ["$$dateParts", 1]}},
                                                                        "day": {"$toInt": {"$arrayElemAt": ["$$dateParts", 2]}},
                                                                    }
                                                                }
                                                            },
                                                            6,  # Friday
                                                        ]
                                                    },
                                                ]
                                            },
                                        }
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    result = list(attendance_col.aggregate(pipeline))

    if result:
        r = result[0]
        present_days = round_half_up_2(r["present_days"])
        leave_days = r["leave_days"]
        late_count = r["late_count"]
        total_late_minutes = r["total_late_minutes"]
        total_overtime_minutes = r["total_overtime_minutes"]
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    # F3: attendance_pct with round_half_up_2
    if working_days > 0:
        attendance_pct = round_half_up_2(present_days / working_days * 100)
    else:
        attendance_pct = 0.0

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


# ---------------------------------------------------------------------------
# GET /analytics/departments/summary
# ---------------------------------------------------------------------------
@app.get("/analytics/departments/summary")
async def department_summary(
    month: str = Query(..., description="Month in YYYY-MM format"),
):
    """Department summary: headcount, present days, avg work hours, late count."""
    # Validate month
    year, mon = _validate_month_str(month)

    month_start_str = _month_start_str(year, mon)
    month_end_str = _month_end_str(year, mon)

    pipeline = [
        # Match employees who had joined on or before month end
        {"$match": {"joined_on": {"$lte": month_end_str}}},
        # Group by department
        {
            "$group": {
                "_id": "$department",
                "emp_codes": {"$addToSet": "$emp_code"},
                "headcount": {"$sum": 1},
            }
        },
        # Lookup into attendance_logs with pipeline-style lookup
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"dept_emp_codes": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$dept_emp_codes"]},
                                    {"$gte": ["$date", month_start_str]},
                                    {"$lte": ["$date", month_end_str]},
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "total_work_hours": {
                                "$sum": {"$ifNull": ["$work_hours", 0]}
                            },
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": ["$late_minutes", 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "attendance_stats",
            }
        },
        # Project final fields
        {
            "$project": {
                "department": "$_id",
                "headcount": 1,
                "present_count": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$attendance_stats.present_count", 0]},
                        0,
                    ]
                },
                "late_count": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$attendance_stats.late_count", 0]},
                        0,
                    ]
                },
                "total_work_hours": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$attendance_stats.total_work_hours", 0]},
                        0,
                    ]
                },
                "emp_codes": 1,
            }
        },
        # Sort by department name ascending
        {"$sort": {"department": 1}},
    ]

    results = list(employees_col.aggregate(pipeline))

    summary = []
    for r in results:
        headcount = r["headcount"]
        present_count = r.get("present_count", 0)
        total_work_hours = r.get("total_work_hours", 0)
        late_count = r.get("late_count", 0)

        avg_work_hours = (
            round_half_up_2(total_work_hours / headcount)
            if headcount > 0
            else 0.0
        )

        summary.append({
            "department": r.get("department", r["_id"]),
            "headcount": headcount,
            "present_days": round_half_up_2(float(present_count)),
            "avg_work_hours": avg_work_hours,
            "late_count": late_count,
            "total_late_minutes": 0,
            "leave_count": 0,
            "on_duty_count": 0,
        })

    return {"month": month, "items": summary}


# ---------------------------------------------------------------------------
# GET /analytics/leaderboard/late
# ---------------------------------------------------------------------------
@app.get("/analytics/leaderboard/late")
async def late_leaderboard(
    month: str = Query(..., description="Month in YYYY-MM format"),
    limit: int = Query(10, ge=1, le=50, description="Max number of employees to return"),
):
    """Late-arrival leaderboard with competition ranking ($rank), orphan drop (F5)."""
    # Validate month
    year, mon = _validate_month_str(month)

    month_start_str = _month_start_str(year, mon)
    month_end_str = _month_end_str(year, mon)

    pipeline = [
        # Match attendance for the month with late_minutes > 0
        {
            "$match": {
                "date": {"$gte": month_start_str, "$lte": month_end_str},
                "late_minutes": {"$gt": 0},
            }
        },
        # Group by emp_code
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        # Lookup employees — this joins with the employees collection
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        # Unwind — drops orphans (attendance without matching employee) (F5)
        {"$unwind": "$employee"},
        # SetWindowFields with $rank (sorted by total_late_minutes DESC)
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}},
                },
            }
        },
        # Match rank <= limit
        {"$match": {"rank": {"$lte": limit}}},
        # Project final fields
        {
            "$project": {
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
                "rank": 1,
            }
        },
        # Sort by total_late_minutes DESC for final output
        {"$sort": {"total_late_minutes": -1}},
    ]

    results = list(attendance_col.aggregate(pipeline))

    leaderboard = []
    for r in results:
        leaderboard.append({
            "rank": r["rank"],
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": r["total_late_minutes"],
            "late_count": r["late_count"],
        })

    return {"month": month, "items": leaderboard}


# ---------------------------------------------------------------------------
# GET /analytics/departments/{department}/trend
# ---------------------------------------------------------------------------
@app.get("/analytics/departments/{department}/trend")
async def department_trend(
    department: str = Path(..., description="Department name"),
    from_date: str = Query(..., alias="from", description="Start date YYYY-MM-DD"),
    to_date: str = Query(..., alias="to", description="End date YYYY-MM-DD"),
):
    """7-day MA trend with $densify for gap-fill, integer-arith rounding (F6)."""
    # Validate from/to dates
    from_d = _validate_date_str(from_date, "from_date")
    to_d = _validate_date_str(to_date, "to_date")

    if from_d > to_d:
        raise HTTPException(status_code=422, detail="from_date must be <= to_date")

    # F6: range must not exceed 92 days inclusive
    if (to_d - from_d).days + 1 > TREND_MAX_DAYS:
        raise HTTPException(
            status_code=422,
            detail=f"Date range must not exceed {TREND_MAX_DAYS} days inclusive",
        )

    # Verify department has employees
    dept_emp_codes = employees_col.distinct("emp_code", {"department": department})
    if not dept_emp_codes:
        raise HTTPException(
            status_code=404,
            detail="Department not found or has no employees",
        )

    # Count headcount (employees joined on or before to_date)
    headcount = employees_col.count_documents({
        "department": department,
        "joined_on": {"$lte": to_date},
    })

    # MongoDB $dayOfWeek: 1=Sunday, 2=Monday, ..., 6=Friday, 7=Saturday
    # Mon-Fri = dayOfWeek in [2, 3, 4, 5, 6]
    pipeline = [
        # 1. Match attendance for department employees in date range
        {
            "$match": {
                "emp_code": {"$in": dept_emp_codes},
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        # 2. AddFields: dateObj from date string
        {
            "$addFields": {
                "dateObj": {
                    "$dateFromParts": {
                        "year": {"$toInt": {"$substr": ["$date", 0, 4]}},
                        "month": {"$toInt": {"$substr": ["$date", 5, 2]}},
                        "day": {"$toInt": {"$substr": ["$date", 8, 2]}},
                    }
                }
            }
        },
        # 3. Group by dateObj
        {
            "$group": {
                "_id": "$dateObj",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        # 4. Densify to fill in missing dates
        {
            "$densify": {
                "field": "_id",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        datetime(from_d.year, from_d.month, from_d.day, tzinfo=UTC),
                        datetime(to_d.year, to_d.month, to_d.day, 23, 59, 59, tzinfo=UTC),
                    ],
                },
            }
        },
        # 5. Set date string, present_count, late_count defaults
        {
            "$set": {
                "date": {
                    "$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}
                },
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        # 6. Set is_working_day (Mon-Fri) and headcount
        {
            "$set": {
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$_id"}, 2]},  # Monday = 2
                        {"$lte": [{"$dayOfWeek": "$_id"}, 6]},  # Friday = 6
                    ]
                },
                "headcount": headcount,
            }
        },
        # 7. F6: Set attendance_rate using integer arithmetic
        #    rate = floor((20000 * 2*present_count + 2*headcount) / (2 * 2*headcount)) / 10000
        #    Only when is_working_day AND headcount > 0, else null
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$divide": [
                                {
                                    "$floor": [
                                        {
                                            "$divide": [
                                                {
                                                    "$add": [
                                                        {
                                                            "$multiply": [
                                                                20000,
                                                                {"$multiply": [2, "$present_count"]},
                                                            ]
                                                        },
                                                        {"$multiply": [2, "$headcount"]},
                                                    ]
                                                },
                                                {
                                                    "$multiply": [
                                                        2,
                                                        {"$multiply": [2, "$headcount"]},
                                                    ]
                                                },
                                            ]
                                        }
                                    ]
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        # 8. F6: SetWindowFields for 7-day moving average
        #    _rate_sum = sum of (attendance_rate * 10000) over window [-6, 0]
        #    _rate_count = count of non-null attendance_rate over window [-6, 0]
        {
            "$setWindowFields": {
                "partitionBy": None,
                "sortBy": {"_id": 1},
                "output": {
                    "_rate_sum": {
                        "$sum": {
                            "$cond": [
                                {"$ne": ["$attendance_rate", None]},
                                {"$multiply": ["$attendance_rate", 10000]},
                                0,
                            ]
                        },
                        "window": {"documents": [-6, 0]},
                    },
                    "_rate_count": {
                        "$sum": {
                            "$cond": [
                                {"$ne": ["$attendance_rate", None]},
                                1,
                                0,
                            ]
                        },
                        "window": {"documents": [-6, 0]},
                    },
                },
            }
        },
        # 9. Compute moving_avg_7d using integer arithmetic
        #    _rate_sum already contains attendance_rate * 10000
        #    moving_avg_7d = floor((2 * _rate_sum + _rate_count) / (2 * _rate_count)) / 10000
        {
            "$set": {
                "moving_avg_7d": {
                    "$cond": [
                        {"$gt": ["$_rate_count", 0]},
                        {
                            "$divide": [
                                {
                                    "$floor": [
                                        {
                                            "$divide": [
                                                {
                                                    "$add": [
                                                        {
                                                            "$multiply": [
                                                                2,
                                                                "$_rate_sum",
                                                            ]
                                                        },
                                                        "$_rate_count",
                                                    ]
                                                },
                                                {"$multiply": [2, "$_rate_count"]},
                                            ]
                                        }
                                    ]
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        # 10. Unset temporary fields
        {"$unset": ["_rate_sum", "_rate_count"]},
        # 11. Sort by date ascending
        {"$sort": {"date": 1}},
        # 12. Project final fields
        {
            "$project": {
                "date": 1,
                "present_count": 1,
                "late_count": 1,
                "is_working_day": 1,
                "headcount": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]

    results = list(attendance_col.aggregate(pipeline))

    # Build trend list for response
    trend = []
    for r in results:
        trend.append({
            "date": r.get("date"),
            "present_count": r.get("present_count", 0),
            "late_count": r.get("late_count", 0),
            "is_working_day": r.get("is_working_day"),
            "headcount": r.get("headcount"),
            "attendance_rate": r.get("attendance_rate"),
            "moving_avg_7d": r.get("moving_avg_7d"),
        })

    return {
        "department": department,
        "items": trend,
    }


# ---------------------------------------------------------------------------
# GET /admin/explain/{endpoint}
# ---------------------------------------------------------------------------

VALID_EXPLAIN_ENDPOINTS: dict[str, dict] = {
    "attendance_list": {
        "collection": "attendance_logs",
        "pipeline": [
            {"$match": {"date": {"$gte": "2024-01-01", "$lte": "2024-01-31"}}},
            {"$sort": {"date": -1, "emp_code": 1}},
            {"$skip": 0},
            {"$limit": 20},
        ],
    },
    "employee_monthly": {
        "collection": "attendance_logs",
        "pipeline": [
            {
                "$match": {
                    "emp_code": "EMP0001",
                    "date": {"$gte": "2024-01-01", "$lte": "2024-01-31"},
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {"$sum": 1},
                    "total_late_minutes": {"$sum": "$late_minutes"},
                }
            },
        ],
    },
    "department_summary": {
        "collection": "employees",
        "pipeline": [
            {"$match": {"joined_on": {"$lte": "2024-01-31"}}},
            {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
            {"$sort": {"_id": 1}},
        ],
    },
    "late_leaderboard": {
        "collection": "attendance_logs",
        "pipeline": [
            {
                "$match": {
                    "date": {"$gte": "2024-01-01", "$lte": "2024-01-31"},
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                }
            },
            {"$sort": {"total_late_minutes": -1}},
            {"$limit": 10},
        ],
    },
    "department_trend": {
        "collection": "attendance_logs",
        "pipeline": [
            {
                "$match": {
                    "emp_code": {"$in": ["EMP0001"]},
                    "date": {"$gte": "2024-01-01", "$lte": "2024-01-31"},
                }
            },
            {"$group": {"_id": "$date", "present_count": {"$sum": 1}}},
            {"$sort": {"_id": 1}},
        ],
    },
}


@app.get("/admin/explain/{endpoint}")
async def explain_query(
    endpoint: str = Path(
        ...,
        description=f"Endpoint name. One of: {', '.join(VALID_EXPLAIN_ENDPOINTS.keys())}",
    ),
):
    """Return MongoDB explain plan for an analytics endpoint (F7)."""
    if endpoint not in VALID_EXPLAIN_ENDPOINTS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid endpoint. Must be one of: {', '.join(VALID_EXPLAIN_ENDPOINTS.keys())}",
        )

    config = VALID_EXPLAIN_ENDPOINTS[endpoint]
    collection_name = config["collection"]
    pipeline = config["pipeline"]

    try:
        # F7: Use db.command("explain", ..., verbosity="executionStats")
        explain_doc = db.command(
            "explain",
            {
                "aggregate": collection_name,
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        # F7: Convert with json.loads(json_util.dumps(explain_doc))
        # This handles BSON types like ObjectId, datetime, Decimal128, etc.
        explain_json = json.loads(json_util.dumps(explain_doc))
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to explain query: {e!s}",
        )

    return {
        "endpoint": endpoint,
        "collection": collection_name,
        "explain": explain_json,
    }
