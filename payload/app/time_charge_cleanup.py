from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import StringIO
from pathlib import Path
from typing import Any
import csv
import json
import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import Date, DateTime, Integer, String, Text, delete, desc, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .config import get_settings
from .database import Base, engine, get_db
from .models import QboConnection
from .qbo_client import QboError, qbo_query, qbo_request

settings = get_settings()
router = APIRouter(prefix="/time-charges", tags=["time-charge-cleanup"])
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

SCAN_TTL_MINUTES = 45
MAX_SCAN_RECORDS = 25_000
PAGE_SIZE = 1_000
DELETE_CHUNK_LIMIT = 40
BATCH_SIZE = 10
PROTECTED_BILLABLE_STATUS = "HasBeenBilled"


class TimeChargeScan(Base):
    __tablename__ = "time_charge_cleanup_scans"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    filters_json: Mapped[str] = mapped_column(Text, nullable=False)
    records_json: Mapped[str] = mapped_column(Text, nullable=False)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    backup_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    backup_downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TimeChargeDeletionLog(Base):
    __tablename__ = "time_charge_cleanup_deletion_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scan_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    batch_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    qbo_time_activity_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    sync_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    txn_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    customer_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    worker_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    item_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    billable_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hours_decimal: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# This module is intentionally self-contained. Creating only its two tables here
# lets the feature be added without modifying the existing models.py or migration
# workflow. checkfirst=True makes repeated deploys safe.
TimeChargeScan.__table__.create(bind=engine, checkfirst=True)
TimeChargeDeletionLog.__table__.create(bind=engine, checkfirst=True)


class ScanRequest(BaseModel):
    start_date: date | None = None
    end_date: date | None = None
    customer_text: str = Field(default="", max_length=250)
    worker_text: str = Field(default="", max_length=250)
    item_text: str = Field(default="", max_length=250)
    description_text: str = Field(default="", max_length=500)
    billable_status: str = Field(default="all", max_length=64)
    customer_required: bool = True


class ScanSelectionRequest(BaseModel):
    scan_id: str = Field(min_length=1, max_length=64)
    ids: list[str] = Field(min_length=1, max_length=MAX_SCAN_RECORDS)
    csrf_token: str = Field(min_length=1, max_length=256)


class DeleteChunkRequest(ScanSelectionRequest):
    expected_total: int = Field(gt=0, le=MAX_SCAN_RECORDS)
    confirmation: str = Field(min_length=1, max_length=128)



def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response



def _utcnow() -> datetime:
    return datetime.now(timezone.utc)



def _qbo_status(db: Session) -> dict[str, str | bool | None]:
    connection = db.get(QboConnection, 1)
    return {
        "connected": connection is not None,
        "realm_id": connection.realm_id if connection else None,
        "company_name": connection.company_name if connection else None,
        "read_only": settings.qbo_read_only,
        "env": settings.normalized_qbo_env,
    }



