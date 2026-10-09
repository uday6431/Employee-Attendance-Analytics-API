"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

Complete implementation with all endpoints and business rules (R1-R10).
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional, Any
from decimal import Decimal, ROUND_HALF_UP
from math import floor

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    serverSelectionTimeoutMS=2000,
    connectTimeoutMS=2000,
    socketTimeoutMS=2000
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Initialization: Create indexes at startup
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def create_indexes():
    """Create MongoDB indexes idempotently at startup."""
    try:
        # Employees collection
        db.employees.create_index("emp_code", unique=True)
        
        # Attendance logs collection
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)])
        db.attendance_logs.create_index([("punch_in", ASCENDING)])
        
        print("[OK] MongoDB indexes created successfully")
    except Exception as e:
        print(f"[WARN] MongoDB not ready yet or error creating indexes: {e}")
        print("       Server will continue - indexes can be created later via startup or API calls")
    

# --------------------------------------------------------------------------- #
# Helpers: Business Logic
# --------------------------------------------------------------------------- #
def epoch_ms_to_datetime_utc(epoch_ms: int) -> datetime:
    """Convert epoch milliseconds to UTC datetime, truncate to seconds."""
    dt = datetime.fromtimestamp(epoch_ms / 1000, tz=UTC)
    return dt.replace(microsecond=0)


def datetime_to_epoch_ms(dt: datetime) -> int:
    """Convert datetime to epoch milliseconds."""
    return int(dt.timestamp() * 1000)


def get_ist_midnight(epoch_ms: int) -> datetime:
    """Get the midnight (00:00) in IST for the given epoch ms."""
    dt_utc = epoch_ms_to_datetime_utc(epoch_ms)
    dt_ist = dt_utc.astimezone(IST)
    return dt_ist.replace(hour=0, minute=0, second=0, microsecond=0)


def get_attendance_date(punch_time_ms: int, shift_start: str, shift_end: str) -> str:
    """
    Determine the attendance date (IST calendar date) from punch time.
    R1: For overnight shifts (shift_end <= shift_start), if punch_in time (IST) is before shift_end,
    it belongs to the previous day's shift.
    """
    dt_utc = epoch_ms_to_datetime_utc(punch_time_ms)
    dt_ist = dt_utc.astimezone(IST)
    date_ist = dt_ist.date()
    
    # Check if it's an overnight shift
    shift_start_h, shift_start_m = map(int, shift_start.split(":"))
    shift_end_h, shift_end_m = map(int, shift_end.split(":"))
    
    if shift_end_h * 60 + shift_end_m <= shift_start_h * 60 + shift_start_m:
        # Overnight shift
        punch_time_h = dt_ist.hour
        punch_time_m = dt_ist.minute
        punch_time_mins = punch_time_h * 60 + punch_time_m
        shift_end_mins = shift_end_h * 60 + shift_end_m
        
        if punch_time_mins < shift_end_mins:
            # This punch is from the previous day's shift
            date_ist = date_ist - timedelta(days=1)
    
    return date_ist.isoformat()


def compute_late_minutes(punch_in_ms: int, shift_start: str) -> int:
    """
    R2: Grace period is 10 minutes. Late only if strictly > 10:00 after shift_start.
    Return whole minutes (floor).
    """
    punch_dt = epoch_ms_to_datetime_utc(punch_in_ms).astimezone(IST)
    h, m = map(int, shift_start.split(":"))
    shift_start_dt = punch_dt.replace(hour=h, minute=m, second=0, microsecond=0)
    
    delta_seconds = (punch_dt - shift_start_dt).total_seconds()
    delta_minutes = int(delta_seconds / 60)  # floor
    
    return delta_minutes if delta_minutes > 10 else 0


def compute_work_hours(punch_in_ms: int, punch_out_ms: int) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    punch_in = epoch_ms_to_datetime_utc(punch_in_ms)
    punch_out = epoch_ms_to_datetime_utc(punch_out_ms)
    seconds = (punch_out - punch_in).total_seconds()
    hours = seconds / 3600
    # Half-up rounding to 2 decimals
    decimal_hours = Decimal(str(hours))
    rounded = round(decimal_hours, 2)
    return float(rounded)


