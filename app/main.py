"""Employee Attendance & Analytics API.

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience;
      real environment variables take priority).
"""
import bisect
import calendar
import os
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Annotated, Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

EPOCH_MIN = 100000000000
EPOCH_MAX = 4102444800000

PRESENT_STATUSES = ("PRESENT", "WFH", "ON_DUTY")

DATE_RE = r"^\d{4}-\d{2}-\d{2}$"


# --------------------------------------------------------------------------- #
# Indexes
# --------------------------------------------------------------------------- #
_index_lock = threading.Lock()
_indexes_done = False


def ensure_indexes() -> None:
    """Create all indexes idempotently."""
    db.employees.create_index("emp_code", unique=True, name="uniq_emp_code")
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="dept_empcode",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uniq_emp_date",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="date_empcode",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="emp_punchin",
    )


def ensure_indexes_maybe() -> None:
    global _indexes_done
    if _indexes_done:
        return
    with _index_lock:
        if _indexes_done:
            return
        try:
            ensure_indexes()
            _indexes_done = True
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_indexes_maybe()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0",
              lifespan=lifespan)


@app.middleware("http")
async def _ensure_indexes_middleware(request, call_next):
    ensure_indexes_maybe()
    return await call_next(request)


# --------------------------------------------------------------------------- #
# Time helpers (R1-R5, R8)
# --------------------------------------------------------------------------- #
def dt_from_ms(ms: int) -> datetime:
    """Epoch millis -> aware UTC datetime (millisecond precision)."""
    s, rem = divmod(int(ms), 1000)
    return datetime.fromtimestamp(s, tz=UTC).replace(microsecond=rem * 1000)


def trunc_sec(dt: datetime) -> datetime:
    """Truncate an instant to whole seconds (R1). Returns aware UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return dt.replace(microsecond=0)


def ms_from_dt(dt: datetime) -> int:
    """Aware/naive datetime -> epoch millis. Naive is assumed UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return calendar.timegm(dt.utctimetuple()) * 1000 + dt.microsecond // 1000


def parse_hm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start


def attendance_date(punch_utc: datetime, shift_start: str, shift_end: str) -> str:
    """IST calendar date of a punch, with the overnight-shift rule (R1)."""
    ist = punch_utc.astimezone(IST)
    if is_overnight(shift_start, shift_end):
        eh, em = parse_hm(shift_end)
        if (ist.hour, ist.minute, ist.second) < (eh, em, 0):
            return (ist - timedelta(days=1)).date().isoformat()
    return ist.date().isoformat()


def shift_start_dt(date_str: str, shift_start: str) -> datetime:
    y, mo, d = map(int, date_str.split("-"))
    h, mi = parse_hm(shift_start)
    return datetime(y, mo, d, h, mi, tzinfo=IST)


def shift_end_dt(date_str: str, shift_start: str, shift_end: str) -> datetime:
    y, mo, d = map(int, date_str.split("-"))
    h, mi = parse_hm(shift_end)
    dt = datetime(y, mo, d, h, mi, tzinfo=IST)
    if is_overnight(shift_start, shift_end):
        dt += timedelta(days=1)
    return dt


