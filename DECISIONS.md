# DECISIONS.md

1. **Indexes.** I went with six indexes: unique `emp_code` on employees (duplicate-create safety + lookups), `department` on employees, unique `(emp_code, date)` compound on attendance_logs (this is the big one -- prevents duplicate punch-ins at the DB level), `(date DESC, emp_code ASC)` for the attendance list sort order, `(date, status)` and `(emp_code, date, status)` for analytics, and `(emp_code, late_minutes)` for the leaderboard. I thought about a `(department, emp_code)` index on attendance_logs but department lives on the employees collection so we'd need a lookup anyway -- didn't seem worth it.

2. **Punch-in race.** Two identical punch-ins arriving at the same time is a real concern. Both build the doc and call `insert_one`. The unique `(emp_code, date)` index means the second insert raises `DuplicateKeyError`, which I catch and convert to 409. Exactly one doc gets written, exactly one caller gets 201. Clean.

3. **Ties.** I used `$setWindowFields` with `$rank` (standard competition ranking) sorted by `total_late_minutes` descending. Tied values share a rank and the next one skips (so you get 1,2,2,4 not 1,2,2,3). I apply `$match` on `rank <= limit` *after* ranking, so if two people tie at rank 10 and `limit=10`, both appear. I considered dense rank but the spec said competition ranking so that's what we go with.

4. **Headcount.** The department summary pipeline groups employees by department where `joined_on <= end_of_month`, then does a pipeline-style `$lookup` into attendance_logs. Employees with zero logs still show up because the `$group` on employees produces them -- the lookup just gives an empty stats array which defaults to zero counts. I initially worried about left-join semantics but `$group` + `$lookup` handles it naturally.

5. **If I had to scale this 100x.** I'd switch the analytics to use pre-aggregated daily/department summary collections updated via change streams. Right now every analytics request scans attendance_logs which works fine at the current scale but would get expensive with millions of records. I also thought about materialized views but change streams give us more control over error handling and retry.