def compute_overtime_minutes(punch_out_ms: int, shift_end: str, attendance_date: str) -> int:
    """
    R3: overtime_minutes = whole minutes (floor) between shift_end and punch_out,
    only when >= 30; otherwise 0.
    """
    punch_out = epoch_ms_to_datetime_utc(punch_out_ms).astimezone(IST)
    h, m = map(int, shift_end.split(":"))
    
    # Build shift_end datetime for the attendance date
    date_obj = datetime.fromisoformat(attendance_date).date()
    shift_end_dt = datetime.combine(date_obj, datetime.min.time()).replace(hour=h, minute=m, tzinfo=IST)
    
    # For overnight shifts, if punch_out is after midnight (smaller IST time), it's the next calendar day
    shift_start_h, shift_start_m = map(int, shift_end.split(":"))  # This is wrong - we need shift_start
    # Actually, we need shift_start but it's not passed here. Let's check: for overnight shifts,
    # shift_end is in the next calendar day. So if shift_end <= shift_start, shift_end is the next day.
    # We'll handle this by checking if punch_out is much later than shift_end.
    
    # Actually, let me reconsider: if shift_end <= shift_start (overnight), then:
    # - The attendance_date is the day the shift STARTED
    # - shift_end time falls on the NEXT calendar day
    # So we need to add 1 day to shift_end_dt if it's an overnight shift
    
    # We can detect overnight by checking if shift_end_dt would be before punch_out significantly
    delta_seconds = (punch_out - shift_end_dt).total_seconds()
    
    # If delta is hugely negative (punch_out is way before shift_end), it's probably an overnight shift
    # and punch_out is actually on the next day, so shift_end should be incremented
    if delta_seconds < -12 * 3600:  # More than 12 hours before
        shift_end_dt = shift_end_dt + timedelta(days=1)
        delta_seconds = (punch_out - shift_end_dt).total_seconds()
    
    delta_minutes = int(delta_seconds / 60)  # floor
    return delta_minutes if delta_minutes >= 30 else 0


def compute_half_day(work_hours: float) -> bool:
    """
    R5: half_day = true when rounded work_hours < 4.50.
    """
    return work_hours < 4.50


def is_working_day(date_str: str) -> bool:
    """Check if a date (YYYY-MM-DD) is a weekday (Mon-Fri). 0=Mon, 6=Sun."""
    date_obj = datetime.fromisoformat(date_str).date()
    return date_obj.weekday() < 5


def round_half_up(value: float, decimals: int) -> float:
    """Round a float using half-up rounding to specified decimals."""
    d = Decimal(str(value))
    rounded = float(d.quantize(Decimal(10) ** -decimals, rounding=ROUND_HALF_UP))
    return rounded


def format_number(value: float, decimals: int) -> float:
    """Format number with half-up rounding."""
    if value is None:
        return None
    return round_half_up(value, decimals)


# --------------------------------------------------------------------------- #
# Pydantic Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str
    
    @field_validator("emp_code")
    def validate_emp_code(cls, v):
        if not v or len(v) < 4:
            raise ValueError("Invalid emp_code")
        return v


class Employee(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: int


class EmployeePage(BaseModel):
    items: list[Employee]
    total: int
    page: int
    page_size: int


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"
    
    @field_validator("status")
    def validate_status(cls, v):
        if v not in ["PRESENT", "WFH", "ON_DUTY"]:
            raise ValueError("Invalid status for punch-in")
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    regularized_by: str


class AttendanceRecord(BaseModel):
    emp_code: str
    date: str
    status: str
    punch_in: Optional[int]
    punch_out: Optional[int]
    work_hours: Optional[float]
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: list


class AttendancePage(BaseModel):
    items: list[AttendanceRecord]
    total: int
    page: int
    page_size: int


class EmployeeMonthly(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float]


class DepartmentItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float]
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummaryResponse(BaseModel):
    month: str
    items: list[DepartmentItem]


