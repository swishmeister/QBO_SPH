from __future__ import annotations

from copy import deepcopy
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
from sqlalchemy import Date, DateTime, Integer, Numeric, String, Text, delete, desc, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .config import get_settings
from .database import Base, engine, get_db
from .models import QboConnection
from .qbo_client import QboError, qbo_query, qbo_request

settings = get_settings()
router = APIRouter(prefix="/billable-expenses", tags=["billable-expense-cleanup"])
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

SCAN_TTL_MINUTES = 45
PAGE_SIZE = 1_000
MAX_MATCHED_LINES = 25_000
MAX_SOURCE_TRANSACTIONS = 50_000
UPDATE_TRANSACTION_LIMIT = 10
SUPPORTED_ENTITY_TYPES = ("Purchase", "Bill", "VendorCredit")
DETAIL_TYPES = ("AccountBasedExpenseLineDetail", "ItemBasedExpenseLineDetail")
PROTECTED_BILLABLE_STATUS = "HasBeenBilled"
TARGET_BILLABLE_STATUS = "Billable"
NEW_BILLABLE_STATUS = "NotBillable"


class BillableExpenseScan(Base):
    __tablename__ = "billable_expense_cleanup_scans"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    filters_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    transaction_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    backup_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    backup_downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BillableExpenseUpdateLog(Base):
    __tablename__ = "billable_expense_cleanup_update_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scan_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    batch_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    qbo_transaction_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    qbo_line_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sync_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    txn_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    doc_number: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vendor_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    customer_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    account_item_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    old_billable_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    new_billable_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_transaction_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


BillableExpenseScan.__table__.create(bind=engine, checkfirst=True)
BillableExpenseUpdateLog.__table__.create(bind=engine, checkfirst=True)


class ScanRequest(BaseModel):
    start_date: date | None = None
    end_date: date | None = None
    transaction_types: list[str] = Field(default_factory=lambda: ["Purchase", "Bill"], min_length=1, max_length=3)
    billable_status: str = Field(default=TARGET_BILLABLE_STATUS, max_length=64)
    customer_required: bool = True
    customer_text: str = Field(default="", max_length=250)
    vendor_text: str = Field(default="", max_length=250)
    account_item_text: str = Field(default="", max_length=250)
    description_text: str = Field(default="", max_length=500)
    min_amount: Decimal | None = None
    max_amount: Decimal | None = None


class ScanSelectionRequest(BaseModel):
    scan_id: str = Field(min_length=1, max_length=64)
    ids: list[str] = Field(min_length=1, max_length=MAX_MATCHED_LINES)
    csrf_token: str = Field(min_length=1, max_length=256)


