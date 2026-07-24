from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Annotated
import base64
import hmac

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .calculations import quote_totals
from .config import get_settings
from .database import Base, engine, get_db
from .models import QboConnection, QboCustomer, QboItem, Quote, QuoteLine
from .qbo_client import (
    QboError,
    build_authorization_url,
    create_qbo_estimate,
    exchange_code_for_tokens,
    fetch_all_items,
    fetch_company_info,
    fetch_estimate_by_doc_number_or_id,
    fetch_estimate_by_id,
    fetch_estimates_from_date,
    qbo_query,
    revoke_qbo_tokens,
    update_qbo_estimate_sph,
    update_qbo_item_prices,
)

settings = get_settings()

Base.metadata.create_all(bind=engine)


def _run_lightweight_migrations() -> None:
    """Add columns introduced after the first MVP. This keeps Render Postgres upgrades simple."""
    inspector = inspect(engine)
    type_by_table = {
        "qbo_connections": {
            "company_name": "VARCHAR(255)",
        },
        "qbo_items": {
            "sync_token": "VARCHAR(64)",
            "sku": "VARCHAR(255)",
            "original_unit_price": "NUMERIC(12, 2) DEFAULT 0",
            "original_purchase_cost": "NUMERIC(12, 2) DEFAULT 0",
            "qty_on_hand": "NUMERIC(12, 2)",
            "variable_cost": "BOOLEAN DEFAULT FALSE",
            "last_synced_at": "TIMESTAMP WITH TIME ZONE",
        },
        "quotes": {
            "quoted_labor_hours": "NUMERIC(12, 2) DEFAULT 0",
            "hourly_labor_rate": "NUMERIC(12, 2) DEFAULT 0",
            "qbo_sync_token": "VARCHAR(64)",
            "qbo_txn_date": "DATE",
            "qbo_total_amount": "NUMERIC(12, 2) DEFAULT 0",
            "qbo_last_updated_time": "TIMESTAMP WITH TIME ZONE",
            "last_synced_at": "TIMESTAMP WITH TIME ZONE",
            "sph_uploaded_at": "TIMESTAMP WITH TIME ZONE",
        },
        "quote_lines": {
            "qbo_line_id": "VARCHAR(64)",
            "product_service_name": "VARCHAR(500)",
            "is_section_header": "BOOLEAN DEFAULT FALSE",
            "is_variable_cost": "BOOLEAN DEFAULT FALSE",
        },
    }
    with engine.begin() as conn:
        for table_name, columns in type_by_table.items():
            if table_name not in inspector.get_table_names():
                continue
            existing = {col["name"] for col in inspector.get_columns(table_name)}
            for column_name, ddl_type in columns.items():
                if column_name not in existing:
                    conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl_type}"))


_run_lightweight_migrations()

app = FastAPI(title="QBO SPH Calculator and File Editor")
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.app_secret_key,
    https_only=settings.secure_cookies,
    same_site="lax",
)
BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["quote_totals"] = quote_totals


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    if settings.enable_hsts and request.url.scheme == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


def _unauthorized_response() -> Response:
    return Response(
        content="Authentication required",
        status_code=401,
        headers={"WWW-Authenticate": "Basic realm=QBO SPH Calculator"},
    )


@app.middleware("http")
async def basic_auth_middleware(request: Request, call_next):
    if not settings.require_basic_auth:
        return await call_next(request)
    if request.url.path.startswith(("/static", "/health")):
        return await call_next(request)
    if not settings.app_username or not settings.app_password:
        return Response(content="Basic auth is enabled but APP_USERNAME or APP_PASSWORD is missing.", status_code=500)
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Basic "):
        return _unauthorized_response()
    try:
        decoded = base64.b64decode(authorization.split(" ", 1)[1]).decode("utf-8")
        username, password = decoded.split(":", 1)
    except Exception:
        return _unauthorized_response()
    if not (hmac.compare_digest(username, settings.app_username) and hmac.compare_digest(password, settings.app_password)):
        return _unauthorized_response()
    return await call_next(request)


@app.get("/health")
def health_check():
    return {"status": "ok"}


