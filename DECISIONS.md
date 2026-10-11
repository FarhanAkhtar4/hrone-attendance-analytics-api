# DECISIONS.md

1. **Indexes.** I created six indexes: unique `emp_code` on employees (duplicate-create safety,* employee lookup); `department` on employees (department filter); unique `(emp_code, date)` compound on attendance_logs (punch-in race safety, per-employee monthly lookup); `(date DESC, emp_code ASC)` (attendance list sort); `(date, status)` (analytics date-range + status filter); `(emp_code, date, status)` (per-employee analytics); `(emp_code, late_minutes)` (leaderboard). I considered a `(department, emp_code)` index on attendance_logs but rejected it because department lives on employees, not on attendance_logs.

2. **Punch-in race.** Two identical punch-ins arrive simultaneously. Both build the document and call `insert_one`. The unique compound index on `(emp_code, date)` means the second insert raises `DuplicateKeyError`, which I catch and convert to 409. Exactly one document is written; exactly one caller gets 201.

3. **Ties.** My leaderboard uses `$setWindowFields` with `$rank` (standard competition ranking) sorted by `total_late_minutes` descending. Ties share a rank and the next rank is skipped (1,2,2,4). I apply `$match` on `rank <= limit` after ranking, so if two employees tie at rank 10 and `limit=10`, both appear.

4. **Headcount.** The department summary pipeline groups employees by department with `joined_on <= end_of_month`, then `$lookup` into attendance_logs using a pipeline-style lookup filtered to the month. Employees with zero logs still appear because the `$group` on employees produces them; the lookup simply returns an empty stats array, which defaults to zero counts.

5. **One thing I would change** if this had to serve 100× the data: I'd switch the attendance list and analytics to use **capped pre-aggregated daily/department summary collections** updated via change streams, eliminating the need to scan the full attendance_logs collection for each analytics request.