def _csrf_for_request(request: Request) -> str:
    token = request.session.get("time_charge_cleanup_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["time_charge_cleanup_csrf"] = token
    return token



def _validate_csrf(request: Request, submitted: str) -> None:
    expected = request.session.get("time_charge_cleanup_csrf")
    if not expected or not secrets.compare_digest(str(expected), str(submitted)):
        raise HTTPException(status_code=403, detail="The page security token expired. Reload the page and try again.")



def _cleanup_expired_scans(db: Session) -> None:
    db.execute(delete(TimeChargeScan).where(TimeChargeScan.expires_at < _utcnow()))
    db.commit()



def _safe_text(value: Any) -> str:
    return str(value or "").strip()



def _ref_name(payload: dict[str, Any], key: str) -> str:
    ref = payload.get(key) or {}
    if not isinstance(ref, dict):
        return ""
    return _safe_text(ref.get("name") or ref.get("value"))



def _worker_name(activity: dict[str, Any]) -> str:
    name_of = _safe_text(activity.get("NameOf"))
    if name_of.casefold() == "vendor":
        return _ref_name(activity, "VendorRef")
    if name_of.casefold() == "employee":
        return _ref_name(activity, "EmployeeRef")
    return _ref_name(activity, "EmployeeRef") or _ref_name(activity, "VendorRef") or name_of



def _safe_int(value: Any) -> int:
    try:
        return int(Decimal(str(value or 0)))
    except (InvalidOperation, ValueError, TypeError):
        return 0



def _decimal_hours(activity: dict[str, Any]) -> str:
    try:
        hours = Decimal(str(activity.get("Hours") or 0))
        minutes = Decimal(str(activity.get("Minutes") or 0))
        total = hours + (minutes / Decimal("60"))
        return f"{total.quantize(Decimal('0.01'))}"
    except (InvalidOperation, ValueError, TypeError):
        return "0.00"



def _normalize_activity(activity: dict[str, Any]) -> dict[str, Any]:
    meta = activity.get("MetaData") or {}
    txn_date = _safe_text(activity.get("TxnDate"))[:10]
    normalized = {
        "id": _safe_text(activity.get("Id")),
        "sync_token": _safe_text(activity.get("SyncToken")),
        "txn_date": txn_date,
        "customer_name": _ref_name(activity, "CustomerRef"),
        "customer_id": _safe_text((activity.get("CustomerRef") or {}).get("value")) if isinstance(activity.get("CustomerRef"), dict) else "",
        "worker_name": _worker_name(activity),
        "worker_type": _safe_text(activity.get("NameOf")),
        "item_name": _ref_name(activity, "ItemRef"),
        "item_id": _safe_text((activity.get("ItemRef") or {}).get("value")) if isinstance(activity.get("ItemRef"), dict) else "",
        "hours": _safe_int(activity.get("Hours")),
        "minutes": _safe_int(activity.get("Minutes")),
        "hours_decimal": _decimal_hours(activity),
        "billable_status": _safe_text(activity.get("BillableStatus")) or "NotBillable",
        "description": _safe_text(activity.get("Description")),
        "hourly_rate": _safe_text(activity.get("HourlyRate")),
        "cost_rate": _safe_text(activity.get("CostRate")),
        "start_time": _safe_text(activity.get("StartTime")),
        "end_time": _safe_text(activity.get("EndTime")),
        "created_time": _safe_text(meta.get("CreateTime")),
        "last_updated_time": _safe_text(meta.get("LastUpdatedTime")),
        "protected": _safe_text(activity.get("BillableStatus")) == PROTECTED_BILLABLE_STATUS,
        "raw": activity,
    }
    return normalized



def _matches_filter(record: dict[str, Any], request: ScanRequest) -> bool:
    if request.customer_required and not record["customer_name"]:
        return False

    status = request.billable_status.strip()
    if status and status.casefold() != "all" and record["billable_status"].casefold() != status.casefold():
        return False

    fields = (
        (request.customer_text, record["customer_name"]),
        (request.worker_text, record["worker_name"]),
        (request.item_text, record["item_name"]),
        (request.description_text, record["description"]),
    )
    for needle, haystack in fields:
        normalized_needle = needle.strip().casefold()
        if normalized_needle and normalized_needle not in str(haystack or "").casefold():
            return False
    return True



def _public_record(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key != "raw"}



def _build_time_activity_query(scan: ScanRequest, start_position: int) -> str:
    predicates: list[str] = []
    if scan.start_date:
        predicates.append(f"TxnDate >= '{scan.start_date.isoformat()}'")
    if scan.end_date:
        predicates.append(f"TxnDate <= '{scan.end_date.isoformat()}'")
    where = f" where {' and '.join(predicates)}" if predicates else ""
    return f"select * from TimeActivity{where} startposition {start_position} maxresults {PAGE_SIZE}"


async def _fetch_time_activities(db: Session, scan: ScanRequest) -> tuple[list[dict[str, Any]], bool]:
    records: list[dict[str, Any]] = []
    start_position = 1
    truncated = False

    while True:
        payload = await qbo_query(db, _build_time_activity_query(scan, start_position))
        batch = payload.get("QueryResponse", {}).get("TimeActivity", [])
        if not batch:
            break

        for activity in batch:
            normalized = _normalize_activity(activity)
            if normalized["id"] and _matches_filter(normalized, scan):
                records.append(normalized)
                if len(records) >= MAX_SCAN_RECORDS:
                    truncated = True
                    break
        if truncated or len(batch) < PAGE_SIZE:
            break
        start_position += PAGE_SIZE

    records.sort(key=lambda row: (row["txn_date"], row["id"]), reverse=True)
    return records, truncated



def _get_scan(db: Session, scan_id: str) -> TimeChargeScan:
    scan = db.get(TimeChargeScan, scan_id)
    if scan is None:
        raise HTTPException(status_code=404, detail="That scan is no longer available. Run a new scan.")
    expires_at = scan.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < _utcnow():
        db.delete(scan)
        db.commit()
        raise HTTPException(status_code=410, detail="That scan expired. Run a new scan before deleting anything.")
    return scan



def _scan_records(scan: TimeChargeScan) -> list[dict[str, Any]]:
    try:
        value = json.loads(scan.records_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="The saved scan could not be read. Run a new scan.") from exc
    return value if isinstance(value, list) else []



def _records_by_id(scan: TimeChargeScan) -> dict[str, dict[str, Any]]:
    return {str(record.get("id")): record for record in _scan_records(scan) if record.get("id")}



def _dedupe_ids(ids: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in ids:
        cleaned = str(value or "").strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result



def _backup_ids(scan: TimeChargeScan) -> set[str]:
    try:
        values = json.loads(scan.backup_ids_json or "[]")
    except json.JSONDecodeError:
        values = []
    return {str(value) for value in values if str(value).strip()}



def _parse_fault(response_item: dict[str, Any]) -> tuple[str, str]:
    fault = response_item.get("Fault") or {}
    errors = fault.get("Error") or []
    if not isinstance(errors, list):
        errors = [errors]
    messages: list[str] = []
    code = ""
    for error in errors:
        if not isinstance(error, dict):
            continue
        code = code or _safe_text(error.get("code"))
        detail = _safe_text(error.get("Detail"))
        message = _safe_text(error.get("Message"))
        combined = ": ".join(part for part in (message, detail) if part)
        if combined:
            messages.append(combined)
    return code, " | ".join(messages) or "QuickBooks returned an unspecified error."



def _deterministic_request_id(scan_id: str, ids: list[str]) -> str:
    seed = f"{scan_id}:{','.join(ids)}".encode("utf-8")
    return sha256(seed).hexdigest()[:32]



def _date_or_none(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None



def _add_log(
    db: Session,
    *,
    scan_id: str,
    batch_request_id: str | None,
    record: dict[str, Any],
    status: str,
    error_message: str | None = None,
) -> None:
    db.add(
        TimeChargeDeletionLog(
            scan_id=scan_id,
            batch_request_id=batch_request_id,
            qbo_time_activity_id=str(record.get("id") or ""),
            sync_token=str(record.get("sync_token") or "") or None,
            txn_date=_date_or_none(str(record.get("txn_date") or "")),
            customer_name=str(record.get("customer_name") or "") or None,
            worker_name=str(record.get("worker_name") or "") or None,
            item_name=str(record.get("item_name") or "") or None,
            billable_status=str(record.get("billable_status") or "") or None,
            hours_decimal=str(record.get("hours_decimal") or "") or None,
            description=str(record.get("description") or "") or None,
            status=status,
            error_message=error_message,
            raw_payload_json=json.dumps(record.get("raw") or {}, separators=(",", ":")),
            created_at=_utcnow(),
        )
    )


@router.get("", response_class=HTMLResponse)
def time_charge_cleanup_page(request: Request, db: Session = Depends(get_db)):
    _cleanup_expired_scans(db)
    csrf_token = _csrf_for_request(request)
    history = db.scalars(select(TimeChargeDeletionLog).order_by(desc(TimeChargeDeletionLog.created_at)).limit(50)).all()
    response = templates.TemplateResponse(
        request=request,
        name="time_charge_cleanup.html",
        context={
            "qbo": _qbo_status(db),
            "csrf_token": csrf_token,
            "history": history,
            "scan_ttl_minutes": SCAN_TTL_MINUTES,
            "delete_chunk_limit": DELETE_CHUNK_LIMIT,
        },
    )
    return _no_store(response)


@router.post("/api/scan")
async def scan_time_charges(payload: ScanRequest, request: Request, db: Session = Depends(get_db)):
    _csrf_for_request(request)
    _cleanup_expired_scans(db)
    if db.get(QboConnection, 1) is None:
        raise HTTPException(status_code=409, detail="Connect QuickBooks before scanning time charges.")
    if payload.start_date and payload.end_date and payload.start_date > payload.end_date:
        raise HTTPException(status_code=400, detail="The start date cannot be after the end date.")

    try:
        records, truncated = await _fetch_time_activities(db, payload)
    except QboError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    now = _utcnow()
    scan_id = uuid.uuid4().hex
    scan = TimeChargeScan(
        id=scan_id,
        created_at=now,
        expires_at=now + timedelta(minutes=SCAN_TTL_MINUTES),
        filters_json=payload.model_dump_json(),
        records_json=json.dumps(records, separators=(",", ":"), default=str),
        record_count=len(records),
        backup_ids_json="[]",
        backup_downloaded_at=None,
    )
    db.add(scan)
    db.commit()

    counts = {
        "total": len(records),
        "safe": sum(1 for row in records if not row["protected"]),
        "billable": sum(1 for row in records if row["billable_status"] == "Billable"),
        "not_billable": sum(1 for row in records if row["billable_status"] == "NotBillable"),
        "protected": sum(1 for row in records if row["protected"]),
    }
    response = JSONResponse(
        {
            "scan_id": scan_id,
            "expires_at": scan.expires_at.isoformat(),
            "records": [_public_record(record) for record in records],
            "counts": counts,
            "truncated": truncated,
            "max_scan_records": MAX_SCAN_RECORDS,
        }
    )
    return _no_store(response)


@router.post("/api/export")
def export_time_charge_backup(payload: ScanSelectionRequest, request: Request, db: Session = Depends(get_db)):
    _validate_csrf(request, payload.csrf_token)
    scan = _get_scan(db, payload.scan_id)
    by_id = _records_by_id(scan)
    ids = _dedupe_ids(payload.ids)
    missing = [record_id for record_id in ids if record_id not in by_id]
    if missing:
        raise HTTPException(status_code=400, detail="One or more selected records are not part of this scan. Run the scan again.")

    output = StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "QuickBooks TimeActivity ID",
            "SyncToken",
            "Transaction Date",
            "Customer or Job",
            "Customer ID",
            "Employee or Vendor",
            "Worker Type",
            "Service Item",
            "Item ID",
            "Hours",
            "Minutes",
            "Decimal Hours",
            "Billable Status",
            "Hourly Rate",
            "Cost Rate",
            "Description",
            "Start Time",
            "End Time",
            "Created Time",
            "Last Updated Time",
            "Raw QuickBooks JSON",
        ],
    )
    writer.writeheader()
    for record_id in ids:
        row = by_id[record_id]
        writer.writerow(
            {
                "QuickBooks TimeActivity ID": row["id"],
                "SyncToken": row["sync_token"],
                "Transaction Date": row["txn_date"],
                "Customer or Job": row["customer_name"],
                "Customer ID": row["customer_id"],
                "Employee or Vendor": row["worker_name"],
                "Worker Type": row["worker_type"],
                "Service Item": row["item_name"],
                "Item ID": row["item_id"],
                "Hours": row["hours"],
                "Minutes": row["minutes"],
                "Decimal Hours": row["hours_decimal"],
                "Billable Status": row["billable_status"],
                "Hourly Rate": row["hourly_rate"],
                "Cost Rate": row["cost_rate"],
                "Description": row["description"],
                "Start Time": row["start_time"],
                "End Time": row["end_time"],
                "Created Time": row["created_time"],
                "Last Updated Time": row["last_updated_time"],
                "Raw QuickBooks JSON": json.dumps(row.get("raw") or {}, separators=(",", ":")),
            }
        )

    backed_up = _backup_ids(scan)
    backed_up.update(ids)
    scan.backup_ids_json = json.dumps(sorted(backed_up), separators=(",", ":"))
    scan.backup_downloaded_at = _utcnow()
    db.commit()

    filename = f"qbo-time-activity-backup-{_utcnow().strftime('%Y%m%d-%H%M%S')}.csv"
    response = Response(
        content=output.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
    return _no_store(response)


@router.post("/api/delete-chunk")
async def delete_time_charge_chunk(payload: DeleteChunkRequest, request: Request, db: Session = Depends(get_db)):
    _validate_csrf(request, payload.csrf_token)
    if settings.qbo_read_only:
        raise HTTPException(status_code=403, detail="QuickBooks write operations are disabled. Set QBO_READ_ONLY=false before deleting.")
    if db.get(QboConnection, 1) is None:
        raise HTTPException(status_code=409, detail="QuickBooks is not connected.")

    expected_phrase = f"DELETE {payload.expected_total} TIME CHARGES"
    if payload.confirmation.strip() != expected_phrase:
        raise HTTPException(status_code=400, detail=f"Type the exact confirmation phrase: {expected_phrase}")

    scan = _get_scan(db, payload.scan_id)
    age = _utcnow() - (scan.created_at if scan.created_at.tzinfo else scan.created_at.replace(tzinfo=timezone.utc))
    if age > timedelta(minutes=SCAN_TTL_MINUTES):
        raise HTTPException(status_code=410, detail="The scan is too old. Run a new scan before deleting anything.")

    ids = _dedupe_ids(payload.ids)
    if len(ids) > DELETE_CHUNK_LIMIT:
        raise HTTPException(status_code=400, detail=f"A delete chunk can contain at most {DELETE_CHUNK_LIMIT} records.")

    by_id = _records_by_id(scan)
    unknown = [record_id for record_id in ids if record_id not in by_id]
    if unknown:
        raise HTTPException(status_code=400, detail="One or more selected records are not part of this scan.")

    backed_up = _backup_ids(scan)
    not_backed_up = [record_id for record_id in ids if record_id not in backed_up]
    if not_backed_up:
        raise HTTPException(status_code=409, detail="Download a new backup CSV after changing the selection.")

    results: list[dict[str, Any]] = []
    safe_records: list[dict[str, Any]] = []
    for record_id in ids:
        record = by_id[record_id]
        if record.get("billable_status") == PROTECTED_BILLABLE_STATUS:
            message = "Protected because QuickBooks reports this time activity as already billed."
            _add_log(
                db,
                scan_id=scan.id,
                batch_request_id=None,
                record=record,
                status="protected",
                error_message=message,
            )
            results.append({"id": record_id, "status": "protected", "message": message})
        else:
            safe_records.append(record)
    db.commit()

    for offset in range(0, len(safe_records), BATCH_SIZE):
        batch_records = safe_records[offset : offset + BATCH_SIZE]
        batch_ids = [str(record["id"]) for record in batch_records]
        request_id = _deterministic_request_id(scan.id, batch_ids)
        request_items = [
            {
                "bId": f"d{index}",
                "operation": "delete",
                "TimeActivity": {
                    "Id": str(record["id"]),
                    "SyncToken": str(record.get("sync_token") or "0"),
                },
            }
            for index, record in enumerate(batch_records)
        ]

        try:
            response_payload = await qbo_request(
                db,
                "POST",
                "/batch",
                params={"requestid": request_id},
                json_body={"BatchItemRequest": request_items},
            )
        except QboError as exc:
            message = str(exc)
            for record in batch_records:
                _add_log(
                    db,
                    scan_id=scan.id,
                    batch_request_id=request_id,
                    record=record,
                    status="request_failed",
                    error_message=message,
                )
                results.append({"id": record["id"], "status": "failed", "message": message})
            db.commit()
            continue

        response_items = response_payload.get("BatchItemResponse") or []
        response_by_bid = {str(item.get("bId")): item for item in response_items if isinstance(item, dict)}

        for index, record in enumerate(batch_records):
            response_item = response_by_bid.get(f"d{index}", {})
            if response_item.get("Fault"):
                error_code, message = _parse_fault(response_item)
                if error_code == "610":
                    status = "already_missing"
                    user_status = "already_missing"
                else:
                    status = "failed"
                    user_status = "failed"
                _add_log(
                    db,
                    scan_id=scan.id,
                    batch_request_id=request_id,
                    record=record,
                    status=status,
                    error_message=message,
                )
                results.append({"id": record["id"], "status": user_status, "message": message, "error_code": error_code})
            elif response_item:
                _add_log(
                    db,
                    scan_id=scan.id,
                    batch_request_id=request_id,
                    record=record,
                    status="deleted",
                )
                results.append({"id": record["id"], "status": "deleted", "message": "Deleted from QuickBooks."})
            else:
                message = "QuickBooks did not return a result for this batch item."
                _add_log(
                    db,
                    scan_id=scan.id,
                    batch_request_id=request_id,
                    record=record,
                    status="unknown",
                    error_message=message,
                )
                results.append({"id": record["id"], "status": "unknown", "message": message})
        db.commit()

    counts = {
        "deleted": sum(1 for result in results if result["status"] == "deleted"),
        "already_missing": sum(1 for result in results if result["status"] == "already_missing"),
        "protected": sum(1 for result in results if result["status"] == "protected"),
        "failed": sum(1 for result in results if result["status"] in {"failed", "unknown"}),
    }
    response = JSONResponse({"results": results, "counts": counts, "recommended_delay_ms": 8_000})
    return _no_store(response)
