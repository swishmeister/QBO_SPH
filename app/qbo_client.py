from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode
import secrets

import httpx
from sqlalchemy.orm import Session

from .config import get_settings
from .models import QboConnection, Quote
from .calculations import quote_totals

settings = get_settings()

AUTH_BASE_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
REVOCATION_URL = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"
SCOPE = "com.intuit.quickbooks.accounting"


class QboError(RuntimeError):
    pass


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
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.qbo_redirect_uri,
            },
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
    connection.access_token = payload["access_token"]
    connection.refresh_token = payload["refresh_token"]
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
            data={
                "grant_type": "refresh_token",
                "refresh_token": connection.refresh_token,
            },
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        )

    if response.status_code >= 400:
        raise QboError(f"Token refresh failed: {response.status_code} {response.text}")

    payload = response.json()
    connection.access_token = payload["access_token"]
    connection.refresh_token = payload.get("refresh_token", connection.refresh_token)
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

    token_to_revoke = connection.refresh_token or connection.access_token
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
                "Authorization": f"Bearer {connection.access_token}",
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


def build_estimate_payload(quote: Quote) -> dict[str, Any]:
    totals = quote_totals(quote)
    lines: list[dict[str, Any]] = []

    for line in sorted(quote.lines, key=lambda item: item.sort_order):
        if not line.include_on_qbo_estimate:
            continue
        if not line.qbo_item_id:
            raise QboError(f"Line '{line.description}' is marked for QBO but has no QBO item selected.")

        amount = _decimal_to_float(line.revenue)
        lines.append(
            {
                "DetailType": "SalesItemLineDetail",
                "Amount": amount,
                "Description": line.description,
                "SalesItemLineDetail": {
                    "ItemRef": {
                        "value": line.qbo_item_id,
                        "name": line.qbo_item_name or line.description,
                    },
                    "Qty": _decimal_to_float(line.quantity),
                    "UnitPrice": _decimal_to_float(line.unit_price),
                },
            }
        )

    if not lines:
        raise QboError("No quote lines are marked for the QBO estimate.")
    if not quote.customer_qbo_id:
        raise QboError("Quote has no QBO customer selected.")

    payload: dict[str, Any] = {
        "CustomerRef": {"value": quote.customer_qbo_id, "name": quote.customer_name},
        "Line": lines,
        "PrivateNote": (
            f"Internal estimate metrics from quote app: "
            f"Revenue ${totals['revenue']}, Cost ${totals['cost']}, "
            f"Gross Profit ${totals['gross_profit']}, Margin {totals['gross_margin_percent']}%, "
            f"Labor Hours {totals['labor_hours']}, Profit/Hour ${totals['profit_per_hour']}"
        ),
        "CustomField": [
            {
                "DefinitionId": settings.qbo_cf_margin_id,
                "Name": "Est Margin %",
                "Type": "StringType",
                "StringValue": f"{totals['gross_margin_percent']}%",
            },
            {
                "DefinitionId": settings.qbo_cf_profit_id,
                "Name": "Est Profit $",
                "Type": "StringType",
                "StringValue": f"${totals['gross_profit']}",
            },
            {
                "DefinitionId": settings.qbo_cf_profit_per_hour_id,
                "Name": "Profit / Hour",
                "Type": "StringType",
                "StringValue": f"${totals['profit_per_hour']}/hr",
            },
        ],
    }
    return payload


async def create_qbo_estimate(db: Session, quote: Quote) -> dict[str, Any]:
    payload = build_estimate_payload(quote)
    return await qbo_request(db, "POST", "/estimate", json_body=payload)