def parse_decimal(value: str | None, default: str = "0.00") -> Decimal:
    """Parse form/API numeric values safely.

    Browsers sometimes submit blank strings, and users may paste values like
    "$1,250.00" or "35%". Keeping this tolerant prevents local saves from
    turning into 500 errors.
    """
    try:
        if value in (None, ""):
            cleaned = default
        else:
            cleaned = str(value).strip().replace("$", "").replace(",", "").replace("%", "")
            if cleaned == "":
                cleaned = default
        return Decimal(cleaned).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid numeric value: {value}") from exc


def decimal_from_qbo(value: object, default: str = "0.00") -> Decimal:
    return parse_decimal(str(value if value not in (None, "") else default), default)


def parse_qbo_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def parse_qbo_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    cleaned = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def get_qbo_status(db: Session) -> dict[str, str | bool | None]:
    connection = db.get(QboConnection, 1)
    return {
        "connected": connection is not None,
        "realm_id": connection.realm_id if connection else None,
        "company_name": connection.company_name if connection else None,
        "read_only": settings.qbo_read_only,
        "env": settings.normalized_qbo_env,
    }


def is_variable_cost_item_name(name: str | None) -> bool:
    if not name:
        return False
    final_segment = name.split(":")[-1].strip().upper()
    first_token = final_segment.split()[0].strip().upper() if final_segment.split() else final_segment
    return final_segment in settings.variable_cost_item_codes or first_token in settings.variable_cost_item_codes


def item_is_variable(item: QboItem | None, fallback_name: str | None = None) -> bool:
    if item and item.variable_cost:
        return True
    return is_variable_cost_item_name(fallback_name or (item.fully_qualified_name if item else None) or (item.name if item else None))


def _name_segments(name: str | None) -> list[str]:
    if not name:
        return []
    cleaned = str(name).strip()
    parts = [part.strip().upper() for part in cleaned.split(":") if part.strip()]
    segments = [cleaned.upper()]
    segments.extend(parts)
    # Rebuild adjacent pairs so names like "LC:MA Labor maintenance" are recognized as one code segment.
    for idx in range(len(parts) - 1):
        segments.append(f"{parts[idx]}:{parts[idx + 1]}")
    return segments


def is_labor_item_name(name: str | None) -> bool:
    """Return True for QBO labor service codes such as LC:MA, LC:PL, etc."""
    for segment in _name_segments(name):
        for prefix in settings.labor_item_prefixes:
            normalized_prefix = prefix.strip().upper()
            if normalized_prefix and segment.startswith(normalized_prefix):
                return True
    return False


def item_is_labor(item: QboItem | None, fallback_name: str | None = None) -> bool:
    return is_labor_item_name(fallback_name or (item.fully_qualified_name if item else None) or (item.name if item else None))


def find_cached_item(db: Session, qbo_item_id: str | None) -> QboItem | None:
    if not qbo_item_id:
        return None
    return db.scalar(select(QboItem).where(QboItem.qbo_id == str(qbo_item_id)))


def find_item_by_name(db: Session, name: str | None) -> QboItem | None:
    cleaned = (name or "").strip()
    if not cleaned:
        return None
    return db.scalar(
        select(QboItem).where((QboItem.name == cleaned) | (QboItem.fully_qualified_name == cleaned)).limit(1)
    )


def upsert_qbo_item(db: Session, item: dict, *, keep_local_edits: bool = False) -> QboItem:
    qbo_id = str(item["Id"])
    record = db.scalar(select(QboItem).where(QboItem.qbo_id == qbo_id))
    if record is None:
        record = QboItem(qbo_id=qbo_id, name=item.get("Name", "Unnamed"))
        db.add(record)
    unit_price = parse_decimal(str(item.get("UnitPrice", "0")))
    purchase_cost = parse_decimal(str(item.get("PurchaseCost", "0")))
    item_name = item.get("FullyQualifiedName") or item.get("Name")
    record.sync_token = str(item.get("SyncToken", record.sync_token or "0"))
    record.name = item.get("Name", record.name)
    record.fully_qualified_name = item.get("FullyQualifiedName")
    record.sku = item.get("Sku") or item.get("SKU")
    record.item_type = item.get("Type")
    record.active = item.get("Active", True)
    record.qty_on_hand = decimal_from_qbo(item.get("QtyOnHand"), "0.00") if item.get("QtyOnHand") is not None else None
    record.variable_cost = is_variable_cost_item_name(item_name)
    if not keep_local_edits or not record.is_changed:
        record.unit_price = unit_price
        record.purchase_cost = purchase_cost
    record.original_unit_price = unit_price
    record.original_purchase_cost = purchase_cost
    record.last_synced_at = datetime.now(timezone.utc)
    return record


