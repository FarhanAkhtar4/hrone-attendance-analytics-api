# REVIEW.md

Bugs I found in the starter `app/main.py`:

| # | Where | Bug | Catch | Fix |
|---|-------|-----|-------|-----|
| 1 | `load_dotenv()` | No `override=False` -- .env wins over real env vars | Set MONGO_URI in both env and .env with different values | `load_dotenv(override=False)` |
| 2 | `health()` | Never pings Mongo, so can't return 503 | Stop mongo, hit /health -> still 200 | Added `client.admin.command("ping")` |
| 3 | `compute_late_minutes()` | Grace check `minutes > 10` instead of `diff_seconds > 600`; naive datetime arithmetic | shift 09:30, punch 09:40:01 -> wrong late value | IST-aware comparison, check `diff_seconds <= 600` |
| 4 | `compute_work_hours()` | Uses Python `round()` (banker's rounding, not half-up) | 4.495 hours -> 4.49 instead of 4.50 | `Decimal.quantize(ROUND_HALF_UP)` |
| 5 | `compute_overtime()` | No 30-min threshold; naive datetime for shift_end; `IST.localize()` doesn't exist on timezone | 5 min overtime -> returns 5, should be 0 | Added threshold, fixed tz handling |
| 6 | `PunchInIn` model | `if body.punched_at` -- value 0 treated as None | `punched_at=0` -> "use now" instead of error | `is not None` check |
| 7 | `punch_in()` | No 404 for unknown employee -> 500 | `emp_code="UNKNOWN"` -> 500 | Added 404 check |
| 8 | `punch_in()` | Check-then-insert race condition | Two parallel punch-ins -> two 201s | Unique compound index + DuplicateKeyError -> 409 |
| 9 | `punch_in()` | Leaks `_id` in response | Response has `"id"` field | Removed the line |
| 10 | `punch_in()` | `fromtimestamp` creates naive datetime | All time calcs wrong | `fromtimestamp(secs, tz=UTC)` |
| 11 | `punch_in()` | No status validation -- ABSENT accepted | `status="ABSENT"` via punch-in | Pydantic validator for PRESENT/WFH/ON_DUTY |
| 12 | `punch_in()` | No truncation to whole seconds | Sub-second precision stored | `truncate_to_seconds()` |
| 13 | `punch_in()` | Date derivation wrong for overnight shifts (uses UTC date) | 05:00 IST punch -> wrong date | `get_ist_date()` with overnight logic |
| 14 | `create_employee()` | `datetime.now()` is naive | `created_at` stored as naive | `datetime.now(UTC)` |
| 15 | `list_employees()` | `skip = page * page_size` (off by one) | page=1 skips first 20 | `(page - 1) * page_size` |
| 16 | `list_employees()` | `total` ignores department filter | Filter by dept -> total shows all | `count_documents(q)` |
| 17 | `list_employees()` | No sort by emp_code | Arbitrary order | `.sort("emp_code", 1)` |
| 18 | `list_employees()` | No page_size max validation | `page_size=10000` -> OOM | Validation for 1-100 |
| 19 | `list_attendance()` | Loads entire collection into memory | 100k records -> OOM | DB-side sort/skip/limit |
| 20 | `list_attendance()` | Sorts in Python | Slow at scale | `.sort()` in MongoDB |
| 21 | `list_attendance()` | Leaks `id` | Response has `"id"` | Removed |
| 22 | `list_attendance()` | No epoch ms conversion | BSON dates not JSON-serializable | `serialize_attendance_record()` |
| 23 | `MongoClient` | No `tz_aware=True` | `to_ist()` produces wrong results | `tz_aware=True, tzinfo=UTC` |
| 24 | Module-level | No timeouts on MongoClient | Unreachable Mongo -> hangs forever | Added timeout params |
| 25 | No indexes | COLLSCAN on every query | 100k records -> very slow | `_create_indexes()` in lifespan |

Not defects (I looked at these and decided they're fine):
- `PunchInIn.status` defaulting to `PRESENT` -- that's correct per the spec
- `Optional[int] = None` for `punched_at` is right (defaults to "now" when you don't send it)
- `EmployeeIn` structure is fine, just needed stricter field validation
