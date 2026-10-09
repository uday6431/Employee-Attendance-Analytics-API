# DECISIONS.md

## 1. Indexes

**Indexes created:**
- `emp_code` (unique): Enforces unique employee codes and enables fast employee lookups. Serves `/employees` POST 409 duplicate detection and employee verification in all punch/analytics endpoints.
- `(emp_code, date)` (unique): The natural key of attendance records. Enables fast lookup of a specific day's record for punch-in/out/regularize. Enforces at most one record per (emp_code, date), required by the contract.
- `(date DESC, emp_code ASC)`: Sorted order for listing attendance by date descending, then emp_code ascending. Eliminates in-memory sort; essential for fast pagination on 100k records.
- `(emp_code, date)` non-unique: For monthly analytics queries that filter by emp_code + date range.
- `punch_in` (ascending): For `punch_out` endpoint which does `.find({punch_in: {$lte: punch_time}})` to locate the record to close.

**Rejected:** Separate indexes on `status`, `department`, or `joined_on`. These are covered by the multi-key indexes above or don't appear in the hot path. Department filtering happens at the Python layer via joins, not MongoDB queries on attendance_logs.

## 2. Punch-in race condition

**Scenario:** Two identical punch-in requests arrive simultaneously for the same employee on the same date.

**What happens:** 
1. Both requests pass the `.find_one()` check (both see no existing record) and proceed.
2. Both call `insert_one()` on attendance_logs.
3. The unique index on `(emp_code, date)` causes the second insert to fail with `DuplicateKeyError`.
4. The first request returns 201, the second gets 409.
5. A race condition in multiple threads is eliminated—exactly as the contract requires.

**Code:** Try/except catches `DuplicateKeyError` and raises 409. Proper ordering: the database enforces uniqueness, not application logic.

## 3. Ties on leaderboard

**Scenario:** Two employees are tied with 35 late minutes at a cutoff `limit=3`.

**What returns:** All 4 employees. The ranking uses standard competition ranking (1, 2, 2, 4). Both tied employees have rank=2, which is ≤ limit (3). When we filter by `rank <= limit`, both are included. The response shows more than `limit` rows because we must include all employees tied at a qualifying rank.

**Why:** This matches the contract: "The response can therefore contain more than `limit` rows when employees tie at a rank <= `limit`."

## 4. Headcount (employees with zero logs)

**How it works:** 
- R9 says headcount = employees with `joined_on` <= end-of-period, regardless of logs.
- For monthly summaries, we query: `db.employees.count_documents({"department": dept, "joined_on": {"$lte": "2026-07-31"}})`.
- For daily trend, we iterate through all dates and for each day count employees with `joined_on` <= that day.
- Employees who joined this month but have no punch records (zero logs) are still counted; they bring down headcount-based rates.

**Result:** If a 5-person department has only 2 people punched in one day (3 with zero logs), attendance_rate = 2/5 = 40%, not 100%.

## 5. One thing to change for 100x data

**Current bottleneck:** The 7-day moving average calculation for department trend iterates through dates in Python, maintaining a growing array of rates. For a 92-day range, this is fine; for 100x data (~9000 days), it's wasteful.

**Better approach:** Use a MongoDB `$densify` stage to fill all dates and a `$setWindowFields` stage to compute the 7-day moving average directly in the aggregation pipeline. This keeps all computation in the database, avoiding Python serialization and memory overhead. MongoDB can then return only the final result, not 9000 rows of intermediate rates.

**Benefit:** All 9000 rows computed in one aggregation pass; no Python loop. Faster, scales linearly, no intermediate arrays in memory.

---

*Implementation strictly follows R1-R10 and the OpenAPI contract. All timestamps are stored as BSON datetimes (UTC) and converted to epoch milliseconds at the API boundary. IST timezone used only for calendar date and shift time calculations.*