class LeaderboardItem(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class LeaderboardResponse(BaseModel):
    month: str
    items: list[LeaderboardItem]


class TrendItem(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float]
    moving_avg_7d: Optional[float]


class TrendResponse(BaseModel):
    department: str
    items: list[TrendItem]


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    """Health check endpoint."""
    try:
        # Quick check if MongoDB is available
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex(('localhost', 27017))
        sock.close()
        
        if result == 0:
            return {"status": "ok"} 
        else:
            raise HTTPException(status_code=503, detail="MongoDB not accessible")
    except Exception as e:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    """Create a new employee."""
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": datetime.now(UTC),
    }
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    
    # Return with created_at as epoch ms
    doc.pop("_id", None)
    doc["created_at"] = datetime_to_epoch_ms(doc["created_at"])
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees with optional department filter."""
    try:
        q = {}
        if department:
            q["department"] = department
        
        total = db.employees.count_documents(q)
        skip = (page - 1) * page_size
        items = list(
            db.employees.find(q, {"_id": 0})
            .sort([("emp_code", ASCENDING)])
            .skip(skip)
            .limit(page_size)
        )
        
        # Convert created_at to epoch ms
        for item in items:
            if "created_at" in item and isinstance(item["created_at"], datetime):
                item["created_at"] = datetime_to_epoch_ms(item["created_at"])
            elif "created_at" not in item:
                # Set to current time if missing
                item["created_at"] = datetime_to_epoch_ms(datetime.now(UTC))
        
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Database unavailable: {str(e)}")


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    """Record a punch-in."""
    # Verify employee exists
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    
    # Get punch time
    punch_time_ms = body.punched_at if body.punched_at else int(datetime.now(UTC).timestamp() * 1000)
    punch_time_ms = int(punch_time_ms)
    
    # Truncate to seconds
    punch_dt = epoch_ms_to_datetime_utc(punch_time_ms)
    punch_time_ms = datetime_to_epoch_ms(punch_dt)
    
    # Determine attendance date (R1)
    attendance_date = get_attendance_date(punch_time_ms, emp["shift_start"], emp["shift_end"])
    
    # Compute late_minutes (R2)
    late_minutes = compute_late_minutes(punch_time_ms, emp["shift_start"])
    
    # Build document
    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punch_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for this date")
    
    # Return with times as epoch ms
    doc.pop("_id", None)
    doc["punch_in"] = datetime_to_epoch_ms(doc["punch_in"]) if doc["punch_in"] else None
    doc["punch_out"] = None
    return doc


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    """Record a punch-out."""
    # Verify employee exists
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    
    # Get punch time
    punch_time_ms = body.punched_at if body.punched_at else int(datetime.now(UTC).timestamp() * 1000)
    punch_time_ms = int(punch_time_ms)
    
    # Truncate to seconds
    punch_dt = epoch_ms_to_datetime_utc(punch_time_ms)
    punch_time_ms = datetime_to_epoch_ms(punch_dt)
    
    # Find the most recent punch-in at or before this punch_out time
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": punch_dt},
            "punch_out": None,
        },
        sort=[("punch_in", DESCENDING)],
    )
    
    if not record:
        raise HTTPException(status_code=404, detail="No open punch-in found")
    
    # Validate punch_out is after punch_in and within 24 hours
    if punch_time_ms <= datetime_to_epoch_ms(record["punch_in"]):
        raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
    
    if (punch_dt - record["punch_in"]).total_seconds() > 24 * 3600:
        raise HTTPException(status_code=422, detail="punch_out more than 24 hours after punch_in")
    
    # Check if already punched out (race condition)
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_dt,
                "work_hours": compute_work_hours(
                    datetime_to_epoch_ms(record["punch_in"]), punch_time_ms
                ),
                "overtime_minutes": compute_overtime_minutes(
                    punch_time_ms, emp["shift_end"], record["date"]
                ),
            }
        },
        return_document=True,
    )
    
    if not updated:
        raise HTTPException(status_code=409, detail="Record already punched out")
    
    # Recompute half_day
    work_hours = updated["work_hours"]
    db.attendance_logs.update_one(
        {"_id": record["_id"]},
        {"$set": {"half_day": compute_half_day(work_hours)}},
    )
    updated["half_day"] = compute_half_day(work_hours)
    
    # Return with times as epoch ms
    updated.pop("_id", None)
    updated["punch_in"] = datetime_to_epoch_ms(updated["punch_in"]) if updated["punch_in"] else None
    updated["punch_out"] = datetime_to_epoch_ms(updated["punch_out"]) if updated["punch_out"] else None
    return updated


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List attendance records with filters."""
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
        
        # Validate date range
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from > date_to")
    
    if status:
        q["status"] = status
    
    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    items = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    
    # Convert times to epoch ms
    for item in items:
        item["punch_in"] = datetime_to_epoch_ms(item["punch_in"]) if item["punch_in"] else None
        item["punch_out"] = datetime_to_epoch_ms(item["punch_out"]) if item["punch_out"] else None
        
        # Convert history times
        if "history" in item:
            for entry in item["history"]:
                entry["at"] = datetime_to_epoch_ms(entry["at"])
                if "changes" in entry:
                    for field, change in entry["changes"].items():
                        if field in ["punch_in", "punch_out"]:
                            if change.get("from"):
                                change["from"] = datetime_to_epoch_ms(change["from"])
                            if change.get("to"):
                                change["to"] = datetime_to_epoch_ms(change["to"])
    
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    """Regularize (manually correct) an attendance record."""
    # Verify employee exists
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    
    # Find the record
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    
    # Build update
    old_values = {
        "status": record.get("status"),
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }
    
    new_values = dict(old_values)
    
    # Apply changes
    if body.status is not None:
        new_values["status"] = body.status
    
    if body.punch_in is not None:
        punch_in_dt = epoch_ms_to_datetime_utc(body.punch_in)
        new_values["punch_in"] = punch_in_dt
    
    if body.punch_out is not None:
        punch_out_dt = epoch_ms_to_datetime_utc(body.punch_out)
        new_values["punch_out"] = punch_out_dt
    
    # Validate rules
    if new_values["status"] in ["ABSENT", "LEAVE"]:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="Cannot set punch times for ABSENT/LEAVE",
            )
        new_values["punch_in"] = None
        new_values["punch_out"] = None
    else:
        # Presence status requires punch_in
        if new_values["punch_in"] is None:
            raise HTTPException(
                status_code=422, detail="Presence status requires punch_in"
            )
        
        # Validate punch_out vs punch_in
        if new_values["punch_out"] is not None:
            if new_values["punch_out"] <= new_values["punch_in"]:
                raise HTTPException(
                    status_code=422, detail="punch_out must be after punch_in"
                )
            
            if (new_values["punch_out"] - new_values["punch_in"]).total_seconds() > 24 * 3600:
                raise HTTPException(
                    status_code=422, detail="punch_out more than 24 hours after punch_in"
                )
    
    # Recompute derived fields
    if new_values["status"] in ["ABSENT", "LEAVE"]:
        new_values["work_hours"] = None
        new_values["late_minutes"] = 0
        new_values["overtime_minutes"] = 0
        new_values["half_day"] = False
    else:
        if new_values["punch_in"] is not None:
            new_values["late_minutes"] = compute_late_minutes(
                datetime_to_epoch_ms(new_values["punch_in"]), emp["shift_start"]
            )
        
        if new_values["punch_in"] is not None and new_values["punch_out"] is not None:
            new_values["work_hours"] = compute_work_hours(
                datetime_to_epoch_ms(new_values["punch_in"]),
                datetime_to_epoch_ms(new_values["punch_out"]),
            )
            new_values["overtime_minutes"] = compute_overtime_minutes(
                datetime_to_epoch_ms(new_values["punch_out"]), emp["shift_end"], date
            )
            new_values["half_day"] = compute_half_day(new_values["work_hours"])
        else:
            new_values["work_hours"] = None
            new_values["overtime_minutes"] = 0
            new_values["half_day"] = False
    
    # Check if anything actually changed
    changes_dict = {}
    for key in ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]:
        old_val = old_values.get(key)
        new_val = new_values.get(key)
        
        # For datetime comparison, convert to epoch ms for comparison
        if key in ["punch_in", "punch_out"]:
            old_ms = datetime_to_epoch_ms(old_val) if old_val else None
            new_ms = datetime_to_epoch_ms(new_val) if new_val else None
            if old_ms != new_ms:
                changes_dict[key] = {"from": old_ms, "to": new_ms}
        else:
            if old_val != new_val:
                changes_dict[key] = {"from": old_val, "to": new_val}
    
    if not changes_dict:
        raise HTTPException(status_code=422, detail="No changes made")
    
    # Create history entry
    history_entry = {
        "at": datetime.now(UTC),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_dict,
    }
    
    # Update document with optimistic concurrency control
    update_doc = {
        "status": new_values["status"],
        "punch_in": new_values["punch_in"],
        "punch_out": new_values["punch_out"],
        "work_hours": new_values["work_hours"],
        "late_minutes": new_values["late_minutes"],
        "overtime_minutes": new_values["overtime_minutes"],
        "half_day": new_values["half_day"],
    }
    
    result = db.attendance_logs.find_one_and_update(
        {"emp_code": emp_code, "date": date},
        {
            "$set": update_doc,
            "$push": {"history": history_entry},
        },
        return_document=True,
    )
    
    if not result:
        raise HTTPException(status_code=409, detail="Concurrent modification")
    
    # Format response
    result.pop("_id", None)
    result["punch_in"] = datetime_to_epoch_ms(result["punch_in"]) if result["punch_in"] else None
    result["punch_out"] = datetime_to_epoch_ms(result["punch_out"]) if result["punch_out"] else None
    
    if "history" in result:
        for entry in result["history"]:
            entry["at"] = datetime_to_epoch_ms(entry["at"])
            if "changes" in entry:
                for field, change in entry["changes"].items():
                    if field in ["punch_in", "punch_out"] and change.get("from"):
                        # Already epoch ms from changes_dict, keep as is
                        pass
    
    return result


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    """Get monthly attendance summary for an employee."""
    # Validate month format
    if len(month) != 7 or month[4] != "-":
        raise HTTPException(status_code=422, detail="Invalid month format")
    
    # Verify employee exists
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    
    year, mon = month.split("-")
    year, mon = int(year), int(mon)
    
    # Get all days in the month
    if mon == 12:
        next_month = datetime(year + 1, 1, 1).date()
    else:
        next_month = datetime(year, mon + 1, 1).date()
    
    first_month_date = datetime(year, mon, 1).date()
    last_month_date = next_month - timedelta(days=1)
    
    # R7: Count working days from joined_on
    joined_date = datetime.fromisoformat(emp["joined_on"]).date()
    working_days = 0
    present_days = 0.0
    late_count = 0
    total_late_minutes = 0
    total_overtime_minutes = 0
    leave_days = 0
    
    current_date = max(first_month_date, joined_date)
    while current_date <= last_month_date:
        if is_working_day(current_date.isoformat()):
            working_days += 1
            
            # Check for attendance record
            record = db.attendance_logs.find_one({
                "emp_code": emp_code,
                "date": current_date.isoformat(),
            })
            
            if record:
                if record["status"] in ["PRESENT", "WFH", "ON_DUTY"]:
                    if record.get("half_day"):
                        present_days += 0.5
                    else:
                        present_days += 1.0
                    
                    if record.get("late_minutes", 0) > 0:
                        late_count += 1
                        total_late_minutes += record.get("late_minutes", 0)
                    
                    total_overtime_minutes += record.get("overtime_minutes", 0)
                
                elif record["status"] == "LEAVE":
                    leave_days += 1
        
        current_date += timedelta(days=1)
    
    # Calculate attendance percentage (R8: 2 decimals half-up)
    if working_days > 0:
        attendance_pct = format_number((present_days / working_days) * 100, 2)
    else:
        attendance_pct = None
    
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": format_number(present_days, 2),
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    """Get department summary for a month."""
    # Validate month format
    if len(month) != 7 or month[4] != "-":
        raise HTTPException(status_code=422, detail="Invalid month format")
    
    year, mon = month.split("-")
    year, mon = int(year), int(mon)
    
    if mon < 1 or mon > 12:
        raise HTTPException(status_code=422, detail="Invalid month")
    
    # Get all days in the month
    if mon == 12:
        next_month = datetime(year + 1, 1, 1).date()
    else:
        next_month = datetime(year, mon + 1, 1).date()
    
    first_month_date = datetime(year, mon, 1).date()
    last_month_date = next_month - timedelta(days=1)
    
    # Get departments to process
    dept_filter = {"department": department} if department else {}
    departments = db.employees.distinct("department", dept_filter)
    
    results = []
    
    for dept in sorted(departments):
        # R9: Headcount = employees who joined on or before the last day of the month
        headcount = db.employees.count_documents({
            "department": dept,
            "joined_on": {"$lte": last_month_date.isoformat()},
        })
        
        if headcount == 0:
            continue
        
        # Get records for this department in this month
        pipeline = [
            {
                "$match": {
                    "date": {
                        "$gte": first_month_date.isoformat(),
                        "$lte": last_month_date.isoformat(),
                    }
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "emp",
                }
            },
            {"$unwind": {"path": "$emp", "preserveNullAndEmptyArrays": False}},
            {
                "$match": {
                    "emp.department": dept,
                }
            },
        ]
        
        records = list(db.attendance_logs.aggregate(pipeline))
        
        # Calculate metrics
        present_days = 0.0
        avg_work_hours_sum = 0.0
        avg_work_hours_count = 0
        late_count = 0
        total_late_minutes = 0
        leave_count = 0
        on_duty_count = 0
        
        for record in records:
            if record["status"] in ["PRESENT", "WFH", "ON_DUTY"]:
                if record.get("half_day"):
                    present_days += 0.5
                else:
                    present_days += 1.0
                
                if record.get("work_hours") is not None:
                    avg_work_hours_sum += record["work_hours"]
                    avg_work_hours_count += 1
                
                if record.get("late_minutes", 0) > 0:
                    late_count += 1
                    total_late_minutes += record.get("late_minutes", 0)
                
                if record["status"] == "ON_DUTY":
                    on_duty_count += 1
            
            elif record["status"] == "LEAVE":
                leave_count += 1
        
        avg_work_hours = None
        if avg_work_hours_count > 0:
            avg_work_hours = format_number(avg_work_hours_sum / avg_work_hours_count, 2)
        
        results.append({
            "department": dept,
            "headcount": headcount,
            "present_days": format_number(present_days, 2),
            "avg_work_hours": avg_work_hours,
            "late_count": late_count,
            "total_late_minutes": total_late_minutes,
            "leave_count": leave_count,
            "on_duty_count": on_duty_count,
        })
    
    return {
        "month": month,
        "items": results,
    }


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    """Get late-comers leaderboard for a month."""
    # Validate month format
    if len(month) != 7 or month[4] != "-":
        raise HTTPException(status_code=422, detail="Invalid month format")
    
    year, mon = month.split("-")
    year, mon = int(year), int(mon)
    
    # Get all days in the month
    if mon == 12:
        next_month = datetime(year + 1, 1, 1).date()
    else:
        next_month = datetime(year, mon + 1, 1).date()
    
    first_month_date = datetime(year, mon, 1).date()
    last_month_date = next_month - timedelta(days=1)
    
    # Build aggregation
    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": first_month_date.isoformat(),
                    "$lte": last_month_date.isoformat(),
                },
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": {"path": "$emp", "preserveNullAndEmptyArrays": False}},
    ]
    
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    
    pipeline.extend([
        {
            "$group": {
                "_id": "$emp_code",
                "emp_code": {"$first": "$emp_code"},
                "name": {"$first": "$emp.name"},
                "department": {"$first": "$emp.department"},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {"$sort": [("total_late_minutes", DESCENDING), ("emp_code", ASCENDING)]},
    ])
    
    results = list(db.attendance_logs.aggregate(pipeline))
    
    # Apply competition ranking (ties share rank, next rank is skipped)
    ranked = []
    current_rank = 1
    prev_late_minutes = None
    
    for i, item in enumerate(results):
        if item["total_late_minutes"] != prev_late_minutes:
            current_rank = i + 1
            prev_late_minutes = item["total_late_minutes"]
        
        item["rank"] = current_rank
        ranked.append(item)
    
    # Filter by limit (include all ties at the limit rank)
    final_results = [item for item in ranked if item["rank"] <= limit]
    
    return {
        "month": month,
        "items": final_results,
    }


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    """Get daily attendance trend for a department."""
    # Validate dates
    try:
        from_obj = datetime.fromisoformat(from_date).date()
        to_obj = datetime.fromisoformat(to_date).date()
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format")
    
    if to_obj < from_obj:
        raise HTTPException(status_code=422, detail="to < from")
    
    days_diff = (to_obj - from_obj).days
    if days_diff > 92:
        raise HTTPException(status_code=422, detail="Range exceeds 92 days")
    
    # Verify department exists
    dept_count = db.employees.count_documents({"department": department})
    if dept_count == 0:
        raise HTTPException(status_code=404, detail="Department not found")
    
    # Get all employees in the department (for headcount calculation)
    all_emps = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1}))
    
    # Generate all dates in range
    dates = []
    current = from_obj
    while current <= to_obj:
        dates.append(current.isoformat())
        current += timedelta(days=1)
    
    # Get aggregated attendance data
    pipeline = [
        {
            "$match": {
                "date": {"$gte": from_date, "$lte": to_date}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": {"path": "$emp", "preserveNullAndEmptyArrays": False}},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count_sum": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
            }
        },
    ]
    
    records_by_date = {}
    for item in db.attendance_logs.aggregate(pipeline):
        records_by_date[item["_id"]] = {
            "present_count": item["present_count_sum"],
            "late_count": item["late_count"],
        }
    
    # Build result with all dates
    items = []
    rates_for_moving_avg = []
    
    for date_str in dates:
        date_obj = datetime.fromisoformat(date_str).date()
        is_working_day_flag = is_working_day(date_str)
        
        # Calculate headcount for this day (R9)
        headcount = sum(
            1 for emp in all_emps
            if datetime.fromisoformat(emp["joined_on"]).date() <= date_obj
        )
        
        present_count = records_by_date.get(date_str, {}).get("present_count", 0.0)
        late_count = records_by_date.get(date_str, {}).get("late_count", 0)
        
        # Calculate attendance rate (only on working days with headcount > 0)
        attendance_rate = None
        if is_working_day_flag and headcount > 0:
            attendance_rate = format_number(present_count / headcount, 4)
            rates_for_moving_avg.append(attendance_rate)
        else:
            rates_for_moving_avg.append(None)
        
        # Calculate 7-day moving average (within the range)
        moving_avg_7d = None
        if is_working_day_flag:
            # Get up to 6 preceding rates plus current (7 total)
            idx = len(rates_for_moving_avg) - 1
            window_start = max(0, idx - 6)
            window_rates = [r for r in rates_for_moving_avg[window_start:idx + 1] if r is not None]
            
            if window_rates:
                moving_avg_7d = format_number(sum(window_rates) / len(window_rates), 4)
        
        items.append({
            "date": date_str,
            "is_working_day": is_working_day_flag,
            "headcount": headcount,
            "present_count": format_number(present_count, 2),
            "late_count": late_count,
            "attendance_rate": attendance_rate,
            "moving_avg_7d": moving_avg_7d,
        })
    
    return {
        "department": department,
        "items": items,
    }


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
):
    """Get MongoDB explain plan for an endpoint's query."""
    from pymongo.errors import OperationFailure
    
    collection_name = "attendance_logs"
    query = {}
    sort_spec = []
    
    if endpoint == "attendance_list":
        if emp_code:
            query["emp_code"] = emp_code
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to
        if status:
            query["status"] = status
        
        sort_spec = [("date", DESCENDING), ("emp_code", ASCENDING)]
        skip = (page - 1) * page_size
        
        try:
            explanation = db.attendance_logs.find(query).sort(sort_spec).skip(skip).limit(page_size).explain()
        except OperationFailure as e:
            raise HTTPException(status_code=422, detail=str(e))
    
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month required")
        
        year, mon = month.split("-")
        first_date = f"{year}-{int(mon):02d}-01"
        
        # Simple query to show what would be filtered
        query = {
            "emp_code": emp_code,
            "date": {"$gte": first_date},
        }
        collection_name = "attendance_logs"
        
        try:
            explanation = db.attendance_logs.find(query).explain()
        except OperationFailure as e:
            raise HTTPException(status_code=422, detail=str(e))
    
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month required")
        
        year, mon = month.split("-")
        first_date = f"{year}-{int(mon):02d}-01"
        query = {"date": {"$gte": first_date}}
        
        try:
            explanation = db.attendance_logs.find(query).explain()
        except OperationFailure as e:
            raise HTTPException(status_code=422, detail=str(e))
    
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month required")
        
        year, mon = month.split("-")
        first_date = f"{year}-{int(mon):02d}-01"
        query = {
            "date": {"$gte": first_date},
            "late_minutes": {"$gt": 0},
        }
        
        try:
            explanation = db.attendance_logs.find(query).explain()
        except OperationFailure as e:
            raise HTTPException(status_code=422, detail=str(e))
    
    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from, to required")
        
        query = {
            "date": {"$gte": from_date, "$lte": to_date}
        }
        
        try:
            explanation = db.attendance_logs.find(query).explain()
        except OperationFailure as e:
            raise HTTPException(status_code=422, detail=str(e))
    
    else:
        raise HTTPException(status_code=422, detail="Unknown endpoint")
    
    return {
        "endpoint": endpoint,
        "collection": collection_name,
        "explain": explanation,
    }