def qbo_estimate_line_to_quote_line(db: Session, estimate_line: dict, quote_id: int, sort_order: int, existing: QuoteLine | None = None) -> QuoteLine | None:
    detail_type = estimate_line.get("DetailType")
    if detail_type == "DescriptionOnly":
        description = estimate_line.get("Description") or ""
        line = existing or QuoteLine(quote_id=quote_id)
        line.line_type = "Header"
        line.description = str(description)
        line.qbo_line_id = str(estimate_line.get("Id")) if estimate_line.get("Id") is not None else None
        line.qbo_item_id = None
        line.qbo_item_name = None
        line.product_service_name = ""
        line.quantity = Decimal("0.00")
        line.unit_cost = Decimal("0.00")
        line.unit_price = Decimal("0.00")
        line.labor_hours = Decimal("0.00")
        line.include_on_qbo_estimate = True
        line.is_section_header = True
        line.is_variable_cost = False
        line.sort_order = sort_order
        return line

    if detail_type != "SalesItemLineDetail":
        return None

    detail = estimate_line.get("SalesItemLineDetail") or {}
    item_ref = detail.get("ItemRef") or {}
    qbo_item_id = str(item_ref.get("value")) if item_ref.get("value") is not None else None
    cached_item = find_cached_item(db, qbo_item_id)
    qbo_item_name = item_ref.get("name") or (cached_item.fully_qualified_name if cached_item else None) or (cached_item.name if cached_item else None)
    variable_cost = item_is_variable(cached_item, qbo_item_name)
    labor_item = item_is_labor(cached_item, qbo_item_name)

    qty = decimal_from_qbo(detail.get("Qty"), "1.00")
    unit_price = detail.get("UnitPrice")
    if unit_price is None and qty != 0:
        unit_price = decimal_from_qbo(estimate_line.get("Amount"), "0.00") / qty

    imported_unit_price = decimal_from_qbo(unit_price, "0.00")
    if labor_item:
        # Labor service codes such as LC:MA and LC:PL represent quoted hours.
        # Set cost equal to the labor rate so labor contributes hours/rate but not gross item markup.
        unit_cost = imported_unit_price
    elif existing is None:
        unit_cost = Decimal("0.00") if variable_cost else Decimal(cached_item.purchase_cost or Decimal("0.00")) if cached_item else Decimal("0.00")
    else:
        unit_cost = Decimal(existing.unit_cost or Decimal("0.00"))

    description = estimate_line.get("Description") or qbo_item_name or "Imported QBO estimate line"
    line = existing or QuoteLine(quote_id=quote_id)
    line.line_type = "Labor" if labor_item else ("Variable Cost" if variable_cost else "Imported")
    line.qbo_line_id = str(estimate_line.get("Id")) if estimate_line.get("Id") is not None else line.qbo_line_id
    line.qbo_item_id = qbo_item_id
    line.qbo_item_name = qbo_item_name
    line.product_service_name = qbo_item_name
    line.description = str(description)
    line.quantity = qty
    line.unit_cost = unit_cost
    line.unit_price = imported_unit_price
    line.labor_hours = qty if labor_item else Decimal("0.00")
    line.include_on_qbo_estimate = True
    line.is_section_header = False
    line.is_variable_cost = variable_cost
    line.sort_order = sort_order
    return line