class UpdateChunkRequest(ScanSelectionRequest):
    expected_total: int = Field(gt=0, le=MAX_MATCHED_LINES)
    confirmation: str = Field(min_length=1, max_length=160)


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
    token = request.session.get("billable_expense_cleanup_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["billable_expense_cleanup_csrf"] = token
    return token


def _validate_csrf(request: Request, submitted: str) -> None:
    expected = request.session.get("billable_expense_cleanup_csrf")
    if not expected or not secrets.compare_digest(str(expected), str(submitted)):
        raise HTTPException(status_code=403, detail="The page security token expired. Reload the page and try again.")


def _cleanup_expired_scans(db: Session) -> None:
    db.execute(delete(BillableExpenseScan).where(BillableExpenseScan.expires_at < _utcnow()))
    db.commit()


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _safe_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _ref_name(ref: Any) -> str:
    if not isinstance(ref, dict):
        return ""
    return _safe_text(ref.get("name") or ref.get("value"))


def _ref_id(ref: Any) -> str:
    if not isinstance(ref, dict):
        return ""
    return _safe_text(ref.get("value"))


def _transaction_key(entity_type: str, transaction_id: str) -> str:
    return f"{entity_type}:{transaction_id}"


def _row_id(entity_type: str, transaction_id: str, line_id: str, line_index: int) -> str:
    locator = line_id or f"index-{line_index}"
    return f"{entity_type}:{transaction_id}:{locator}"


def _entity_path(entity_type: str) -> str:
    return {
        "Purchase": "purchase",
        "Bill": "bill",
        "VendorCredit": "vendorcredit",
    }[entity_type]


def _add_required_update_fields(
    entity_type: str,
    source_transaction: dict[str, Any],
    update_transaction: dict[str, Any],
    fallback_transaction: dict[str, Any] | None = None,
) -> None:
    """Preserve QBO header fields required when expense lines are updated.

    QuickBooks validates parent transaction fields whenever the Line collection
    is supplied. The direct read-by-ID response is preferred, while the scan
    snapshot is retained as a fallback because QBO query responses can omit
    fields that are still required on an update.
    """

    required_fields_by_entity = {
        "Bill": ("VendorRef", "APAccountRef"),
        "VendorCredit": ("VendorRef", "APAccountRef"),
        "Purchase": ("PaymentType", "AccountRef", "EntityRef"),
    }

    fallback = fallback_transaction or {}
    for field_name in required_fields_by_entity.get(entity_type, ()):
        field_value = source_transaction.get(field_name)
        if field_value is None or field_value == "":
            field_value = fallback.get(field_name)
        if field_value is not None and field_value != "":
            update_transaction[field_name] = deepcopy(field_value)


def _build_update_transaction(
    entity_type: str,
    latest: dict[str, Any],
    scanned: dict[str, Any],
) -> dict[str, Any]:
    """Build a transaction update without dropping required QBO headers.

    Purchases are updated from the complete read-by-ID object. This is more
    reliable than reconstructing a sparse Purchase because PaymentType is
    mandatory and may be omitted from a query response. Bills and vendor
    credits retain the smaller sparse update shape.
    """

    if entity_type == "Purchase":
        update_transaction = deepcopy(latest)
        # These response-only fields are not needed in an update request. QBO
        # ignores most read-only properties, but removing them keeps the payload
        # focused while preserving every writable Purchase header and line.
        for field_name in ("MetaData", "domain", "status"):
            update_transaction.pop(field_name, None)
        update_transaction.pop("sparse", None)
        update_transaction["Id"] = _safe_text(latest.get("Id"))
        update_transaction["SyncToken"] = _safe_text(latest.get("SyncToken"))
        update_transaction["Line"] = deepcopy(latest.get("Line") or [])
    else:
        update_transaction = {
            "Id": _safe_text(latest.get("Id")),
            "SyncToken": _safe_text(latest.get("SyncToken")),
            "sparse": True,
            # QuickBooks treats Line as a replace-style collection during many
            # transaction updates. Re-send every current line and modify only
            # the selected BillableStatus fields.
            "Line": deepcopy(latest.get("Line") or []),
        }

    _add_required_update_fields(
        entity_type,
        latest,
        update_transaction,
        fallback_transaction=scanned,
    )
    return update_transaction


def _transaction_type_label(entity_type: str, transaction: dict[str, Any]) -> str:
    if entity_type == "Bill":
        return "Bill"
    if entity_type == "VendorCredit":
        return "Vendor credit"
    payment_type = _safe_text(transaction.get("PaymentType")).casefold()
    if payment_type == "check":
        return "Check"
    if payment_type == "creditcard":
        return "Credit card expense"
    if payment_type == "cash":
        return "Expense"
    return "Purchase / expense"


def _vendor_name(entity_type: str, transaction: dict[str, Any]) -> str:
    if entity_type in {"Bill", "VendorCredit"}:
        return _ref_name(transaction.get("VendorRef"))
    return _ref_name(transaction.get("EntityRef")) or _ref_name(transaction.get("VendorRef"))


def _line_detail(line: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    detail_type = _safe_text(line.get("DetailType"))
    if detail_type not in DETAIL_TYPES:
        return "", {}
    detail = line.get(detail_type)
    return detail_type, detail if isinstance(detail, dict) else {}


def _customer_ref(line: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    nested = detail.get("CustomerRef")
    if isinstance(nested, dict) and _ref_id(nested):
        return nested
    project = line.get("ProjectRef")
    if isinstance(project, dict) and _ref_id(project):
        return project
    customer = line.get("CustomerRef")
    if isinstance(customer, dict) and _ref_id(customer):
        return customer
    return {}


def _account_or_item_ref(detail_type: str, detail: dict[str, Any]) -> dict[str, Any]:
    if detail_type == "AccountBasedExpenseLineDetail":
        value = detail.get("AccountRef")
    else:
        value = detail.get("ItemRef")
    return value if isinstance(value, dict) else {}


def _normalize_line(
    entity_type: str,
    transaction: dict[str, Any],
    line: dict[str, Any],
    line_index: int,
) -> dict[str, Any] | None:
    transaction_id = _safe_text(transaction.get("Id"))
    if not transaction_id:
        return None
    detail_type, detail = _line_detail(line)
    if not detail_type:
        return None

    billable_status = _safe_text(detail.get("BillableStatus")) or NEW_BILLABLE_STATUS
    customer_ref = _customer_ref(line, detail)
    account_item_ref = _account_or_item_ref(detail_type, detail)
    line_id = _safe_text(line.get("Id"))
    meta = transaction.get("MetaData") or {}
    if not isinstance(meta, dict):
        meta = {}

    amount = _safe_decimal(line.get("Amount"))
    return {
        "id": _row_id(entity_type, transaction_id, line_id, line_index),
        "transaction_key": _transaction_key(entity_type, transaction_id),
        "entity_type": entity_type,
        "transaction_type": _transaction_type_label(entity_type, transaction),
        "transaction_id": transaction_id,
        "line_id": line_id,
        "line_index": line_index,
        "sync_token": _safe_text(transaction.get("SyncToken")),
        "txn_date": _safe_text(transaction.get("TxnDate"))[:10],
        "doc_number": _safe_text(transaction.get("DocNumber")),
        "vendor_name": _vendor_name(entity_type, transaction),
        "customer_name": _ref_name(customer_ref),
        "customer_id": _ref_id(customer_ref),
        "account_item_name": _ref_name(account_item_ref),
        "account_item_id": _ref_id(account_item_ref),
        "detail_type": detail_type,
        "amount": f"{amount.quantize(Decimal('0.01'))}",
        "billable_status": billable_status,
        "description": _safe_text(line.get("Description") or transaction.get("PrivateNote")),
        "memo": _safe_text(transaction.get("PrivateNote")),
        "created_time": _safe_text(meta.get("CreateTime")),
        "last_updated_time": _safe_text(meta.get("LastUpdatedTime")),
        "protected": billable_status == PROTECTED_BILLABLE_STATUS,
        "selectable": billable_status == TARGET_BILLABLE_STATUS,
    }


def _valid_transaction_types(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        cleaned = _safe_text(value)
        if cleaned in SUPPORTED_ENTITY_TYPES and cleaned not in result:
            result.append(cleaned)
    if not result:
        raise HTTPException(status_code=400, detail="Select at least one supported transaction type.")
    return result


def _matches_filter(record: dict[str, Any], scan: ScanRequest) -> bool:
    if scan.customer_required and not record["customer_name"]:
        return False

    status = scan.billable_status.strip()
    if status and status.casefold() != "all" and record["billable_status"].casefold() != status.casefold():
        return False

    for needle, haystack in (
        (scan.customer_text, record["customer_name"]),
        (scan.vendor_text, record["vendor_name"]),
        (scan.account_item_text, record["account_item_name"]),
        (scan.description_text, record["description"]),
    ):
        normalized = needle.strip().casefold()
        if normalized and normalized not in _safe_text(haystack).casefold():
            return False

    amount = _safe_decimal(record["amount"])
    if scan.min_amount is not None and amount < scan.min_amount:
        return False
    if scan.max_amount is not None and amount > scan.max_amount:
        return False
    return True


def _build_query(entity_type: str, scan: ScanRequest, start_position: int) -> str:
    predicates: list[str] = []
    if scan.start_date:
        predicates.append(f"TxnDate >= '{scan.start_date.isoformat()}'")
    if scan.end_date:
        predicates.append(f"TxnDate <= '{scan.end_date.isoformat()}'")
    where = f" where {' and '.join(predicates)}" if predicates else ""
    return f"select * from {entity_type}{where} startposition {start_position} maxresults {PAGE_SIZE}"


async def _fetch_billable_expense_lines(
    db: Session,
    scan: ScanRequest,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], bool, int]:
    records: list[dict[str, Any]] = []
    transactions: dict[str, dict[str, Any]] = {}
    source_count = 0
    truncated = False

    for entity_type in _valid_transaction_types(scan.transaction_types):
        start_position = 1
        while True:
            payload = await qbo_query(db, _build_query(entity_type, scan, start_position))
            batch = payload.get("QueryResponse", {}).get(entity_type, [])
            if not isinstance(batch, list) or not batch:
                break

            for transaction in batch:
                if not isinstance(transaction, dict):
                    continue
                source_count += 1
                transaction_id = _safe_text(transaction.get("Id"))
                if not transaction_id:
                    continue

                matched_for_transaction = False
                lines = transaction.get("Line") or []
                if not isinstance(lines, list):
                    lines = []
                for line_index, line in enumerate(lines):
                    if not isinstance(line, dict):
                        continue
                    normalized = _normalize_line(entity_type, transaction, line, line_index)
                    if normalized and _matches_filter(normalized, scan):
                        records.append(normalized)
                        matched_for_transaction = True
                        if len(records) >= MAX_MATCHED_LINES:
                            truncated = True
                            break
                if matched_for_transaction:
                    transactions[_transaction_key(entity_type, transaction_id)] = transaction
                if truncated or source_count >= MAX_SOURCE_TRANSACTIONS:
                    truncated = True
                    break

            if truncated:
                break
            start_position += len(batch)
        if truncated:
            break

    records.sort(
        key=lambda row: (row["txn_date"], row["transaction_type"], row["transaction_id"], row["line_index"]),
        reverse=True,
    )
    return records, transactions, truncated, source_count


def _get_scan(db: Session, scan_id: str) -> BillableExpenseScan:
    scan = db.get(BillableExpenseScan, scan_id)
    if scan is None:
        raise HTTPException(status_code=404, detail="That scan is no longer available. Run a new scan.")
    expires_at = scan.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < _utcnow():
        db.delete(scan)
        db.commit()
        raise HTTPException(status_code=410, detail="That scan expired. Run a new scan before updating anything.")
    return scan


def _scan_payload(scan: BillableExpenseScan) -> dict[str, Any]:
    try:
        value = json.loads(scan.payload_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="The saved scan could not be read. Run a new scan.") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=500, detail="The saved scan is invalid. Run a new scan.")
    return value


def _records_by_id(scan: BillableExpenseScan) -> dict[str, dict[str, Any]]:
    payload = _scan_payload(scan)
    records = payload.get("records") or []
    return {str(row.get("id")): row for row in records if isinstance(row, dict) and row.get("id")}


def _transactions_by_key(scan: BillableExpenseScan) -> dict[str, dict[str, Any]]:
    payload = _scan_payload(scan)
    transactions = payload.get("transactions") or {}
    return transactions if isinstance(transactions, dict) else {}


def _dedupe_ids(ids: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in ids:
        cleaned = _safe_text(value)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


def _backup_ids(scan: BillableExpenseScan) -> set[str]:
    try:
        values = json.loads(scan.backup_ids_json or "[]")
    except json.JSONDecodeError:
        values = []
    return {_safe_text(value) for value in values if _safe_text(value)}


def _escape_query_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


async def _fetch_latest_transactions(
    db: Session,
    transaction_keys: list[str],
) -> dict[str, dict[str, Any]]:
    """Read each selected transaction directly by ID before updating it.

    A normal QBO query is sufficient for scanning, but direct entity reads are
    used for writes because they return the most complete transaction shape,
    including mandatory Purchase fields such as PaymentType. Update chunks are
    already limited to ten source transactions, so the additional reads remain
    bounded.
    """

    latest: dict[str, dict[str, Any]] = {}
    for key in _dedupe_ids(transaction_keys):
        entity_type, separator, transaction_id = key.partition(":")
        if not separator or entity_type not in SUPPORTED_ENTITY_TYPES or not transaction_id:
            continue

        payload = await qbo_request(
            db,
            "GET",
            f"/{_entity_path(entity_type)}/{transaction_id}",
        )
        transaction = payload.get(entity_type)
        if not isinstance(transaction, dict):
            continue
        returned_id = _safe_text(transaction.get("Id"))
        if returned_id:
            latest[_transaction_key(entity_type, returned_id)] = transaction
    return latest


def _locate_line(transaction: dict[str, Any], record: dict[str, Any]) -> tuple[int, dict[str, Any]] | None:
    lines = transaction.get("Line") or []
    if not isinstance(lines, list):
        return None

    target_line_id = _safe_text(record.get("line_id"))
    if target_line_id:
        for index, line in enumerate(lines):
            if isinstance(line, dict) and _safe_text(line.get("Id")) == target_line_id:
                return index, line

    try:
        line_index = int(record.get("line_index"))
    except (TypeError, ValueError):
        return None
    if 0 <= line_index < len(lines) and isinstance(lines[line_index], dict):
        return line_index, lines[line_index]
    return None


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


def _deterministic_request_id(scan_id: str, row_ids: list[str]) -> str:
    seed = f"{scan_id}:{','.join(sorted(row_ids))}".encode("utf-8")
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
    transaction: dict[str, Any] | None,
    status: str,
    new_status: str | None = None,
    error_message: str | None = None,
) -> None:
    db.add(
        BillableExpenseUpdateLog(
            scan_id=scan_id,
            batch_request_id=batch_request_id,
            entity_type=_safe_text(record.get("entity_type")),
            qbo_transaction_id=_safe_text(record.get("transaction_id")),
            qbo_line_id=_safe_text(record.get("line_id")) or None,
            sync_token=_safe_text(record.get("sync_token")) or None,
            txn_date=_date_or_none(_safe_text(record.get("txn_date"))),
            doc_number=_safe_text(record.get("doc_number")) or None,
            vendor_name=_safe_text(record.get("vendor_name")) or None,
            customer_name=_safe_text(record.get("customer_name")) or None,
            account_item_name=_safe_text(record.get("account_item_name")) or None,
            amount=_safe_decimal(record.get("amount")),
            old_billable_status=_safe_text(record.get("billable_status")) or None,
            new_billable_status=new_status,
            status=status,
            error_message=error_message,
            raw_transaction_json=json.dumps(transaction or {}, separators=(",", ":")),
            created_at=_utcnow(),
        )
    )


def _updated_response_line_status(
    transaction: dict[str, Any],
    record: dict[str, Any],
) -> str:
    located = _locate_line(transaction, record)
    if not located:
        return ""
    _, line = located
    _, detail = _line_detail(line)
    return _safe_text(detail.get("BillableStatus"))


@router.get("", response_class=HTMLResponse)
def billable_expense_cleanup_page(request: Request, db: Session = Depends(get_db)):
    _cleanup_expired_scans(db)
    csrf_token = _csrf_for_request(request)
    history = db.scalars(
        select(BillableExpenseUpdateLog).order_by(desc(BillableExpenseUpdateLog.created_at)).limit(50)
    ).all()
    response = templates.TemplateResponse(
        request=request,
        name="billable_expense_cleanup.html",
        context={
            "qbo": _qbo_status(db),
            "csrf_token": csrf_token,
            "history": history,
            "scan_ttl_minutes": SCAN_TTL_MINUTES,
            "update_transaction_limit": UPDATE_TRANSACTION_LIMIT,
        },
    )
    return _no_store(response)


@router.post("/api/scan")
async def scan_billable_expenses(payload: ScanRequest, request: Request, db: Session = Depends(get_db)):
    _csrf_for_request(request)
    _cleanup_expired_scans(db)
    if db.get(QboConnection, 1) is None:
        raise HTTPException(status_code=409, detail="Connect QuickBooks before scanning billable expenses.")
    if payload.start_date and payload.end_date and payload.start_date > payload.end_date:
        raise HTTPException(status_code=400, detail="The start date cannot be after the end date.")
    if payload.min_amount is not None and payload.max_amount is not None and payload.min_amount > payload.max_amount:
        raise HTTPException(status_code=400, detail="The minimum amount cannot exceed the maximum amount.")
    payload.transaction_types = _valid_transaction_types(payload.transaction_types)

    try:
        records, transactions, truncated, source_count = await _fetch_billable_expense_lines(db, payload)
    except QboError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    now = _utcnow()
    scan_id = uuid.uuid4().hex
    scan_payload = {"records": records, "transactions": transactions}
    scan = BillableExpenseScan(
        id=scan_id,
        created_at=now,
        expires_at=now + timedelta(minutes=SCAN_TTL_MINUTES),
        filters_json=payload.model_dump_json(),
        payload_json=json.dumps(scan_payload, separators=(",", ":"), default=str),
        record_count=len(records),
        transaction_count=len(transactions),
        backup_ids_json="[]",
        backup_downloaded_at=None,
    )
    db.add(scan)
    db.commit()

    total_amount = sum((_safe_decimal(row["amount"]) for row in records), Decimal("0"))
    safe_amount = sum((_safe_decimal(row["amount"]) for row in records if row["selectable"]), Decimal("0"))
    counts = {
        "total": len(records),
        "transactions": len(transactions),
        "safe": sum(1 for row in records if row["selectable"]),
        "protected": sum(1 for row in records if row["protected"]),
        "billable": sum(1 for row in records if row["billable_status"] == TARGET_BILLABLE_STATUS),
        "total_amount": f"{total_amount.quantize(Decimal('0.01'))}",
        "safe_amount": f"{safe_amount.quantize(Decimal('0.01'))}",
    }
    response = JSONResponse(
        {
            "scan_id": scan_id,
            "expires_at": scan.expires_at.isoformat(),
            "records": records,
            "counts": counts,
            "truncated": truncated,
            "source_transactions_scanned": source_count,
            "max_matched_lines": MAX_MATCHED_LINES,
            "max_source_transactions": MAX_SOURCE_TRANSACTIONS,
        }
    )
    return _no_store(response)


@router.post("/api/export")
def export_billable_expense_backup(
    payload: ScanSelectionRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    _validate_csrf(request, payload.csrf_token)
    scan = _get_scan(db, payload.scan_id)
    records_by_id = _records_by_id(scan)
    transactions_by_key = _transactions_by_key(scan)
    ids = _dedupe_ids(payload.ids)
    missing = [row_id for row_id in ids if row_id not in records_by_id]
    if missing:
        raise HTTPException(status_code=400, detail="One or more selected lines are not part of this scan. Run the scan again.")

    output = StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "Selection ID",
            "Transaction Type",
            "QBO Entity",
            "QBO Transaction ID",
            "QBO Line ID",
            "Line Index",
            "SyncToken",
            "Transaction Date",
            "Document Number",
            "Vendor or Payee",
            "Customer or Job",
            "Customer ID",
            "Account or Item",
            "Account or Item ID",
            "Line Detail Type",
            "Amount",
            "Billable Status",
            "Description",
            "Memo",
            "Created Time",
            "Last Updated Time",
            "Raw QuickBooks Transaction JSON",
        ],
    )
    writer.writeheader()
    for row_id in ids:
        row = records_by_id[row_id]
        transaction = transactions_by_key.get(row["transaction_key"], {})
        writer.writerow(
            {
                "Selection ID": row["id"],
                "Transaction Type": row["transaction_type"],
                "QBO Entity": row["entity_type"],
                "QBO Transaction ID": row["transaction_id"],
                "QBO Line ID": row["line_id"],
                "Line Index": row["line_index"],
                "SyncToken": row["sync_token"],
                "Transaction Date": row["txn_date"],
                "Document Number": row["doc_number"],
                "Vendor or Payee": row["vendor_name"],
                "Customer or Job": row["customer_name"],
                "Customer ID": row["customer_id"],
                "Account or Item": row["account_item_name"],
                "Account or Item ID": row["account_item_id"],
                "Line Detail Type": row["detail_type"],
                "Amount": row["amount"],
                "Billable Status": row["billable_status"],
                "Description": row["description"],
                "Memo": row["memo"],
                "Created Time": row["created_time"],
                "Last Updated Time": row["last_updated_time"],
                "Raw QuickBooks Transaction JSON": json.dumps(transaction, separators=(",", ":")),
            }
        )

    backed_up = _backup_ids(scan)
    backed_up.update(ids)
    scan.backup_ids_json = json.dumps(sorted(backed_up), separators=(",", ":"))
    scan.backup_downloaded_at = _utcnow()
    db.commit()

    filename = f"qbo-billable-expense-backup-{_utcnow().strftime('%Y%m%d-%H%M%S')}.csv"
    response = Response(
        content=output.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
    return _no_store(response)


@router.post("/api/update-chunk")
async def update_billable_expense_chunk(
    payload: UpdateChunkRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    _validate_csrf(request, payload.csrf_token)
    if settings.qbo_read_only:
        raise HTTPException(
            status_code=403,
            detail="QuickBooks write operations are disabled. Set QBO_READ_ONLY=false before updating expenses.",
        )
    if db.get(QboConnection, 1) is None:
        raise HTTPException(status_code=409, detail="QuickBooks is not connected.")

    expected_phrase = f"MARK {payload.expected_total} EXPENSE CHARGES NOT BILLABLE"
    if payload.confirmation.strip() != expected_phrase:
        raise HTTPException(status_code=400, detail=f"Type the exact confirmation phrase: {expected_phrase}")

    scan = _get_scan(db, payload.scan_id)
    ids = _dedupe_ids(payload.ids)
    records_by_id = _records_by_id(scan)
    scanned_transactions = _transactions_by_key(scan)

    unknown = [row_id for row_id in ids if row_id not in records_by_id]
    if unknown:
        raise HTTPException(status_code=400, detail="One or more selected lines are not part of this scan.")

    transaction_keys = _dedupe_ids([records_by_id[row_id]["transaction_key"] for row_id in ids])
    if len(transaction_keys) > UPDATE_TRANSACTION_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"An update chunk can contain at most {UPDATE_TRANSACTION_LIMIT} transactions.",
        )

    backed_up = _backup_ids(scan)
    not_backed_up = [row_id for row_id in ids if row_id not in backed_up]
    if not_backed_up:
        raise HTTPException(status_code=409, detail="Download a new backup CSV after changing the selection.")

    selected_by_transaction: dict[str, list[dict[str, Any]]] = {}
    results: list[dict[str, Any]] = []
    for row_id in ids:
        record = records_by_id[row_id]
        if record.get("billable_status") == PROTECTED_BILLABLE_STATUS:
            message = "Protected because QuickBooks reports this expense line as already billed."
            _add_log(
                db,
                scan_id=scan.id,
                batch_request_id=None,
                record=record,
                transaction=scanned_transactions.get(record["transaction_key"]),
                status="protected",
                error_message=message,
            )
            results.append({"id": row_id, "status": "protected", "message": message})
            continue
        if record.get("billable_status") != TARGET_BILLABLE_STATUS:
            message = "This line was not Billable when scanned."
            _add_log(
                db,
                scan_id=scan.id,
                batch_request_id=None,
                record=record,
                transaction=scanned_transactions.get(record["transaction_key"]),
                status="not_billable",
                new_status=record.get("billable_status"),
                error_message=message,
            )
            results.append({"id": row_id, "status": "already_not_billable", "message": message})
            continue
        selected_by_transaction.setdefault(record["transaction_key"], []).append(record)
    db.commit()

    if not selected_by_transaction:
        response = JSONResponse({"results": results, "updated": 0, "failed": 0})
        return _no_store(response)

    try:
        latest_transactions = await _fetch_latest_transactions(db, list(selected_by_transaction))
    except QboError as exc:
        message = f"Could not refresh the latest QuickBooks transactions before updating: {exc}"
        for records in selected_by_transaction.values():
            for record in records:
                _add_log(
                    db,
                    scan_id=scan.id,
                    batch_request_id=None,
                    record=record,
                    transaction=scanned_transactions.get(record["transaction_key"]),
                    status="refresh_failed",
                    error_message=message,
                )
                results.append({"id": record["id"], "status": "failed", "message": message})
        db.commit()
        response = JSONResponse({"results": results, "updated": 0, "failed": len(selected_by_transaction)})
        return _no_store(response)

    update_requests: list[dict[str, Any]] = []
    request_context: dict[str, dict[str, Any]] = {}

    for request_index, (transaction_key, selected_records) in enumerate(selected_by_transaction.items()):
        latest = latest_transactions.get(transaction_key)
        scanned = scanned_transactions.get(transaction_key) or {}
        if latest is None:
            message = "The source transaction no longer exists in QuickBooks."
            for record in selected_records:
                _add_log(
                    db,
                    scan_id=scan.id,
                    batch_request_id=None,
                    record=record,
                    transaction=scanned,
                    status="already_missing",
                    error_message=message,
                )
                results.append({"id": record["id"], "status": "already_missing", "message": message})
            continue

        if _safe_text(latest.get("SyncToken")) != _safe_text(scanned.get("SyncToken")):
            message = "This transaction changed after the scan. Run a new scan and download a new backup before updating it."
            for record in selected_records:
                _add_log(
                    db,
                    scan_id=scan.id,
                    batch_request_id=None,
                    record=record,
                    transaction=latest,
                    status="changed_since_scan",
                    error_message=message,
                )
                results.append({"id": record["id"], "status": "changed_since_scan", "message": message})
            continue

        entity_type = _safe_text(selected_records[0].get("entity_type"))
        update_transaction = _build_update_transaction(entity_type, latest, scanned)

        if entity_type == "Purchase" and not _safe_text(update_transaction.get("PaymentType")):
            message = (
                "QuickBooks did not return the Purchase PaymentType, so the transaction was not changed. "
                "Run a new scan; if this repeats, inspect the raw backup JSON for this Purchase."
            )
            for record in selected_records:
                _add_log(
                    db,
                    scan_id=scan.id,
                    batch_request_id=None,
                    record=record,
                    transaction=latest,
                    status="missing_required_field",
                    error_message=message,
                )
                results.append({"id": record["id"], "status": "failed", "message": message})
            continue

        changed_records: list[dict[str, Any]] = []

        for record in selected_records:
            located = _locate_line(update_transaction, record)
            if not located:
                message = "The expense line could not be found in the current transaction."
                _add_log(
                    db,
                    scan_id=scan.id,
                    batch_request_id=None,
                    record=record,
                    transaction=latest,
                    status="line_missing",
                    error_message=message,
                )
                results.append({"id": record["id"], "status": "line_missing", "message": message})
                continue

            _, line = located
            detail_type, detail = _line_detail(line)
            current_status = _safe_text(detail.get("BillableStatus")) or NEW_BILLABLE_STATUS
            if current_status == PROTECTED_BILLABLE_STATUS:
                message = "Protected because this expense line is now marked HasBeenBilled in QuickBooks."
                _add_log(
                    db,
                    scan_id=scan.id,
                    batch_request_id=None,
                    record=record,
                    transaction=latest,
                    status="protected",
                    error_message=message,
                )
                results.append({"id": record["id"], "status": "protected", "message": message})
                continue
            if current_status == NEW_BILLABLE_STATUS:
                message = "This expense line is already NotBillable in QuickBooks."
                _add_log(
                    db,
                    scan_id=scan.id,
                    batch_request_id=None,
                    record=record,
                    transaction=latest,
                    status="already_not_billable",
                    new_status=NEW_BILLABLE_STATUS,
                    error_message=message,
                )
                results.append({"id": record["id"], "status": "already_not_billable", "message": message})
                continue
            if current_status != TARGET_BILLABLE_STATUS:
                message = f"This line now has unsupported billable status '{current_status}'."
                _add_log(
                    db,
                    scan_id=scan.id,
                    batch_request_id=None,
                    record=record,
                    transaction=latest,
                    status="status_changed",
                    error_message=message,
                )
                results.append({"id": record["id"], "status": "status_changed", "message": message})
                continue

            detail["BillableStatus"] = NEW_BILLABLE_STATUS
            line[detail_type] = detail
            changed_records.append(record)

        if not changed_records:
            continue

        bid = f"u{request_index}"
        update_requests.append(
            {
                "bId": bid,
                "operation": "update",
                entity_type: update_transaction,
            }
        )
        request_context[bid] = {
            "entity_type": entity_type,
            "transaction_key": transaction_key,
            "latest": latest,
            "records": changed_records,
        }

    db.commit()

    if not update_requests:
        response = JSONResponse(
            {
                "results": results,
                "updated": sum(1 for item in results if item["status"] == "updated"),
                "failed": sum(1 for item in results if item["status"] == "failed"),
            }
        )
        return _no_store(response)

    request_row_ids = [record["id"] for context in request_context.values() for record in context["records"]]
    request_id = _deterministic_request_id(scan.id, request_row_ids)
    try:
        response_payload = await qbo_request(
            db,
            "POST",
            "/batch",
            params={"requestid": request_id},
            json_body={"BatchItemRequest": update_requests},
        )
    except QboError as exc:
        message = str(exc)
        for context in request_context.values():
            for record in context["records"]:
                _add_log(
                    db,
                    scan_id=scan.id,
                    batch_request_id=request_id,
                    record=record,
                    transaction=context["latest"],
                    status="request_failed",
                    error_message=message,
                )
                results.append({"id": record["id"], "status": "failed", "message": message})
        db.commit()
        response = JSONResponse({"results": results, "updated": 0, "failed": len(request_row_ids)})
        return _no_store(response)

    response_items = response_payload.get("BatchItemResponse") or []
    response_by_bid = {str(item.get("bId")): item for item in response_items if isinstance(item, dict)}

    for bid, context in request_context.items():
        response_item = response_by_bid.get(bid, {})
        if response_item.get("Fault"):
            _, message = _parse_fault(response_item)
            for record in context["records"]:
                _add_log(
                    db,
                    scan_id=scan.id,
                    batch_request_id=request_id,
                    record=record,
                    transaction=context["latest"],
                    status="failed",
                    error_message=message,
                )
                results.append({"id": record["id"], "status": "failed", "message": message})
            continue

        updated_transaction = response_item.get(context["entity_type"])
        if not isinstance(updated_transaction, dict):
            message = "QuickBooks returned no updated transaction payload. Run a new scan to verify the result."
            for record in context["records"]:
                _add_log(
                    db,
                    scan_id=scan.id,
                    batch_request_id=request_id,
                    record=record,
                    transaction=context["latest"],
                    status="updated_unverified",
                    new_status=NEW_BILLABLE_STATUS,
                    error_message=message,
                )
                results.append({"id": record["id"], "status": "updated_unverified", "message": message})
            continue

        for record in context["records"]:
            response_status = _updated_response_line_status(updated_transaction, record)
            if response_status == NEW_BILLABLE_STATUS:
                message = "Marked NotBillable in QuickBooks."
                log_status = "updated"
                user_status = "updated"
            else:
                message = "QuickBooks accepted the transaction update, but the returned line did not confirm NotBillable. Run a new scan."
                log_status = "updated_unverified"
                user_status = "updated_unverified"
            _add_log(
                db,
                scan_id=scan.id,
                batch_request_id=request_id,
                record=record,
                transaction=updated_transaction,
                status=log_status,
                new_status=NEW_BILLABLE_STATUS,
                error_message=None if log_status == "updated" else message,
            )
            results.append({"id": record["id"], "status": user_status, "message": message})

    db.commit()
    response = JSONResponse(
        {
            "results": results,
            "updated": sum(1 for item in results if item["status"] == "updated"),
            "failed": sum(1 for item in results if item["status"] == "failed"),
        }
    )
    return _no_store(response)
