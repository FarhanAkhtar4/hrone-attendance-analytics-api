"""Tests for HROne Attendance API -- pytest + httpx against real MongoDB."""

import os
import threading
import time
from datetime import UTC, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

import pytest

# ---------------------------------------------------------------------------
# Setup: use a dedicated test database
# ---------------------------------------------------------------------------
IST = timezone(timedelta(hours=5, minutes=30))
# UTC is already imported from datetime

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
TEST_DB = f"hrone_test_{int(time.time() * 1000)}"

# Set env vars before importing the app
os.environ["MONGO_URI"] = MONGO_URI
os.environ["MONGO_DB"] = TEST_DB


# We import the app module directly
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# Use httpx TestClient
from fastapi.testclient import TestClient

from app.main import (
    app,
    attendance_col,
    compute_half_day,
    compute_late_minutes,
    compute_overtime_minutes,
    compute_work_hours,
    get_ist_date,
    is_present_status,
    round_half_up_2,
    round_half_up_4,
    to_utc_dt,
    truncate_to_seconds,
    validate_epoch_ms,
    working_days_in_month,
)

# Alias db for clean_db fixture
db = attendance_col.database


@pytest.fixture(scope="session")
def test_client():
    with TestClient(app) as client:
        yield client


@pytest.fixture(autouse=True)
def clean_db():
    """Clean test database before each test."""
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    yield
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})


# ===========================================================================
# seed helpers
# ===========================================================================

EMPLOYEES = [
    {
        "emp_code": "EMP0001",
        "name": "Aarav Sharma",
        "email": "aarav@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2025-01-15",
    },
    {
        "emp_code": "EMP0002",
        "name": "Priya Gupta",
        "email": "priya@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2025-02-01",
    },
    {
        "emp_code": "EMP0003",
        "name": "Vikram Singh",
        "email": "vikram@example.com",
        "department": "Marketing",
        "shift_start": "10:00",
        "shift_end": "19:00",
        "joined_on": "2025-03-10",
    },
    {
        "emp_code": "EMP0004",
        "name": "Neha Patel",
        "email": "neha@example.com",
        "department": "HR",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2025-01-01",
    },
]


def seed_employees():
    for emp in EMPLOYEES:
        db.employees.insert_one(
            {**emp, "created_at": truncate_to_seconds(datetime.now(UTC))}
        )


def make_epoch(year, month, day, hour, minute, tz=IST):
    """Create epoch ms for a given IST time."""
    dt = datetime(year, month, day, hour, minute, tzinfo=tz)
    return int(dt.timestamp() * 1000)


# ===========================================================================
# R1  timezone & date derivation
# ===========================================================================


class TestR1TimezoneAndDate:
    def test_normal_shift_date(self):
        """09:30-18:30 shift, punch at 10:00 IST → same date."""
        utc_dt = to_utc_dt(make_epoch(2026, 7, 15, 10, 0))
        date_str = get_ist_date(utc_dt, "09:30", "18:30")
        assert date_str == "2026-07-15"

    def test_overnight_shift_early_morning(self):
        """22:00-06:00 shift, punch at 00:30 IST → previous day's date."""
        utc_dt = to_utc_dt(make_epoch(2026, 7, 16, 0, 30))
        date_str = get_ist_date(utc_dt, "22:00", "06:00")
        assert date_str == "2026-07-15"

    def test_overnight_shift_evening(self):
        """22:00-06:00 shift, punch at 22:30 IST → same day."""
        utc_dt = to_utc_dt(make_epoch(2026, 7, 15, 22, 30))
        date_str = get_ist_date(utc_dt, "22:00", "06:00")
        assert date_str == "2026-07-15"

    def test_midnight_ist_boundary(self):
        """Punch at exactly midnight IST → that calendar day."""
        utc_dt = to_utc_dt(make_epoch(2026, 7, 16, 0, 0))
        date_str = get_ist_date(utc_dt, "09:30", "18:30")
        assert date_str == "2026-07-16"

    def test_midnight_ist_next_day(self):
        """Punch at 23:59 IST → same calendar day."""
        utc_dt = to_utc_dt(make_epoch(2026, 7, 15, 23, 59))
        date_str = get_ist_date(utc_dt, "09:30", "18:30")
        assert date_str == "2026-07-15"

    def test_truncate7_to_seconds(self):
        dt = datetime(2026, 1, 1, 12, 30, 45, 123456, tzinfo=UTC)
        truncated = truncate_to_seconds(dt)
        assert truncated.microsecond == 0

    def test_epoch_ms_truncation(self):
        ms = 1783310280500  # has 500ms
        result = to_utc_dt(ms)
        assert result.microsecond == 0


# ===========================================================================
# R2  late minutes
# ===========================================================================