def upsert_quote_from_qbo_estimate(db: Session, estimate: dict) -> Quote:
    customer_ref = estimate.get("CustomerRef") or {}
    qbo_customer_id = str(customer_ref.get("value")) if customer_ref.get("value") is not None else None
    customer_name = customer_ref.get("name") or "Imported QBO Customer"
    doc_number = estimate.get("DocNumber") or str(estimate.get("Id"))
    qbo_id = str(estimate.get("Id"))
    quote = db.scalar(select(Quote).where(Quote.qbo_estimate_id == qbo_id))
    if quote is None:
        quote = Quote(title=f"Estimate {doc_number}", customer_name=customer_name, qbo_estimate_id=qbo_id)
        db.add(quote)
        db.flush()
    quote.title = f"Estimate {doc_number}"
    quote.customer_qbo_id = qbo_customer_id
    quote.customer_name = customer_name
    quote.qbo_estimate_doc_number = estimate.get("DocNumber")
    quote.qbo_sync_token = str(estimate.get("SyncToken", ""))
    quote.qbo_txn_date = parse_qbo_date(estimate.get("TxnDate"))
    quote.qbo_total_amount = decimal_from_qbo(estimate.get("TotalAmt"), "0.00")
    quote.qbo_last_updated_time = parse_qbo_datetime((estimate.get("MetaData") or {}).get("LastUpdatedTime"))
    quote.last_synced_at = datetime.now(timezone.utc)
    quote.status = "Imported from QBO"

    existing_by_line_id = {line.qbo_line_id: line for line in quote.lines if line.qbo_line_id}
    existing_by_sort = {line.sort_order: line for line in quote.lines}
    seen_line_ids: set[int] = set()
    for sort_order, estimate_line in enumerate(estimate.get("Line", []), start=1):
        qbo_line_id = str(estimate_line.get("Id")) if estimate_line.get("Id") is not None else None
        existing = existing_by_line_id.get(qbo_line_id) if qbo_line_id else existing_by_sort.get(sort_order)
        quote_line = qbo_estimate_line_to_quote_line(db, estimate_line, quote.id, sort_order, existing)
        if quote_line is None:
            continue
        db.add(quote_line)
        db.flush()
        seen_line_ids.add(quote_line.id)

    for line in list(quote.lines):
        if line.id and line.id not in seen_line_ids:
            db.delete(line)

    labor_lines = [line for line in quote.lines if line.line_type == "Labor" and not line.is_section_header]
    total_labor_hours = sum((Decimal(line.quantity or Decimal("0.00")) for line in labor_lines), Decimal("0.00"))
    total_labor_revenue = sum((Decimal(line.quantity or Decimal("0.00")) * Decimal(line.unit_price or Decimal("0.00")) for line in labor_lines), Decimal("0.00"))
    if total_labor_hours > 0:
        quote.quoted_labor_hours = total_labor_hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        quote.hourly_labor_rate = (total_labor_revenue / total_labor_hours).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return quote


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Annotated[Session, Depends(get_db)]):
    quote_count = db.scalar(select(func.count(Quote.id))) or 0
    item_count = db.scalar(select(func.count(QboItem.id))) or 0
    changed_items = db.scalars(select(QboItem)).all()
    pending_price_changes = sum(1 for item in changed_items if item.is_changed and not item.variable_cost)
    latest_quotes = db.scalars(select(Quote).order_by(Quote.updated_at.desc()).limit(8)).all()
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "quotes": latest_quotes,
            "quote_count": quote_count,
            "item_count": item_count,
            "pending_price_changes": pending_price_changes,
            "qbo": get_qbo_status(db),
        },
    )


@app.get("/qbo/connect")
def qbo_connect(request: Request):
    if not settings.qbo_client_id or not settings.qbo_client_secret:
        raise HTTPException(status_code=400, detail="Set QBO_CLIENT_ID and QBO_CLIENT_SECRET first.")
    auth_url, state = build_authorization_url()
    request.session["qbo_oauth_state"] = state
    return RedirectResponse(auth_url)


@app.get("/qbo/callback")
async def qbo_callback(request: Request, db: Annotated[Session, Depends(get_db)], code: str | None = None, realmId: str | None = None, state: str | None = None, error: str | None = None):
    if error:
        raise HTTPException(status_code=400, detail=f"QBO authorization error: {error}")
    expected_state = request.session.get("qbo_oauth_state")
    if not expected_state or state != expected_state:
        raise HTTPException(status_code=400, detail="Invalid OAuth state. Try connecting to QuickBooks again.")
    if not code or not realmId:
        raise HTTPException(status_code=400, detail="Missing authorization code or realmId.")
    try:
        await exchange_code_for_tokens(code, realmId, db)
        try:
            company = await fetch_company_info(db)
            connection = db.get(QboConnection, 1)
            if connection:
                connection.company_name = company.get("CompanyName") or company.get("LegalName")
                db.commit()
        except QboError:
            pass
    except QboError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    request.session.pop("qbo_oauth_state", None)
    return RedirectResponse("/?connected=1", status_code=303)


@app.post("/qbo/disconnect")
async def qbo_disconnect(db: Annotated[Session, Depends(get_db)]):
    connection = db.get(QboConnection, 1)
    if connection:
        try:
            await revoke_qbo_tokens(db)
        except QboError:
            pass
        connection = db.get(QboConnection, 1)
        if connection:
            db.delete(connection)
            db.commit()
    return RedirectResponse("/qbo/disconnected", status_code=303)


