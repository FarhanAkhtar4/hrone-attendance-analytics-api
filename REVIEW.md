# REVIEW.md

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `load_dotenv()` | Called without `override=False`, so real env vars don't win over `.env` | Set `MONGO_URI` in env and in `.env` with different values; the `.env` value wins | Changed to `load_dotenv(override=False)` |
| 2 | `health()` | Never touches MongoDB; contract requires 503 when Mongo is down | Stop MongoDB, call `/health` → returns 200 instead of 503 | Added `client.admin.command("ping")` with 503 on exception |
| 3 | `compute_late_minutes()` | Grace check uses `minutes > 10` instead of `diff_seconds > 600`; also computes minutes from `punch_in - start` using `replace()` on naive datetime | Input: shift 09:30, punch at 09:40:01 → should be 10, but returns 0 | Rewrote to use IST-aware comparison, check `diff_seconds <= 600`, return `int(diff_seconds // 60)` |
| 4 | `compute_work_hours()` | Uses Python `round()` which is banker's rounding, not half-up per R4 | Input: 4.495 hours → `round()` gives 4.49 (banker's), should be 4.50 | Replaced with `Decimal.quantize(ROUND_HALF_UP)` |
| 5 | `compute_overtime()` | No ≥30 minute threshold check (R3); uses naive datetime for shift_end; uses `IST.localize()` which doesn't exist | Input: 5 min overtime → returns 5, should be 0 | Added threshold, fixed timezone handling |
| 6 | `PunchInIn` model | `punched_at: Optional[int] = None` with truthiness check `if body.punched_at`; value 0 treated as None | Input: `punched_at=0` → treated as "use now" | Used `is not None` check; added epoch ms validation |
| 7 | `punch_in()` | No 404 for unknown employee — `emp` is None → 500 | Input: `emp_code="UNKNOWN"` → 500 | Added `if not emp: raise HTTPException(404)` |
| 8 | `punch_in()` | Check-then-insert race condition | Two parallel punch-ins → two 201s | Added unique compound index, catch `DuplicateKeyError` → 409 |
| 9 | `punch_in()` | Leaks `id` in response | Response contains `"id"` field | Removed the line |
| 10 | `punch_in()` | `datetime.fromtimestamp()` creates naive datetime | All time calculations wrong | Changed to `datetime.fromtimestamp(secs, tz=UTC)` |
| 11 | `punch_in()` | No status validation — ABSENT/LEAVE accepted | Input: `status="ABSENT"` → creates ABSENT record | Added Pydantic validator restricting to PRESENT/WFH/ON_DUTY |
| 12 | `punch_in()` | No truncation to whole seconds per R1 | Input: `punched_at` with milliseconds | Added `truncate_to_seconds()` in `to_utc_dt()` |
| 13 | `punch_in()` | Date derivation wrong for overnight shifts (R1) — uses UTC date | Input: overnight shift employee punching at 05:00 IST | Added `get_ist_date()` with overnight shift logic |
| 14 | `create_employee()` | `datetime.now()` is naive; should be UTC | `created_at` stored as naive datetime | Changed to `datetime.now(UTC)` |
| 15 | `list_employees()` | `skip = page * page_size` — off by one | page=1, page_size=20 → skips first 20 | Fixed to `(page - 1) * page_size` |
| 16 | `list_employees()` | `total` ignores the department filter | Filter by department → total shows all | Fixed to `db.employees.count_documents(q)` |
| 17 | `list_employees()` | No sort by `emp_code` as required | Results in arbitrary order | Added `.sort("emp_code", 1)` |
| 18 | `list_employees()` | No page_size max validation (R10: max 100) | Input: `page_size=10000` | Added validation for `page_size` 1-100 and `page` >= 1 |
| 19 | `list_attendance()` | Loads entire collection into memory | On 100k records → OOM | Changed to DB-side sort/skip/limit |
| 20 | `list_attendance()` | Sorts in Python, not in DB | Slow at scale | Changed to `.sort()` in MongoDB |
| 21 | `list_attendance()` | Leaks `id` | Response contains `"id"` field | Removed |
| 22 | `list_attendance()` | No epoch ms conversion | Response contains datetime objects | Added `serialize_attendance_record()` |
| 23 | `MongoClient` | No `tz_aware=True` | PyMongo returns naive datetimes | Added `tz_aware=True, tzinfo=UTC` |
| 24 | Module-level | No server/connect timeouts on MongoClient | Slow MongoDB → hangs forever | Added timeout parameters |
| 25 | No indexes at startup | No indexes → COLLSCAN on every query | `explain` shows COLLSCAN | Added `_create_indexes()` in lifespan |