def compute_late_minutes(punch_utc: datetime, shift_start: str, date_str: str) -> int:
    """Whole minutes since shift start, 0 unless strictly more than 10:00 late (R2)."""
    start = shift_start_dt(date_str, shift_start).astimezone(UTC)
    diff = (punch_utc - start).total_seconds()
    if diff <= 600:
        return 0
    return int(diff // 60)


def compute_overtime_minutes(
    punch_out_utc: datetime, shift_start: str, shift_end: str, date_str: str
) -> int:
    """Whole minutes from shift end, counted only when >= 30 (R3)."""
    end = shift_end_dt(date_str, shift_start, shift_end).astimezone(UTC)
    diff = (punch_out_utc - end).total_seconds()
    if diff < 1800:
        return 0
    return int(diff // 60)


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """(out - in) in seconds / 3600, 2 decimals half-up (R4)."""
    secs = (ms_from_dt(punch_out_utc) - ms_from_dt(punch_in_utc)) // 1000
    return float(
        (Decimal(secs) / Decimal(3600)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    )


def round2(x: Any) -> float:
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round4(x: Any) -> float:
    return float(Decimal(str(x)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def norm_num(x: Any) -> Any:
    """Return ints as int and whole floats as int for clean JSON numbers."""
    if isinstance(x, bool):
        return x
    if isinstance(x, float) and x.is_integer():
        return int(x)
    return x


# --------------------------------------------------------------------------- #
# Validation helpers -> 422
# --------------------------------------------------------------------------- #
def check_date(value: str, name: str = "date") -> str:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except (ValueError, TypeError):
        raise HTTPException(422, f"invalid {name}: {value!r}")
    return value


def check_month(value: str) -> str:
    import re as _re

    if not _re.match(r"^\d{4}-(0[1-9]|1[0-2])$", value or ""):
        raise HTTPException(422, f"invalid month: {value!r}")
    return value


def month_bounds(month: str) -> tuple[str, str]:
    y, m = map(int, month.split("-"))
    last = calendar.monthrange(y, m)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def working_days(first: str, last: str, joined_on: str) -> int:
    """Mon-Fri days in [max(first, joined_on), last] (R7)."""
    start = max(first, joined_on)
    if start > last:
        return 0
    d0 = date.fromisoformat(start)
    d1 = date.fromisoformat(last)
    n = 0
    cur = d0
    while cur <= d1:
        if cur.weekday() < 5:
            n += 1
        cur += timedelta(days=1)
    return n


# --------------------------------------------------------------------------- #
# Serialization (Mongo <-> API boundary)
# --------------------------------------------------------------------------- #
def serialize_history(entries: Any) -> list:
    out = []
    for e in entries or []:
        changes: dict[str, Any] = {}
        for k, v in (e.get("changes") or {}).items():
            frm, to = v.get("from"), v.get("to")
            if isinstance(frm, datetime):
                frm = ms_from_dt(frm)
            if isinstance(to, datetime):
                to = ms_from_dt(to)
            changes[k] = {"from": frm, "to": to}
        out.append(
            {
                "at": ms_from_dt(e["at"]),
                "by": e.get("by"),
                "reason": e.get("reason"),
                "changes": changes,
            }
        )
    return out


def serialize_record(doc: dict) -> dict:
    punch_in = doc.get("punch_in")
    punch_out = doc.get("punch_out")
    return {
        "emp_code": doc.get("emp_code"),
        "date": doc.get("date"),
        "status": doc.get("status"),
        "punch_in": ms_from_dt(punch_in) if isinstance(punch_in, datetime) else None,
        "punch_out": ms_from_dt(punch_out) if isinstance(punch_out, datetime) else None,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0) or 0,
        "overtime_minutes": doc.get("overtime_minutes", 0) or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": serialize_history(doc.get("history")),
    }


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc.get("emp_code"),
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": ms_from_dt(doc["created_at"]),
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
EpochMs = Annotated[int, Field(strict=True, ge=EPOCH_MIN, le=EPOCH_MAX)]

SHIFT_RE = r"^([01]\d|2[0-3]):[0-5]\d$"


class Status(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class EmployeeIn(BaseModel):
    model_config = {"extra": "ignore"}

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(
        max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    )
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=SHIFT_RE)
    shift_end: str = Field(default="18:30", pattern=SHIFT_RE)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_joined(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except (ValueError, TypeError):
            raise ValueError(f"invalid joined_on: {v!r}")
        return v

    @model_validator(mode="after")
    def _shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = {"extra": "ignore"}

    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: str = "PRESENT"

    @field_validator("status")
    @classmethod
    def _presence_only(cls, v: str) -> str:
        if v not in PRESENT_STATUSES:
            raise ValueError(f"invalid punch-in status: {v!r}")
        return v


class PunchOutIn(BaseModel):
    model_config = {"extra": "ignore"}

    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    model_config = {"extra": "ignore"}

    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Shared aggregation fragments
# --------------------------------------------------------------------------- #
def _dow_expr():
    # 1=Sunday .. 7=Saturday; Mon-Fri == 2..6. Pure calendar date -> tz free.
    return {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}


def _is_working_expr():
    return {
        "$and": [{"$gte": [_dow_expr(), 2]}, {"$lte": [_dow_expr(), 6]}]
    }


def _presence_flag():
    return {"$in": ["$status", list(PRESENT_STATUSES)]}


def _present_day_value():
    # half day counts 0.5 of a present day (R5); Mon-Fri only (R7).
    return {
        "$cond": [
            {"$and": [_presence_flag(), _is_working_expr()]},
            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
            0,
        ]
    }


def _late_flag():
    return {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}


# --------------------------------------------------------------------------- #
# System / employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        return JSONResponse({"detail": "mongodb unavailable"}, status_code=503)


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = trunc_sec(datetime.now(tz=UTC))
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: dict[str, Any] = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [serialize_employee(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance: punch-in / punch-out / list / regularize
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    ts = trunc_sec(
        dt_from_ms(body.punched_at) if body.punched_at is not None
        else datetime.now(tz=UTC)
    )
    att_date = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], att_date),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    doc.pop("_id", None)
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    ts = trunc_sec(
        dt_from_ms(body.punched_at) if body.punched_at is not None
        else datetime.now(tz=UTC)
    )
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if rec is None or not isinstance(rec.get("punch_in"), datetime):
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    punch_in = trunc_sec(rec["punch_in"])
    if ts <= punch_in:
        raise HTTPException(422, "punched_at must be after punch_in")
    if (ts - punch_in).total_seconds() > 24 * 3600:
        raise HTTPException(422, "punched_at more than 24 hours after punch_in")
    work_hours = compute_work_hours(punch_in, ts)
    overtime = compute_overtime_minutes(
        ts, emp["shift_start"], emp["shift_end"], rec["date"]
    )
    half_day = work_hours < 4.50
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        cur = db.attendance_logs.find_one({"_id": rec["_id"]})
        if cur is None or cur.get("punch_out") is not None:
            raise HTTPException(409, "already punched out")
        raise HTTPException(409, "concurrent punch-out, please retry")
    return serialize_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from is not None:
        check_date(date_from, "date_from")
    if date_to is not None:
        check_date(date_to, "date_to")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    q: dict[str, Any] = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        q["date"] = {}
        if date_from is not None:
            q["date"]["$gte"] = date_from
        if date_to is not None:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status.value
    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [serialize_record(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def _recompute_derived(
    status: str,
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
    shift_start: str,
    shift_end: str,
    date_str: str,
) -> dict[str, Any]:
    if status in ("ABSENT", "LEAVE"):
        return {
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }
    late = compute_late_minutes(punch_in, shift_start, date_str)  # type: ignore[arg-type]
    if punch_out is None:
        return {
            "work_hours": None,
            "late_minutes": late,
            "overtime_minutes": 0,
            "half_day": False,
        }
    work = compute_work_hours(punch_in, punch_out)  # type: ignore[arg-type]
    return {
        "work_hours": work,
        "late_minutes": late,
        "overtime_minutes": compute_overtime_minutes(
            punch_out, shift_start, shift_end, date_str
        ),
        "half_day": work < 4.50,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    check_date(date, "date")
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no record for this date")

    old_status: str = rec.get("status")
    old_in = rec.get("punch_in")
    old_out = rec.get("punch_out")
    if not isinstance(old_in, datetime):
        old_in = None
    if not isinstance(old_out, datetime):
        old_out = None

    if body.status in (Status.ABSENT, Status.LEAVE) and (
        body.punch_in is not None or body.punch_out is not None
    ):
        raise HTTPException(
            422, "punch_in/punch_out cannot be supplied with ABSENT or LEAVE"
        )

    new_status = body.status.value if body.status is not None else old_status
    new_in = trunc_sec(dt_from_ms(body.punch_in)) if body.punch_in is not None else old_in
    new_out: Optional[datetime] = None
    if body.punch_out is not None:
        new_out = trunc_sec(dt_from_ms(body.punch_out))
    elif body.status in (Status.ABSENT, Status.LEAVE):
        new_out = None
    else:
        new_out = old_out

    if new_status in ("ABSENT", "LEAVE"):
        new_in = None
        new_out = None
    else:
        if new_in is None:
            raise HTTPException(422, "presence status requires a punch_in")
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, "punch_in must stay on the record's date")
        if new_out is not None:
            if new_out <= new_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (new_out - new_in).total_seconds() > 24 * 3600:
                raise HTTPException(422, "punch_out must be within 24 hours")

    old_in_ms = ms_from_dt(old_in) if old_in is not None else None
    old_out_ms = ms_from_dt(old_out) if old_out is not None else None
    new_in_ms = ms_from_dt(new_in) if new_in is not None else None
    new_out_ms = ms_from_dt(new_out) if new_out is not None else None
    if new_status == old_status and new_in_ms == old_in_ms and new_out_ms == old_out_ms:
        raise HTTPException(422, "request changes nothing")

    derived = _recompute_derived(
        new_status, new_in, new_out, emp["shift_start"], emp["shift_end"], date
    )

    old_vals: dict[str, Any] = {
        "status": old_status,
        "punch_in": old_in_ms,
        "punch_out": old_out_ms,
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes", 0) or 0,
        "overtime_minutes": rec.get("overtime_minutes", 0) or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    new_vals: dict[str, Any] = {
        "status": new_status,
        "punch_in": new_in_ms,
        "punch_out": new_out_ms,
        **derived,
    }
    changes: dict[str, Any] = {}
    for key in (
        "status",
        "punch_in",
        "punch_out",
        "work_hours",
        "late_minutes",
        "overtime_minutes",
        "half_day",
    ):
        if old_vals[key] != new_vals[key]:
            changes[key] = {"from": old_vals[key], "to": new_vals[key]}

    entry = {
        "at": trunc_sec(datetime.now(tz=UTC)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    filt: dict[str, Any] = {"_id": rec["_id"]}
    if "history" in rec:
        filt["history"] = rec["history"]
    else:
        filt["history"] = {"$exists": False}
    res = db.attendance_logs.update_one(
        filt,
        {
            "$set": {
                "status": new_status,
                "punch_in": new_in,
                "punch_out": new_out,
                **derived,
            },
            "$push": {"history": entry},
        },
    )
    if res.matched_count == 0:
        raise HTTPException(409, "concurrent modification, please retry")
    fresh = db.attendance_logs.find_one({"_id": rec["_id"]})
    return serialize_record(fresh)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
def build_monthly_pipeline(emp_code: str, first: str, last: str) -> list[dict]:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": _present_day_value()},
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {"$sum": _late_flag()},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    first, last = month_bounds(month)
    rows = list(db.attendance_logs.aggregate(build_monthly_pipeline(emp_code, first, last)))
    agg = rows[0] if rows else {}
    wd = working_days(first, last, emp["joined_on"])
    present = agg.get("present_days", 0) or 0
    pct = round2(Decimal(str(present)) / Decimal(wd) * 100) if wd else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": norm_num(present),
        "leave_days": int(agg.get("leave_days", 0) or 0),
        "late_count": int(agg.get("late_count", 0) or 0),
        "total_late_minutes": int(agg.get("total_late_minutes", 0) or 0),
        "total_overtime_minutes": int(agg.get("total_overtime_minutes", 0) or 0),
        "attendance_pct": pct,
    }


def build_summary_pipeline(
    first: str, last: str, department: Optional[str]
) -> list[dict]:
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "_emp",
            }
        },
        {"$unwind": "$_emp"},
    ]
    if department is not None:
        pipeline.append({"$match": {"_emp.department": department}})
    pipeline.append(
        {
            "$group": {
                "_id": "$_emp.department",
                "present_days": {"$sum": _present_day_value()},
                "avg_work_hours": {
                    "$avg": {
                        "$cond": [
                            {
                                "$and": [
                                    _presence_flag(),
                                    {"$ne": ["$work_hours", None]},
                                ]
                            },
                            "$work_hours",
                            None,
                        ]
                    }
                },
                "late_count": {"$sum": _late_flag()},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                },
            }
        }
    )
    pipeline.append({"$sort": {"_id": 1}})
    return pipeline


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    first, last = month_bounds(month)
    emp_q: dict[str, Any] = {"joined_on": {"$lte": last}}
    if department is not None:
        emp_q["department"] = department
    depts = sorted(db.employees.distinct("department", emp_q))
    headcounts = {
        d: db.employees.count_documents(
            {"department": d, "joined_on": {"$lte": last}}
        )
        for d in depts
    }
    agg = {
        r["_id"]: r
        for r in db.attendance_logs.aggregate(
            build_summary_pipeline(first, last, department)
        )
    }
    items = []
    for d in depts:
        hc = headcounts[d]
        if hc == 0:
            continue
        r = agg.get(d, {})
        avg = r.get("avg_work_hours")
        items.append(
            {
                "department": d,
                "headcount": hc,
                "present_days": norm_num(r.get("present_days", 0) or 0),
                "avg_work_hours": round2(avg) if avg is not None else None,
                "late_count": int(r.get("late_count", 0) or 0),
                "total_late_minutes": int(r.get("total_late_minutes", 0) or 0),
                "leave_count": int(r.get("leave_count", 0) or 0),
                "on_duty_count": int(r.get("on_duty_count", 0) or 0),
            }
        )
    return {"month": month, "items": items}


def build_leaderboard_pipeline(
    first: str, last: str, department: Optional[str], limit: int
) -> list[dict]:
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "_emp",
            }
        },
        {"$unwind": "$_emp"},
    ]
    if department is not None:
        pipeline.append({"$match": {"_emp.department": department}})
    pipeline.extend(
        [
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$late_minutes", 0]}
                    },
                    "late_count": {"$sum": _late_flag()},
                    "name": {"$first": "$_emp.name"},
                    "department": {"$first": "$_emp.department"},
                }
            },
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
        ]
    )
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = month_bounds(month)
    rows = db.attendance_logs.aggregate(
        build_leaderboard_pipeline(first, last, department, limit)
    )
    return {
        "month": month,
        "items": [
            {
                "rank": r["rank"],
                "emp_code": r["_id"],
                "name": r["name"],
                "department": r["department"],
                "total_late_minutes": r["total_late_minutes"],
                "late_count": r["late_count"],
            }
            for r in rows
        ],
    }