@app.get("/qbo/disconnected", response_class=HTMLResponse)
def qbo_disconnected(request: Request, db: Annotated[Session, Depends(get_db)]):
    return templates.TemplateResponse("disconnected.html", {"request": request, "qbo": get_qbo_status(db)})


@app.post("/qbo/cache-customers")
async def cache_customers(db: Annotated[Session, Depends(get_db)]):
    try:
        payload = await qbo_query(db, "select * from Customer where Active = true maxresults 1000")
    except QboError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    for customer in payload.get("QueryResponse", {}).get("Customer", []):
        qbo_id = str(customer["Id"])
        record = db.scalar(select(QboCustomer).where(QboCustomer.qbo_id == qbo_id))
        if record is None:
            record = QboCustomer(qbo_id=qbo_id, display_name=customer.get("DisplayName", "Unnamed"))
            db.add(record)
        record.display_name = customer.get("DisplayName", record.display_name)
        record.active = customer.get("Active", True)
    db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/qbo/cache-items")
async def cache_items(db: Annotated[Session, Depends(get_db)]):
    try:
        items = await fetch_all_items(db, active_only=True)
    except QboError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    for item in items:
        upsert_qbo_item(db, item, keep_local_edits=True)
    db.commit()
    return RedirectResponse("/items", status_code=303)


@app.get("/estimates", response_class=HTMLResponse)
def estimate_library(request: Request, db: Annotated[Session, Depends(get_db)], search: str = ""):
    stmt = select(Quote).order_by(Quote.qbo_txn_date.desc().nullslast(), Quote.updated_at.desc())
    quotes = db.scalars(stmt).all()
    search_clean = search.strip().lower()
    if search_clean:
        quotes = [q for q in quotes if search_clean in (q.customer_name or "").lower() or search_clean in (q.qbo_estimate_doc_number or "").lower() or search_clean in (q.title or "").lower()]
    return templates.TemplateResponse("estimate_library.html", {"request": request, "quotes": quotes, "search": search, "qbo": get_qbo_status(db)})


@app.post("/estimates/import-year")
async def import_current_year_estimates(db: Annotated[Session, Depends(get_db)]):
    start = date.today().replace(month=1, day=1).isoformat()
    try:
        estimates = await fetch_estimates_from_date(db, start)
    except QboError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    for estimate in estimates:
        upsert_quote_from_qbo_estimate(db, estimate)
    db.commit()
    return RedirectResponse("/estimates?imported_year=1", status_code=303)


@app.post("/estimates/refresh")
async def refresh_estimate_library(db: Annotated[Session, Depends(get_db)]):
    return await import_current_year_estimates(db)


@app.get("/qbo/import-estimate", response_class=HTMLResponse)
def import_estimate_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    return templates.TemplateResponse("import_estimate.html", {"request": request, "qbo": get_qbo_status(db)})


@app.post("/qbo/import-estimate")
async def import_estimate_from_qbo(db: Annotated[Session, Depends(get_db)], estimate_identifier: Annotated[str, Form()]):
    try:
        estimate = await fetch_estimate_by_doc_number_or_id(db, estimate_identifier)
        quote = upsert_quote_from_qbo_estimate(db, estimate)
        db.commit()
        db.refresh(quote)
    except QboError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(f"/quotes/{quote.id}?imported=1", status_code=303)


async def _refresh_quote_from_qbo(quote_id: int, db: Session) -> RedirectResponse:
    quote = db.get(Quote, quote_id)
    if quote is None or not quote.qbo_estimate_id:
        raise HTTPException(status_code=404, detail="Linked QBO Estimate not found for this quote.")
    try:
        # For already-linked estimates, direct GET by Id is safer than a QBO query.
        # This avoids DocNumber/Id query syntax issues and pulls the latest SyncToken.
        estimate = await fetch_estimate_by_id(db, quote.qbo_estimate_id)
        quote = upsert_quote_from_qbo_estimate(db, estimate)
        db.commit()
        db.refresh(quote)
    except QboError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(f"/quotes/{quote.id}?refreshed=1", status_code=303)


