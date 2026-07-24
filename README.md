# QBO SPH Calculator and File Editor — Estimate Library Build

Internal FastAPI app for QuickBooks Online. It imports QBO estimates, calculates SPH from gross markup, and manages item purchase/sale pricing without replacing QuickBooks as the accounting system.

## Production URL layout

- Host domain: `sph.delightfulgardens.com`
- Launch URL: `https://sph.delightfulgardens.com/`
- Connect/Reconnect URL: `https://sph.delightfulgardens.com/qbo/connect`
- Disconnect URL: `https://sph.delightfulgardens.com/qbo/disconnected`
- OAuth Redirect URI: `https://sph.delightfulgardens.com/qbo/callback`

## Major features in this build

### Estimate Library

- Import all current-year QBO estimates from January 1 forward.
- Refresh the estimate library quickly.
- Search the local estimate library by customer name or estimate number.
- Import older estimates one at a time by estimate number.
- Open each estimate in a spreadsheet-style worksheet.

### Spreadsheet Estimate Editor

Visible columns:

```text
Product/Service | Description | Qty | Cost | Markup % | Rate | Amount | Delete
```

Line math:

```text
Amount = Qty × Rate
Gross Markup = (Rate - Cost) × Qty
Markup % = (Rate - Cost) / Cost
Rate = Cost × (1 + Markup %)
```

SPH math:

```text
SPH = (Gross Markup ÷ Quoted Labor Hours) + Hourly Labor Rate
```

The SPH summary at the top of each estimate updates live in the browser as worksheet values change.

### Variable-cost material-code handling

Generic non-inventory material codes such as `MC`, `MI`, and `MP` are treated as variable-cost placeholders.

Rules:

- The app ignores the QBO item-list default price/cost for these items during estimate import.
- The app uses the actual QBO estimate line rate/amount from the transaction.
- The internal cost field is left as its saved line-level value, or blank/zero for new imports, so the user can enter the real vendor cost.
- These items are locked out of Item Price Manager uploads.

Configure the code list with:

```env
VARIABLE_COST_ITEM_CODES=MC,MI,MP
```

### Upload SPH to QBO

The app can upload the calculated SPH value to the SPH custom field on the linked QBO estimate.

```env
QBO_CF_SPH_ID=3
QBO_CF_SPH_NAME=SPH
```

Keep `QBO_READ_ONLY=true` until you are ready to allow uploads.

### Item Price Manager

- Import QBO items.
- Search/filter item library.
- Edit purchase price, markup %, and sale price locally.
- Review pending price changes before upload.
- Upload only `PurchaseCost` and `UnitPrice` to QBO.
- Does not edit quantity on hand.

## Recommended production environment variables

```env
QBO_ENV=production
QBO_CLIENT_ID=<Intuit production client ID>
QBO_CLIENT_SECRET=<Intuit production client secret>
QBO_REDIRECT_URI=https://sph.delightfulgardens.com/qbo/callback
QBO_READ_ONLY=true

QBO_CF_SPH_ID=<SPH custom field DefinitionId>
QBO_CF_SPH_NAME=SPH
VARIABLE_COST_ITEM_CODES=MC,MI,MP

REQUIRE_BASIC_AUTH=true
APP_USERNAME=<admin username>
APP_PASSWORD=<strong password>
APP_SECRET_KEY=<long random secret; do not change after connecting QBO>
SECURE_COOKIES=true
ENABLE_HSTS=true

DATABASE_URL=<Render Postgres internal database URL>
PYTHON_VERSION=3.12.8
```

## Important security notes

- Do not commit `.env`, database URLs, Intuit client secrets, or Render secrets to GitHub.
- `APP_SECRET_KEY` is used for session signing and token encryption-at-rest. Once production QBO is connected, changing `APP_SECRET_KEY` will make encrypted stored tokens unreadable and require reconnecting QuickBooks.
- This build defaults to `QBO_READ_ONLY=true`. That means importing, editing locally, and calculating work; QBO uploads are blocked.
- Set `QBO_READ_ONLY=false` only after production import testing is complete.

## Production deployment steps

1. Push this build to the private GitHub repo.
2. In Render, verify `PYTHON_VERSION=3.12.8`.
3. Verify the start command is:
   ```bash
   python -m uvicorn app.main:app --host 0.0.0.0 --port $PORT
   ```
4. Use the Render Postgres **internal** database URL for `DATABASE_URL`.
5. Set `QBO_ENV=production` and production Intuit credentials.
6. Keep `QBO_READ_ONLY=true` for the first production test.
7. Add `https://sph.delightfulgardens.com/qbo/callback` to Intuit's Production Redirect URIs.
8. Reconnect QuickBooks from `https://sph.delightfulgardens.com/qbo/connect`.
9. Import several real estimates and confirm the math before enabling writes.

## Local development

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

For local development, set `SECURE_COOKIES=false` and use sandbox credentials.
