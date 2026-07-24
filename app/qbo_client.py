from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode
import base64
import hashlib
import secrets

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from .config import get_settings
from .models import QboConnection, QboItem, Quote
from .calculations import quote_totals

settings = get_settings()

AUTH_BASE_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
REVOCATION_URL = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"
SCOPE = "com.intuit.quickbooks.accounting"


class QboError(RuntimeError):
    pass


def _token_cipher() -> Fernet:
    digest = hashlib.sha256(settings.app_secret_key.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def _encrypt_token(token: str) -> str:
    if not token:
        return ""
    if token.startswith("enc:"):
        return token
    encrypted = _token_cipher().encrypt(token.encode("utf-8")).decode("utf-8")
    return f"enc:{encrypted}"


def _decrypt_token(token: str) -> str:
    if not token:
        return ""
    if not token.startswith("enc:"):
        return token
    try:
        return _token_cipher().decrypt(token[4:].encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise QboError("Stored QuickBooks token could not be decrypted. Reconnect QuickBooks from the app.") from exc


def build_authorization_url(state: str | None = None) -> tuple[str, str]:
    oauth_state = state or secrets.token_urlsafe(32)
    params = {
        "client_id": settings.qbo_client_id,
        "redirect_uri": settings.qbo_redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "state": oauth_state,
    }
    return f"{AUTH_BASE_URL}?{urlencode(params)}", oauth_state


def _expires_at(seconds_from_now: int | None, fallback_seconds: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=int(seconds_from_now or fallback_seconds))


async def exchange_code_for_tokens(code: str, realm_id: str, db: Session) -> QboConnection:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            TOKEN_URL,
            auth=(settings.qbo_client_id, settings.qbo_client_secret),
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": settings.qbo_redirect_uri},
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        )
    if response.status_code >= 400:
        raise QboError(f"Token exchange failed: {response.status_code} {response.text}")

    payload = response.json()
    connection = db.get(QboConnection, 1)
    if connection is None:
        connection = QboConnection(id=1, realm_id=realm_id, access_token="", refresh_token="", access_token_expires_at=datetime.now(timezone.utc))
        db.add(connection)

    connection.realm_id = realm_id
    connection.access_token = _encrypt_token(payload["access_token"])
    connection.refresh_token = _encrypt_token(payload["refresh_token"])
    connection.access_token_expires_at = _expires_at(payload.get("expires_in"), 3600)
    connection.refresh_token_expires_at = _expires_at(payload.get("x_refresh_token_expires_in"), 8726400)
    db.commit()
    db.refresh(connection)
    return connection


async def refresh_tokens_if_needed(db: Session, connection: QboConnection) -> QboConnection:
    now = datetime.now(timezone.utc)
    expires_at = connection.access_token_expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at > now + timedelta(minutes=5):
        return connection

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            TOKEN_URL,
            auth=(settings.qbo_client_id, settings.qbo_client_secret),
            data={"grant_type": "refresh_token", "refresh_token": _decrypt_token(connection.refresh_token)},
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        )
    if response.status_code >= 400:
        raise QboError(f"Token refresh failed: {response.status_code} {response.text}")

    payload = response.json()
    connection.access_token = _encrypt_token(payload["access_token"])
    if payload.get("refresh_token"):
        connection.refresh_token = _encrypt_token(payload["refresh_token"])
    connection.access_token_expires_at = _expires_at(payload.get("expires_in"), 3600)
    if payload.get("x_refresh_token_expires_in"):
        connection.refresh_token_expires_at = _expires_at(payload.get("x_refresh_token_expires_in"), 8726400)
    db.commit()
    db.refresh(connection)
    return connection


async def revoke_qbo_tokens(db: Session) -> None:
    connection = db.get(QboConnection, 1)
    if connection is None:
        return
    token_to_revoke = _decrypt_token(connection.refresh_token or connection.access_token)
    if not token_to_revoke:
        return
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            REVOCATION_URL,
            auth=(settings.qbo_client_id, settings.qbo_client_secret),
            data={"token": token_to_revoke},
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        )
    if response.status_code >= 400:
        raise QboError(f"Token revoke failed: {response.status_code} {response.text}")


