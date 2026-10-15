# Employee Attendance & Analytics API

FastAPI + MongoDB implementation of the HROne assignment (contract: `openapi.yaml`, storage: `DATA_MODEL.md`).

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # or export MONGO_URI / MONGO_DB (env vars win)
python sample_seed.py  # optional sample data
uvicorn app.main:app --port 8000
```

Requires MongoDB 6.0+ (tested on 7.0/8.x). All code is in `app/main.py`;
indexes are created idempotently at startup.

## Notes

- Instants are epoch milliseconds at the API boundary, BSON dates in Mongo;
  dates are IST `YYYY-MM-DD` strings per R1 (incl. overnight shifts).
- Analytics (`monthly`, department `summary`, late `leaderboard`, department
  `trend`) are MongoDB aggregation pipelines (competition `$rank` for ties,
  `$densify` gap-filling for the trend); derived fields are read, not recomputed.
- `GET /admin/explain/{endpoint}` returns the raw `executionStats` explain of
  the endpoint's own query/pipeline; all plans are index-only (IXSCAN, no COLLSCAN).
- Findings on the starter are in `REVIEW.md`; index/race/tie/headcount
  rationale in `DECISIONS.md`.