@app.post("/quotes/{quote_id}/refresh-from-qbo")
async def refresh_quote_from_qbo_post(quote_id: int, db: Annotated[Session, Depends(get_db)]):
    return await _refresh_quote_from_qbo(quote_id, db)


@app.get("/quotes/{quote_id}/refresh-from-qbo")
async def refresh_quote_from_qbo_get(quote_id: int, db: Annotated[Session, Depends(get_db)]):
    return await _refresh_quote_from_qbo(quote_id, db)


@app.get("/quotes/new", response_class=HTMLResponse)
def new_quote(request: Request, db: Annotated[Session, Depends(get_db)]):
    customers = db.scalars(select(QboCustomer).order_by(QboCustomer.display_name)).all()
    return templates.TemplateResponse("quote_new.html", {"request": request, "customers": customers, "qbo": get_qbo_status(db)})


@app.post("/quotes")
def create_quote(db: Annotated[Session, Depends(get_db)], title: Annotated[str, Form()], customer_qbo_id: Annotated[str, Form()] = "", customer_name_manual: Annotated[str, Form()] = ""):
    customer_name = customer_name_manual.strip()
    if customer_qbo_id:
        customer = db.scalar(select(QboCustomer).where(QboCustomer.qbo_id == customer_qbo_id))
        if customer:
            customer_name = customer.display_name
    if not customer_name:
        raise HTTPException(status_code=400, detail="Choose a cached QBO customer or enter a manual customer name.")
    quote = Quote(title=title.strip(), customer_qbo_id=customer_qbo_id or None, customer_name=customer_name)
    db.add(quote)
    db.commit()
    db.refresh(quote)
    return RedirectResponse(f"/quotes/{quote.id}", status_code=303)


@app.get("/quotes/{quote_id}", response_class=HTMLResponse)
def quote_detail(request: Request, quote_id: int, db: Annotated[Session, Depends(get_db)]):
    quote = db.get(Quote, quote_id)
    if quote is None:
        raise HTTPException(status_code=404, detail="Quote not found")
    items = db.scalars(select(QboItem).where(QboItem.active == True).order_by(QboItem.fully_qualified_name, QboItem.name)).all()  # noqa: E712
    return templates.TemplateResponse("quote_detail.html", {"request": request, "quote": quote, "totals": quote_totals(quote), "items": items, "qbo": get_qbo_status(db)})


