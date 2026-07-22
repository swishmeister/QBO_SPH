from decimal import Decimal, InvalidOperation
from typing import Annotated
from pathlib import Path
import base64
import hmac

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, select
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
    fetch_estimate_by_doc_number_or_id,
    qbo_query,
    revoke_qbo_tokens,
)

settings = get_settings()

Base.metadata.create_all(bind=engine)

app = FastAPI(title="QBO Quote Margin App")
app.add_middleware(SessionMiddleware, secret_key=settings.app_secret_key)
BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _unauthorized_response() -> Response:
    return Response(
        content="Authentication required",
        status_code=401,
        headers={"WWW-Authenticate": "Basic realm=QBO Quote Margin"},
    )


@app.middleware("http")
async def basic_auth_middleware(request: Request, call_next):
    if not settings.require_basic_auth:
        return await call_next(request)

    public_prefixes = ("/static", "/health")
    if request.url.path.startswith(public_prefixes):
        return await call_next(request)

    if not settings.app_username or not settings.app_password:
        return Response(
            content="Basic auth is enabled but APP_USERNAME or APP_PASSWORD is missing.",
            status_code=500,
        )

    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Basic "):
        return _unauthorized_response()

    try:
        decoded = base64.b64decode(authorization.split(" ", 1)[1]).decode("utf-8")
        username, password = decoded.split(":", 1)
    except Exception:
        return _unauthorized_response()

    valid_username = hmac.compare_digest(username, settings.app_username)
    valid_password = hmac.compare_digest(password, settings.app_password)
    if not (valid_username and valid_password):
        return _unauthorized_response()

    return await call_next(request)


@app.get("/health")
def health_check():
    return {"status": "ok"}


def parse_decimal(value: str | None, default: str = "0.00") -> Decimal:
    try:
        return Decimal(value if value not in (None, "") else default)
    except InvalidOperation as exc:
        raise HTTPException(status_code=400, detail=f"Invalid numeric value: {value}") from exc


def get_qbo_status(db: Session) -> dict[str, str | bool | None]:
    connection = db.get(QboConnection, 1)
    return {
        "connected": connection is not None,
        "realm_id": connection.realm_id if connection else None,
        "read_only": settings.qbo_read_only,
    }


def decimal_from_qbo(value: object, default: str = "0.00") -> Decimal:
    return parse_decimal(str(value if value not in (None, "") else default), default)




def normalize_line_quantity_and_hours(line_type: str, quantity: str | None, labor_hours: str | None) -> tuple[Decimal, Decimal]:
    parsed_quantity = parse_decimal(quantity, "1.00")
    parsed_labor_hours = parse_decimal(labor_hours, "0.00")

    if (line_type or "").strip().lower() == "labor":
        # For labor rows, quantity represents labor hours. This lets labor lines price as
        # hours × hourly rate while also feeding the quote-level SPH calculation.
        if parsed_quantity == 0 and parsed_labor_hours != 0:
            parsed_quantity = parsed_labor_hours
        parsed_labor_hours = parsed_quantity

    return parsed_quantity, parsed_labor_hours


def find_cached_item(db: Session, qbo_item_id: str | None) -> QboItem | None:
    if not qbo_item_id:
        return None
    return db.scalar(select(QboItem).where(QboItem.qbo_id == str(qbo_item_id)))