def get_connection(db: Session) -> QboConnection:
    connection = db.get(QboConnection, 1)
    if connection is None:
        raise QboError("QuickBooks is not connected yet.")
    return connection


async def qbo_request(db: Session, method: str, path: str, *, params: dict[str, Any] | None = None, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
    connection = await refresh_tokens_if_needed(db, get_connection(db))
    url = f"{settings.qbo_api_base_url}/v3/company/{connection.realm_id}{path}"
    request_params = dict(params or {})
    if settings.qbo_minor_version:
        request_params["minorversion"] = settings.qbo_minor_version
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.request(
            method,
            url,
            params=request_params,
            json=json_body,
            headers={
                "Authorization": f"Bearer {_decrypt_token(connection.access_token)}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
    if response.status_code >= 400:
        raise QboError(f"QuickBooks API error: {response.status_code} {response.text}")
    return response.json()


async def qbo_query(db: Session, query: str) -> dict[str, Any]:
    return await qbo_request(db, "GET", "/query", params={"query": query})


def _decimal_to_float(value: Decimal) -> float:
    return float(Decimal(value or 0))


def _escape_qbo_query_string(value: str) -> str:
    return value.replace("'", "\\'")


async def fetch_company_info(db: Session) -> dict[str, Any]:
    connection = get_connection(db)
    payload = await qbo_request(db, "GET", f"/companyinfo/{connection.realm_id}")
    return payload.get("CompanyInfo", payload)


async def fetch_estimate_by_doc_number_or_id(db: Session, identifier: str) -> dict[str, Any]:
    cleaned = identifier.strip()
    if not cleaned:
        raise QboError("Enter a QBO Estimate number or ID.")
    escaped = _escape_qbo_query_string(cleaned)
    queries = [
        f"select * from Estimate where DocNumber = '{escaped}' maxresults 1",
        f"select * from Estimate where Id = '{escaped}' maxresults 1",
    ]
    for query in queries:
        payload = await qbo_query(db, query)
        estimates = payload.get("QueryResponse", {}).get("Estimate", [])
        if estimates:
            return estimates[0]
    raise QboError(f"No QBO Estimate found for '{cleaned}'.")


async def fetch_estimates_from_date(db: Session, start_date: str, *, page_size: int = 100) -> list[dict[str, Any]]:
    estimates: list[dict[str, Any]] = []
    start_position = 1
    while True:
        query = f"select * from Estimate where TxnDate >= '{start_date}' startposition {start_position} maxresults {page_size}"
        payload = await qbo_query(db, query)
        batch = payload.get("QueryResponse", {}).get("Estimate", [])
        if not batch:
            break
        estimates.extend(batch)
        if len(batch) < page_size:
            break
        start_position += page_size
    return estimates


async def fetch_all_items(db: Session, *, active_only: bool = True, page_size: int = 100) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    start_position = 1
    where = " where Active in (true, false)" if not active_only else " where Active = true"
    while True:
        query = f"select * from Item{where} startposition {start_position} maxresults {page_size}"
        payload = await qbo_query(db, query)
        batch = payload.get("QueryResponse", {}).get("Item", [])
        if not batch:
            break
        items.extend(batch)
        if len(batch) < page_size:
            break
        start_position += page_size
    return items


def build_estimate_payload(quote: Quote) -> dict[str, Any]:
    totals = quote_totals(quote)
    lines: list[dict[str, Any]] = []
    for line in sorted(quote.lines, key=lambda item: item.sort_order):
        if not line.include_on_qbo_estimate:
            continue
        if line.is_section_header:
            lines.append({"DetailType": "DescriptionOnly", "Description": line.description})
            continue
        if not line.qbo_item_id:
            raise QboError(f"Line '{line.description}' is marked for QBO but has no QBO item selected.")
        lines.append(
            {
                "DetailType": "SalesItemLineDetail",
                "Amount": _decimal_to_float(line.revenue),
                "Description": line.description,
                "SalesItemLineDetail": {
                    "ItemRef": {"value": line.qbo_item_id, "name": line.qbo_item_name or line.product_service_name or line.description},
                    "Qty": _decimal_to_float(line.quantity),
                    "UnitPrice": _decimal_to_float(line.unit_price),
                },
            }
        )
    if not lines:
        raise QboError("No quote lines are marked for the QBO estimate.")
    if not quote.customer_qbo_id:
        raise QboError("Quote has no QBO customer selected.")

    return {
        "CustomerRef": {"value": quote.customer_qbo_id, "name": quote.customer_name},
        "Line": lines,
        "PrivateNote": (
            f"Internal estimate metrics from SPH app: Revenue ${totals['revenue']}, Cost ${totals['cost']}, "
            f"Gross Markup ${totals['gross_markup']}, Markup {totals['markup_percent']}%, SPH ${totals['sph']}/hr"
        ),
        "CustomField": [
            {"DefinitionId": settings.qbo_cf_margin_id, "Name": "Est Margin %", "Type": "StringType", "StringValue": f"{totals['gross_margin_percent']}%"},
            {"DefinitionId": settings.qbo_cf_profit_id, "Name": "Est Profit $", "Type": "StringType", "StringValue": f"${totals['gross_markup']}"},
            {"DefinitionId": settings.qbo_cf_sph_id, "Name": settings.qbo_cf_sph_name, "Type": "StringType", "StringValue": f"{totals['sph']}"},
        ],
    }


async def create_qbo_estimate(db: Session, quote: Quote) -> dict[str, Any]:
    return await qbo_request(db, "POST", "/estimate", json_body=build_estimate_payload(quote))


async def update_qbo_estimate_sph(db: Session, quote: Quote) -> dict[str, Any]:
    if not quote.qbo_estimate_id:
        raise QboError("This quote is not linked to a QBO Estimate.")
    latest = await qbo_request(db, "GET", f"/estimate/{quote.qbo_estimate_id}")
    estimate = latest.get("Estimate")
    if not estimate:
        raise QboError("Could not retrieve latest QBO Estimate before updating SPH.")
    totals = quote_totals(quote)
    custom_fields = estimate.get("CustomField") or []
    sph_written = False
    for field in custom_fields:
        if str(field.get("DefinitionId")) == str(settings.qbo_cf_sph_id) or (field.get("Name") or "").strip().lower() == settings.qbo_cf_sph_name.strip().lower():
            field["Type"] = "StringType"
            field["StringValue"] = f"{totals['sph']}"
            sph_written = True
    if not sph_written:
        custom_fields.append({"DefinitionId": settings.qbo_cf_sph_id, "Name": settings.qbo_cf_sph_name, "Type": "StringType", "StringValue": f"{totals['sph']}"})
    payload = {"Id": estimate["Id"], "SyncToken": estimate["SyncToken"], "sparse": True, "CustomField": custom_fields}
    return await qbo_request(db, "POST", "/estimate", json_body=payload)


async def update_qbo_item_prices(db: Session, item: QboItem) -> dict[str, Any]:
    if item.variable_cost:
        raise QboError(f"{item.fully_qualified_name or item.name} is a variable-cost item and is locked from price upload.")
    if (item.item_type or "").lower() in {"category", "group"}:
        raise QboError(f"{item.fully_qualified_name or item.name} is not a standard editable item type.")
    latest_payload = await qbo_request(db, "GET", f"/item/{item.qbo_id}")
    latest = latest_payload.get("Item")
    if not latest:
        raise QboError(f"Could not retrieve latest QBO Item {item.qbo_id}.")
    payload = {
        "Id": item.qbo_id,
        "SyncToken": latest.get("SyncToken", item.sync_token or "0"),
        "sparse": True,
        "PurchaseCost": _decimal_to_float(item.purchase_cost),
        "UnitPrice": _decimal_to_float(item.unit_price),
    }
    return await qbo_request(db, "POST", "/item", json_body=payload)
