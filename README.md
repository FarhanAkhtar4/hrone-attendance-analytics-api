# HROne Attendance API

## Setup
```bash
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

Set `MONGO_URI` and `MONGO_DB` in env or `.env`. Real env vars win over `.env`
since we use `load_dotenv(override=False)`.

## Run
```bash
uvicorn app.main:app --port 8000
```

We try to create indexes at startup, but if Mongo isn't ready yet they get
created lazily on the first successful `/health` call. `/health` returns 200
when MongoDB is reachable, 503 otherwise.

## Tests
```bash
pytest tests/test_api.py -v
```

114 tests against a real MongoDB instance. Each run gets a fresh test db
(named `hrone_test_<timestamp>`) so runs don't step on each other.

### Test Breakdown
| Cat | # | What |
|-----|---|------|
| R1-R8 rules | 36 | tz, late, OT, hours, half day, presence, working days, rounding |
| Employee CRUD | 6 | create, dup, list, page, filter, no id leak |
| Punch in/out | 10 | ok, 404, dup, late, OT, half day, already out |
| Attendance list | 2 | no id leak, pagination |
| Regularize | 4 | status, no-op 422, ABSENT clear, 404 |
| Analytics | 5 | monthly, dept, leaderboard, trend, 404 |
| F1 overnight | 8 | 00:30->150, 02:00->240, 0500->420, grace, API |
| F2 strict epoch | 7 | rejects float/str/bool in punch |
| F3 2dp precision | 3 | oracle, halfway, endpoint |
| F4 atomic out | 10 | 422/409, 24h, race, derived fields |
| F5 orphan rank | 3 | no orphans, comp ranking (1,2,2,4), tie cutoff |
| F6 trend round | 7 | half-up, 92d, 1/day, wknd null, MA |
| F7 explain | 2 | valid, invalid |
| F8 epoch val | 3 | secs-scale, bool, valid |

## Lint
```bash
ruff check app/ tests/
```

## Notes
- Everything lives in `app/main.py` (single-file convention)
- Needs MongoDB 7.0+ for `$setWindowFields`, `$densify`, `$rank`
- Analytics are all aggregation pipelines, no Python loops
- Trend uses `$densify` for gap-filling and `$setWindowFields` for 7-day MA
- We use `Decimal.quantize(ROUND_HALF_UP)` everywhere. The trend pipeline
  uses integer arithmetic to dodge Mongo's `$round` half-to-even issue
- Punch-out: atomic `find_one_and_update` to prevent races
- Regularize: optimistic concurrency via history length check
