# DECISIONS.md

1. **Indexes.** Unique `employees.emp_code` (identity + 409 races); unique `(emp_code, date)` on logs (punch-in race, monthly match, regularize lookup); `(date −1, emp_code)` (list sort/pagination and every analytics date-range `$match`); `(emp_code, punch_in −1)` (punch-out "most recent" lookup); `(department, emp_code)` (dept filter + sort, summary/trend headcount). I considered a standalone `status` index and a separate `{date: 1}` index and rejected both: status has five values so it is never selective, and the date prefix of the compound index already serves range scans.

2. **Punch-in race.** Both requests compute the same truncated instant, IST date (R1) and late minutes, then `insert_one`. The unique `(emp_code, date)` index makes one insert win; the loser gets `DuplicateKeyError`, mapped to 409. No check-then-insert, so simultaneity is decided by the database, and 10 parallel punches yield exactly one 201.

3. **Ties.** Ranking uses `$setWindowFields` with `$rank` (competition ranking: 1, 2, 2, 4), never the array position. `limit` filters on `rank <= limit` after ranking, so a tie at the cutoff returns extra rows (e.g. limit 3 over totals 60/40/40/40 returns all four). Rows order by total desc, `emp_code` asc.

4. **Headcount.** Department summary never counts logs for headcount: it counts `employees` with `joined_on <= month-end` per department, so zero-log people count and later joiners don't. Presence/averages come from a separate `$lookup` aggregation; departments with headcount 0 are omitted.

5. **One thing you would change** if this had to serve 100x the data: pre-aggregate. Nightly (or change-stream) rollups per `(department, date)` and per `(emp_code, month)` would make summary/leaderboard/trend O(days) instead of scanning millions of logs, with the raw logs kept only for drill-down and corrections, which would invalidate the affected rollup keys.