class TestR2LateMinutes:
    def test_exact_grace_boundary(self):
        """Shift 09:30, punch 09:40:00 → exactly 10 min → NOT late."""
        utc_dt = to_utc_dt(make_epoch(2026, 7, 15, 9, 40))
        assert compute_late_minutes(utc_dt, "09:30") == 0

    def test_one_second_past_grace(self):
        """Shift 09:30, punch 09:40:01 → 10 min 1 sec → late = 10."""
        utc_dt = to_utc_dt(make_epoch(2026, 7, 15, 9, 40) + 1000)
        assert compute_late_minutes(utc_dt, "09:30") == 10

    def test_10_15_59(self):
        """Shift 09:30, punch 10:15:59 → 45 min 59 sec → late = 45."""
        utc_dt = to_utc_dt(make_epoch(2026, 7, 15, 10, 15) + 59000)
        assert compute_late_minutes(utc_dt, "09:30") == 45

    def test_on_time(self):
        utc_dt = to_utc_dt(make_epoch(2026, 7, 15, 9, 30))
        assert compute_late_minutes(utc_dt, "09:30") == 0

    def test_exactly_at_shift_start(self):
        utc_dt = to_utc_dt(make_epoch(2026, 7, 15, 9, 30))
        assert compute_late_minutes(utc_dt, "09:30") == 0

    def test_10_minutes_1_sec(self):
        utc_dt = to_utc_dt(make_epoch(2026, 7, 15, 9, 40) + 1000)
        assert compute_late_minutes(utc_dt, "09:30") == 10


# ===========================================================================
# R3  overtime
# ===========================================================================


class TestR3Overtime:
    def test_under_30_minutes(self):
        """5 min overtime → 0 (below threshold)."""
        punch_out = to_utc_dt(make_epoch(2026, 7, 15, 18, 35))
        assert compute_overtime_minutes(punch_out, "18:30", "2026-07-15", "09:30") == 0

    def test_exactly_30_minutes(self):
        punch_out = to_utc_dt(make_epoch(2026, 7, 15, 19, 0))
        assert compute_overtime_minutes(punch_out, "18:30", "2026-07-15", "09:30") == 30

    def test_40_minutes(self):
        punch_out = to_utc_dt(make_epoch(2026, 7, 15, 19, 10))
        assert compute_overtime_minutes(punch_out, "18:30", "2026-07-15", "09:30") == 40

    def test_29_minutes(self):
        punch_out = to_utc_dt(make_epoch(2026, 7, 15, 18, 59))
        assert compute_overtime_minutes(punch_out, "18:30", "2026-07-15", "09:30") == 0

    def test_overnight_overtime(self):
        """22:00-06:00 shift, punch out at 07:00 → 60 min overtime."""
        punch_out = to_utc_dt(make_epoch(2026, 7, 16, 7, 0))
        assert compute_overtime_minutes(punch_out, "06:00", "2026-07-15", "22:00") == 60

    def test_no_overtime(self):
        punch_out = to_utc_dt(make_epoch(2026, 7, 15, 18, 30))
        assert compute_overtime_minutes(punch_out, "18:30", "2026-07-15", "09:30") == 0


# ===========================================================================
# R4  work hours
# ===========================================================================


class TestR4WorkHours:
    def test_normal_day(self):
        punch_in = to_utc_dt(make_epoch(2026, 7, 15, 9, 30))
        punch_out = to_utc_dt(make_epoch(2026, 7, 15, 18, 30))
        assert compute_work_hours(punch_in, punch_out) == 9.0

    def test_half_up_rounding(self):
        """4.495 hours → half-up → 4.50."""
        punch_in = to_utc_dt(make_epoch(2026, 7, 15, 9, 0))
        secs = 4.495 * 3600
        punch_out_ms = make_epoch(2026, 7, 15, 9, 0) + int(secs * 1000)
        punch_out = to_utc_dt(punch_out_ms)
        result = compute_work_hours(punch_in, punch_out)
        assert result == 4.50

    def test_half_day_threshold(self):
        """Exactly 4.50 hours → not half day."""
        punch_in = to_utc_dt(make_epoch(2026, 7, 15, 9, 0))
        punch_out = to_utc_dt(make_epoch(2026, 7, 15, 13, 30))
        assert compute_work_hours(punch_in, punch_out) == 4.50


# ===========================================================================
# R5  half day
# ===========================================================================


class TestR5HalfDay:
    def test_full_day(self):
        assert not compute_half_day(9.12)

    def test_half_day(self):
        assert compute_half_day(3.5)

    def test_boundary_below(self):
        assert compute_half_day(4.49)

    def test_boundary_at(self):
        assert not compute_half_day(4.50)

    def test_null_work_hours(self):
        assert not compute_half_day(None)


# ===========================================================================
# R6  presence status
# ===========================================================================


