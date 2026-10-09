# DECISIONS.md

**1. Indexes.** I created unique indexes on `employees.emp_code` and
`attendance_logs.(emp_code, date)` — these enforce identity and make the create and
punch-in races safe. `(date, emp_code)` serves the list sort and month-range matches,
`(emp_code, punch_in)` serves punch-out's "latest record" lookup, and
`(department, joined_on)` / `(joined_on)` serve the summary and trend headcount. I
rejected a single-field `status` index: it is low-selectivity and would push the planner
toward an in-memory sort.

**2. Punch-in race.** Both requests pass validation and both call `insert_one`. The
unique `(emp_code, date)` index lets exactly one succeed; the other raises
`DuplicateKeyError`, which I catch and return as 409. No check-then-insert.

**3. Ties.** I rank with `$rank` (competition ranking) and apply `limit` *after* ranking,
so a tie at the cutoff returns every tied row — e.g. `limit=3` with ranks 1,2,2 returns
all three.

**4. Headcount.** The summary starts from `employees` (not logs) and `$sum: 1` over them,
so people with zero logs still count; their logs are joined in with a sub-pipeline.

**5. For 100× data** I would shard `attendance_logs` on `{emp_code, date}` (or pre-aggregate
monthly rollups) so analytics scan far fewer documents.