def qbo_estimate_line_to_quote_line(db: Session, estimate_line: dict, quote_id: int, sort_order: int) -> QuoteLine | None:
    if estimate_line.get("DetailType") != "SalesItemLineDetail":
        return None

    detail = estimate_line.get("SalesItemLineDetail") or {}
    item_ref = detail.get("ItemRef") or {}
    qbo_item_id = str(item_ref.get("value")) if item_ref.get("value") is not None else None
    cached_item = find_cached_item(db, qbo_item_id)

    qty = decimal_from_qbo(detail.get("Qty"), "1.00")
    unit_price = detail.get("UnitPrice")
    if unit_price is None and qty != 0:
        unit_price = decimal_from_qbo(estimate_line.get("Amount"), "0.00") / qty

    unit_cost = Decimal("0.00")
    if cached_item and cached_item.purchase_cost is not None:
        unit_cost = Decimal(cached_item.purchase_cost)

    qbo_item_name = (
        item_ref.get("name")
        or (cached_item.fully_qualified_name if cached_item else None)
        or (cached_item.name if cached_item else None)
    )

    description = estimate_line.get("Description") or qbo_item_name or "Imported QBO estimate line"

    return QuoteLine(
        quote_id=quote_id,
        line_type="Imported",
        qbo_item_id=qbo_item_id,
        qbo_item_name=qbo_item_name,
        description=str(description),
        quantity=qty,
        unit_cost=unit_cost,
        unit_price=decimal_from_qbo(unit_price, "0.00"),
        labor_hours=Decimal("0.00"),
        include_on_qbo_estimate=True,
        sort_order=sort_order,
    )


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Annotated[Session, Depends(get_db)]):
    quotes = db.scalars(select(Quote).order_by(Quote.created_at.desc())).all()
    qbo_customers_count = db.scalar(select(QboCustomer).count()) if False else len(db.scalars(select(QboCustomer)).all())
    qbo_items_count = db.scalar(select(QboItem).count()) if False else len(db.scalars(select(QboItem)).all())
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "quotes": quotes,
            "qbo": get_qbo_status(db),
            "customer_count": qbo_customers_count,
            "item_count": qbo_items_count,
        },
    )


@app.get("/qbo/connect")
def qbo_connect(request: Request):
    if not settings.qbo_client_id or not settings.qbo_client_secret:
        raise HTTPException(status_code=400, detail="Set QBO_CLIENT_ID and QBO_CLIENT_SECRET in .env first.")
    auth_url, state = build_authorization_url()
    request.session["qbo_oauth_state"] = state
    return RedirectResponse(auth_url)