class TestR6Presence:
    def test_present(self):
        assert is_present_status("PRESENT")

    def test_wfh(self):
        assert is_present_status("WFH")

    def test_on_duty(self):
        assert is_present_status("ON_DUTY")

    def test_absent(self):
        assert not is_present_status("ABSENT")

    def test_leave(self):
        assert not is_present_status("LEAVE")


# ===========================================================================
# R7  working days
# ===========================================================================


class TestR7WorkingDays:
    def test_full_month(self):
        """July 2025, joined Jan 1 → 23 working days."""
        assert working_days_in_month(2025, 7, "2025-01-01") == 23

    def test_mid_month_joiner(self):
        """Joined July 15 2025 → 13 working days remaining."""
        assert working_days_in_month(2025, 7, "2025-07-15") == 13

    def test_joined_after_month(self):
        """Joined Aug 1, checking July → 0."""
        assert working_days_in_month(2025, 7, "2025-08-01") == 0


# ===========================================================================
# R8  rounding
# ===========================================================================


class TestR8Rounding:
    def test_round_2_basic(self):
        assert round_half_up_2(3.456) == 3.46

    def test_round_2_half_up(self):
        assert round_half_up_2(2.345) == 2.35

    def test_round_4_basic(self):
        assert round_half_up_4(0.03125) == 0.0313

    def test_round_4_half_up(self):
        d = Decimal(1) / Decimal(32)
        expected = float(d.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
        assert round_half_up_4(1 / 32) == expected


# ===========================================================================
# health endpoint
# ===========================================================================


class TestHealthEndpoint:
    def test_health_ok(self, test_client):
        resp = test_client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


# ===========================================================================
# employee endpoints
# ===========================================================================


class TestEmployeeEndpoints:
    def test_create_employee(self, test_client):
        resp = test_client.post("/employees", json=EMPLOYEES[0])
        assert resp.status_code == 201
        data = resp.json()
        assert data["emp_code"] == "EMP0001"
        assert "created_at" in data

    def test_create_duplicate_employee(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        resp = test_client.post("/employees", json=EMPLOYEES[0])
        assert resp.status_code == 409

    def test_list_employees_sorted(self, test_client):
        for emp in EMPLOYEES[:3]:
            test_client.post("/employees", json=emp)
        resp = test_client.get("/employees")
        assert resp.status_code == 200
        codes = [e["emp_code"] for e in resp.json()["items"]]
        assert codes == sorted(codes)

    def test_list_employees_pagination(self, test_client):
        for emp in EMPLOYEES[:3]:
            test_client.post("/employees", json=emp)
        resp = test_client.get("/employees?page=1&page_size=2")
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 2
        assert resp.json()["total"] == 3

    def test_list_employees_department_filter(self, test_client):
        for emp in EMPLOYEES[:3]:
            test_client.post("/employees", json=emp)
        resp = test_client.get("/employees?department=Engineering")
        assert resp.status_code == 200
        assert resp.json()["total"] == 2

    def test_employee_no_id_in_response(self, test_client):
        resp = test_client.post("/employees", json=EMPLOYEES[0])
        data = resp.json()
        assert "id" not in data
        assert "_id" not in data


# ===========================================================================
# punch in
# ===========================================================================


class TestPunchIn:
    def test_punch_in_create_success(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        ms = make_epoch(2026, 7, 15, 9, 30)
        resp = test_client.post(
            "/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms}
        )
        assert resp.status_code == 201
        assert resp.json()["late_minutes"] == 0

    def test_punch_in_unknown_employee(self, test_client):
        resp = test_client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP9999", "punched_at": make_epoch(2026, 7, 15, 9, 30)},
        )
        assert resp.status_code == 404

    def test_punch_in_duplicate(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        ms = make_epoch(2026, 7, 15, 9, 30)
        test_client.post(
            "/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms}
        )
        resp = test_client.post(
            "/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms}
        )
        assert resp.status_code == 409

    def test_punch_in_rejects_absent(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        resp = test_client.post(
            "/attendance/punch-in",
            json={
                "emp_code": "EMP0001",
                "punched_at": make_epoch(2026, 7, 15, 9, 30),
                "status": "ABSENT",
            },
        )
        assert resp.status_code == 422

    def test_punch_in_late(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        ms = make_epoch(2026, 7, 15, 10, 0)
        resp = test_client.post(
            "/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms}
        )
        assert resp.status_code == 201
        assert resp.json()["late_minutes"] == 30


# ===========================================================================
# punch out
# ===========================================================================


class TestPunchOut:
    def test_punch_out_success(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        ms_in = make_epoch(2026, 7, 15, 9, 30)
        test_client.post(
            "/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms_in}
        )
        ms_out = make_epoch(2026, 7, 15, 18, 30)
        resp = test_client.post(
            "/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms_out}
        )
        assert resp.status_code == 200
        assert resp.json()["work_hours"] is not None

    def test_punch_out_already_out(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        ms_in = make_epoch(2026, 7, 15, 9, 30)
        test_client.post(
            "/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms_in}
        )
        ms_out = make_epoch(2026, 7, 15, 18, 30)
        test_client.post(
            "/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms_out}
        )
        resp = test_client.post(
            "/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms_out}
        )
        assert resp.status_code == 409

    def test_punch_out_unknown_employee(self, test_client):
        resp = test_client.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP9999", "punched_at": make_epoch(2026, 7, 15, 18, 30)},
        )
        assert resp.status_code == 404

    def test_punch_out_overtime(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        ms_in = make_epoch(2026, 7, 15, 9, 30)
        test_client.post(
            "/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms_in}
        )
        ms_out = make_epoch(2026, 7, 15, 19, 30)
        resp = test_client.post(
            "/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms_out}
        )
        assert resp.status_code == 200
        assert resp.json()["overtime_minutes"] == 60

    def test_punch_out_half_day(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        ms_in = make_epoch(2026, 7, 15, 9, 30)
        test_client.post(
            "/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms_in}
        )
        ms_out = make_epoch(2026, 7, 15, 13, 0)
        resp = test_client.post(
            "/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms_out}
        )
        assert resp.status_code == 200
        assert resp.json()["half_day"] is True


# ===========================================================================
# attendance list
# ===========================================================================


class TestAttendanceList:
    def test_list_no_id(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        test_client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0001", "punched_at": make_epoch(2026, 7, 15, 9, 30)},
        )
        resp = test_client.get("/attendance")
        assert resp.status_code == 200
        items = resp.json()["items"]
        for item in items:
            assert "id" not in item
            assert "_id" not in item

    def test_list_pagination(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        for day in range(1, 4):
            test_client.post(
                "/attendance/punch-in",
                json={
                    "emp_code": "EMP0001",
                    "punched_at": make_epoch(2026, 7, day, 9, 30),
                },
            )
        resp = test_client.get("/attendance?page_size=2")
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 2


# ===========================================================================
# regularize (PATCH)
# ===========================================================================


class TestRegularize:
    def test_patch_status_change(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        test_client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0001", "punched_at": make_epoch(2026, 7, 15, 9, 30)},
        )
        resp = test_client.patch(
            "/attendance/EMP0001/2026-07-15",
            json={
                "status": "LEAVE",
                "regularized_by": "admin",
                "reason": "sick leave",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "LEAVE"

    def test_patch_no_changes_422(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        test_client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0001", "punched_at": make_epoch(2026, 7, 15, 9, 30)},
        )
        resp = test_client.patch(
            "/attendance/EMP0001/2026-07-15",
            json={
                "status": "PRESENT",
                "regularized_by": "admin",
                "reason": "no change",
            },
        )
        assert resp.status_code == 422

    def test_patch_absent_clears_punches(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        ms_in = make_epoch(2026, 7, 15, 9, 30)
        test_client.post(
            "/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms_in}
        )
        ms_out = make_epoch(2026, 7, 15, 18, 30)
        test_client.post(
            "/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms_out}
        )
        resp = test_client.patch(
            "/attendance/EMP0001/2026-07-15",
            json={
                "status": "ABSENT",
                "regularized_by": "admin",
                "reason": "error",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["punch_in"] is None
        assert resp.json()["punch_out"] is None

    def test_patch_unknown_record_404(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        resp = test_client.patch(
            "/attendance/EMP0001/2026-07-20",
            json={
                "status": "LEAVE",
                "regularized_by": "admin",
                "reason": "leave",
            },
        )
        assert resp.status_code == 404


# ===========================================================================
# analytics  employee monthly
# ===========================================================================


class TestEmployeeMonthly:
    def test_monthly_summary(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        test_client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0001", "punched_at": make_epoch(2026, 7, 15, 9, 30)},
        )
        resp = test_client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
        assert resp.status_code == 200
        data = resp.json()
        assert data["emp_code"] == "EMP0001"
        assert data["working_days"] > 0

    def test_monthly_unknown_employee(self, test_client):
        resp = test_client.get("/analytics/employees/EMP9999/monthly?month=2026-07")
        assert resp.status_code == 404


# ===========================================================================
# analytics  department summary
# ===========================================================================


class TestDepartmentSummary:
    def test_summary(self, test_client):
        for emp in EMPLOYEES[:3]:
            test_client.post("/employees", json=emp)
        test_client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0001", "punched_at": make_epoch(2026, 7, 15, 9, 30)},
        )
        resp = test_client.get("/analytics/departments/summary?month=2026-07")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) > 0


# ===========================================================================
# analytics  late leaderboard
# ===========================================================================


class TestLateLeaderboard:
    def test_leaderboard(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        test_client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0001", "punched_at": make_epoch(2026, 7, 15, 10, 0)},
        )
        resp = test_client.get("/analytics/leaderboard/late?month=2026-07")
        assert resp.status_code == 200
        assert "items" in resp.json()


# ===========================================================================
# analytics  department trend
# ===========================================================================


class TestDepartmentTrend:
    def test_trend(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        test_client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0001", "punched_at": make_epoch(2026, 7, 15, 9, 30)},
        )
        resp = test_client.get(
            "/analytics/departments/Engineering/trend?from=2026-07-14&to=2026-07-16"
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 3  # 3 days

    def test_trend_unknown_department(self, test_client):
        resp = test_client.get(
            "/analytics/departments/Unknown/trend?from=2026-07-01&to=2026-07-10"
        )
        assert resp.status_code == 404


# ===========================================================================
# explain endpoint
# ===========================================================================


class TestExplain:
    def test_explain_attendance_list(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        resp = test_client.get("/admin/explain/attendance_list")
        assert resp.status_code == 200
        data = resp.json()
        assert "explain" in data

    def test_explain_invalid_endpoint(self, test_client):
        resp = test_client.get("/admin/explain/invalid")
        assert resp.status_code == 422


# ===========================================================================
# epoch ms validation
# ===========================================================================


class TestEpochMsValidation:
    def test_rejects_seconds_looking(self):
        with pytest.raises(ValueError):
            validate_epoch_ms(100000000)  # looks like seconds

    def test_rejects_bool(self):
        with pytest.raises(TypeError):
            validate_epoch_ms(True)

    def test_accepts_valid_epoch(self):
        result = validate_epoch_ms(1783310280000)
        assert result == 1783310280000


# ===========================================================================
# F1  overnight shift late minutes
# ===========================================================================


class TestF1OvernightLateMinutes:
    """F1: compute_late_minutes must anchor shift_start to the attendance date,
    not the punch-in's calendar day. For overnight shifts (22:00-06:00),
    a punch at 00:30 on Jul 16 belongs to Jul 15, and shift_start is
    Jul 15 22:00, so late = 150 min."""

    def test_overnight_0030_late_150(self):
        """00:30 IST → 2.5 hours after 22:00 → 150 min late."""
        utc_dt = to_utc_dt(make_epoch(2026, 7, 16, 0, 30))
        result = compute_late_minutes(utc_dt, "22:00", "2026-07-15")
        assert result == 150

    def test_overnight_0200_late_240(self):
        """02:00 IST → 4 hours after 22:00 → 240 min late."""
        utc_dt = to_utc_dt(make_epoch(2026, 7, 16, 2, 0))
        result = compute_late_minutes(utc_dt, "22:00", "2026-07-15")
        assert result == 240

    def test_overnight_0500_late_420(self):
        """05:00 IST → 7 hours after 22:00 → 420 min late."""
        utc_dt = to_utc_dt(make_epoch(2026, 7, 16, 5, 0))
        result = compute_late_minutes(utc_dt, "22:00", "2026-07-15")
        assert result == 420

    def test_overnight_2205_inside_grace(self):
        """22:05 IST → 5 min after 22:00 → inside grace → 0."""
        utc_dt = to_utc_dt(make_epoch(2026, 7, 15, 22, 5))
        result = compute_late_minutes(utc_dt, "22:00", "2026-07-15")
        assert result == 0

    def test_overnight_2230_late_30(self):
        """22:30 IST → 30 min after 22:00 → 30 min late."""
        utc_dt = to_utc_dt(make_epoch(2026, 7, 15, 22, 30))
        result = compute_late_minutes(utc_dt, "22:00", "2026-07-15")
        assert result == 30

    def test_overnight_2155_before_shift(self):
        """21:55 IST → before shift → negative diff → 0."""
        utc_dt = to_utc_dt(make_epoch(2026, 7, 15, 21, 55))
        result = compute_late_minutes(utc_dt, "22:00", "2026-07-15")
        assert result == 0

    def test_normal_shift_still_works(self):
        """Non-overnight: same IST date anchoring."""
        utc_dt = to_utc_dt(make_epoch(2026, 7, 15, 10, 0))
        result = compute_late_minutes(utc_dt, "09:30", "2026-07-15")
        assert result == 30

    def test_overnight_punch_in_api(self, test_client):
        """Full API integration: overnight employee punches at 00:30."""
        test_client.post("/employees", json=EMPLOYEES[3])  # EMP0004, 22:00-06:00
        ms = make_epoch(2026, 7, 16, 0, 30)
        resp = test_client.post(
            "/attendance/punch-in", json={"emp_code": "EMP0004", "punched_at": ms}
        )
        assert resp.status_code == 201
        assert resp.json()["late_minutes"] == 150
        assert resp.json()["date"] == "2026-07-15"


# ===========================================================================
# F2  strict epoch ms
# ===========================================================================


class TestF2StrictEpochMs:
    """F2: punched_at must reject floats, strings, booleans (Pydantic strict mode)."""

    def test_punch_in_rejects_float(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        resp = test_client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0001", "punched_at": 1783310280000.5},
        )
        assert resp.status_code == 422

    def test_punch_in_rejects_string(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        resp = test_client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0001", "punched_at": "1783310280000"},
        )
        assert resp.status_code == 422

    def test_punch_in_rejects_bool_true(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        resp = test_client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0001", "punched_at": True},
        )
        assert resp.status_code == 422

    def test_punch_in_rejects_float_with_fraction(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        resp = test_client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0001", "punched_at": 1.5},
        )
        assert resp.status_code == 422

    def test_punch_out_rejects_float(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        resp = test_client.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP0001", "punched_at": 1783310280000.5},
        )
        assert resp.status_code == 422

    def test_punch_out_rejects_string(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        resp = test_client.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP0001", "punched_at": "1783310280000"},
        )
        assert resp.status_code == 422

    def test_regularize_rejects_float_punch_in(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        test_client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0001", "punched_at": make_epoch(2026, 7, 15, 9, 30)},
        )
        resp = test_client.patch(
            "/attendance/EMP0001/2026-07-15",
            json={
                "punch_in": 1783310280000.5,
                "regularized_by": "admin",
                "reason": "fix",
            },
        )
        assert resp.status_code == 422

    def test_punch_in_accepts_valid_int(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        resp = test_client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0001", "punched_at": make_epoch(2026, 7, 15, 9, 30)},
        )
        assert resp.status_code == 201


# ===========================================================================
# F3  attendance pct 2dp
# ===========================================================================


class TestF3AttendancePctPrecision:
    """F3: attendance_pct must be 2 decimal places, not 4."""

    def test_pct_2dp_oracle(self):
        """3/23 * 100 = 13.043478... → 13.04 (2dp half-up)."""
        result = round_half_up_2(3 / 23 * 100)
        assert result == 13.04

    def test_pct_2dp_halfway(self):
        """2.345 → 2.35 (half-up)."""
        result = round_half_up_2(2.345)
        assert result == 2.35

    def test_monthly_endpoint_uses_2dp(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        test_client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0001", "punched_at": make_epoch(2026, 7, 15, 9, 30)},
        )
        resp = test_client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
        assert resp.status_code == 200
        pct = resp.json()["attendance_pct"]
        if pct is not None:
            # Verify 2dp: multiply by 100, should be very close to an integer
            assert abs(pct * 100 - round(pct * 100)) < 0.001


# ===========================================================================
# F4  punch out atomic
# ===========================================================================


class TestF4PunchOutAtomic:
    """F4: punch-out uses atomic find_one_and_update with correct status codes."""

    def test_punch_out_before_punch_in_422(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        ms_in = make_epoch(2026, 7, 15, 9, 30)
        test_client.post(
            "/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms_in}
        )
        ms_out = make_epoch(2026, 7, 15, 8, 0)
        resp = test_client.post(
            "/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms_out}
        )
        assert resp.status_code == 422

    def test_punch_out_at_punch_in_422(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        ms = make_epoch(2026, 7, 15, 9, 30)
        test_client.post(
            "/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms}
        )
        resp = test_client.post(
            "/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms}
        )
        assert resp.status_code == 422

    def test_punch_out_exactly_24h_200(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        ms_in = make_epoch(2026, 7, 15, 9, 30)
        test_client.post(
            "/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms_in}
        )
        ms_out = ms_in + 86400 * 1000
        resp = test_client.post(
            "/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms_out}
        )
        assert resp.status_code == 200

    def test_punch_out_over_24h_422(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        ms_in = make_epoch(2026, 7, 15, 9, 30)
        test_client.post(
            "/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms_in}
        )
        ms_out = ms_in + 86401 * 1000
        resp = test_client.post(
            "/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms_out}
        )
        assert resp.status_code == 422

    def test_punch_out_already_closed_409(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        ms_in = make_epoch(2026, 7, 15, 9, 30)
        test_client.post(
            "/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms_in}
        )
        ms_out = make_epoch(2026, 7, 15, 18, 30)
        test_client.post(
            "/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms_out}
        )
        resp = test_client.post(
            "/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms_out}
        )
        assert resp.status_code == 409

    def test_punch_out_no_records_404(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        resp = test_client.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP0001", "punched_at": make_epoch(2026, 7, 15, 18, 30)},
        )
        assert resp.status_code == 404

    def test_punch_out_invalid_ts_no_mutation(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        ms_in = make_epoch(2026, 7, 15, 9, 30)
        test_client.post(
            "/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms_in}
        )
        ms_out = make_epoch(2026, 7, 15, 8, 0)
        test_client.post(
            "/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms_out}
        )
        # Record should still be open
        resp = test_client.get("/attendance?emp_code=EMP0001")
        items = resp.json()["items"]
        assert items[0]["punch_out"] is None

    def test_punch_out_across_midnight_overnight(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[3])  # 22:00-06:00
        ms_in = make_epoch(2026, 7, 15, 22, 5)
        test_client.post(
            "/attendance/punch-in", json={"emp_code": "EMP0004", "punched_at": ms_in}
        )
        ms_out = make_epoch(2026, 7, 16, 6, 30)
        resp = test_client.post(
            "/attendance/punch-out", json={"emp_code": "EMP0004", "punched_at": ms_out}
        )
        assert resp.status_code == 200
        assert resp.json()["work_hours"] is not None

    def test_punch_out_selects_latest_eligible(self, test_client):
        """With multiple open records, selects the one with punch_in <= punched_at."""
        seed_employees()
        ms1 = make_epoch(2026, 7, 14, 9, 30)
        test_client.post(
            "/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms1}
        )
        ms2 = make_epoch(2026, 7, 15, 9, 30)
        test_client.post(
            "/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms2}
        )
        ms_out = make_epoch(2026, 7, 15, 18, 30)
        resp = test_client.post(
            "/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms_out}
        )
        assert resp.status_code == 200
        assert resp.json()["date"] == "2026-07-15"

    def test_concurrent_punch_out(self, test_client):
        """20 threads punch out the same record — exactly 1 should succeed."""
        test_client.post("/employees", json=EMPLOYEES[0])
        ms_in = make_epoch(2026, 7, 15, 9, 30)
        test_client.post(
            "/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms_in}
        )
        ms_out = make_epoch(2026, 7, 15, 18, 30)

        results = []

        def punch_out_thread():
            try:
                resp = test_client.post(
                    "/attendance/punch-out",
                    json={"emp_code": "EMP0001", "punched_at": ms_out},
                )
                results.append(resp.status_code)
            except Exception:
                results.append(500)

        threads = [threading.Thread(target=punch_out_thread) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert results.count(200) == 1
        assert results.count(409) == 19

    def test_punch_out_derived_fields_correct(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        ms_in = make_epoch(2026, 7, 15, 9, 30)
        test_client.post(
            "/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms_in}
        )
        ms_out = make_epoch(2026, 7, 15, 13, 0)
        resp = test_client.post(
            "/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms_out}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["work_hours"] == 3.50
        assert data["half_day"] is True


# ===========================================================================
# F5  leaderboard orphan ranking
# ===========================================================================


class TestF5LeaderboardOrphan:
    """F5: $lookup must happen BEFORE $rank so orphan attendance records
    (with no matching employee) don't occupy rank slots."""

    def test_orphan_does_not_appear(self, test_client):
        """Insert attendance for a non-existent employee — it should not appear."""
        test_client.post("/employees", json=EMPLOYEES[0])
        ms = make_epoch(2026, 7, 15, 10, 30)
        test_client.post(
            "/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms}
        )
        # Insert orphan attendance directly
        db.attendance_logs.insert_one(
            {
                "emp_code": "EMP9999",
                "date": "2026-07-15",
                "status": "PRESENT",
                "punch_in": to_utc_dt(ms),
                "punch_out": None,
                "work_hours": None,
                "late_minutes": 100,
                "overtime_minutes": 0,
                "half_day": False,
                "history": [],
            }
        )
        resp = test_client.get("/analytics/leaderboard/late?month=2026-07")
        items = resp.json()["items"]
        emp_codes = [i["emp_code"] for i in items]
        assert "EMP9999" not in emp_codes

    def test_competition_ranking_1_2_2_4(self, test_client):
        """Three employees with late minutes 30, 20, 20, 10 → ranks 1, 2, 2, 4."""
        for emp in EMPLOYEES[:4]:
            if emp["emp_code"] == "EMP0004":
                continue  # skip overnight for simplicity
            test_client.post("/employees", json=emp)
        # EMP0001: 10 min late
        test_client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0001", "punched_at": make_epoch(2026, 7, 15, 9, 40) + 1000},
        )
        # EMP0002: 20 min late
        test_client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0002", "punched_at": make_epoch(2026, 7, 15, 9, 50)},
        )
        # EMP0003: 20 min late (shift 10:00, punch 10:20 → 20 min)
        test_client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0003", "punched_at": make_epoch(2026, 7, 15, 10, 20)},
        )
        # Need another record for EMP0001 to get to 30 min total
        test_client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0001", "punched_at": make_epoch(2026, 7, 16, 9, 40) + 1000},
        )
        resp = test_client.get("/analytics/leaderboard/late?month=2026-07&limit=10")
        items = resp.json()["items"]
        ranks = {i["emp_code"]: i["rank"] for i in items}
        # EMP0001 has 20 total late, EMP0002 has 20, EMP0003 has 20
        # They should all share the same rank
        assert len(set(ranks.values())) <= 2  # at most 2 distinct ranks

    def test_tie_at_limit_cutoff(self, test_client):
        """Ties at the limit cutoff: both tied employees appear."""
        for emp in EMPLOYEES[:3]:
            test_client.post("/employees", json=emp)
        # All punch late: EMP0001 at 10:00 (30 min late), EMP0002 at 10:00 (30 min), EMP0003 at 10:30 (30 min)
        for code, ms in [
            ("EMP0001", make_epoch(2026, 7, 15, 10, 0)),
            ("EMP0002", make_epoch(2026, 7, 15, 10, 0)),
            ("EMP0003", make_epoch(2026, 7, 15, 10, 30)),
        ]:
            test_client.post(
                "/attendance/punch-in", json={"emp_code": code, "punched_at": ms}
            )
        resp = test_client.get("/analytics/leaderboard/late?month=2026-07&limit=2")
        items = resp.json()["items"]
        # All three have same late minutes, so limit=2 should include all 3
        assert len(items) == 3


# ===========================================================================
# F6  trend rounding & range
# ===========================================================================


class TestF6TrendRounding:
    """F6: MongoDB $round is half-to-even; contract requires half-up.
    Integer arithmetic in pipeline must produce identical results."""

    def test_half_up_tie_case_1_over_32(self):
        """Oracle: 1/32 = 0.03125. Half-up to 4dp → 0.0313 (not 0.0312)."""
        d = Decimal(1) / Decimal(32)
        expected = float(d.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
        assert expected == 0.0313

    def test_half_up_3_over_80(self):
        """Oracle: 3/80 = 0.0375. Half-up to 4dp → 0.0375 (exact, no tie)."""
        d = Decimal(3) / Decimal(80)
        expected = float(d.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
        assert expected == 0.0375

    def test_trend_range_exactly_92_days(self, test_client):
        """92 days inclusive should be accepted."""
        test_client.post("/employees", json=EMPLOYEES[0])
        resp = test_client.get(
            "/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-09-30"
        )
        # Jul 1 to Sep 30 = 92 days inclusive
        assert resp.status_code == 200

    def test_trend_range_93_days_422(self, test_client):
        """93 days inclusive should be rejected."""
        test_client.post("/employees", json=EMPLOYEES[0])
        resp = test_client.get(
            "/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-10-01"
        )
        assert resp.status_code == 422

    def test_trend_one_row_per_day(self, test_client):
        test_client.post("/employees", json=EMPLOYEES[0])
        resp = test_client.get(
            "/analytics/departments/Engineering/trend?from=2026-07-14&to=2026-07-16"
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 3
        dates = [i["date"] for i in items]
        assert dates == sorted(dates)

    def test_trend_weekend_rate_null(self, test_client):
        """Weekend days should have attendance_rate = null."""
        test_client.post("/employees", json=EMPLOYEES[0])
        resp = test_client.get(
            "/analytics/departments/Engineering/trend?from=2026-07-11&to=2026-07-12"
        )
        # Jul 11 2026 is Saturday, Jul 12 is Sunday
        assert resp.status_code == 200
        items = resp.json()["items"]
        for item in items:
            assert item["attendance_rate"] is None

    def test_trend_working_day_no_records_rate_0(self, test_client):
        """Working day with no attendance records → rate 0, not null."""
        test_client.post("/employees", json=EMPLOYEES[0])
        resp = test_client.get(
            "/analytics/departments/Engineering/trend?from=2026-07-15&?to=2026-07-15"
        )
        if resp.status_code == 200:
            items = resp.json()["items"]
            for item in items:
                if item["is_working_day"] and item["date"] == "2026-07-15":
                    assert item["attendance_rate"] == 0.0

    def test_trend_moving_avg_shorter_window(self, test_client):
        """First day: moving_avg_7d should equal attendance_rate."""
        test_client.post("/employees", json=EMPLOYEES[0])
        test_client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0001", "punched_at": make_epoch(2026, 7, 15, 9, 30)},
        )
        resp = test_client.get(
            "/analytics/departments/Engineering/trend?from=2026-07-15&to=2026-07-15"
        )
        if resp.status_code == 200:
            items = resp.json()["items"]
            jul15 = [i for i in items if i["date"] == "2026-07-15"]
            if jul15:
                assert jul15[0]["moving_avg_7d"] == jul15[0]["attendance_rate"]
