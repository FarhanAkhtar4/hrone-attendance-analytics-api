# HROne Employee Attendance & Analytics API

## Setup
```bash
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

## Environment Variables
- `MONGO_URI` — MongoDB connection string (default: `mongodb://localhost:27017`)
- `MONGO_DB` — Database name (default: `attendance_db`)

Real environment variables take priority over `.env` file.

## Run
```bash
uvicorn app.main:app --port 8000
```

The app creates all required indexes at startup (idempotently). `/health` returns 200 when MongoDB is reachable, 503 otherwise.

## Tests
```bash
pytest tests/test_api.py -v
```

**114 tests pass** against MongoDB 7.0.14. Tests use a dedicated test database with a real MongoDB instance.

## Lint
```bash
ruff check app/main.py tests/test_api.py
```

## Notes
- All application code is in `app/main.py`
- MongoDB 7.0+ required for `$setWindowFields`, `$densify`, `$rank`
- Analytics are implemented as MongoDB aggregation pipelines (not Python loops)
- All rounding uses `Decimal.quantize(ROUND_HALF_UP)` per R8; trend pipeline uses integer arithmetic to avoid MongoDB `$round` half-to-even divergence
- Punch-out uses atomic `find_one_and_update` to prevent race conditions
- Regularize uses history-length optimistic concurrency control