@app.get("/qbo/callback")
async def qbo_callback(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    code: str | None = None,
    realmId: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    if error:
        raise HTTPException(status_code=400, detail=f"QBO authorization error: {error}")
    expected_state = request.session.get("qbo_oauth_state")
    if not expected_state or state != expected_state:
        raise HTTPException(status_code=400, detail="Invalid OAuth state. Try connecting to QuickBooks again.")
    if not code or not realmId:
        raise HTTPException(status_code=400, detail="Missing authorization code or realmId.")

    try:
        await exchange_code_for_tokens(code, realmId, db)
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
            # Clear the local connection even if Intuit token revocation fails, because
            # the user explicitly requested disconnect from this app instance.
            pass
        connection = db.get(QboConnection, 1)
        if connection:
            db.delete(connection)
            db.commit()
    return RedirectResponse("/qbo/disconnected", status_code=303)


@app.get("/qbo/disconnected", response_class=HTMLResponse)
def qbo_disconnected(request: Request, db: Annotated[Session, Depends(get_db)]):
    return templates.TemplateResponse(
        "disconnected.html",
        {"request": request, "qbo": get_qbo_status(db)},
    )


@app.post("/qbo/cache-customers")
async def cache_customers(db: Annotated[Session, Depends(get_db)]):
    try:
        payload = await qbo_query(db, "select * from Customer where Active = true maxresults 1000")
    except QboError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    customers = payload.get("QueryResponse", {}).get("Customer", [])
    for customer in customers:
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
        payload = await qbo_query(db, "select * from Item where Active = true maxresults 1000")
    except QboError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    items = payload.get("QueryResponse", {}).get("Item", [])
    for item in items:
        qbo_id = str(item["Id"])
        record = db.scalar(select(QboItem).where(QboItem.qbo_id == qbo_id))
        if record is None:
            record = QboItem(qbo_id=qbo_id, name=item.get("Name", "Unnamed"))
            db.add(record)
        record.name = item.get("Name", record.name)
        record.fully_qualified_name = item.get("FullyQualifiedName")
        record.item_type = item.get("Type")
        record.unit_price = parse_decimal(str(item.get("UnitPrice", "0")))
        record.purchase_cost = parse_decimal(str(item.get("PurchaseCost", "0")))
        record.active = item.get("Active", True)
    db.commit()
    return RedirectResponse("/", status_code=303)


@app.get("/qbo/import-estimate", response_class=HTMLResponse)
def import_estimate_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    return templates.TemplateResponse(
        "import_estimate.html",
        {"request": request, "qbo": get_qbo_status(db)},
    )


@app.post("/qbo/import-estimate")
async def import_estimate_from_qbo(
    db: Annotated[Session, Depends(get_db)],
    estimate_identifier: Annotated[str, Form()],
    target_margin_percent: Annotated[str, Form()] = "40.00",
):
    try:
        estimate = await fetch_estimate_by_doc_number_or_id(db, estimate_identifier)
    except QboError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    existing = db.scalar(select(Quote).where(Quote.qbo_estimate_id == str(estimate.get("Id"))))
    if existing:
        return RedirectResponse(f"/quotes/{existing.id}", status_code=303)

    customer_ref = estimate.get("CustomerRef") or {}
    qbo_customer_id = str(customer_ref.get("value")) if customer_ref.get("value") is not None else None
    customer_name = customer_ref.get("name") or "Imported QBO Customer"
    doc_number = estimate.get("DocNumber") or str(estimate.get("Id"))

    quote = Quote(
        title=f"Imported QBO Estimate {doc_number}",
        customer_qbo_id=qbo_customer_id,
        customer_name=customer_name,
        target_margin_percent=parse_decimal(target_margin_percent, "40.00"),
        qbo_estimate_id=str(estimate.get("Id")),
        qbo_estimate_doc_number=estimate.get("DocNumber"),
        status="Imported from QBO",
    )
    db.add(quote)
    db.commit()
    db.refresh(quote)

    imported_count = 0
    for sort_order, estimate_line in enumerate(estimate.get("Line", []), start=1):
        quote_line = qbo_estimate_line_to_quote_line(db, estimate_line, quote.id, sort_order)
        if quote_line is None:
            continue
        db.add(quote_line)
        imported_count += 1

    if imported_count == 0:
        db.delete(quote)
        db.commit()
        raise HTTPException(status_code=400, detail="The QBO Estimate did not contain any sales item lines to import.")

    db.commit()
    return RedirectResponse(f"/quotes/{quote.id}?imported=1", status_code=303)


@app.get("/quotes/new", response_class=HTMLResponse)
def new_quote(request: Request, db: Annotated[Session, Depends(get_db)]):
    customers = db.scalars(select(QboCustomer).order_by(QboCustomer.display_name)).all()
    return templates.TemplateResponse(
        "quote_new.html",
        {"request": request, "customers": customers, "qbo": get_qbo_status(db)},
    )


@app.post("/quotes")
def create_quote(
    db: Annotated[Session, Depends(get_db)],
    title: Annotated[str, Form()],
    customer_qbo_id: Annotated[str, Form()],
    customer_name_manual: Annotated[str, Form()] = "",
    target_margin_percent: Annotated[str, Form()] = "40.00",
):
    customer_name = customer_name_manual.strip()
    if customer_qbo_id:
        customer = db.scalar(select(QboCustomer).where(QboCustomer.qbo_id == customer_qbo_id))
        if customer:
            customer_name = customer.display_name

    if not customer_name:
        raise HTTPException(status_code=400, detail="Choose a cached QBO customer or enter a manual customer name.")

    quote = Quote(
        title=title.strip(),
        customer_qbo_id=customer_qbo_id or None,
        customer_name=customer_name,
        target_margin_percent=parse_decimal(target_margin_percent, "40.00"),
    )
    db.add(quote)
    db.commit()
    db.refresh(quote)
    return RedirectResponse(f"/quotes/{quote.id}", status_code=303)


@app.get("/quotes/{quote_id}", response_class=HTMLResponse)
def quote_detail(request: Request, quote_id: int, db: Annotated[Session, Depends(get_db)]):
    quote = db.get(Quote, quote_id)
    if quote is None:
        raise HTTPException(status_code=404, detail="Quote not found")
    items = db.scalars(select(QboItem).order_by(QboItem.fully_qualified_name, QboItem.name)).all()
    return templates.TemplateResponse(
        "quote_detail.html",
        {
            "request": request,
            "quote": quote,
            "totals": quote_totals(quote),
            "items": items,
            "qbo": get_qbo_status(db),
        },
    )


@app.post("/quotes/{quote_id}/lines")
def add_quote_line(
    quote_id: int,
    db: Annotated[Session, Depends(get_db)],
    line_type: Annotated[str, Form()] = "Material",
    qbo_item_id: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    quantity: Annotated[str, Form()] = "1.00",
    unit_cost: Annotated[str, Form()] = "0.00",
    unit_price: Annotated[str, Form()] = "0.00",
    labor_hours: Annotated[str, Form()] = "0.00",
    include_on_qbo_estimate: Annotated[str | None, Form()] = None,
):
    quote = db.get(Quote, quote_id)
    if quote is None:
        raise HTTPException(status_code=404, detail="Quote not found")

    qbo_item_name = None
    if qbo_item_id:
        item = db.scalar(select(QboItem).where(QboItem.qbo_id == qbo_item_id))
        qbo_item_name = item.fully_qualified_name or item.name if item else None

    parsed_quantity, parsed_labor_hours = normalize_line_quantity_and_hours(line_type, quantity, labor_hours)

    line = QuoteLine(
        quote_id=quote.id,
        line_type=line_type,
        qbo_item_id=qbo_item_id or None,
        qbo_item_name=qbo_item_name,
        description=description.strip() or qbo_item_name or line_type,
        quantity=parsed_quantity,
        unit_cost=parse_decimal(unit_cost, "0.00"),
        unit_price=parse_decimal(unit_price, "0.00"),
        labor_hours=parsed_labor_hours,
        include_on_qbo_estimate=include_on_qbo_estimate == "on",
        sort_order=len(quote.lines) + 1,
    )
    db.add(line)
    db.commit()
    return RedirectResponse(f"/quotes/{quote.id}", status_code=303)


@app.post("/quotes/{quote_id}/lines/{line_id}/update")
def update_quote_line(
    quote_id: int,
    line_id: int,
    db: Annotated[Session, Depends(get_db)],
    line_type: Annotated[str, Form()] = "Material",
    qbo_item_id: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    quantity: Annotated[str, Form()] = "1.00",
    unit_cost: Annotated[str, Form()] = "0.00",
    unit_price: Annotated[str, Form()] = "0.00",
    labor_hours: Annotated[str, Form()] = "0.00",
    include_on_qbo_estimate: Annotated[str | None, Form()] = None,
):
    quote = db.get(Quote, quote_id)
    if quote is None:
        raise HTTPException(status_code=404, detail="Quote not found")
    line = db.get(QuoteLine, line_id)
    if line is None or line.quote_id != quote.id:
        raise HTTPException(status_code=404, detail="Line not found")

    qbo_item_name = None
    if qbo_item_id:
        item = db.scalar(select(QboItem).where(QboItem.qbo_id == qbo_item_id))
        qbo_item_name = item.fully_qualified_name or item.name if item else None

    parsed_quantity, parsed_labor_hours = normalize_line_quantity_and_hours(line_type, quantity, labor_hours)

    line.line_type = line_type
    line.qbo_item_id = qbo_item_id or None
    line.qbo_item_name = qbo_item_name
    line.description = description.strip() or qbo_item_name or line_type
    line.quantity = parsed_quantity
    line.unit_cost = parse_decimal(unit_cost, "0.00")
    line.unit_price = parse_decimal(unit_price, "0.00")
    line.labor_hours = parsed_labor_hours
    line.include_on_qbo_estimate = include_on_qbo_estimate == "on"
    db.commit()
    return RedirectResponse(f"/quotes/{quote.id}", status_code=303)


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


@app.post("/quotes/{quote_id}/status")
def update_quote_status(
    quote_id: int,
    db: Annotated[Session, Depends(get_db)],
    status: Annotated[str, Form()] = "Draft",
):
    quote = db.get(Quote, quote_id)
    if quote is None:
        raise HTTPException(status_code=404, detail="Quote not found")
    quote.status = status
    db.commit()
    return RedirectResponse(f"/quotes/{quote.id}", status_code=303)


@app.post("/quotes/{quote_id}/sync-estimate")
async def sync_estimate_to_qbo(quote_id: int, db: Annotated[Session, Depends(get_db)]):
    if settings.qbo_read_only:
        raise HTTPException(status_code=400, detail="QBO read-only mode is enabled. Disable QBO_READ_ONLY to create estimates in QuickBooks.")

    quote = db.get(Quote, quote_id)
    if quote is None:
        raise HTTPException(status_code=404, detail="Quote not found")
    if quote.qbo_estimate_id:
        raise HTTPException(status_code=400, detail="This quote is already linked to a QBO Estimate. Create a revised quote if you need a new estimate.")

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