def build_trend_pipeline(
    codes: list[str], from_dt: datetime, to_excl: datetime
) -> list[dict]:
    from_s = from_dt.date().isoformat()
    to_s = (to_excl - timedelta(days=1)).date().isoformat()
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": from_s, "$lte": to_s}}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            _presence_flag(),
                            {
                                "$cond": [
                                    {"$ifNull": ["$half_day", False]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "late_count": {"$sum": _late_flag()},
            }
        },
        {
            "$project": {
                "_id": 0,
                "dateObj": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "dateObj",
                "range": {"step": 1, "unit": "day", "bounds": [from_dt, to_excl]},
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {
                    "$dateToString": {"date": "$dateObj", "format": "%Y-%m-%d"}
                },
                "dow": {"$dayOfWeek": "$dateObj"},
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$project": {
                "date": 1,
                "present_count": 1,
                "late_count": 1,
                "is_working_day": {
                    "$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
):
    check_date(from_, "from")
    check_date(to, "to")
    if to < from_:
        raise HTTPException(422, "'to' must not be before 'from'")
    d0 = date.fromisoformat(from_)
    d1 = date.fromisoformat(to)
    if (d1 - d0).days + 1 > 92:
        raise HTTPException(422, "range must not exceed 92 days")
    emps = list(db.employees.find({"department": department}, {"_id": 0}))
    if not emps:
        raise HTTPException(404, "unknown department")
    codes = [e["emp_code"] for e in emps]
    joined = sorted(e["joined_on"] for e in emps)
    from_dt = datetime(d0.year, d0.month, d0.day, tzinfo=UTC)
    to_excl = datetime(d1.year, d1.month, d1.day, tzinfo=UTC) + timedelta(days=1)
    rows = list(db.attendance_logs.aggregate(build_trend_pipeline(codes, from_dt, to_excl)))

    items = []
    window: list[Decimal] = []
    for r in rows:
        day = r["date"]
        hc = bisect.bisect_right(joined, day)
        present = r.get("present_count", 0) or 0
        working = r.get("is_working_day", False)
        rate = (
            round4(Decimal(str(present)) / Decimal(hc))
            if working and hc > 0
            else None
        )
        window.append(Decimal(str(rate)) if rate is not None else None)  # type: ignore[arg-type]
        window = window[-7:]
        vals = [v for v in window if v is not None]
        mov = (
            float(
                (sum(vals, Decimal(0)) / len(vals)).quantize(
                    Decimal("0.0001"), rounding=ROUND_HALF_UP
                )
            )
            if vals
            else None
        )
        items.append(
            {
                "date": day,
                "is_working_day": bool(working),
                "headcount": hc,
                "present_count": norm_num(present),
                "late_count": int(r.get("late_count", 0) or 0),
                "attendance_rate": rate,
                "moving_avg_7d": mov,
            }
        )
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Explain
# --------------------------------------------------------------------------- #
EXPLAIN_ENDPOINTS = (
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
)


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(
        None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"
    ),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint not in EXPLAIN_ENDPOINTS:
        raise HTTPException(422, f"unknown endpoint: {endpoint!r}")
    collection = "attendance_logs"
    if endpoint == "attendance_list":
        if date_from is not None:
            check_date(date_from, "date_from")
        if date_to is not None:
            check_date(date_to, "date_to")
        if (
            date_from is not None
            and date_to is not None
            and date_from > date_to
        ):
            raise HTTPException(422, "date_from must not be after date_to")
        q: dict[str, Any] = {}
        if emp_code is not None:
            q["emp_code"] = emp_code
        if date_from is not None or date_to is not None:
            q["date"] = {}
            if date_from is not None:
                q["date"]["$gte"] = date_from
            if date_to is not None:
                q["date"]["$lte"] = date_to
        if status is not None:
            q["status"] = status.value
        inner: dict[str, Any] = {
            "find": collection,
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    elif endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise HTTPException(422, "emp_code and month are required")
        if db.employees.find_one({"emp_code": emp_code}) is None:
            raise HTTPException(404, "unknown employee")
        first, last = month_bounds(month)
        inner = {
            "aggregate": collection,
            "pipeline": build_monthly_pipeline(emp_code, first, last),
            "cursor": {},
        }
    elif endpoint == "department_summary":
        if month is None:
            raise HTTPException(422, "month is required")
        first, last = month_bounds(month)
        inner = {
            "aggregate": collection,
            "pipeline": build_summary_pipeline(first, last, department),
            "cursor": {},
        }
    elif endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(422, "month is required")
        first, last = month_bounds(month)
        inner = {
            "aggregate": collection,
            "pipeline": build_leaderboard_pipeline(first, last, department, limit),
            "cursor": {},
        }
    else:  # department_trend
        if department is None or from_ is None or to is None:
            raise HTTPException(422, "department, from and to are required")
        check_date(from_, "from")
        check_date(to, "to")
        if to < from_:
            raise HTTPException(422, "'to' must not be before 'from'")
        d0 = date.fromisoformat(from_)
        d1 = date.fromisoformat(to)
        if (d1 - d0).days + 1 > 92:
            raise HTTPException(422, "range must not exceed 92 days")
        emps = list(db.employees.find({"department": department}))
        if not emps:
            raise HTTPException(404, "unknown department")
        codes = [e["emp_code"] for e in emps]
        from_dt = datetime(d0.year, d0.month, d0.day, tzinfo=UTC)
        to_excl = datetime(d1.year, d1.month, d1.day, tzinfo=UTC) + timedelta(days=1)
        inner = {
            "aggregate": collection,
            "pipeline": build_trend_pipeline(codes, from_dt, to_excl),
            "cursor": {},
        }
    explain = db.command({"explain": inner, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": collection, "explain": explain}