@app.post("/quotes/{quote_id}/save")
async def save_quote_sheet(quote_id: int, request: Request, db: Annotated[Session, Depends(get_db)]):
    quote = db.get(Quote, quote_id)
    if quote is None:
        raise HTTPException(status_code=404, detail="Quote not found")

    form = await request.form()

    try:
        quote.quoted_labor_hours = parse_decimal(form.get("quoted_labor_hours"), "0.00")
        quote.hourly_labor_rate = parse_decimal(form.get("hourly_labor_rate"), "0.00")

        for line in sorted(list(quote.lines), key=lambda x: x.sort_order or 0):
            prefix = f"line_{line.id}_"

            # If a line was not present in the submitted form, leave it unchanged.
            # This protects mobile/desktop layout differences and future hidden rows.
            if prefix + "description" not in form and prefix + "product_service" not in form:
                continue

            product_service = str(form.get(prefix + "product_service") or "").strip()
            description = str(form.get(prefix + "description") or "").strip()
            quantity = parse_decimal(form.get(prefix + "quantity"), "0.00")
            unit_cost = parse_decimal(form.get(prefix + "unit_cost"), "0.00")
            submitted_rate = parse_decimal(form.get(prefix + "unit_price"), "0.00")
            submitted_markup_raw = form.get(prefix + "markup_percent")

            line.product_service_name = product_service
            line.description = description
            line.quantity = quantity
            line.unit_cost = unit_cost

            matched = find_item_by_name(db, product_service)
            if matched:
                line.qbo_item_id = matched.qbo_id
                line.qbo_item_name = matched.fully_qualified_name or matched.name
                line.is_variable_cost = matched.variable_cost
            else:
                line.is_variable_cost = is_variable_cost_item_name(product_service or line.qbo_item_name)

            labor_item = is_labor_item_name(product_service or line.qbo_item_name)
            empty_numeric = quantity == 0 and unit_cost == 0 and submitted_rate == 0 and submitted_markup_raw in (None, "")
            line.is_section_header = product_service == "" and empty_numeric

            if line.is_section_header:
                line.line_type = "Header"
                line.qbo_item_id = None
                line.qbo_item_name = None
                line.quantity = Decimal("0.00")
                line.unit_cost = Decimal("0.00")
                line.unit_price = Decimal("0.00")
                line.labor_hours = Decimal("0.00")
                continue

            if labor_item:
                # LC:* lines are quoted labor. They should supply hours/rate for SPH
                # but should not create gross material markup.
                line.unit_price = submitted_rate
                line.unit_cost = submitted_rate
                line.labor_hours = quantity
                line.line_type = "Labor"
            else:
                if submitted_markup_raw not in (None, ""):
                    submitted_markup = parse_decimal(submitted_markup_raw, "0.00")
                    line.unit_price = (unit_cost * (Decimal("1.00") + (submitted_markup / Decimal("100.00")))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                else:
                    line.unit_price = submitted_rate
                line.labor_hours = Decimal("0.00")
                line.line_type = "Variable Cost" if line.is_variable_cost else "Imported"

        labor_lines = [line for line in quote.lines if line.line_type == "Labor" and not line.is_section_header]
        total_labor_hours = sum((Decimal(line.quantity or Decimal("0.00")) for line in labor_lines), Decimal("0.00"))
        total_labor_revenue = sum((Decimal(line.quantity or Decimal("0.00")) * Decimal(line.unit_price or Decimal("0.00")) for line in labor_lines), Decimal("0.00"))
        if total_labor_hours > 0:
            quote.quoted_labor_hours = total_labor_hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            quote.hourly_labor_rate = (total_labor_revenue / total_labor_hours).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Save Locally failed: {type(exc).__name__}: {exc}") from exc

    return RedirectResponse(f"/quotes/{quote.id}?saved=1", status_code=303)


@app.post("/quotes/{quote_id}/lines")
def add_quote_line(quote_id: int, db: Annotated[Session, Depends(get_db)]):
    quote = db.get(Quote, quote_id)
    if quote is None:
        raise HTTPException(status_code=404, detail="Quote not found")
    line = QuoteLine(quote_id=quote.id, line_type="Imported", description="", quantity=Decimal("1.00"), unit_cost=Decimal("0.00"), unit_price=Decimal("0.00"), sort_order=len(quote.lines) + 1)
    db.add(line)
    db.commit()
    return RedirectResponse(f"/quotes/{quote.id}#line-{line.id}", status_code=303)


@app.post("/quotes/{quote_id}/lines/header")
def add_header_line(quote_id: int, db: Annotated[Session, Depends(get_db)]):
    quote = db.get(Quote, quote_id)
    if quote is None:
        raise HTTPException(status_code=404, detail="Quote not found")
    line = QuoteLine(quote_id=quote.id, line_type="Header", description="New section", quantity=Decimal("0.00"), unit_cost=Decimal("0.00"), unit_price=Decimal("0.00"), include_on_qbo_estimate=True, is_section_header=True, sort_order=len(quote.lines) + 1)
    db.add(line)
    db.commit()
    return RedirectResponse(f"/quotes/{quote.id}#line-{line.id}", status_code=303)


@app.post("/quotes/{quote_id}/lines/{line_id}/delete")
def delete_quote_line(quote_id: int, line_id: int, db: Annotated[Session, Depends(get_db)]):
    quote = db.get(Quote, quote_id)
    if quote is None:
        raise HTTPException(status_code=404, detail="Quote not found")
    line = db.get(QuoteLine, line_id)
    if line is None or line.quote_id != quote.id:
        raise HTTPException(status_code=404, detail="Line not found")
    db.delete(line)
    db.commit()
    return RedirectResponse(f"/quotes/{quote.id}", status_code=303)


@app.post("/quotes/{quote_id}/upload-sph")
async def upload_sph_to_qbo(quote_id: int, db: Annotated[Session, Depends(get_db)]):
    if settings.qbo_read_only:
        raise HTTPException(status_code=400, detail="QBO read-only mode is enabled. Disable QBO_READ_ONLY to upload SPH to QuickBooks.")
    quote = db.get(Quote, quote_id)
    if quote is None:
        raise HTTPException(status_code=404, detail="Quote not found")
    try:
        await update_qbo_estimate_sph(db, quote)
    except QboError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    quote.sph_uploaded_at = datetime.now(timezone.utc)
    db.commit()
    return RedirectResponse(f"/quotes/{quote.id}?uploaded_sph=1", status_code=303)


@app.post("/quotes/{quote_id}/sync-estimate")
async def sync_estimate_to_qbo(quote_id: int, db: Annotated[Session, Depends(get_db)]):
    if settings.qbo_read_only:
        raise HTTPException(status_code=400, detail="QBO read-only mode is enabled. Disable QBO_READ_ONLY to create estimates in QuickBooks.")
    quote = db.get(Quote, quote_id)
    if quote is None:
        raise HTTPException(status_code=404, detail="Quote not found")
    if quote.qbo_estimate_id:
        raise HTTPException(status_code=400, detail="This quote is already linked to a QBO Estimate. Use Upload SPH for imported estimates.")
    try:
        response = await create_qbo_estimate(db, quote)
    except QboError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    estimate = response.get("Estimate")
    if not estimate:
        raise HTTPException(status_code=400, detail=f"Unexpected QBO response: {response}")
    quote.qbo_estimate_id = str(estimate.get("Id"))
    quote.qbo_estimate_doc_number = estimate.get("DocNumber")
    quote.status = "Synced to QBO"
    db.commit()
    return RedirectResponse(f"/quotes/{quote.id}?synced=1", status_code=303)


@app.get("/items", response_class=HTMLResponse)
def item_price_manager(request: Request, db: Annotated[Session, Depends(get_db)], search: str = "", changed: str = ""):
    items = db.scalars(select(QboItem).order_by(QboItem.fully_qualified_name, QboItem.name)).all()
    if search.strip():
        needle = search.strip().lower()
        items = [i for i in items if needle in (i.name or "").lower() or needle in (i.fully_qualified_name or "").lower() or needle in (i.sku or "").lower()]
    if changed == "1":
        items = [i for i in items if i.is_changed]
    pending_count = sum(1 for i in db.scalars(select(QboItem)).all() if i.is_changed and not i.variable_cost)
    return templates.TemplateResponse("item_manager.html", {"request": request, "items": items, "search": search, "changed": changed, "pending_count": pending_count, "qbo": get_qbo_status(db)})


@app.post("/items/import")
async def import_items(db: Annotated[Session, Depends(get_db)]):
    return await cache_items(db)


@app.post("/items/save")
async def save_item_prices(request: Request, db: Annotated[Session, Depends(get_db)]):
    form = await request.form()
    for item in db.scalars(select(QboItem)).all():
        prefix = f"item_{item.id}_"
        if prefix + "purchase_cost" not in form:
            continue
        if item.variable_cost:
            continue
        item.purchase_cost = parse_decimal(form.get(prefix + "purchase_cost"), "0.00")
        item.unit_price = parse_decimal(form.get(prefix + "unit_price"), "0.00")
    db.commit()
    return RedirectResponse("/items?changed=1&saved=1", status_code=303)


@app.get("/items/pending", response_class=HTMLResponse)
def pending_item_changes(request: Request, db: Annotated[Session, Depends(get_db)]):
    items = [item for item in db.scalars(select(QboItem).order_by(QboItem.fully_qualified_name, QboItem.name)).all() if item.is_changed and not item.variable_cost]
    return templates.TemplateResponse("item_pending.html", {"request": request, "items": items, "qbo": get_qbo_status(db)})


@app.post("/items/upload-changes")
async def upload_item_price_changes(db: Annotated[Session, Depends(get_db)]):
    if settings.qbo_read_only:
        raise HTTPException(status_code=400, detail="QBO read-only mode is enabled. Disable QBO_READ_ONLY to upload item price changes.")
    changed_items = [item for item in db.scalars(select(QboItem)).all() if item.is_changed and not item.variable_cost]
    for item in changed_items:
        try:
            payload = await update_qbo_item_prices(db, item)
        except QboError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        qbo_item = payload.get("Item")
        if qbo_item:
            upsert_qbo_item(db, qbo_item, keep_local_edits=False)
    db.commit()
    return RedirectResponse("/items?uploaded=1", status_code=303)


@app.post("/items/revert-changes")
def revert_item_changes(db: Annotated[Session, Depends(get_db)]):
    for item in db.scalars(select(QboItem)).all():
        item.purchase_cost = item.original_purchase_cost or Decimal("0.00")
        item.unit_price = item.original_unit_price or Decimal("0.00")
    db.commit()
    return RedirectResponse("/items", status_code=303)
